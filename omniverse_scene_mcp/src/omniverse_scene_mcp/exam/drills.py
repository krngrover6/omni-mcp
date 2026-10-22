"""Timed NCP-OUSD drills: short rounds (default 10 questions, 45 s each) on one focus, graded in one go.

Focuses: ``weak`` (from saved progress and spaced repetition), ``mistakes`` (recently wrong), ``mixed`` (whole bank
at exam weights), ``objective`` / ``domain`` (one study-guide area) and ``flash`` (true/false and one-word prompts
made only from existing bank items). Questions come from the shipped bank (``bank.py``); answers are saved through
``ProgressStore.record_answer(source='drill')`` (flash prompts are not saved: they test one option, not a question).

Drill sessions live in ``drill_sessions.json`` beside the progress file (``$OMNIVERSE_EXAM_PREP_DIR`` or
``<workspace>/exam-prep``), or only in this process when the progress store is memory-only. Answers are never
written into a session; grading looks them up in the bank.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import random
import re
import secrets
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import bank as exam_bank
from . import mock as exam_mock
from . import srs
from .progress import ENV_DIR, ProgressStore, workspace_root

SCHEMA_VERSION = 1
FILE_NAME = "drill_sessions.json"
PROGRESS_FILE_NAME = "progress.json"
MOCK_FILE_NAME = exam_mock.FILE_NAME
SOURCE = "drill"
TEST_ENV = "OMNIVERSE_SCENE_MCP_TEST"
_OFF_VALUES = {"", "0", "false", "no", "off"}

DEFAULT_COUNT = 10
MAX_COUNT = 30
DEFAULT_SECONDS = 45
MIN_SECONDS = 10
MAX_SECONDS = 300
MAX_SESSIONS_KEPT = 100
MAX_MEMORY_SESSIONS = 200
HISTORY_LOOKBACK = 200
DUE_LOOKBACK = 100
WEAK_ACCURACY = 0.8
TEXT_CAP = 600
CHOICES_CAP = 10
ONE_WORD_MAX = 40
ONE_WORD_SHARE = 0.4
LETTERS = exam_bank.LETTERS

ACTIVE = "active"
FINISHED = "finished"

FOCUSES: dict[str, dict[str, str]] = {
    "weak": {
        "needs": "",
        "explained": "Questions you got wrong last time, cards due for spaced-repetition review and questions from your "
        "lowest-accuracy objectives and domains. With no saved answers yet it becomes a mixed drill.",
    },
    "mistakes": {
        "needs": "",
        "explained": "Only questions whose most recent saved answer was wrong (newest first), then ones you missed "
        "earlier but later fixed. Nothing is padded in: fewer mistakes means a shorter drill.",
    },
    "mixed": {
        "needs": "",
        "explained": "A random draw from the whole bank, split across domains by the official exam weights.",
    },
    "objective": {
        "needs": "objective",
        "explained": "Questions tagged with one study-guide objective, such as '1.6'. Due reviews come first.",
    },
    "domain": {
        "needs": "domain",
        "explained": "Questions from one exam domain (name, prefix such as 'comp', or number such as 3). "
        "Due reviews come first.",
    },
    "flash": {
        "needs": "",
        "explained": "Quick concept flashes: true/false statements and one-word answers built only from bank questions "
        "(an option paired with its question). Optional objective or domain filter. Not saved to progress.",
    },
}

HOW_TO_ANSWER = (
    "Answer every question at once with answer_drill(drill_id, answers). answers is a dict {question id or number: "
    "choice} or a list in question order (None skips one). A choice is a letter ('B'), a 0-based index (1), the "
    "full option text, or a list / 'A,C' for select-several questions. Numbers always mean 0-based option indices, "
    "never option text. Flash prompts take True/False (or A/B) and one-word prompts take the word."
)
FLASH_PROGRESS_NOTE = (
    "Flash prompts check one option of a question, not the whole question, so they are not saved to progress or "
    "spaced repetition."
)

_MEMORY: dict[str, dict[str, Any]] = {}
_CUSTOM_BANKS: dict[str, Any] = {}
_TRUE_WORDS = {"true", "t", "yes", "y", "a"}
_FALSE_WORDS = {"false", "f", "no", "n", "b"}
_LETTER_ONLY = re.compile(r"^\(?([A-Fa-f])[.)]?$")
_LETTER_AND_TEXT = re.compile(r"^\(?([A-Fa-f])[.)]\s+(.+)$", re.S)
_ONE_WORD = re.compile(r"^[A-Za-z0-9_][\w:.\-/]*$")


class DrillError(ValueError):
    """A drill request that cannot be carried out (unknown id, already answered, unreadable answer, ...)."""


# ---------------------------------------------------------------- public API


def list_drill_focuses() -> dict[str, Any]:
    """The drill focuses, what each one picks and which argument it needs."""
    return {
        "ok": True,
        "focuses": [{"name": name, "needs": info["needs"] or None, "explained": info["explained"]}
                    for name, info in FOCUSES.items()],
        "defaults": {"count": DEFAULT_COUNT, "seconds_per_question": DEFAULT_SECONDS},
        "limits": {"count": [1, MAX_COUNT], "seconds_per_question": [MIN_SECONDS, MAX_SECONDS]},
        "how_to_answer": HOW_TO_ANSWER,
    }


def start_drill(
    store: Any = None,
    *,
    focus: str = "weak",
    objective: str | None = None,
    domain: str | int | None = None,
    count: int = DEFAULT_COUNT,
    seconds_per_question: float = DEFAULT_SECONDS,
    seed: int | None = None,
    bank: Any = None,
    exclude_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start a timed drill and return its questions (answers withheld) and time limit.

    ``store`` is the ProgressStore used to find weak spots and mistakes and to save answers (default: the exam-prep
    store, memory-only in test mode). ``bank`` is an optional custom question list (as for mock exams). Questions of
    an open mock exam and ``exclude_ids`` are never served, because a drill shows its answers.
    """
    try:
        return _start(store, focus, objective, domain, count, seconds_per_question, seed, bank, exclude_ids, now)
    except (DrillError, ValueError, TypeError) as exc:
        return _error(exc)


def answer_drill(
    drill_id: str,
    answers: Mapping[Any, Any] | list[Any] | tuple[Any, ...],
    *,
    elapsed_s: float | None = None,
    store: Any = None,
    bank: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grade a whole drill in one go, save the answers to progress (source 'drill') and close the drill.

    ``elapsed_s`` is the time you took; when omitted it is measured from the drill's start. An answer that cannot be
    read returns ok=False with the problems listed and leaves the drill open, so it can be sent again.
    """
    try:
        return _answer(drill_id, answers, elapsed_s, store, bank, now)
    except (DrillError, ValueError, TypeError) as exc:
        return _error(exc, drill_id=drill_id)


def drill_status(
    drill_id: str, *, store: Any = None, bank: Any = None, now: datetime | None = None
) -> dict[str, Any]:
    """Time left and the questions again for an open drill, or the saved result of an answered one."""
    try:
        return _status(drill_id, store, bank, now)
    except (DrillError, ValueError, TypeError) as exc:
        return _error(exc, drill_id=drill_id)


# ---------------------------------------------------------------- start


def _start(store, focus, objective, domain, count, seconds, seed, bank, exclude_ids, now) -> dict[str, Any]:
    moment = srs.as_utc(now).replace(microsecond=0)
    notes: list[str] = []
    focus_name = _clean_focus(focus)
    total = _clean_count(count, notes)
    per_question = _clean_seconds(seconds, notes)
    if seed is None:
        seed = secrets.randbelow(2**31)
    elif isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be a whole number")
    if focus_name == "objective" and objective is None:
        raise DrillError("focus='objective' needs objective, for example objective='1.6'")
    if focus_name == "domain" and domain is None:
        raise DrillError("focus='domain' needs domain, for example domain='Composition' or domain='comp'")

    progress = _progress_store(store)
    adapter = exam_mock._adapt(bank)
    bank_label = "default" if adapter is exam_bank else "custom"
    items = [item for item in adapter.load_bank() if isinstance(item, dict)]
    if not items:
        raise DrillError("the question bank is empty, so there is nothing to drill")
    blocked = {str(qid) for qid in exclude_ids or ()} | _open_mock_ids(progress)
    pool = [item for item in items if str(item["id"]) not in blocked]
    if blocked & {str(item["id"]) for item in items}:
        notes.append("Questions from your open mock exam (or excluded ids) were left out.")
    if not pool:
        raise DrillError("every question is excluded (open mock exam or exclude_ids); finish the mock first")

    filters = _filters(focus_name, objective, domain, items)
    pool = [item for item in pool if _matches(item, filters)]
    if not pool:
        raise DrillError(f"no questions match {_filter_text(filters)}")
    rng = random.Random(seed)

    if focus_name == "weak":
        picked, explained = _pick_weak(pool, progress, total, rng, moment)
    elif focus_name == "mistakes":
        picked, explained = _pick_mistakes(pool, progress, total)
        if not picked:
            raise DrillError(
                "No wrong answers are saved yet" + (f" for {_filter_text(filters)}" if filters else "")
                + ", so there is no mistakes drill. Try focus='weak' or focus='mixed' first."
            )
    elif focus_name == "mixed":
        picked = _pick_mixed(pool, total, rng)
        explained = FOCUSES["mixed"]["explained"]
    elif focus_name in ("objective", "domain"):
        picked = _pick_filtered(pool, progress, total, rng, moment)
        explained = f"{len(picked)} question(s) on {_filter_text(filters)}; due reviews first, then a random draw."
    else:  # flash
        picked = _pick_mixed(pool, total, rng) if not filters else _shuffled(pool, rng)[:total]
        explained = FOCUSES["flash"]["explained"] + (f" Filter: {_filter_text(filters)}." if filters else "")

    if len(picked) < total:
        notes.append(f"Only {len(picked)} of the {total} requested questions fit this focus.")
    limit_s = int(round(per_question * len(picked)))
    deadline = moment + timedelta(seconds=limit_s)

    if focus_name == "flash":
        entries, views = _flash_prompts(picked, rng)
    else:
        entries = [
            {
                "id": str(item["id"]),
                "kind": "question",
                "domain": str(item["domain"]),
                "objective_ids": [str(o) for o in item.get("objective_ids") or []],
                "type": item["type"],
                "option_count": len(item["options"]),
                "select": len(item["answer"]) if item["type"] == "multi" else 1,
            }
            for item in picked
        ]
        views = [{"number": n, **exam_mock._view(adapter, item)} for n, item in enumerate(picked, 1)]

    sessions = _Sessions.for_store(progress)
    rec = {
        "id": "",
        "status": ACTIVE,
        "focus": focus_name,
        "filters": filters,
        "started_at": srs.to_iso(moment),
        "deadline": srs.to_iso(deadline),
        "seconds_per_question": per_question,
        "time_limit_s": limit_s,
        "requested_count": total,
        "seed": seed,
        "bank": bank_label,
        "questions": entries,
        "finished_at": None,
        "result": None,
    }
    drill_id = sessions.add(rec, moment, progress)
    if bank_label == "custom":
        _CUSTOM_BANKS[drill_id] = adapter
        while len(_CUSTOM_BANKS) > MAX_MEMORY_SESSIONS:
            _CUSTOM_BANKS.pop(next(iter(_CUSTOM_BANKS)))
    return {
        "ok": True,
        "drill_id": drill_id,
        "focus": focus_name,
        "focus_explained": explained,
        "filters": filters,
        "count": len(entries),
        "requested_count": total,
        "seconds_per_question": per_question,
        "time_limit_s": limit_s,
        "started_at": rec["started_at"],
        "deadline": rec["deadline"],
        "seed": seed,
        "bank": bank_label,
        "questions": views,
        "how_to_answer": HOW_TO_ANSWER,
        "notes": notes,
        "storage": sessions.info(),
    }


def _clean_focus(value: Any) -> str:
    text = str(value).strip().lower() if isinstance(value, str) else ""
    if text not in FOCUSES:
        raise DrillError(f"unknown focus {value!r}; choose one of {', '.join(FOCUSES)}")
    return text


def _clean_count(value: Any, notes: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("count must be a whole number")
    if int(value) != value:
        raise ValueError("count must be a whole number")
    number = int(value)
    if not 1 <= number <= MAX_COUNT:
        notes.append(f"count {number} is outside 1-{MAX_COUNT}; clamped.")
    return max(1, min(MAX_COUNT, number))


def _clean_seconds(value: Any, notes: list[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("seconds_per_question must be a number")
    if not MIN_SECONDS <= value <= MAX_SECONDS:
        notes.append(f"seconds_per_question {value} is outside {MIN_SECONDS}-{MAX_SECONDS}; clamped.")
    number = max(MIN_SECONDS, min(MAX_SECONDS, value))
    return int(number) if float(number).is_integer() else round(float(number), 1)


def _filters(focus: str, objective: Any, domain: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if objective is not None:
        text = str(objective).strip()
        known = set(exam_bank.OBJECTIVE_IDS) | {str(o) for item in items for o in item.get("objective_ids") or []}
        if isinstance(objective, bool) or text not in known:
            raise DrillError(f"unknown objective {objective!r}; use a study-guide number such as '1.6'")
        filters["objective"] = text
    if domain is not None:
        names = list(exam_mock.official_weights())
        names += sorted({str(item["domain"]) for item in items} - set(names))
        try:
            filters["domain"] = exam_mock._resolve_domain(domain, names)
        except ValueError as exc:
            raise DrillError(str(exc)) from None
    return filters


def _matches(item: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "objective" in filters and filters["objective"] not in [str(o) for o in item.get("objective_ids") or []]:
        return False
    return "domain" not in filters or str(item["domain"]) == filters["domain"]


def _filter_text(filters: dict[str, Any]) -> str:
    parts = []
    if "objective" in filters:
        parts.append(f"objective {filters['objective']}")
    if "domain" in filters:
        parts.append(f"domain {filters['domain']}")
    return " and ".join(parts) or "the whole bank"


def _okey(objective: str) -> tuple[int, list[int], str]:
    """Sort key for objective ids: study-guide numbers in numeric order, anything else after them."""
    if re.fullmatch(r"\d+(\.\d+)*", objective):
        return (0, [int(part) for part in objective.split(".")], objective)
    return (1, [], objective)


def _shuffled(pool: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    ordered = sorted(pool, key=lambda item: str(item["id"]))
    rng.shuffle(ordered)
    return ordered


def _pick_mixed(pool: list[dict[str, Any]], total: int, rng: random.Random) -> list[dict[str, Any]]:
    """Official-weight split across domains (mock.allocate), each domain shuffled; uniform if no weight applies."""
    pools: dict[str, list[dict[str, Any]]] = {}
    for item in pool:
        pools.setdefault(str(item["domain"]), []).append(item)
    weights = exam_mock._resolve_weights(None, pools)
    available = {name: len(pools.get(name, [])) for name in weights}
    allocation, _ = exam_mock.allocate(weights, available, total)
    picked: list[dict[str, Any]] = []
    for name in weights:
        picked.extend(_shuffled(pools.get(name, []), rng)[: allocation[name]])
    if len(picked) < total:  # domains outside the official weights, or a tiny bank
        chosen = {str(item["id"]) for item in picked}
        rest = [item for item in pool if str(item["id"]) not in chosen]
        picked.extend(_shuffled(rest, rng)[: total - len(picked)])
    rng.shuffle(picked)
    return picked


def _due_ids(progress: Any, moment: datetime) -> list[str]:
    try:
        return [str(row["question_id"]) for row in progress.due_reviews(limit=DUE_LOOKBACK, now=moment)]
    except (AttributeError, TypeError, ValueError, KeyError):
        return []


def _latest_results(progress: Any) -> tuple[dict[str, bool], list[str]]:
    """Latest saved result per question (from history, newest first) and every id ever answered wrong, newest first."""
    try:
        rows = progress.history(limit=HISTORY_LOOKBACK)
    except (AttributeError, TypeError, ValueError):
        return {}, []
    latest: dict[str, bool] = {}
    ever_wrong: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("question_id"):
            continue
        qid = str(row["question_id"])
        ok = row.get("is_correct") is True
        latest.setdefault(qid, ok)
        if not ok and qid not in ever_wrong:
            ever_wrong.append(qid)
    return latest, ever_wrong


def _pick_filtered(pool, progress, total, rng, moment) -> list[dict[str, Any]]:
    due = _due_ids(progress, moment)
    by_id = {str(item["id"]): item for item in pool}
    first = [by_id[qid] for qid in dict.fromkeys(due) if qid in by_id][:total]
    taken = {str(item["id"]) for item in first}
    rest = [item for item in _shuffled(pool, rng) if str(item["id"]) not in taken]
    picked = first + rest[: total - len(first)]
    rng.shuffle(picked)
    return picked


def _pick_mistakes(pool, progress, total) -> tuple[list[dict[str, Any]], str]:
    latest, ever_wrong = _latest_results(progress)
    by_id = {str(item["id"]): item for item in pool}
    still_wrong = [qid for qid in ever_wrong if latest.get(qid) is False and qid in by_id]
    fixed = [qid for qid in ever_wrong if latest.get(qid) is True and qid in by_id]
    ids = (still_wrong + fixed)[:total]
    picked = [by_id[qid] for qid in ids]
    wrong_now = min(len(still_wrong), total)
    explained = f"{wrong_now} question(s) whose latest saved answer was wrong"
    if len(ids) > wrong_now:
        explained += f", plus {len(ids) - wrong_now} you missed before and later got right"
    explained += f" (from your last {HISTORY_LOOKBACK} saved answers, newest first)."
    return picked, explained


def _pick_weak(pool, progress, total, rng, moment) -> tuple[list[dict[str, Any]], str]:
    try:
        summary = progress.summary(now=moment)
    except (AttributeError, TypeError, ValueError):
        summary = {}
    answered = int(((summary or {}).get("totals") or {}).get("answered") or 0)
    if not answered:
        explained = ("No saved answers yet, so there are no weak spots to target: this is a mixed drill split by the "
                     "official exam weights. Your answers will shape the next weak drill.")
        return _pick_mixed(pool, total, rng), explained
    latest, _ = _latest_results(progress)
    due = set(_due_ids(progress, moment))
    weak_objectives = {
        str(row["objective_id"]): row.get("accuracy")
        for row in summary.get("weakest_objectives") or []
        if isinstance(row, dict) and row.get("objective_id")
    }
    weak_domains = {
        str(name): row.get("accuracy")
        for name, row in (summary.get("domains") or {}).items()
        if isinstance(row, dict) and row.get("accuracy") is not None and row["accuracy"] < WEAK_ACCURACY
    }
    answered_domains = {str(name) for name, row in (summary.get("domains") or {}).items()
                        if isinstance(row, dict) and row.get("answered")}
    order = {str(item["id"]): index for index, item in enumerate(_shuffled(pool, rng))}
    scored = []
    for item in pool:
        qid = str(item["id"])
        score = 0
        score += 4 if latest.get(qid) is False else 0
        score += 2 if qid in due else 0
        score += 2 if any(str(o) in weak_objectives for o in item.get("objective_ids") or []) else 0
        score += 1 if str(item["domain"]) in weak_domains else 0
        score -= 1 if latest.get(qid) is True and qid not in due else 0
        if score > 0:
            scored.append((-score, order[qid], item))
    scored.sort(key=lambda row: (row[0], row[1]))
    picked = [row[2] for row in scored[:total]]
    filled = 0
    if len(picked) < total:
        # Fill with questions never answered, from domains not tried yet first, then the rest of the bank.
        taken = {str(item["id"]) for item in picked}
        unseen = [item for item in pool if str(item["id"]) not in taken and str(item["id"]) not in latest]
        untested = [item for item in unseen if str(item["domain"]) not in answered_domains]
        others = [item for item in unseen if str(item["domain"]) in answered_domains]
        extra = _pick_mixed(untested, total - len(picked), rng) if untested else []
        if len(picked) + len(extra) < total and others:
            extra += _pick_mixed(others, total - len(picked) - len(extra), rng)
        filled = len(extra)
        picked += extra
    rng.shuffle(picked)
    parts = []
    wrong_n = sum(1 for item in picked if latest.get(str(item["id"])) is False)
    due_n = sum(1 for item in picked if str(item["id"]) in due)
    if wrong_n:
        parts.append(f"{wrong_n} you got wrong last time")
    if due_n:
        parts.append(f"{due_n} due for spaced-repetition review")
    if weak_objectives:
        parts.append("weakest objectives " + ", ".join(sorted(weak_objectives, key=_okey)[:5]))
    if weak_domains:
        lowest = sorted(weak_domains, key=lambda name: (weak_domains[name], name))[:3]
        parts.append(f"lowest-accuracy domains under {WEAK_ACCURACY:.0%}: " + ", ".join(lowest))
    explained = "Targets " + ("; ".join(parts) if parts else "your saved results") + "."
    if filled:
        explained += f" {filled} unseen question(s) were added because there were not enough weak ones."
    return picked, explained


def _open_mock_ids(progress: Any) -> set[str]:
    path = getattr(progress, "path", None)
    if path is None or getattr(progress, "memory_only", False):
        return set()
    target = Path(path).with_name(MOCK_FILE_NAME)
    if not target.exists():
        return set()
    try:
        return exam_mock.MockExamStore(target).open_question_ids()
    except (OSError, ValueError, TypeError):
        return set()


# ---------------------------------------------------------------- flash prompts


def _flash_prompts(items: list[dict[str, Any]], rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """True/false or one-word prompts from bank items; the record keeps only (question, kind, option), no answer."""
    entries: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for number, item in enumerate(items, 1):
        answer = sorted(item["answer"])
        options = item["options"]
        word = options[answer[0]].strip() if item["type"] == "single" else ""
        can_word = (
            bool(word) and len(word) <= ONE_WORD_MAX and bool(_ONE_WORD.match(word))
            and "following" not in item["stem"].lower()
            and len({_norm(o) for o in options}) == len(options)
        )
        flash_id = f"flash-{number}"
        base = {
            "id": flash_id,
            "question_id": str(item["id"]),
            "domain": str(item["domain"]),
            "objective_ids": [str(o) for o in item.get("objective_ids") or []],
        }
        view: dict[str, Any] = {"number": number, "id": flash_id, "question_id": str(item["id"]),
                                "domain": str(item["domain"]), "objective_ids": list(base["objective_ids"])}
        if item.get("usda"):
            view["usda"] = dict(item["usda"])
            view["root"] = item.get("root") or next(iter(item["usda"]))
        if can_word and rng.random() < ONE_WORD_SHARE:
            entries.append({**base, "kind": "one_word", "option": answer[0]})
            view.update(
                kind="one_word",
                prompt=f"Type the one-word answer (options hidden): {item['stem']}",
                hint=f"{len(word)} characters, starts with '{word[0]}'",
            )
        else:
            option = rng.choice(answer) if rng.random() < 0.5 else rng.choice(range(len(options)))
            entries.append({**base, "kind": "true_false", "option": int(option)})
            if item["type"] == "multi":
                ask = f"Is this one of the correct choices? {options[option]}"
            else:
                ask = f"Proposed answer: {options[option]}"
            view.update(
                kind="true_false",
                prompt=f"True or false? Question: {item['stem']}\n{ask}",
                options=["A. True", "B. False"],
            )
        views.append(view)
    return entries, views


def _norm(text: Any) -> str:
    """Compare form of an answer: whitespace collapsed, surrounding quotes/backticks and one final period removed."""
    value = " ".join(str(text).split())
    previous = None
    while value != previous:
        previous = value
        value = value.strip().strip("`'\"")
        if value.endswith(".") and not value.endswith(".."):
            value = value[:-1]
    return value.casefold()


def _same_word(given: str, expected: str) -> bool:
    """Same text ignoring case, surrounding quotes/backticks and a final period; numbers compare by value."""
    if _norm(given) == _norm(expected):
        return True
    try:
        a, b = float(_norm(given)), float(_norm(expected))
    except ValueError:
        return False
    return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _parse_true_false(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().rstrip(".").strip().casefold()
        if not text:
            return None
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    raise DrillError(f"{raw!r} is not True or False; answer True/False (or the letter A/B). Numbers are not accepted "
                     "here because 1 could mean True or option B.")


def _parse_word(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise DrillError(f"{raw!r} is not a word; type the answer as text")
    return raw.strip()[:ONE_WORD_MAX * 2] or None


# ---------------------------------------------------------------- answers


def _parse_choice(raw: Any, options: list[str]) -> list[int]:
    """Letters, 0-based indices, option text (also 'B. text'), lists or 'A,C' -> sorted option indices ([] = skipped)."""
    if raw is None:
        return []
    if isinstance(raw, bool):
        raise DrillError(f"{raw!r} is not an option; use a letter, a 0-based index or the option text")
    if isinstance(raw, int):
        return [_coerce(raw, options)]
    if isinstance(raw, str):
        if not raw.strip():
            return []
        index = _single(raw, options)
        if index is not None:
            return [index]
        try:
            return exam_mock._parse_chosen(raw, len(options))
        except ValueError:
            raise DrillError(_unreadable(raw, options)) from None
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = sorted(raw, key=str) if isinstance(raw, (set, frozenset)) else list(raw)
        if len(values) > CHOICES_CAP:
            raise DrillError(f"at most {CHOICES_CAP} choices per question")
        picked: set[int] = set()
        for value in values:
            if isinstance(value, (list, tuple, set, frozenset, dict)):
                raise DrillError("choices inside a list must be letters, indices or option text")
            picked.update(_parse_choice(value, options))
        return sorted(picked)
    raise DrillError(f"{raw!r} is not an answer; use a letter, a 0-based index, option text or a list of them")


def _coerce(value: Any, options: list[str]) -> int:
    try:
        return exam_mock._coerce_choice(value, len(options))
    except ValueError:
        raise DrillError(_unreadable(value, options)) from None


def _single(text: str, options: list[str]) -> int | None:
    """One choice written as a letter, an index, 'B.' / '(b)', 'B. option text' or the option text itself."""
    value = text.strip()
    if value.isdigit():
        return _coerce(value, options)
    match = _LETTER_ONLY.match(value)
    if match:
        return _coerce(match.group(1), options)
    wanted = _norm(value)
    found = [i for i, option in enumerate(options) if _norm(option) == wanted]
    if len(found) == 1:
        return found[0]
    match = _LETTER_AND_TEXT.match(value)
    if match:
        index = _coerce(match.group(1), options)
        rest = _norm(match.group(2))
        if rest == _norm(options[index]):
            return index
        others = [i for i, option in enumerate(options) if _norm(option) == rest]
        if others:
            raise DrillError(f"{text!r}: letter {LETTERS[index]} does not match that option text "
                             f"(the text is option {LETTERS[others[0]]})")
    return None


def _unreadable(raw: Any, options: list[str]) -> str:
    last = len(options) - 1
    return (f"could not read {raw!r}: use a letter A-{LETTERS[last]}, a 0-based index 0-{last} or the full "
            "option text")


def _answers_by_entry(rec: dict[str, Any], answers: Any) -> dict[str, Any]:
    entries = rec["questions"]
    if isinstance(answers, Mapping):
        mapped: dict[str, Any] = {}
        for key, value in answers.items():
            entry = _entry(entries, key)
            if entry is None:
                raise DrillError(f"{key!r} is not a question of drill {rec['id']}; use its id or 1-based number")
            if entry["id"] in mapped:
                raise DrillError(f"question {entry['id']} is answered twice")
            mapped[entry["id"]] = value
        return mapped
    if isinstance(answers, (list, tuple)):
        if len(answers) > len(entries):
            raise DrillError(f"{len(answers)} answers for {len(entries)} questions")
        return {entries[i]["id"]: value for i, value in enumerate(answers)}
    raise DrillError("answers must be a dict {question id or number: choice} or a list in question order")


def _entry(entries: list[dict[str, Any]], key: Any) -> dict[str, Any] | None:
    if isinstance(key, bool):
        return None
    text = str(key).strip()
    for entry in entries:
        if entry["id"] == text:
            return entry
    for entry in entries:  # flash prompts can also be keyed by their bank question id
        if entry.get("question_id") == text:
            return entry
    if text.isdigit() and 1 <= int(text) <= len(entries):
        return entries[int(text) - 1]
    return None


def _answer(drill_id, answers, elapsed_s, store, bank, now) -> dict[str, Any]:
    moment = srs.as_utc(now).replace(microsecond=0)
    sessions, rec, progress = _locate(drill_id, store)
    if rec["status"] != ACTIVE:
        raise DrillError(f"drill {rec['id']} was already answered; see drill_status('{rec['id']}') for the result")
    adapter = _bank_for(rec, bank)
    items = {str(item["id"]): item for item in adapter.load_bank() if isinstance(item, dict)}
    given = _answers_by_entry(rec, answers)
    elapsed = _elapsed(elapsed_s, rec, moment)

    # Read every answer first, so one unreadable answer changes nothing.
    parsed: dict[str, Any] = {}
    problems: list[str] = []
    missing: list[str] = []
    for number, entry in enumerate(rec["questions"], 1):
        qid = entry.get("question_id") or entry["id"]
        item = items.get(qid)
        if item is None:
            missing.append(qid)
            continue
        raw = given.get(entry["id"])
        try:
            if entry["kind"] == "true_false":
                parsed[entry["id"]] = _parse_true_false(raw)
            elif entry["kind"] == "one_word":
                parsed[entry["id"]] = _parse_word(raw)
            else:
                picked = _parse_choice(raw, item["options"])
                if item["type"] == "single" and len(picked) > 1:
                    raise DrillError("this question has one answer; choose one option")
                parsed[entry["id"]] = picked
        except DrillError as exc:
            problems.append(f"question {number} ({entry['id']}): {exc}")
    if problems:
        return {
            "ok": False,
            "drill_id": rec["id"],
            "error": "Some answers could not be read; nothing was graded or saved. Fix them and send all answers again.",
            "problems": problems[:20],
            "how_to_answer": HOW_TO_ANSWER,
        }

    rows: list[dict[str, Any]] = []
    to_record: list[dict[str, Any]] = []
    for number, entry in enumerate(rec["questions"], 1):
        qid = entry.get("question_id") or entry["id"]
        item = items.get(qid)
        if item is None:
            continue
        if entry["kind"] == "question":
            row, record = _grade_question(number, entry, item, parsed[entry["id"]], adapter)
            if record:
                to_record.append(record)
        else:
            row = _grade_flash(number, entry, item, parsed[entry["id"]])
        rows.append(row)

    total = len(rows)
    correct = sum(1 for row in rows if row["right"])
    answered = sum(1 for row in rows if row["answered"])
    limit = int(rec["time_limit_s"])
    too_slow = elapsed > limit
    pct = round(100.0 * correct / total, 1) if total else None
    weak_spots = _weak_spots(rows)
    is_flash = rec["focus"] == "flash"
    if is_flash:
        progress_info = {"recorded": 0, "source": SOURCE, "note": FLASH_PROGRESS_NOTE}
    else:
        seconds = round(elapsed / max(1, answered), 1) if answered else None
        progress_info = _record(progress, to_record, seconds, moment)
    notes = []
    if missing:
        notes.append(f"{len(missing)} question(s) are no longer in the bank and were left out: {', '.join(missing[:5])}.")
    if answered < total:
        notes.append(f"{total - answered} question(s) were skipped; they count as wrong and are not saved to progress.")
    result = {
        "ok": True,
        "drill_id": rec["id"],
        "focus": rec["focus"],
        "filters": rec.get("filters") or {},
        "score": pct,
        "correct": correct,
        "total": total,
        "answered": answered,
        "skipped": total - answered,
        "elapsed_s": round(elapsed, 1),
        "time_limit_s": limit,
        "seconds_per_question_used": round(elapsed / total, 1) if total else None,
        "too_slow": too_slow,
        "per_question": rows,
        "weak_spots": weak_spots,
        "next_drill_suggestion": _suggest(rec, rows, weak_spots, too_slow),
        "encouragement": _encourage(correct, total, too_slow, elapsed, limit),
        "progress": progress_info,
        "notes": notes,
    }
    if too_slow:
        result["time_note"] = (f"You took {round(elapsed)} s for a {limit} s limit ({round(elapsed - limit)} s over). "
                               "The exam averages under 2 minutes per question, so speed matters too.")
    sessions.finish(rec["id"], moment, _compact(result))
    return result


def _elapsed(value: Any, rec: dict[str, Any], moment: datetime) -> float:
    if value is None:
        return max(0.0, (moment - srs.parse_iso(rec["started_at"])).total_seconds())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise DrillError("elapsed_s must be a number of seconds >= 0")
    return float(value)


def _cap(text: Any, limit: int = TEXT_CAP) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _grade_question(number, entry, item, picked, adapter) -> tuple[dict[str, Any], dict[str, Any] | None]:
    graded = adapter.grade(item["id"], list(picked))
    answer = sorted(graded.get("correct_answer") or item["answer"])
    right = bool(picked) and bool(graded.get("correct"))
    whys = item.get("explanations") or [""] * len(item["options"])
    parts = [f"Correct: {LETTERS[i]}. {item['options'][i]} - {whys[i]}" for i in answer]
    parts += [f"You chose {LETTERS[i]}: {whys[i]}" for i in picked if i not in answer]
    row = {
        "number": number,
        "id": entry["id"],
        "kind": "question",
        "your_answer": [LETTERS[i] for i in picked],
        "correct_answer": [LETTERS[i] for i in answer],
        "right": right,
        "answered": bool(picked),
        "explanation": _cap(" ".join(p.strip() for p in parts if p.strip())),
        "objective": entry["objective_ids"][0] if entry["objective_ids"] else None,
        "objective_ids": list(entry["objective_ids"]),
        "domain": entry["domain"],
        "citations": [str(c) for c in item.get("citations") or []][:3],
    }
    if item["type"] == "multi" and picked and len(picked) != len(answer):
        row["note"] = f"This question asks for {len(answer)} choices; you gave {len(picked)}."
    record = None
    if picked:
        record = {
            "question_id": str(item["id"]),
            "domain": entry["domain"],
            "objective_ids": list(entry["objective_ids"]),
            "chosen": list(picked),
            "correct": answer,
            "is_correct": right,
        }
    return row, record


def _grade_flash(number, entry, item, value) -> dict[str, Any]:
    answer = sorted(item["answer"])
    option = int(entry["option"])
    options = item["options"]
    whys = item.get("explanations") or [""] * len(options)
    correct_text = "; ".join(f"{LETTERS[i]}. {options[i]}" for i in answer)
    if entry["kind"] == "true_false":
        truth = option in answer
        right = value is not None and value is truth
        your = None if value is None else ("True" if value else "False")
        expected = "True" if truth else "False"
        explanation = f"{options[option]} - {whys[option]}"
        if not truth:
            explanation += f" The right answer is {correct_text}."
    else:
        expected = options[option].strip()
        right = value is not None and _same_word(value, expected)
        your = value
        explanation = f"{expected} - {whys[option]}"
    return {
        "number": number,
        "id": entry["id"],
        "question_id": entry["question_id"],
        "kind": entry["kind"],
        "your_answer": your,
        "correct_answer": expected,
        "right": right,
        "answered": value is not None,
        "explanation": _cap(explanation.strip()),
        "objective": entry["objective_ids"][0] if entry["objective_ids"] else None,
        "objective_ids": list(entry["objective_ids"]),
        "domain": entry["domain"],
    }


def _weak_spots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    asked: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    domains: dict[str, str] = {}
    for row in rows:
        for objective in row["objective_ids"] or []:
            asked[objective] += 1
            domains.setdefault(objective, row["domain"])
            if not row["right"]:
                missed[objective] += 1
    spots = [
        {"objective": objective, "domain": domains[objective], "missed": missed[objective], "asked": asked[objective]}
        for objective in missed
    ]
    spots.sort(key=lambda row: (-row["missed"], -(row["missed"] / row["asked"]), _okey(row["objective"])))
    return spots[:5]


def _suggest(rec: dict[str, Any], rows: list[dict[str, Any]], spots: list[dict[str, Any]], too_slow: bool) -> dict[str, Any]:
    wrong = [row for row in rows if not row["right"]]
    if rec["focus"] == "flash" and wrong and spots:
        return {"focus": "objective", "objective": spots[0]["objective"],
                "why": f"Your flash misses cluster in objective {spots[0]['objective']}; drill its full questions."}
    if len(wrong) >= 3:
        return {"focus": "mistakes", "why": f"{len(wrong)} misses this round; redo them while the explanations are fresh."}
    if wrong and spots:
        return {"focus": "objective", "objective": spots[0]["objective"],
                "why": f"Most misses were in objective {spots[0]['objective']} ({spots[0]['domain']})."}
    if wrong:
        return {"focus": "mistakes", "why": "Redo the question(s) you missed."}
    if too_slow:
        call = {"focus": rec["focus"], **(rec.get("filters") or {})}
        return {**call, "seconds_per_question": rec["seconds_per_question"],
                "why": "All right but over time; repeat the same focus and aim to finish inside the limit."}
    if rec["focus"] == "weak":
        return {"focus": "mixed", "why": "A clean weak-spot round; check breadth with a mixed drill."}
    return {"focus": "weak", "why": "A clean round; let your saved results choose the next targets."}


def _encourage(correct: int, total: int, too_slow: bool, elapsed: float, limit: int) -> str:
    if not total:
        return "No questions could be graded this time."
    pct = 100.0 * correct / total
    if correct == total:
        text = f"All {total} right."
    elif pct >= 80:
        text = f"{correct} of {total} right: solid. Read the explanations for the misses."
    elif pct >= 50:
        text = f"{correct} of {total} right: getting there. Each explanation below is worth a slow read."
    else:
        text = f"{correct} of {total} right: a tough round, which is exactly what drills are for. Review the misses, then try again."
    if too_slow:
        text += f" Time ran over ({round(elapsed)} s of {limit} s), so practise answering faster."
    elif correct == total:
        text += " Inside the time limit, too."
    return text


def _record(progress: Any, rows: list[dict[str, Any]], seconds: float | None, moment: datetime) -> dict[str, Any]:
    recorded = 0
    errors: list[str] = []
    for row in rows:
        try:
            progress.record_answer(
                row["question_id"],
                domain=row["domain"],
                objective_ids=row["objective_ids"],
                chosen=row["chosen"],
                correct=row["correct"],
                is_correct=row["is_correct"],
                seconds=seconds,
                source=SOURCE,
                now=moment,
            )
            recorded += 1
        except (TypeError, ValueError, OSError) as exc:
            if len(errors) < 10:
                errors.append(f"{row['question_id']}: {_cap(exc, 200)}")
    path = getattr(progress, "path", None)
    return {
        "recorded": recorded,
        "errors": errors,
        "source": SOURCE,
        "seconds_each": seconds,
        "path": str(path) if path else None,
        "persisted": bool(path) and not getattr(progress, "memory_only", False),
    }


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    """What an answered drill keeps (status shows it again): the score, per-question marks and suggestions."""
    keep = ("score", "correct", "total", "answered", "skipped", "elapsed_s", "time_limit_s", "too_slow",
            "weak_spots", "next_drill_suggestion", "encouragement")
    compact = {key: copy.deepcopy(result[key]) for key in keep}
    compact["per_question"] = [
        {key: row.get(key) for key in ("number", "id", "your_answer", "correct_answer", "right", "objective")}
        for row in result["per_question"]
    ]
    compact["progress_recorded"] = result["progress"].get("recorded", 0)
    return compact


# ---------------------------------------------------------------- status


def _status(drill_id, store, bank, now) -> dict[str, Any]:
    moment = srs.as_utc(now)
    _, rec, _ = _locate(drill_id, store)
    started = srs.parse_iso(rec["started_at"])
    deadline = srs.parse_iso(rec["deadline"])
    is_open = rec["status"] == ACTIVE
    payload: dict[str, Any] = {
        "ok": True,
        "drill_id": rec["id"],
        "status": rec["status"],
        "focus": rec["focus"],
        "filters": rec.get("filters") or {},
        "count": len(rec["questions"]),
        "seconds_per_question": rec["seconds_per_question"],
        "time_limit_s": rec["time_limit_s"],
        "started_at": rec["started_at"],
        "deadline": rec["deadline"],
    }
    if is_open:
        elapsed = max(0.0, (moment - started).total_seconds())
        payload.update(
            elapsed_s=int(elapsed),
            remaining_s=max(0, int((deadline - moment).total_seconds())),
            over_time=moment > deadline,
            questions=_questions_again(rec, bank),
            how_to_answer=HOW_TO_ANSWER,
        )
        if moment > deadline:
            payload["message"] = "Time is up, but you can still send your answers; the result will say too_slow."
    else:
        payload.update(finished_at=rec.get("finished_at"), result=copy.deepcopy(rec.get("result")))
    return payload


def _questions_again(rec: dict[str, Any], bank: Any) -> list[dict[str, Any]]:
    try:
        adapter = _bank_for(rec, bank)
    except DrillError:
        return []
    items = {str(item["id"]): item for item in adapter.load_bank() if isinstance(item, dict)}
    if rec["focus"] != "flash":
        return [{"number": n, **exam_mock._view(adapter, items[e["id"]])}
                for n, e in enumerate(rec["questions"], 1) if e["id"] in items]
    rows = []
    for number, entry in enumerate(rec["questions"], 1):
        item = items.get(entry["question_id"])
        if item is None:
            continue
        options = item["options"]
        row: dict[str, Any] = {"number": number, "id": entry["id"], "question_id": entry["question_id"],
                               "kind": entry["kind"], "domain": entry["domain"],
                               "objective_ids": list(entry["objective_ids"])}
        if item.get("usda"):
            row["usda"] = dict(item["usda"])
            row["root"] = item.get("root") or next(iter(item["usda"]))
        if entry["kind"] == "one_word":
            word = options[entry["option"]].strip()
            row.update(prompt=f"Type the one-word answer (options hidden): {item['stem']}",
                       hint=f"{len(word)} characters, starts with '{word[0]}'")
        else:
            ask = (f"Is this one of the correct choices? {options[entry['option']]}" if item["type"] == "multi"
                   else f"Proposed answer: {options[entry['option']]}")
            row.update(prompt=f"True or false? Question: {item['stem']}\n{ask}", options=["A. True", "B. False"])
        rows.append(row)
    return rows


# ---------------------------------------------------------------- stores and sessions


def _default_progress_store() -> ProgressStore:
    """Same folder rules as runtime.build_progress_store: $OMNIVERSE_EXAM_PREP_DIR, memory in test mode, else workspace."""
    folder = os.environ.get(ENV_DIR, "").strip()
    if folder:
        return ProgressStore(Path(folder).expanduser() / PROGRESS_FILE_NAME)
    if os.environ.get(TEST_ENV, "").strip().lower() not in _OFF_VALUES:
        return ProgressStore(memory_only=True)
    return ProgressStore(workspace_root() / "exam-prep" / PROGRESS_FILE_NAME)


def _progress_store(store: Any) -> Any:
    if store is None:
        return _default_progress_store()
    if not callable(getattr(store, "record_answer", None)):
        raise TypeError("store must be a ProgressStore (or have record_answer, summary, history and due_reviews)")
    return store


def _bank_for(rec: dict[str, Any], bank: Any) -> Any:
    if bank is not None:
        return exam_mock._adapt(bank)
    if rec["id"] in _CUSTOM_BANKS:
        return _CUSTOM_BANKS[rec["id"]]
    if rec.get("bank") != "custom":
        return exam_bank
    raise DrillError(f"drill {rec['id']} used a custom question bank that is not loaded in this process; "
                     "pass bank=... with the same questions")


def _locate(drill_id: Any, store: Any) -> tuple["_Sessions", dict[str, Any], Any]:
    if not isinstance(drill_id, str) or not drill_id.strip():
        raise DrillError("drill_id must be the id start_drill returned, such as 'drill-20261022T090000Z-ab12cd'")
    key = drill_id.strip()
    held = _MEMORY.get(key)
    if held is not None and (store is None or store is held["store"]):
        return _Sessions(None, held["store"]), held["rec"], held["store"]
    progress = _progress_store(store)
    sessions = _Sessions.for_store(progress)
    rec = sessions.get(key)
    if rec is None:
        raise DrillError(f"unknown drill id {drill_id!r}; start_drill gives a new one")
    return sessions, rec, progress


class _Sessions:
    """Drill sessions in ``drill_sessions.json`` beside the progress file, or in this process for memory stores."""

    def __init__(self, path: Path | None, progress: Any) -> None:
        self.path = path
        self.progress = progress
        self.recovered_backup: str | None = None

    @classmethod
    def for_store(cls, progress: Any) -> "_Sessions":
        path = getattr(progress, "path", None)
        if path is None or getattr(progress, "memory_only", False):
            return cls(None, progress)
        return cls(Path(path).with_name(FILE_NAME), progress)

    def info(self) -> dict[str, Any]:
        return {"path": str(self.path) if self.path else None, "memory_only": self.path is None,
                "recovered_backup": self.recovered_backup}

    def add(self, rec: dict[str, Any], moment: datetime, progress: Any) -> str:
        if self.path is None:
            rec["id"] = _new_id(moment, set(_MEMORY))
            _MEMORY[rec["id"]] = {"rec": rec, "store": progress}
            while len(_MEMORY) > MAX_MEMORY_SESSIONS:
                _MEMORY.pop(next(iter(_MEMORY)))
            return rec["id"]
        with self._edit() as data:
            rec["id"] = _new_id(moment, {r["id"] for r in data["sessions"]})
            data["sessions"].append(rec)
            del data["sessions"][:-MAX_SESSIONS_KEPT]
        return rec["id"]

    def get(self, drill_id: str) -> dict[str, Any] | None:
        if self.path is None:
            held = _MEMORY.get(drill_id)
            return held["rec"] if held else None
        return next((r for r in self._load()["sessions"] if r["id"] == drill_id), None)

    def finish(self, drill_id: str, moment: datetime, result: dict[str, Any]) -> None:
        if self.path is None:
            rec = _MEMORY[drill_id]["rec"]
            rec.update(status=FINISHED, finished_at=srs.to_iso(moment), result=result)
            return
        with self._edit() as data:
            rec = next((r for r in data["sessions"] if r["id"] == drill_id), None)
            if rec is None:
                raise DrillError(f"drill {drill_id} disappeared from {self.path.name} while it was graded")
            if rec["status"] != ACTIVE:
                raise DrillError(f"drill {drill_id} was answered at the same time elsewhere")
            rec.update(status=FINISHED, finished_at=srs.to_iso(moment), result=result)

    @contextlib.contextmanager
    def _edit(self):
        with exam_mock._file_lock(self.path):
            data = self._load()
            yield data
            stamp = srs.to_iso(srs.utc_now())
            data["updated_at"] = stamp
            data["created_at"] = data.get("created_at") or stamp
            exam_mock._atomic_write_json(self.path, data)

    def _load(self) -> dict[str, Any]:
        assert self.path is not None
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return _empty_data()
        try:
            return _normalize(json.loads(text))
        except (UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError):
            backup = exam_mock._unique_sibling(self.path, "corrupt")
            os.replace(self.path, backup)
            self.recovered_backup = str(backup)
            return _empty_data()


def _empty_data() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "created_at": None, "updated_at": None, "sessions": []}


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("not a drill session file of this version")
    sessions = raw.get("sessions")
    if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
        raise ValueError("'sessions' is not a list of objects")
    for rec in sessions:
        if not isinstance(rec.get("id"), str) or rec.get("status") not in (ACTIVE, FINISHED):
            raise ValueError("a drill has no id or an unknown status")
        if not isinstance(rec.get("questions"), list) or not all(isinstance(q, dict) for q in rec["questions"]):
            raise ValueError(f"drill {rec['id']}: 'questions' is not a list of objects")
        srs.parse_iso(rec["started_at"])
        srs.parse_iso(rec["deadline"])
    return {**_empty_data(), **raw, "sessions": sessions}


def _new_id(moment: datetime, taken: set[str]) -> str:
    while True:
        candidate = f"drill-{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def _error(exc: Exception, **extra: Any) -> dict[str, Any]:
    return {"ok": False, **{k: v for k, v in extra.items() if isinstance(v, (str, int, float)) or v is None},
            "error": str(exc) or type(exc).__name__}


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_SECONDS",
    "DrillError",
    "FOCUSES",
    "answer_drill",
    "drill_status",
    "list_drill_focuses",
    "start_drill",
]
