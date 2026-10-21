"""Timed NCP-OUSD mock exam: weighted question draw, answers, flags, scoring and saved sessions."""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import random
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import bank as exam_bank
from . import srs
from .progress import ENV_DIR, ProgressStore, workspace_root

SCHEMA_VERSION = 1
FILE_NAME = "mock_sessions.json"
PROGRESS_FILE_NAME = "progress.json"
DEFAULT_COUNT = 65
DEFAULT_MINUTES = 120
MAX_COUNT = 100
MIN_MINUTES = 1
MAX_MINUTES = 300
MAX_SESSIONS_KEPT = 50
MAX_LIST_LIMIT = 50
TARGET_PCT = 80.0
TEXT_CAP = 600
OPTION_CAP = 300
CITATIONS_CAP = 5
CHOICES_CAP = 10
LOCK_TIMEOUT_SECONDS = 30.0
LETTERS = exam_bank.LETTERS
SOURCE = "mock"

ACTIVE = "active"
FINISHED = "finished"
ABANDONED = "abandoned"
STATUSES = (ACTIVE, FINISHED, ABANDONED)

HOW_TO_ANSWER = (
    "Answer with answer(session_id, question_id, chosen) using letters or 0-based indices; you can change answers "
    "and flag questions until you finish. finish(session_id) scores the mock and saves answered questions to progress."
)
READINESS_NOTE = (
    "NVIDIA publishes no passing score for NCP-OUSD. The target here is a self-set readiness goal, not an official "
    "pass mark; one mock is a single sample, so compare several before booking."
)


class MockExamError(ValueError):
    """A mock-exam request that cannot be carried out (unknown session, time up, already finished, ...)."""


# ---------------------------------------------------------------- weights and apportioning


def official_weights() -> dict[str, int]:
    """Official domain weights (percent) in study-guide order."""
    ordered = sorted(exam_bank.DOMAINS.items(), key=lambda kv: kv[1]["number"])
    return {name: int(info["weight"]) for name, info in ordered}


def apportion(weights: Mapping[str, float], total: int) -> dict[str, int]:
    """Largest-remainder (Hamilton) split of ``total`` by ``weights``, in exact arithmetic.

    Ties on the remainder go to the heavier domain, then to the earlier one.
    """
    names = list(weights)
    exact = {name: Fraction(weights[name]) for name in names}
    weight_sum = sum(exact.values(), Fraction(0))
    if total <= 0 or weight_sum <= 0:
        return {name: 0 for name in names}
    shares = {name: exact[name] * total / weight_sum for name in names}
    counts = {name: math.floor(shares[name]) for name in names}
    order = sorted(
        range(len(names)),
        key=lambda i: (-(shares[names[i]] - counts[names[i]]), -exact[names[i]], i),
    )
    for index in order[: total - sum(counts.values())]:
        counts[names[index]] += 1
    return counts


def allocate(
    weights: Mapping[str, float], available: Mapping[str, int], total: int
) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``(allocation, quota)``: the weighted quota, then capped by what each domain has.

    A shortfall is handed to domains that still have spare questions, again in proportion to their weights.
    Domains with weight 0 never receive questions.
    """
    quota = apportion(weights, total)
    allocation = {name: min(quota[name], max(0, int(available.get(name, 0)))) for name in weights}
    remaining = total - sum(allocation.values())
    while remaining > 0:
        spare = {
            name: weight
            for name, weight in weights.items()
            if weight > 0 and allocation[name] < int(available.get(name, 0))
        }
        if not spare:
            break
        for name, extra in apportion(spare, remaining).items():
            give = min(extra, int(available[name]) - allocation[name])
            allocation[name] += give
            remaining -= give
    return allocation, quota


def _resolve_domain(key: Any, names: list[str]) -> str:
    text = str(key).strip().casefold()
    if not text or isinstance(key, bool):
        raise ValueError(f"unknown domain {key!r}")
    for name in names:
        if name.casefold() == text:
            return name
    for name, info in exam_bank.DOMAINS.items():
        if name in names and text in (str(info["prefix"]).casefold(), str(info["number"])):
            return name
    starts = [name for name in names if name.casefold().startswith(text)]
    if len(starts) == 1:
        return starts[0]
    raise ValueError(f"unknown or ambiguous domain {key!r}; choose from {names}")


def _resolve_weights(domain_weights: Mapping[str, Any] | None, bank_domains: Iterable[str]) -> dict[str, float]:
    official = official_weights()
    names = list(official) + sorted(name for name in set(bank_domains) if name not in official)
    if domain_weights is None:
        return {name: official.get(name, 0) for name in names}
    if not isinstance(domain_weights, Mapping):
        raise TypeError("domain_weights must map domain names to non-negative numbers")
    weights: dict[str, float] = {name: 0 for name in names}
    seen: set[str] = set()
    for key, value in domain_weights.items():
        name = _resolve_domain(key, names)
        if name in seen:
            raise ValueError(f"domain {name!r} is given twice in domain_weights")
        seen.add(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"weight for {name!r} must be a finite number >= 0, got {value!r}")
        weights[name] = value
    if not any(weight > 0 for weight in weights.values()):
        raise ValueError("domain_weights needs at least one positive weight")
    return weights


# ---------------------------------------------------------------- question banks


class ItemBank:
    """A question bank built from item dicts shaped like questions.json entries (useful for tests and custom sets).

    Needs ``id``, ``domain``, ``type`` ('single'/'multi'), ``stem``, ``options`` and ``answer`` (0-based indices);
    ``objective_ids``, ``explanations``, ``citations``, ``difficulty`` and ``usda`` are optional.
    """

    def __init__(self, items: Iterable[Mapping[str, Any]], name: str = "custom") -> None:
        self.name = str(name)
        self._items: dict[str, dict[str, Any]] = {}
        for raw in items:
            item = _check_item(raw)
            if item["id"] in self._items:
                raise ValueError(f"duplicate question id {item['id']!r}")
            self._items[item["id"]] = item

    def load_bank(self) -> list[dict[str, Any]]:
        """Copies of every item."""
        return copy.deepcopy(list(self._items.values()))

    def public_view(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """The item as a candidate sees it (no answers or explanations)."""
        return _tolerant_view(item)

    def grade(self, question_id: str, chosen: Any) -> dict[str, Any]:
        """Grade like ``bank.grade``: multi-select needs the exact set of correct options."""
        item = self._items.get(str(question_id))
        if item is None:
            raise ValueError(f"unknown question id {question_id!r}")
        values = [chosen] if isinstance(chosen, (int, str)) else list(chosen or [])
        picked = sorted({_coerce_choice(value, len(item["options"])) for value in values})
        answer = sorted(item["answer"])
        return {
            "question_id": item["id"],
            "domain": item["domain"],
            "objective_ids": list(item["objective_ids"]),
            "type": item["type"],
            "correct": picked == answer,
            "chosen": picked,
            "correct_answer": answer,
            "correct_letters": [LETTERS[i] for i in answer],
            "explanations": [
                {
                    "index": i,
                    "letter": LETTERS[i],
                    "option": text,
                    "is_correct": i in answer,
                    "chosen": i in picked,
                    "why": item["explanations"][i],
                }
                for i, text in enumerate(item["options"])
            ],
            "citations": list(item["citations"]),
        }


def _check_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("each bank item must be a dict")
    item = copy.deepcopy(dict(raw))
    qid = item.get("id")
    for key in ("id", "domain", "stem"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f"item {qid!r}: '{key}' must be a non-empty string")
    if item.get("type") not in exam_bank.TYPES:
        raise ValueError(f"item {qid!r}: type must be one of {exam_bank.TYPES}")
    options = item.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= len(LETTERS) or not all(
        isinstance(o, str) and o.strip() for o in options
    ):
        raise ValueError(f"item {qid!r}: need 2-{len(LETTERS)} non-empty string options")
    answer = item.get("answer")
    if (
        not isinstance(answer, list)
        or not answer
        or not all(isinstance(a, int) and not isinstance(a, bool) and 0 <= a < len(options) for a in answer)
        or len(set(answer)) != len(answer)
    ):
        raise ValueError(f"item {qid!r}: answer must be a non-empty list of distinct option indices")
    if item["type"] == "single" and len(answer) != 1:
        raise ValueError(f"item {qid!r}: single questions need exactly one answer")
    explanations = item.get("explanations")
    if explanations is None:
        explanations = [""] * len(options)
    if not isinstance(explanations, list) or len(explanations) != len(options):
        raise ValueError(f"item {qid!r}: need one explanation per option")
    item["explanations"] = [str(e) for e in explanations]
    item["objective_ids"] = [str(o) for o in (item.get("objective_ids") or [])]
    item["citations"] = [str(c) for c in (item.get("citations") or [])]
    return item


def _tolerant_view(item: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        "id": item["id"],
        "domain": item["domain"],
        "objective_ids": list(item.get("objective_ids") or []),
        "type": item["type"],
        "difficulty": item.get("difficulty"),
        "stem": item["stem"],
        "options": [f"{LETTERS[i]}. {text}" for i, text in enumerate(item["options"])],
    }
    if item["type"] == "multi":
        view["select"] = len(item["answer"])
    if item.get("usda"):
        view["usda"] = dict(item["usda"])
        view["root"] = item.get("root") or next(iter(item["usda"]))
    return view


def _adapt(source: Any) -> Any:
    """The bank module itself, an :class:`ItemBank` for a list of items, or any object with load_bank()/grade()."""
    if source is None or source is exam_bank:
        return exam_bank
    if isinstance(source, (list, tuple)):
        return ItemBank(source)
    if callable(getattr(source, "load_bank", None)) and callable(getattr(source, "grade", None)):
        return source
    raise TypeError("bank must be a list of question dicts or an object with load_bank() and grade()")


def _view(adapter: Any, item: Mapping[str, Any]) -> dict[str, Any]:
    viewer = getattr(adapter, "public_view", None)
    view = viewer(item) if callable(viewer) else _tolerant_view(item)
    for secret in ("answer", "explanations", "citations", "verify"):
        view.pop(secret, None)
    return view


# ---------------------------------------------------------------- answers


def _coerce_choice(choice: Any, option_count: int) -> int:
    if isinstance(choice, bool):
        raise ValueError(f"choice {choice!r} must be an option letter or 0-based index")
    if isinstance(choice, int):
        index = choice
    elif isinstance(choice, str) and choice.strip().isdigit():
        index = int(choice.strip())
    elif isinstance(choice, str) and len(choice.strip()) == 1 and choice.strip().upper() in LETTERS:
        index = LETTERS.index(choice.strip().upper())
    else:
        raise ValueError(f"choice {choice!r} must be an option letter or 0-based index")
    if not 0 <= index < option_count:
        raise ValueError(f"choice {choice!r} is out of range for {option_count} options")
    return index


def _parse_chosen(chosen: Any, option_count: int) -> list[int]:
    """Letters, indices, 'A,C' or 'AC' strings, or None/[] (clears the answer) -> sorted option indices."""
    if chosen is None:
        return []
    if isinstance(chosen, bool):
        raise TypeError("chosen must be letters, 0-based indices or a list of them")
    if isinstance(chosen, int):
        values: list[Any] = [chosen]
    elif isinstance(chosen, str):
        values = []
        for token in re.split(r"[\s,;/]+", chosen.strip()):
            if len(token) > 1 and token.isalpha():
                values.extend(token)
            elif token:
                values.append(token)
    elif isinstance(chosen, (list, tuple, set, frozenset)):
        values = list(chosen)
    else:
        raise TypeError("chosen must be letters, 0-based indices or a list of them")
    if len(values) > CHOICES_CAP:
        raise ValueError(f"at most {CHOICES_CAP} choices")
    return sorted({_coerce_choice(value, option_count) for value in values})


# ---------------------------------------------------------------- storage helpers


def default_sessions_path() -> Path:
    """``$OMNIVERSE_EXAM_PREP_DIR/mock_sessions.json``, else ``<workspace>/exam-prep/mock_sessions.json``. Creates nothing."""
    configured = os.environ.get(ENV_DIR, "").strip()
    folder = Path(configured).expanduser() if configured else workspace_root() / "exam-prep"
    return folder / FILE_NAME


@contextlib.contextmanager
def _file_lock(path: Path | None):
    """Exclusive cross-process lock on a ``<name>.lock`` sidecar (copied from progress.py, where it is private)."""
    if path is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"mock session file {path} is locked by another process") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a unique temp file beside ``path`` and swap it in with ``os.replace`` (copied from progress.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _unique_sibling(path: Path, tag: str) -> Path:
    stamp = srs.utc_now().strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.{tag}-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{tag}-{stamp}-{index}")
        index += 1
    return candidate


def _empty_data() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "created_at": None, "updated_at": None, "sessions": []}


def _normalize(raw: Any) -> dict[str, Any]:
    """Check a loaded file; raises ValueError/TypeError/KeyError when it is unusable."""
    if not isinstance(raw, dict):
        raise ValueError("top level is not a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {raw.get('schema_version')!r}")
    sessions = raw.get("sessions", [])
    if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
        raise ValueError("'sessions' is not a list of objects")
    for rec in sessions:
        if not isinstance(rec.get("id"), str) or rec.get("status") not in STATUSES:
            raise ValueError("a session has no id or an unknown status")
        if not isinstance(rec.get("questions"), list) or not all(isinstance(q, dict) for q in rec["questions"]):
            raise ValueError(f"session {rec['id']}: 'questions' is not a list of objects")
        if not isinstance(rec.get("answers"), dict) or not isinstance(rec.get("flagged"), list):
            raise ValueError(f"session {rec['id']}: bad 'answers' or 'flagged'")
        srs.parse_iso(rec["started_at"])
        srs.parse_iso(rec["deadline"])
    return {**_empty_data(), **raw, "sessions": sessions}


def _prune(data: dict[str, Any]) -> None:
    """Keep every open session and the newest MAX_SESSIONS_KEPT closed ones."""
    closed = [rec for rec in data["sessions"] if rec["status"] != ACTIVE]
    drop = {id(rec) for rec in closed[: max(0, len(closed) - MAX_SESSIONS_KEPT)]}
    data["sessions"] = [rec for rec in data["sessions"] if id(rec) not in drop]


# ---------------------------------------------------------------- small helpers


def _clean_count(value: Any, notes: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("count must be a whole number")
    if int(value) != value:
        raise ValueError("count must be a whole number")
    number = int(value)
    if not 1 <= number <= MAX_COUNT:
        notes.append(f"count {number} is outside 1-{MAX_COUNT}; clamped.")
    return max(1, min(MAX_COUNT, number))


def _clean_minutes(value: Any, notes: list[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("minutes must be a number")
    if not MIN_MINUTES <= value <= MAX_MINUTES:
        notes.append(f"minutes {value} is outside {MIN_MINUTES}-{MAX_MINUTES}; clamped.")
    number = max(MIN_MINUTES, min(MAX_MINUTES, value))
    return int(number) if float(number).is_integer() else round(float(number), 2)


def _clean_target(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("target_pct must be a number")
    return max(0.0, min(100.0, float(value)))


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


def _cap(text: Any, limit: int = TEXT_CAP) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _letters(indices: Iterable[int]) -> list[str]:
    return [LETTERS[i] for i in indices if 0 <= i < len(LETTERS)]


def _question(rec: dict[str, Any], question_id: Any) -> tuple[int, dict[str, Any]]:
    """Find a session question by id, or by its 1-based number (int or digit string)."""
    key = str(question_id).strip() if question_id is not None else ""
    for number, question in enumerate(rec["questions"], 1):
        if question["id"] == key:
            return number, question
    if key.isdigit() and 1 <= int(key) <= len(rec["questions"]) and not isinstance(question_id, bool):
        number = int(key)
        return number, rec["questions"][number - 1]
    raise MockExamError(f"question {question_id!r} is not part of mock {rec['id']}")


def _domain_order(rec: dict[str, Any]) -> list[str]:
    names = list(rec.get("weights") or {})
    for question in rec["questions"]:
        if question["domain"] not in names:
            names.append(question["domain"])
    return names


# ---------------------------------------------------------------- the store


class MockExamStore:
    """Timed mock exams kept in one JSON file (atomic writes, cross-process lock, bad files kept as backups).

    The file is re-read on every call, so sessions survive restarts. ``memory_only=True`` never touches disk.
    ``bank`` is the default question bank for new sessions (the shipped bank when None).
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        memory_only: bool = False,
        bank: Any = None,
    ) -> None:
        self.memory_only = bool(memory_only)
        self.path: Path | None = None
        if not self.memory_only:
            self.path = Path(path).expanduser() if path is not None else default_sessions_path()
        self._default_bank = _adapt(bank) if bank is not None else None
        self._banks: dict[str, Any] = {}
        self._memory = _empty_data()
        self.recovered_backup: str | None = None
        self.recovery_reason: str | None = None

    # ------------------------------------------------------------ public API

    def start(
        self,
        count: int = DEFAULT_COUNT,
        minutes: float = DEFAULT_MINUTES,
        seed: int | None = None,
        domain_weights: Mapping[str, Any] | None = None,
        bank: Any = None,
        now: datetime | None = None,
        include_questions: bool = True,
    ) -> dict[str, Any]:
        """Start a timed mock: questions drawn per domain by weight (largest remainder), answers withheld.

        Only one mock may be open at a time; finish or abandon it first. ``domain_weights`` replaces the official
        weights (domains left out get 0). A domain never gets more questions than the bank holds; the shortfall
        goes to other domains by weight and is reported.
        """
        moment = srs.as_utc(now).replace(microsecond=0)
        notes: list[str] = []
        total = _clean_count(count, notes)
        limit_minutes = _clean_minutes(minutes, notes)
        if seed is None:
            seed = secrets.randbelow(2**31)
        elif isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an integer")
        if bank is not None:
            adapter = _adapt(bank)
        else:
            adapter = self._default_bank or exam_bank
        bank_label = "default" if adapter is exam_bank else "custom"

        pools: dict[str, list[dict[str, Any]]] = {}
        for item in adapter.load_bank():
            pools.setdefault(str(item["domain"]), []).append(item)
        weights = _resolve_weights(domain_weights, pools)
        available = {name: len(pools.get(name, [])) for name in weights}
        allocation, quota = allocate(weights, available, total)

        rng = random.Random(seed)
        picked: list[dict[str, Any]] = []
        for name in weights:
            pool = sorted(pools.get(name, []), key=lambda item: str(item["id"]))
            rng.shuffle(pool)
            picked.extend(pool[: allocation[name]])
        rng.shuffle(picked)
        if not picked:
            raise MockExamError("the question bank has no questions in the weighted domains")

        shortfalls = [
            {"domain": name, "wanted": quota[name], "available": available[name], "given": allocation[name]}
            for name in weights
            if allocation[name] < quota[name]
        ]
        redistributed = {name: allocation[name] - quota[name] for name in weights if allocation[name] > quota[name]}
        short_by = total - len(picked)
        if shortfalls:
            listed = ", ".join(f"{row['domain']} (wanted {row['wanted']}, bank has {row['available']})" for row in shortfalls)
            notes.append(f"The bank is short for: {listed}.")
            if redistributed:
                moved = ", ".join(f"{name} +{extra}" for name, extra in redistributed.items())
                notes.append(f"The missing questions went to other domains by weight: {moved}.")
        if short_by:
            notes.append(f"Only {len(picked)} of the {total} requested questions were available in the weighted domains.")
        if len(picked) != DEFAULT_COUNT or domain_weights is not None:
            notes.append(f"A full mock has {DEFAULT_COUNT} questions at the official weights; this one differs.")

        deadline = moment + timedelta(minutes=limit_minutes)
        questions = [
            {
                "id": str(item["id"]),
                "domain": str(item["domain"]),
                "objective_ids": [str(o) for o in item.get("objective_ids") or []],
                "type": item["type"],
                "option_count": len(item["options"]),
                "select": len(item["answer"]) if item["type"] == "multi" else 1,
            }
            for item in picked
        ]
        with self._edit() as data:
            open_rec = _active(data)
            if open_rec is not None:
                raise MockExamError(
                    f"Mock {open_rec['id']} is still open (deadline {open_rec['deadline']}); "
                    "finish(session_id) or abandon(session_id) it before starting another."
                )
            session_id = _new_id(moment, data)
            rec = {
                "id": session_id,
                "status": ACTIVE,
                "started_at": srs.to_iso(moment),
                "deadline": srs.to_iso(deadline),
                "minutes": limit_minutes,
                "requested_count": total,
                "count": len(questions),
                "seed": seed,
                "bank": bank_label,
                "weights": dict(weights),
                "quota": quota,
                "allocation": allocation,
                "shortfalls": shortfalls,
                "redistributed": redistributed,
                "questions": questions,
                "answers": {},
                "flagged": [],
                "finished_at": None,
                "abandoned_at": None,
                "result": None,
            }
            data["sessions"].append(rec)
            _prune(data)
            stamp = srs.to_iso(moment)
        if bank_label == "custom":
            self._banks[session_id] = adapter

        payload: dict[str, Any] = {
            "ok": True,
            "session_id": session_id,
            "status": ACTIVE,
            "started_at": stamp,
            "deadline": rec["deadline"],
            "minutes": limit_minutes,
            "time_limit_seconds": int(limit_minutes * 60),
            "seconds_per_question": round(limit_minutes * 60 / len(questions), 1),
            "requested_count": total,
            "count": len(questions),
            "seed": seed,
            "bank": bank_label,
            "weights": dict(weights),
            "quota": quota,
            "allocation": allocation,
            "shortfalls": shortfalls,
            "redistributed": redistributed,
            "short_by": short_by,
            "question_ids": [q["id"] for q in questions],
            "notes": notes,
            "how_to_answer": HOW_TO_ANSWER,
            "storage": self._storage_info(),
        }
        if include_questions:
            payload["questions"] = [{"number": n, **_view(adapter, item)} for n, item in enumerate(picked, 1)]
        return payload

    def answer(
        self,
        session_id: str | None,
        question_id: str | int,
        chosen: Any,
        now: datetime | None = None,
        allow_late: bool = False,
    ) -> dict[str, Any]:
        """Record (or change) a choice; ``chosen=None`` or [] clears it. Rejected after the deadline unless allow_late."""
        moment = srs.as_utc(now)
        with self._edit() as data:
            rec = _find(data, session_id, want_active=True)
            number, question = _question(rec, question_id)
            late = moment > srs.parse_iso(rec["deadline"])
            if late and not allow_late:
                raise MockExamError(
                    f"Time is up for mock {rec['id']} (deadline {rec['deadline']}); finish it to see your score, "
                    "or pass allow_late=True to keep answering untimed."
                )
            picked = _parse_chosen(chosen, int(question["option_count"]))
            if question["type"] == "single" and len(picked) > 1:
                raise MockExamError(f"question {question['id']} is single-select; choose one option")
            previous = rec["answers"].get(question["id"])
            if picked:
                rec["answers"][question["id"]] = {
                    "chosen": picked,
                    "answered_at": srs.to_iso(moment),
                    "changes": (int(previous.get("changes", 0)) + 1) if previous else 0,
                    "late": bool(late),
                }
            else:
                rec["answers"].pop(question["id"], None)
            answered = len(rec["answers"])
            remaining = (srs.parse_iso(rec["deadline"]) - moment).total_seconds()
        result: dict[str, Any] = {
            "ok": True,
            "session_id": rec["id"],
            "question_id": question["id"],
            "number": number,
            "chosen": picked,
            "chosen_letters": _letters(picked),
            "answered": bool(picked),
            "changed": bool(previous) and bool(picked) and previous.get("chosen") != picked,
            "cleared": bool(previous) and not picked,
            "late": bool(late),
            "answered_count": answered,
            "total": len(rec["questions"]),
            "remaining_seconds": max(0, int(remaining)),
        }
        if question["type"] == "multi" and picked and len(picked) != int(question["select"]):
            result["note"] = f"This question asks you to select {question['select']}; you picked {len(picked)}."
        return result

    def flag(
        self, session_id: str | None, question_id: str | int, flagged: bool = True, now: datetime | None = None
    ) -> dict[str, Any]:
        """Flag (or with flagged=False, unflag) a question for review while the mock is open."""
        srs.as_utc(now)
        with self._edit() as data:
            rec = _find(data, session_id, want_active=True)
            number, question = _question(rec, question_id)
            marks = [qid for qid in rec["flagged"] if qid != question["id"]]
            if flagged:
                marks.append(question["id"])
            order = {q["id"]: i for i, q in enumerate(rec["questions"])}
            rec["flagged"] = sorted(set(marks), key=lambda qid: order.get(qid, len(order)))
            flagged_ids = list(rec["flagged"])
        return {
            "ok": True,
            "session_id": rec["id"],
            "question_id": question["id"],
            "number": number,
            "flagged": bool(flagged),
            "flagged_ids": flagged_ids,
        }

    def status(self, session_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Time left, answered and unanswered counts and flagged ids (the open mock when session_id is None)."""
        moment = srs.as_utc(now)
        rec = _find(self._load(), session_id, want_active=False)
        return _status_payload(rec, moment)

    def questions(
        self,
        session_id: str | None = None,
        bank: Any = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The session's questions again (answers withheld), each with your current choice and flag."""
        rec = _find(self._load(), session_id, want_active=False)
        adapter = self._bank_for(rec, bank)
        items = {str(item["id"]): item for item in adapter.load_bank()}
        start = max(0, int(offset))
        stop = len(rec["questions"]) if limit is None else start + max(0, min(MAX_COUNT, int(limit)))
        flagged = set(rec["flagged"])
        rows = []
        missing = []
        for number, question in enumerate(rec["questions"][start:stop], start + 1):
            item = items.get(question["id"])
            if item is None:
                missing.append(question["id"])
                continue
            chosen = (rec["answers"].get(question["id"]) or {}).get("chosen") or []
            rows.append(
                {
                    "number": number,
                    **_view(adapter, item),
                    "chosen_letters": _letters(chosen),
                    "flagged": question["id"] in flagged,
                }
            )
        payload = {
            "ok": True,
            "session_id": rec["id"],
            "status": rec["status"],
            "offset": start,
            "total": len(rec["questions"]),
            "questions": rows,
        }
        if missing:
            payload["missing_from_bank"] = missing
        return payload

    def finish(
        self,
        session_id: str | None = None,
        store: ProgressStore | None = None,
        now: datetime | None = None,
        bank: Any = None,
        target_pct: float = TARGET_PCT,
    ) -> dict[str, Any]:
        """Score the mock, list wrong questions with answers and explanations, and save answered ones to progress.

        Unanswered questions count as wrong but are not recorded as answered. ``store`` defaults to a
        ProgressStore beside the session file (memory-only when this store is memory-only).
        """
        moment = srs.as_utc(now).replace(microsecond=0)
        target = _clean_target(target_pct)
        with self._edit() as data:
            rec = _find(data, session_id, want_active=True)
            adapter = self._bank_for(rec, bank)
            report, to_record = _grade_session(rec, adapter, moment, target)
            rec["status"] = FINISHED
            rec["finished_at"] = srs.to_iso(moment)
            rec["result"] = _compact(report)
        progress_store = store if store is not None else self._progress_store()
        report["progress"] = _record_progress(progress_store, to_record, report, moment)
        try:
            with self._edit() as data:
                saved = _find(data, rec["id"], want_active=False)
                if isinstance(saved.get("result"), dict):
                    saved["result"]["progress"] = {
                        key: report["progress"][key] for key in ("recorded", "not_recorded_unanswered", "errors")
                    }
        except (OSError, ValueError) as exc:
            report["notes"].append(f"Scored and saved, but the progress summary was not stored on the session: {exc}")
        return report

    def review(self, session_id: str | None = None, bank: Any = None, target_pct: float = TARGET_PCT) -> dict[str, Any]:
        """Re-build a finished mock's full report (wrong questions with explanations) without recording anything."""
        data = self._load()
        rec = _find(data, session_id, want_active=False) if session_id is not None else _latest_finished(data)
        if rec["status"] != FINISHED:
            raise MockExamError(f"mock {rec['id']} is {rec['status']}; only finished mocks can be reviewed")
        adapter = self._bank_for(rec, bank)
        moment = srs.parse_iso(rec["finished_at"])
        report, _ = _grade_session(rec, adapter, moment, _clean_target(target_pct))
        report["progress"] = (rec.get("result") or {}).get("progress")
        return report

    def abandon(self, session_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Close an open mock without scoring or recording it, so a new one can start."""
        moment = srs.as_utc(now)
        with self._edit() as data:
            rec = _find(data, session_id, want_active=True)
            rec["status"] = ABANDONED
            rec["abandoned_at"] = srs.to_iso(moment)
            answered = len(rec["answers"])
        self._banks.pop(rec["id"], None)
        return {
            "ok": True,
            "session_id": rec["id"],
            "status": ABANDONED,
            "abandoned_at": rec["abandoned_at"],
            "answered": answered,
            "total": len(rec["questions"]),
            "message": "Mock abandoned; nothing was scored or saved to progress.",
        }

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Recent mocks, newest first, with their score when finished."""
        count = max(0, min(MAX_LIST_LIMIT, int(limit)))
        rows = []
        for rec in reversed(self._load()["sessions"]):
            if len(rows) >= count:
                break
            score = (rec.get("result") or {}).get("score") or {}
            rows.append(
                {
                    "session_id": rec["id"],
                    "status": rec["status"],
                    "started_at": rec["started_at"],
                    "deadline": rec["deadline"],
                    "finished_at": rec.get("finished_at"),
                    "count": len(rec["questions"]),
                    "answered": len(rec["answers"]),
                    "score_pct": score.get("pct"),
                }
            )
        return rows

    def active(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Status of the open mock, or None."""
        rec = _active(self._load())
        return _status_payload(rec, srs.as_utc(now)) if rec else None

    def open_question_ids(self) -> set[str]:
        """Ids of the questions in the open mock (also after its deadline, until it is finished or abandoned)."""
        rec = _active(self._load())
        return {q["id"] for q in rec["questions"]} if rec else set()

    # ------------------------------------------------------------ internals

    def _bank_for(self, rec: dict[str, Any], bank: Any) -> Any:
        if bank is not None:
            return _adapt(bank)
        if rec["id"] in self._banks:
            return self._banks[rec["id"]]
        if rec.get("bank") != "custom":
            return exam_bank
        if self._default_bank is not None:
            return self._default_bank
        raise MockExamError(
            f"Mock {rec['id']} used a custom question bank that is not loaded in this process; "
            "pass bank=... with the same questions."
        )

    def _progress_store(self) -> ProgressStore:
        if self.path is None:
            return ProgressStore(memory_only=True)
        return ProgressStore(self.path.with_name(PROGRESS_FILE_NAME))

    def _storage_info(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "memory_only": self.memory_only,
            "recovered_backup": self.recovered_backup,
            "recovery_reason": self.recovery_reason,
        }

    @contextlib.contextmanager
    def _edit(self):
        """Locked read-modify-write; nothing is saved if the body raises."""
        with _file_lock(self.path):
            data = self._load()
            yield data
            self._save(data)

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return copy.deepcopy(self._memory)
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return _empty_data()
        except UnicodeDecodeError as exc:
            return self._quarantine(f"not UTF-8 text: {exc}")
        try:
            return _normalize(json.loads(text))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._quarantine(str(exc) or type(exc).__name__)

    def _quarantine(self, reason: str) -> dict[str, Any]:
        """Move an unreadable file aside (never delete it) and start fresh."""
        assert self.path is not None
        backup = _unique_sibling(self.path, "corrupt")
        os.replace(self.path, backup)
        self.recovered_backup = str(backup)
        self.recovery_reason = reason[:TEXT_CAP]
        return _empty_data()

    def _save(self, data: dict[str, Any]) -> None:
        stamp = srs.to_iso(srs.utc_now())
        data["updated_at"] = stamp
        data["created_at"] = data.get("created_at") or stamp
        if self.path is None:
            self._memory = copy.deepcopy(data)
            return
        _atomic_write_json(self.path, data)


# ---------------------------------------------------------------- session helpers


def _active(data: dict[str, Any]) -> dict[str, Any] | None:
    return next((rec for rec in data["sessions"] if rec["status"] == ACTIVE), None)


def _latest_finished(data: dict[str, Any]) -> dict[str, Any]:
    for rec in reversed(data["sessions"]):
        if rec["status"] == FINISHED:
            return rec
    raise MockExamError("no finished mock exam to review")


def _find(data: dict[str, Any], session_id: str | None, *, want_active: bool) -> dict[str, Any]:
    if session_id is None:
        rec = _active(data)
        if rec is None:
            raise MockExamError("No mock exam is open; start one first.")
    else:
        key = str(session_id).strip()
        rec = next((item for item in data["sessions"] if item["id"] == key), None)
        if rec is None:
            raise MockExamError(f"unknown mock session {session_id!r}")
    if want_active and rec["status"] != ACTIVE:
        raise MockExamError(f"mock {rec['id']} is already {rec['status']}")
    return rec


def _new_id(moment: datetime, data: dict[str, Any]) -> str:
    taken = {rec["id"] for rec in data["sessions"]}
    while True:
        candidate = f"mock-{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def _status_payload(rec: dict[str, Any], moment: datetime) -> dict[str, Any]:
    started = srs.parse_iso(rec["started_at"])
    deadline = srs.parse_iso(rec["deadline"])
    is_open = rec["status"] == ACTIVE
    remaining = max(0.0, (deadline - moment).total_seconds()) if is_open else 0.0
    state = rec["status"]
    if is_open:
        state = "in_progress" if moment <= deadline else "time_up"
    total = len(rec["questions"])
    answered_ids = [q["id"] for q in rec["questions"] if q["id"] in rec["answers"]]
    unanswered_ids = [q["id"] for q in rec["questions"] if q["id"] not in rec["answers"]]
    domains: dict[str, dict[str, int]] = {}
    for name in _domain_order(rec):
        rows = [q for q in rec["questions"] if q["domain"] == name]
        if rows:
            domains[name] = {"questions": len(rows), "answered": sum(1 for q in rows if q["id"] in rec["answers"])}
    elapsed_until = moment if is_open else srs.parse_iso(rec.get("finished_at") or rec.get("abandoned_at") or rec["deadline"])
    payload: dict[str, Any] = {
        "ok": True,
        "session_id": rec["id"],
        "status": rec["status"],
        "state": state,
        "started_at": rec["started_at"],
        "deadline": rec["deadline"],
        "minutes": rec.get("minutes"),
        "remaining_seconds": int(remaining),
        "remaining": _duration_text(remaining),
        "elapsed_seconds": max(0, int((elapsed_until - started).total_seconds())),
        "total": total,
        "answered": len(answered_ids),
        "unanswered": len(unanswered_ids),
        "unanswered_ids": unanswered_ids,
        "flagged": list(rec["flagged"]),
        "flagged_count": len(rec["flagged"]),
        "late_answers": sum(1 for a in rec["answers"].values() if a.get("late")),
        "domains": domains,
        "bank": rec.get("bank"),
        "shortfalls": list(rec.get("shortfalls") or []),
    }
    if state == "time_up":
        payload["message"] = "Time is up: finish the mock to see your score (answers now need allow_late=True)."
    if rec["status"] == FINISHED:
        payload["finished_at"] = rec.get("finished_at")
        payload["result"] = rec.get("result")
    if rec["status"] == ABANDONED:
        payload["abandoned_at"] = rec.get("abandoned_at")
    return payload


def _option_rows(graded: dict[str, Any], *, correct: bool) -> list[dict[str, Any]]:
    rows = []
    for row in graded.get("explanations") or []:
        if not isinstance(row, dict):
            continue
        wanted = bool(row.get("is_correct")) if correct else bool(row.get("chosen")) and not row.get("is_correct")
        if wanted:
            rows.append(
                {
                    "letter": row.get("letter"),
                    "option": _cap(row.get("option"), OPTION_CAP),
                    "why": _cap(row.get("why")),
                }
            )
    return rows


def _grade_session(
    rec: dict[str, Any], adapter: Any, moment: datetime, target: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Score every session question; returns the report and the answered rows to save to progress."""
    items = {str(item["id"]): item for item in adapter.load_bank()}
    flagged = set(rec["flagged"])
    names = _domain_order(rec)
    weights = rec.get("weights") or {}
    domains = {
        name: {"weight": weights.get(name), "questions": 0, "answered": 0, "unanswered": 0, "correct": 0, "wrong": 0}
        for name in names
    }
    wrong: list[dict[str, Any]] = []
    ungraded: list[dict[str, Any]] = []
    to_record: list[dict[str, Any]] = []
    late = 0
    for number, question in enumerate(rec["questions"], 1):
        qid = question["id"]
        answer = rec["answers"].get(qid)
        chosen = list(answer["chosen"]) if answer else []
        try:
            graded = adapter.grade(qid, chosen)
        except ValueError as exc:
            ungraded.append({"number": number, "question_id": qid, "reason": _cap(exc, OPTION_CAP)})
            continue
        is_correct = bool(graded.get("correct")) and bool(answer)
        row = domains.setdefault(
            question["domain"],
            {"weight": None, "questions": 0, "answered": 0, "unanswered": 0, "correct": 0, "wrong": 0},
        )
        row["questions"] += 1
        row["answered" if answer else "unanswered"] += 1
        row["correct" if is_correct else "wrong"] += 1
        if answer and answer.get("late"):
            late += 1
        if answer:
            to_record.append(
                {
                    "question_id": qid,
                    "domain": question["domain"],
                    "objective_ids": list(graded.get("objective_ids") or question.get("objective_ids") or []),
                    "chosen": list(graded.get("chosen", chosen)),
                    "correct": list(graded.get("correct_answer") or []),
                    "is_correct": is_correct,
                }
            )
        if not is_correct:
            item = items.get(qid) or {}
            wrong.append(
                {
                    "number": number,
                    "question_id": qid,
                    "domain": question["domain"],
                    "objective_ids": list(question.get("objective_ids") or []),
                    "type": question["type"],
                    "answered": bool(answer),
                    "flagged": qid in flagged,
                    "late": bool(answer and answer.get("late")),
                    "stem": _cap(item.get("stem")),
                    "your_answer": _letters(chosen),
                    "correct_answer": list(graded.get("correct_letters") or _letters(graded.get("correct_answer") or [])),
                    "correct_options": _option_rows(graded, correct=True),
                    "your_wrong_choices": _option_rows(graded, correct=False),
                    "citations": [str(c) for c in (graded.get("citations") or [])][:CITATIONS_CAP],
                }
            )
    domains = {name: row for name, row in domains.items() if row["questions"]}
    for row in domains.values():
        row["pct"] = _pct(row["correct"], row["questions"])

    graded_total = sum(row["questions"] for row in domains.values())
    correct = sum(row["correct"] for row in domains.values())
    answered = sum(row["answered"] for row in domains.values())
    score_pct = _pct(correct, graded_total)
    started = srs.parse_iso(rec["started_at"])
    deadline = srs.parse_iso(rec["deadline"])
    used = max(0.0, (moment - started).total_seconds())
    limit = max(0.0, (deadline - started).total_seconds())
    below = [name for name, row in domains.items() if row["pct"] is not None and row["pct"] < target]
    meets = score_pct is not None and score_pct >= target
    if meets:
        verdict = f"At or above your self-set {target:g}% target on this mock."
    else:
        verdict = f"Below your self-set {target:g}% target on this mock; review the wrong questions first."
    notes: list[str] = []
    if graded_total != DEFAULT_COUNT:
        notes.append(f"This mock scored {graded_total} questions; a full mock has {DEFAULT_COUNT}.")
    if ungraded:
        notes.append(f"{len(ungraded)} question(s) are no longer in the bank and were left out of the score.")
    if late:
        notes.append(f"{late} answer(s) were given after the deadline (allow_late) and still count here.")
    report = {
        "ok": True,
        "session_id": rec["id"],
        "status": FINISHED,
        "started_at": rec["started_at"],
        "deadline": rec["deadline"],
        "finished_at": srs.to_iso(moment),
        "bank": rec.get("bank"),
        "score": {
            "questions": graded_total,
            "answered": answered,
            "unanswered": graded_total - answered,
            "correct": correct,
            "wrong": graded_total - correct,
            "pct": score_pct,
            "accuracy_on_answered_pct": _pct(correct, answered),
            "late_answers": late,
        },
        "domains": domains,
        "time": {
            "limit_seconds": int(limit),
            "used_seconds": int(used),
            "used": _duration_text(used),
            "over_time_seconds": int(max(0.0, used - limit)),
            "finished_after_deadline": moment > deadline,
        },
        "wrong_count": len(wrong),
        "wrong": wrong,
        "flagged": list(rec["flagged"]),
        "ungraded": ungraded,
        "readiness": {
            "target_pct": target,
            "score_pct": score_pct,
            "meets_target": meets,
            "domains_below_target": below,
            "verdict": verdict,
            "note": READINESS_NOTE,
        },
        "notes": notes,
    }
    return report, to_record


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    """What a finished session keeps on disk (the full review can be rebuilt with review())."""
    return {
        "score": dict(report["score"]),
        "domains": copy.deepcopy(report["domains"]),
        "time": dict(report["time"]),
        "readiness": {key: report["readiness"][key] for key in ("target_pct", "score_pct", "meets_target", "domains_below_target")},
        "wrong_ids": [row["question_id"] for row in report["wrong"]],
        "ungraded_ids": [row["question_id"] for row in report["ungraded"]],
        "progress": None,
    }


def _record_progress(
    store: Any, rows: list[dict[str, Any]], report: dict[str, Any], moment: datetime
) -> dict[str, Any]:
    recorded = 0
    errors: list[str] = []
    for row in rows:
        try:
            store.record_answer(
                row["question_id"],
                domain=row["domain"],
                objective_ids=row["objective_ids"],
                chosen=row["chosen"],
                correct=row["correct"],
                is_correct=row["is_correct"],
                seconds=None,
                source=SOURCE,
                now=moment,
            )
            recorded += 1
        except (TypeError, ValueError, OSError) as exc:
            if len(errors) < 10:
                errors.append(f"{row['question_id']}: {_cap(exc, OPTION_CAP)}")
    path = getattr(store, "path", None)
    return {
        "recorded": recorded,
        "not_recorded_unanswered": report["score"]["unanswered"],
        "errors": errors,
        "source": SOURCE,
        "path": str(path) if path else None,
        "persisted": bool(path) and not getattr(store, "memory_only", False),
    }


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_MINUTES",
    "ItemBank",
    "MockExamError",
    "MockExamStore",
    "TARGET_PCT",
    "allocate",
    "apportion",
    "default_sessions_path",
    "official_weights",
]
