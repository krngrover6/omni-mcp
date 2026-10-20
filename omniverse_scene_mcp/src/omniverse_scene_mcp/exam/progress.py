"""Saved NCP-OUSD exam progress: answers, Leitner cards, weak areas and study streaks."""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import srs

SCHEMA_VERSION = 1
ENV_DIR = "OMNIVERSE_EXAM_PREP_DIR"
FILE_NAME = "progress.json"
HISTORY_KEEP = 1000
ACTIVE_DAYS_KEEP = 800
MAX_HISTORY_LIMIT = 200
MAX_DUE_LIMIT = 100
WEAKEST_LIMIT = 5
TEXT_CAP = 200
CHOICES_CAP = 20


def workspace_root() -> Path:
    """Workspace folder from this file: exam -> package -> src -> omniverse_scene_mcp -> workspace."""
    return Path(__file__).resolve().parents[4]


def default_progress_path() -> Path:
    """``$OMNIVERSE_EXAM_PREP_DIR/progress.json``, else ``<workspace>/exam-prep/progress.json``. Creates nothing."""
    configured = os.environ.get(ENV_DIR, "").strip()
    folder = Path(configured).expanduser() if configured else workspace_root() / "exam-prep"
    return folder / FILE_NAME


class ProgressStore:
    """Exam answers and spaced-repetition cards in one JSON file (atomic writes, bad files kept as backups).

    The file is re-read on every call, so separate stores on the same path stay consistent.
    ``memory_only=True`` never reads or writes the disk.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None, memory_only: bool = False) -> None:
        self.memory_only = bool(memory_only)
        self.path: Path | None = None
        if not self.memory_only:
            self.path = Path(path).expanduser() if path is not None else default_progress_path()
        self.recovered_backup: str | None = None
        self.recovery_reason: str | None = None
        self._memory = _empty_data()

    def record_answer(
        self,
        question_id: str,
        *,
        domain: str,
        objective_ids: Iterable[str] | str | None,
        chosen: Any,
        correct: Any,
        is_correct: bool,
        seconds: float | None = None,
        source: str = "quiz",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Save one answer: update its Leitner card and the domain/objective statistics."""
        qid = _clean_text(question_id, "question_id")
        domain_name = _clean_text(domain, "domain")
        objectives = _clean_ids(objective_ids)
        if not isinstance(is_correct, bool):
            raise TypeError("is_correct must be True or False")
        chosen_value = _clean_choice(chosen, "chosen")
        correct_value = _clean_choice(correct, "correct")
        elapsed = _clean_seconds(seconds)
        source_name = _clean_text(source, "source")
        moment = srs.as_utc(now)
        stamp = srs.to_iso(moment)

        with _file_lock(self.path):
            return self._record_locked(
                qid, domain_name, objectives, chosen_value, correct_value, is_correct, elapsed, source_name, moment, stamp
            )

    def _record_locked(
        self,
        qid: str,
        domain_name: str,
        objectives: list[str],
        chosen_value: Any,
        correct_value: Any,
        is_correct: bool,
        elapsed: float | None,
        source_name: str,
        moment: datetime,
        stamp: str,
    ) -> dict[str, Any]:
        # Runs under the cross-process lock: re-read the file so no other writer's answers are lost.
        data = self._load()
        previous = data["cards"].get(qid)
        card = srs.review(previous, is_correct, moment)
        card["domain"] = domain_name
        card["objective_ids"] = list(objectives)
        data["cards"][qid] = card

        domain_stats = _bump(data["domains"], domain_name, is_correct)
        objective_rows = []
        for objective_id in objectives:
            stats = _bump(data["objectives"], objective_id, is_correct)
            stats["domain"] = domain_name
            objective_rows.append({"objective_id": objective_id, "domain": domain_name, **_stat_row(stats)})

        totals = data["totals"]
        totals["answered"] += 1
        totals["correct"] += 1 if is_correct else 0
        if elapsed is not None:
            totals["seconds"] = round(totals["seconds"] + elapsed, 1)
            totals["timed"] += 1

        box_before = previous.get("box") if previous else None
        data["history"].append(
            {
                "question_id": qid,
                "domain": domain_name,
                "objective_ids": list(objectives),
                "chosen": chosen_value,
                "correct": correct_value,
                "is_correct": is_correct,
                "seconds": elapsed,
                "source": source_name,
                "answered_at": stamp,
                "box_before": box_before,
                "box_after": card["box"],
            }
        )
        del data["history"][:-HISTORY_KEEP]
        day = moment.date().isoformat()
        if day not in data["active_days"]:
            data["active_days"].append(day)
            data["active_days"].sort()
            del data["active_days"][:-ACTIVE_DAYS_KEEP]

        self._save(data, stamp)
        return {
            "ok": True,
            "question_id": qid,
            "is_correct": is_correct,
            "answered_at": stamp,
            "box_before": box_before,
            "box": card["box"],
            "due_at": card["due_at"],
            "last_result": card["last_result"],
            "domain": {"name": domain_name, **_stat_row(domain_stats)},
            "objectives": objective_rows,
            "persisted": not self.memory_only,
            "path": str(self.path) if self.path else None,
        }

    def summary(self, now: datetime | None = None, *, known_domains: Iterable[str] | None = None) -> dict[str, Any]:
        """Totals, per-domain accuracy, weakest objectives, due count and streak.

        ``known_domains`` lists domains to report even before any answer (with zero counts).
        """
        moment = srs.as_utc(now)
        data = self._load()
        totals = data["totals"]
        boxes = {str(box): 0 for box in range(srs.FIRST_BOX, srs.LAST_BOX + 1)}
        for card in data["cards"].values():
            boxes[str(srs.clamp_box(card["box"]))] += 1

        if isinstance(known_domains, str):
            known_domains = [known_domains]
        domains: dict[str, dict[str, Any]] = {}
        for name in known_domains or ():
            domains[str(name)] = _stat_row(data["domains"].get(str(name), {}))
        for name in sorted(data["domains"]):
            domains.setdefault(name, _stat_row(data["domains"][name]))

        weak = []
        for objective_id, stats in data["objectives"].items():
            row = _stat_row(stats)
            if row["answered"] and row["correct"] < row["answered"]:
                weak.append({"objective_id": objective_id, "domain": stats.get("domain"), **row})
        weak.sort(key=lambda row: (row["accuracy"], -row["answered"], row["objective_id"]))

        history = data["history"]
        return {
            "as_of": srs.to_iso(moment),
            "totals": {
                **_stat_row(totals),
                "wrong": totals["answered"] - totals["correct"],
                "unique_questions": len(data["cards"]),
                "boxes": boxes,
                "seconds": totals["seconds"],
                "avg_seconds": round(totals["seconds"] / totals["timed"], 1) if totals["timed"] else None,
            },
            "domains": domains,
            "weakest_objectives": weak[:WEAKEST_LIMIT],
            "due_count": len(srs.due_cards(data["cards"], moment)),
            "streak_days": _streak(data["active_days"], moment.date()),
            "last_answered_at": history[-1].get("answered_at") if history else None,
            "storage": self._storage_info(),
        }

    def due_reviews(self, limit: int = 10, now: datetime | None = None) -> list[dict[str, Any]]:
        """Cards due for review, most overdue first."""
        moment = srs.as_utc(now)
        data = self._load()
        rows = []
        for question_id in srs.due_cards(data["cards"], moment, _clamp_limit(limit, MAX_DUE_LIMIT)):
            card = data["cards"][question_id]
            rows.append(
                {
                    "question_id": question_id,
                    "box": srs.clamp_box(card["box"]),
                    "due_at": card["due_at"],
                    "last_result": card.get("last_result"),
                    "domain": card.get("domain"),
                    "objective_ids": list(card.get("objective_ids") or []),
                }
            )
        return rows

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent answers, newest first."""
        count = _clamp_limit(limit, MAX_HISTORY_LIMIT)
        if not count:
            return []
        return [dict(entry) for entry in reversed(self._load()["history"][-count:])]

    def reset(self, confirm: bool = False) -> dict[str, Any]:
        """Clear all progress. Needs ``confirm=True``; an existing file is first copied to ``progress.json.bak-<timestamp>``."""
        if confirm is not True:
            return {
                "ok": False,
                "reset": False,
                "message": "Nothing changed. Call reset(confirm=True) to clear progress; a backup copy is written first.",
            }
        backup = None
        with _file_lock(self.path):
            if self.path is not None and self.path.exists():
                backup = _unique_sibling(self.path, "bak")
                shutil.copy2(self.path, backup)
            self._save(_empty_data(), srs.to_iso(srs.utc_now()))
        return {
            "ok": True,
            "reset": True,
            "backup": str(backup) if backup else None,
            "path": str(self.path) if self.path else None,
        }

    def _storage_info(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "memory_only": self.memory_only,
            "exists": bool(self.path and self.path.exists()),
            "recovered_backup": self.recovered_backup,
            "recovery_reason": self.recovery_reason,
        }

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            # utf-8-sig also accepts a BOM (Windows PowerShell 5.1 writes one), so such files are not quarantined.
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

    def _save(self, data: dict[str, Any], stamp: str) -> None:
        data["updated_at"] = stamp
        data["created_at"] = data.get("created_at") or stamp
        if self.path is None:
            self._memory = data
            return
        _atomic_write_json(self.path, data)


def _empty_data() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": None,
        "updated_at": None,
        "totals": {"answered": 0, "correct": 0, "seconds": 0.0, "timed": 0},
        "cards": {},
        "domains": {},
        "objectives": {},
        "history": [],
        "active_days": [],
    }


def _normalize(raw: Any) -> dict[str, Any]:
    """Check a loaded file and fill missing sections; raises ValueError/TypeError/KeyError if unusable."""
    if not isinstance(raw, dict):
        raise ValueError("top level is not a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {raw.get('schema_version')!r}")
    data = {**_empty_data(), **raw}
    for key in ("cards", "domains", "objectives"):
        section = data[key]
        if not isinstance(section, dict) or not all(isinstance(item, dict) for item in section.values()):
            raise ValueError(f"'{key}' is not an object of objects")
    for key, item_type in (("history", dict), ("active_days", str)):
        section = data[key]
        if not isinstance(section, list) or not all(isinstance(item, item_type) for item in section):
            raise ValueError(f"'{key}' is not a list of {item_type.__name__}")
    if not isinstance(data["totals"], dict):
        raise ValueError("'totals' is not an object")
    totals = {**_empty_data()["totals"], **data["totals"]}
    totals.update(
        answered=int(totals["answered"]),
        correct=int(totals["correct"]),
        seconds=float(totals["seconds"]),
        timed=int(totals["timed"]),
    )
    data["totals"] = totals
    for card in data["cards"].values():
        card["box"] = srs.clamp_box(card["box"])
        srs.parse_iso(card["due_at"])
    for stats in (*data["domains"].values(), *data["objectives"].values()):
        stats["answered"] = int(stats.get("answered", 0))
        stats["correct"] = int(stats.get("correct", 0))
    return data


LOCK_TIMEOUT_SECONDS = 30.0


@contextlib.contextmanager
def _file_lock(path: Path | None):
    """Cross-process exclusive lock on a ``<name>.lock`` sidecar file; does nothing when ``path`` is None.

    The OS releases the lock if the holding process dies, so a stale lock file is harmless.
    """
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
                        raise TimeoutError(f"progress file {path} is locked by another process") from None
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
    """Write a uniquely named temp file next to the file, then swap it in with ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
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


def _bump(section: dict[str, dict[str, Any]], key: str, is_correct: bool) -> dict[str, Any]:
    stats = section.setdefault(key, {"answered": 0, "correct": 0})
    stats["answered"] = int(stats.get("answered", 0)) + 1
    stats["correct"] = int(stats.get("correct", 0)) + (1 if is_correct else 0)
    return stats


def _stat_row(stats: dict[str, Any]) -> dict[str, Any]:
    answered = int(stats.get("answered", 0))
    correct = int(stats.get("correct", 0))
    return {"answered": answered, "correct": correct, "accuracy": round(correct / answered, 3) if answered else None}


def _streak(days: Iterable[str], today: date) -> int:
    """Consecutive UTC days with answers, ending today (or yesterday if nothing yet today)."""
    active = set(days)
    cursor = today if today.isoformat() in active else today - timedelta(days=1)
    count = 0
    while cursor.isoformat() in active:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _clamp_limit(limit: Any, cap: int) -> int:
    return cap if limit is None else max(0, min(cap, int(limit)))


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > TEXT_CAP:
        raise ValueError(f"{field} is longer than {TEXT_CAP} characters")
    return text


def _clean_ids(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    cleaned: list[str] = []
    for value in values:
        text = _clean_text(value, "objective_ids item")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > CHOICES_CAP:
        raise ValueError(f"at most {CHOICES_CAP} objective_ids")
    return cleaned


def _clean_choice(value: Any, field: str) -> str | list[str] | None:
    """An answer as a string, a list of strings (multi-select), or None (skipped)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a string, an integer or a list of them")
    if isinstance(value, (str, int)):
        return str(value)[:TEXT_CAP]
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        if len(value) > CHOICES_CAP:
            raise ValueError(f"{field} has more than {CHOICES_CAP} options")
        items = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise TypeError(f"{field} items must be strings or integers")
            items.append(str(item)[:TEXT_CAP])
        return items
    raise TypeError(f"{field} must be a string, an integer or a list of them")


def _clean_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("seconds must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("seconds must be a finite number >= 0")
    return round(float(value), 1)
