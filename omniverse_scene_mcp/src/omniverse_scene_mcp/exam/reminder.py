"""'What should I do today?': a short, friendly NCP-OUSD study brief built from saved progress.

Everything here only READS. ``today_brief`` reuses ``dashboard.dashboard_data`` (plan day, days to the exam, domain
status, streak), the progress store's due reviews and the Python practice exercise list, and turns them into a
time-boxed plan for today. With ``store=None`` the saved progress file is read into a memory-only copy, so a
damaged file is never moved aside and nothing is ever written back.

``reminder_text`` is a plain-text version (at most 12 lines) for a desktop notification, ``write_today_file``
writes a markdown TODAY.md only to a path you pass explicitly, and ``scheduled_task_commands`` RETURNS the
PowerShell text to create (and remove) a daily Windows reminder: it never runs it, the user does.

Try it from the ``src`` folder:  python -E -s -m omniverse_scene_mcp.exam.reminder
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

from . import dashboard
from .progress import ProgressStore, _normalize, default_progress_path

DEFAULT_MINUTES = 60
MIN_MINUTES = 10
MAX_MINUTES = 480
MIN_BLOCK_MINUTES = 5
MAX_TEXT_LINES = 12
MAX_LINE_CHARS = 110
MAX_TOP_OBJECTIVES = 5
MAX_WEAK_DOMAINS = 3
MAX_DUE_SCAN = 100
TASK_NAME = "NCP-OUSD daily study reminder"
DRILL_PHRASE = "start a drill on weak areas"
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TASK_NAME = re.compile(r"^[A-Za-z0-9 _.-]{1,80}$")
_MD_SUFFIXES = (".md", ".markdown")


# ---------------------------------------------------------------- public API


def today_brief(store: Any = None, *, today: date | datetime | str | None = None,
                minutes: int = DEFAULT_MINUTES) -> dict[str, Any]:
    """Today's study brief: day number, days to the exam, a time-boxed plan and what is due. Reads only.

    ``store`` is a ProgressStore (None reads the saved progress file without writing), ``today`` fixes the date
    (ISO string, date or datetime; None means today) and ``minutes`` is how long you have (10 to 480).
    """
    try:
        day_today = dashboard._as_date(today)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    budget = _minutes(minutes)
    if budget is None:
        return {"ok": False, "message": f"minutes must be a whole number from {MIN_MINUTES} to {MAX_MINUTES}."}
    notes: list[str] = []
    if store is None:
        store = read_only_store(notes=notes)
    moment = datetime.combine(day_today, dtime(12, 0), tzinfo=timezone.utc)

    data = dashboard.dashboard_data(store, today=day_today)
    plan = data["plan"]
    due = _due_reviews(store, moment, data)
    weak = _weak_domains(data)
    practice = python_practice_next(store)
    streak = int(data.get("streak_days") or 0)
    days_left = plan["days_left"]
    mode = _mode(plan)
    missed = int(plan.get("behind_days") or 0) if mode == "study" else 0

    blocks = _plan_blocks(mode, budget, plan, due, weak, practice, data)
    result: dict[str, Any] = {
        "ok": True,
        "date": day_today.isoformat(),
        "mode": mode,
        "day_number": plan["calendar_day"] if plan["configured"] and (plan["calendar_day"] or 0) >= 1 else None,
        "total_days": plan["total_days"] or None,
        "exam_date": plan["exam_date"],
        "days_left_to_exam": days_left,
        "minutes": budget,
        "headline": _headline(mode, plan, days_left, budget),
        "plan": blocks,
        "due_reviews": due,
        "weak_domains": weak,
        "weakest_objectives": [
            {"objective_id": r["objective_id"], "text": _short(r["text"], 90), "accuracy_pct": r["accuracy_pct"]}
            for r in data["weakest_objectives"][:3]
        ],
        "streak": streak,
        "streak_message": _streak_message(streak),
        "missed_days": missed,
        "open_plan_days": _open_days(plan) if mode == "study" else [],
        "catch_up": _catch_up(missed, plan) if mode == "study" else None,
        "python_practice_next": practice,
        "notes": notes,
    }
    result["message_markdown"] = _markdown(result)
    return result


def reminder_text(store: Any = None, today: date | datetime | str | None = None,
                  minutes: int = DEFAULT_MINUTES) -> str:
    """Plain text for a desktop notification: at most 12 short lines, no markdown."""
    brief = today_brief(store, today=today, minutes=minutes)
    if not brief.get("ok"):
        return _cap_line(f"NCP-OUSD study: {brief.get('message', 'could not build the brief for today.')}")
    lines = [brief["headline"]]
    for block in brief["plan"]:
        lines.append(f"- {block['minutes']} min: {block['what']}")
    if brief["due_reviews"]["count"]:
        lines.append(f"Due reviews: {brief['due_reviews']['count']}")
    if brief["streak"]:
        lines.append(brief["streak_message"])
    if brief["catch_up"]:
        lines.append(brief["catch_up"])
    lines.append("Ask the router: what should I do today?")
    lines = [_cap_line(line) for line in lines]
    if len(lines) > MAX_TEXT_LINES:
        lines = lines[: MAX_TEXT_LINES - 1] + [lines[-1]]
    return "\n".join(lines)


def write_today_file(path: str | os.PathLike[str], store: Any = None,
                     today: date | datetime | str | None = None, minutes: int = DEFAULT_MINUTES) -> dict[str, Any]:
    """Write today's brief as markdown to ``path`` (required; must end in .md and its folder must already exist).

    There is no default location on purpose: nothing is written unless you name the file.
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        raise TypeError("write_today_file needs an explicit path such as 'C:/somewhere/TODAY.md'")
    target = Path(path).expanduser()
    if target.suffix.lower() not in _MD_SUFFIXES:
        return {"ok": False, "path": str(target), "message": "The file name must end in .md (for example TODAY.md)."}
    if not target.parent.is_dir():
        return {"ok": False, "path": str(target), "message": f"The folder {target.parent} does not exist; "
                "create it first (this helper never creates folders)."}
    if target.exists() and not target.is_file():
        return {"ok": False, "path": str(target), "message": "That path is a folder, not a file."}
    brief = today_brief(store, today=today, minutes=minutes)
    if not brief.get("ok"):
        return {"ok": False, "path": str(target), "message": brief.get("message")}
    text = f"# Today: {brief['date']}\n\n" + brief["message_markdown"] + "\n"
    payload = text.encode("utf-8")
    _atomic_write(target, payload)
    return {"ok": True, "path": str(target), "bytes": len(payload), "date": brief["date"]}


def scheduled_task_commands(at: str = "19:00", *, task_name: str = TASK_NAME, python_exe: str | None = None,
                            src_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """PowerShell TEXT to create and to remove a daily Windows Scheduled Task that pops up today's reminder.

    Nothing is run, registered or written here: copy the commands into PowerShell yourself if you want them.
    ``at`` is a 24-hour time 'HH:MM'. The task runs ``pythonw -E -s -m omniverse_scene_mcp.exam.reminder --popup``
    from the package's ``src`` folder, which only reads your progress file.
    """
    match = _TIME.match(str(at or "").strip())
    if not match:
        return {"ok": False, "message": "Give the time as 24-hour 'HH:MM', for example '07:30' or '19:00'."}
    when = f"{int(match.group(1)):02d}:{match.group(2)}"
    name = str(task_name or "").strip()
    if not _TASK_NAME.match(name):
        return {"ok": False, "message": "task_name may use letters, digits, spaces, '_', '.' and '-' (1-80 chars)."}
    exe = str(python_exe) if python_exe else _default_pythonw()
    src = str(Path(src_dir)) if src_dir else str(Path(__file__).resolve().parents[2])
    action = (f"$action = New-ScheduledTaskAction -Execute {_ps(exe)} "
              f"-Argument '-E -s -m omniverse_scene_mcp.exam.reminder --popup' -WorkingDirectory {_ps(src)}")
    create = "\n".join([
        action,
        f"$trigger = New-ScheduledTaskTrigger -Daily -At {_ps(when)}",
        f"Register-ScheduledTask -TaskName {_ps(name)} -Action $action -Trigger $trigger "
        f"-Description {_ps('Shows the NCP-OUSD what-to-study-today reminder. Reads progress only.')}",
    ])
    remove = f"Unregister-ScheduledTask -TaskName {_ps(name)} -Confirm:$false"
    return {
        "ok": True,
        "executed": False,
        "time": when,
        "task_name": name,
        "create_command": create,
        "remove_command": remove,
        "check_command": f"Get-ScheduledTask -TaskName {_ps(name)}",
        "run_now_command": f"Start-ScheduledTask -TaskName {_ps(name)}",
        "preview_command": f"Set-Location {_ps(src)}; & {_ps(exe.replace('pythonw.exe', 'python.exe'))} "
                           "-E -s -m omniverse_scene_mcp.exam.reminder",
        "note": "Nothing was created. These commands are text for YOU to run in PowerShell if you want a daily "
                "pop-up; the task runs as you, only while you are signed in, and only reads your progress file. "
                "Run the remove command to delete it again. Check the python path first: it must exist on this PC.",
    }


def read_only_store(path: str | os.PathLike[str] | None = None, *, notes: list[str] | None = None) -> ProgressStore:
    """A memory-only ProgressStore holding a copy of the saved progress file (default location when ``path`` is None).

    The file is only read: a missing or unreadable file gives an empty store (and a note) and is left where it is.
    """
    source = Path(path).expanduser() if path is not None else default_progress_path()
    store = ProgressStore(memory_only=True)
    try:
        store._memory = _normalize(json.loads(source.read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        if notes is not None:
            notes.append("No saved progress yet, so this brief starts from zero.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        if notes is not None:
            notes.append(f"The progress file could not be read ({type(exc).__name__}); it was left untouched.")
    return store


def python_practice_next(store: Any) -> dict[str, Any] | None:
    """The first Python practice exercise (in suggested order) not yet passed, or None when all are passed."""
    from . import python_practice

    listing = python_practice.list_exercises()
    rows = listing.get("exercises") or []
    passed = _passed_exercises(store)
    done = sum(1 for row in rows if row["id"] in passed)
    for row in rows:
        if row["id"] not in passed:
            return {"id": row["id"], "title": row["title"], "level": row["level"], "number": row["number"],
                    "passed": done, "total": len(rows)}
    return None


def main(argv: list[str] | None = None) -> int:
    """Command line: print the reminder (default), ``--markdown`` for the markdown brief, ``--popup`` for a pop-up."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--markdown" in args:
        print(today_brief()["message_markdown"])
        return 0
    text = reminder_text()
    if "--popup" in args and os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "NCP-OUSD: today", 0x40 | 0x40000)  # info icon, topmost
        return 0
    print(text)
    return 0


# ---------------------------------------------------------------- brief pieces


def _minutes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not MIN_MINUTES <= value <= MAX_MINUTES:
        return None
    return value


def _mode(plan: dict[str, Any]) -> str:
    days_left = plan["days_left"]
    if not plan["configured"] and days_left is None:
        return "no_plan"
    if days_left is not None and days_left < 0:
        return "exam_passed"
    if days_left == 0:
        return "exam_day"
    if plan["plan_complete"] or (plan["calendar_day"] or 0) > (plan["total_days"] or 0) > 0:
        return "plan_complete"
    return "study"


def _due_reviews(store: Any, moment: datetime, data: dict[str, Any]) -> dict[str, Any]:
    rows = store.due_reviews(limit=MAX_DUE_SCAN, now=moment)
    counter: Counter[str] = Counter(oid for row in rows for oid in row.get("objective_ids") or [])
    return {
        "count": int(data.get("due_reviews") or len(rows)),
        "top_objectives": [{"objective_id": oid, "due": n} for oid, n in counter.most_common(MAX_TOP_OBJECTIVES)],
        "how": "learning_exam_review",
    }


def _weak_domains(data: dict[str, Any]) -> list[dict[str, Any]]:
    target = data["target_pct"]
    weak = [d for d in data["domains"] if d["status"] == "weak"]
    weak.sort(key=lambda d: (-(d["weight"] or 0) * (target - (d["accuracy_pct"] or 0)), d["name"]))
    return [{"name": d["name"], "accuracy_pct": d["accuracy_pct"], "answered": d["answered"], "weight": d["weight"]}
            for d in weak[:MAX_WEAK_DOMAINS]]


def _passed_exercises(store: Any) -> set[str]:
    loader = getattr(store, "_load", None)
    passed: set[str] = set()
    if callable(loader):
        with contextlib.suppress(Exception):
            for qid, card in (loader().get("cards") or {}).items():
                if isinstance(card, dict) and int(card.get("times_correct") or 0) > 0:
                    passed.add(str(qid))
            return passed
    for row in store.history(limit=200):
        if isinstance(row, dict) and row.get("is_correct"):
            passed.add(str(row.get("question_id")))
    return passed


def _open_days(plan: dict[str, Any]) -> list[int]:
    done = set(plan.get("completed_days") or [])
    last = min(plan["calendar_day"] or 0, (plan["total_days"] or 0) + 1)
    return [n for n in range(1, last) if n not in done]


def _lesson_what(plan: dict[str, Any]) -> str:
    entry = plan.get("next")
    if not entry:
        return "Today's plan lesson"
    topics = entry["topics"]
    kind = {"review": "review day", "final_review": "final review day"}.get(entry.get("type") or "", "lesson")
    if not topics:
        return f"Day {entry['day']} {kind}"
    more = f" (+{len(topics) - 1} more)" if len(topics) > 1 else ""
    return f"Day {entry['day']} {kind}: {_short(topics[0]['label'], 60)}{more}"


def _plan_blocks(mode: str, budget: int, plan: dict[str, Any], due: dict[str, Any], weak: list[dict[str, Any]],
                 practice: dict[str, Any] | None, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate blocks in priority order with weights; as many as fit (one per 10 minutes, at most 5)."""
    count = due["count"]
    candidates: list[tuple[float, dict[str, Any]]] = []
    reviews = (2.0 if count <= 10 else 3.0, {
        "block": "reviews",
        "what": f"Answer your {count} due review question{'s' if count != 1 else ''}",
        "why": "Spaced repetition: questions come back just before you would forget them.",
        "how": "learning_exam_review, then answer each with learning_exam_answer",
    })
    drill = (2.0, {
        "block": "drill",
        "what": "Drill your weakest area" + (f": {weak[0]['name']}" if weak else ""),
        "why": (f"{weak[0]['name']} is {weak[0]['weight']}% of the exam and below your 80% goal."
                if weak else "Short targeted practice fixes gaps fastest."),
        "how": f"say '{DRILL_PHRASE}'",
    })
    python_block = None
    if practice:
        python_block = (2.0, {
            "block": "python",
            "what": f"Python practice {practice['number']}/{practice['total']}: {practice['title']}",
            "why": "Small daily Python steps make the USD API feel familiar for the exam and Isaac Sim.",
            "how": f"python_practice_get('{practice['id']}'), then python_practice_run",
        })
    quiz = (2.0, {
        "block": "quiz",
        "what": "Mixed quiz across all domains",
        "why": "Mixing topics trains you to recognise what a question is about, like the real exam.",
        "how": "learning_exam_quiz",
    })

    if mode == "no_plan":
        candidates.append((1.0, {
            "block": "set_plan",
            "what": "Set your exam date to get a day-by-day plan",
            "why": "A fixed date turns 30 days into small, clear daily steps.",
            "how": "learning_exam_set_date('YYYY-MM-DD')",
        }))
        if count:
            candidates.append(reviews)
        candidates.append(quiz)
        if python_block:
            candidates.append(python_block)
        if weak:
            candidates.append(drill)
    elif mode == "exam_day":
        if count:
            candidates.append((2.0, {**reviews[1], "what": "Light warm-up: a few due reviews, no new topics"}))
        candidates.append((1.0, {
            "block": "rest",
            "what": "Rest, eat, check your exam logistics",
            "why": "A calm, rested brain recalls more than one extra hour of cramming.",
            "how": "no tool needed",
        }))
    elif mode == "exam_passed":
        candidates.append((1.0, {
            "block": "set_plan",
            "what": "If you are retaking or rescheduling, set the new exam date",
            "why": "A new date rebuilds the day-by-day plan from today.",
            "how": "learning_exam_set_date('YYYY-MM-DD')",
        }))
        if count:
            candidates.append(reviews)
        if python_block:
            candidates.append(python_block)
        if weak:
            candidates.append(drill)
    else:
        if mode == "plan_complete":
            candidates.append((5.0, {
                "block": "mock",
                "what": "Timed mock exam (or a long mixed quiz)",
                "why": "Every plan day is done; now practise under exam conditions.",
                "how": "learning_mock_exam_start (or learning_exam_quiz for a shorter set)",
            }))
        else:
            candidates.append((5.0, {
                "block": "lesson",
                "what": _lesson_what(plan),
                "why": f"This is the next open day of your {plan['total_days'] or 'study'}-day plan.",
                "how": "learning_exam_day, then learning_exam_day_complete when done",
            }))
        if count:
            candidates.append(reviews)
        if weak:
            candidates.append(drill)
        if python_block:
            candidates.append(python_block)
        candidates.append(quiz)

    slots = max(1, min(5, budget // 10, len(candidates)))
    chosen = candidates[:slots]
    minutes = _apportion([w for w, _ in chosen], budget)
    return [{**block, "minutes": m} for (_, block), m in zip(chosen, minutes)]


def _apportion(weights: list[float], total: int) -> list[int]:
    """Whole minutes in proportion to ``weights`` that add up to exactly ``total`` (each at least 5 when possible)."""
    if not weights:
        return []
    whole = sum(weights)
    raw = [total * w / whole for w in weights]
    out = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - out[i]), i))
    for i in order[: total - sum(out)]:
        out[i] += 1
    for i in range(len(out)):
        while out[i] < MIN_BLOCK_MINUTES:
            donor = max(range(len(out)), key=lambda j: out[j])
            if donor == i or out[donor] <= MIN_BLOCK_MINUTES:
                break
            out[donor] -= 1
            out[i] += 1
    return out


def _headline(mode: str, plan: dict[str, Any], days_left: int | None, budget: int) -> str:
    if mode == "no_plan":
        return "Welcome! Set your exam date first, then a day-by-day plan shows up here."
    if mode == "exam_passed":
        return (f"Your exam date ({plan['exam_date']}) has passed. If you are retaking it, set a new date; "
                "otherwise keep your reviews ticking over.")
    if mode == "exam_day":
        return "Exam day! Light warm-up only, then rest. You have prepared for this - good luck!"
    left = f"{days_left} day{'s' if days_left != 1 else ''} to the exam" if days_left is not None else "no exam date yet"
    if mode == "plan_complete":
        return f"Every plan day is done and {left}: today is for mock exams and reviews."
    day = plan["calendar_day"]
    total = plan["total_days"]
    where = f"Day {day} of {total}" if day and total else "Today"
    return f"{where}, {left}: {budget} focused minutes keep you on track."


def _streak_message(streak: int) -> str:
    if streak <= 0:
        return "Answer one question today to start a new study streak."
    if streak == 1:
        return "1 study day in a row. Today makes it 2!"
    return f"{streak} study days in a row. Nice rhythm!"


def _catch_up(missed: int, plan: dict[str, Any]) -> str | None:
    nxt = plan.get("next_open_day")
    if missed <= 0:
        return None
    if missed == 1:
        return f"Day {nxt} is still open: just start there today, no need to double up."
    if missed <= 3:
        return (f"{missed} earlier plan days are still open. Start with day {nxt} today; if you find a spare half "
                "hour this week, add one more short day.")
    return (f"{missed} earlier plan days are still open. That is normal; re-plan with learning_exam_set_date "
            "so the remaining days fit your time again.")


def _markdown(brief: dict[str, Any]) -> str:
    lines = [f"**{brief['headline']}**", ""]
    if brief["plan"]:
        lines += [f"## Plan for {brief['minutes']} minutes", ""]
        for number, block in enumerate(brief["plan"], 1):
            lines.append(f"{number}. **{block['minutes']} min - {block['what']}**  ")
            lines.append(f"   Why: {block['why']}  ")
            lines.append(f"   How: `{block['how']}`")
        lines.append("")
    due = brief["due_reviews"]
    facts = [f"- Due reviews: {due['count']}"
             + (f" (most in objective {due['top_objectives'][0]['objective_id']})" if due["top_objectives"] else "")]
    if brief["weak_domains"]:
        facts.append("- Weak domains: " + ", ".join(
            f"{d['name']} ({dashboard._fmt_pct(d['accuracy_pct'])})" for d in brief["weak_domains"]))
    facts.append(f"- Streak: {brief['streak_message']}")
    if brief["catch_up"]:
        facts.append(f"- Catch-up: {brief['catch_up']}")
    practice = brief["python_practice_next"]
    if practice:
        facts.append(f"- Next Python exercise: `{practice['id']}` ({practice['passed']}/{practice['total']} passed)")
    lines += ["## Status", "", *facts]
    for note in brief["notes"]:
        lines.append(f"- Note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------- small helpers


def _cap_line(text: str) -> str:
    return _short(text, MAX_LINE_CHARS)


def _short(text: Any, limit: int) -> str:
    """One line of at most ``limit`` characters (plain ASCII '...' when cut, so any notification can show it)."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _ps(value: str) -> str:
    """A PowerShell single-quoted string literal (a ' inside is doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def _default_pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


__all__ = [
    "python_practice_next",
    "read_only_store",
    "reminder_text",
    "scheduled_task_commands",
    "today_brief",
    "write_today_file",
]

if __name__ == "__main__":
    raise SystemExit(main())
