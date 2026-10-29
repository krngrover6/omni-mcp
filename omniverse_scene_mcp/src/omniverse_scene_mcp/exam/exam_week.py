"""Exam week: the last days before NCP-OUSD as a concrete, data-driven agenda, plus a readiness verdict.

Pure functions over ``dashboard.dashboard_data(...)`` output, so nothing here reads or writes files. The week runs two
full timed mocks (each followed by a review day), drills the weakest *weighted* domains between them, keeps the last
day light and ends with an exam-day pacing checklist. Every step names the exact router tool to call.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

DEFAULT_DAYS = 7
READY_DOMAIN_PCT = 70.0


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def weakest_domains(domains: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Domains ranked by how many exam marks they put at risk: weight x (1 - accuracy); untested counts as 0%."""
    ranked = []
    for row in domains:
        if not row.get("in_blueprint", True):
            continue
        accuracy = row.get("accuracy_pct")
        risk = float(row.get("weight") or 0) * (1 - (accuracy if accuracy is not None else 0.0) / 100)
        ranked.append({"name": row.get("name"), "weight": row.get("weight"), "accuracy_pct": accuracy,
                       "status": row.get("status"), "marks_at_risk": round(risk, 1)})
    ranked.sort(key=lambda row: (-row["marks_at_risk"], -(row["weight"] or 0)))
    return ranked[:limit]


def readiness(data: dict[str, Any]) -> dict[str, Any]:
    """Ready / not yet, with the reasons, from mocks, domain accuracy and question coverage."""
    target = float(data.get("target_pct") or 80.0)
    mocks = data.get("mocks") or {}
    domains = [row for row in data.get("domains") or [] if row.get("in_blueprint", True)]
    reasons: list[str] = []
    best = mocks.get("best_pct")
    if not mocks.get("finished_count"):
        reasons.append("No full mock exam finished yet: do one (learning_mock_exam_start) to measure yourself.")
    elif best is None or best < target:
        reasons.append(f"Best mock score {best}% is under your {target:g}% target.")
    untested = [row["name"] for row in domains if row.get("accuracy_pct") is None]
    if untested:
        reasons.append(f"Never practised: {', '.join(untested)}.")
    low = [f"{row['name']} {row['accuracy_pct']:g}%" for row in domains
           if row.get("accuracy_pct") is not None and row["accuracy_pct"] < READY_DOMAIN_PCT]
    if low:
        reasons.append(f"Under {READY_DOMAIN_PCT:g}%: {', '.join(low)}.")
    due = data.get("due_reviews") or 0
    if due > 20:
        reasons.append(f"{due} spaced-repetition reviews are waiting (learning_exam_review).")
    ready = not reasons
    return {
        "ready": ready,
        "verdict": ("Ready: your best mock beats the target and every domain is at or above "
                    f"{READY_DOMAIN_PCT:g}%." if ready else "Not ready yet."),
        "reasons": reasons,
        "best_mock_pct": best,
        "weighted_accuracy_pct": (data.get("totals") or {}).get("weighted_accuracy_pct"),
        "target_pct": target,
        "note": "NVIDIA publishes no passing score; these are self-set readiness rules, not the exam's.",
    }


def _agenda(offset: int, weak: list[dict[str, Any]], exam: dict[str, Any]) -> dict[str, Any]:
    """The plan for the day `offset` days before the exam (0 = exam day)."""
    names = [row["name"] for row in weak] or ["Composition"]
    first, second, third = (names + names + names)[:3]
    minutes = int(exam.get("time_limit_minutes") or 120)
    if offset == 0:
        return {"title": "Exam day", "minutes": 0, "steps": [
            "No new material. At most 15 minutes of learning_drill_start(focus='flash') to warm up.",
            f"Pacing: {minutes} minutes for {exam.get('questions') or '60-70'} questions is under 2 minutes each. "
            "First pass: answer the ones you are sure of and flag the rest. Second pass: the flagged ones. Keep 10 "
            "minutes at the end.",
            "Read every 'choose two' / 'choose all' question twice; eliminate wrong options before picking.",
            "Follow the exam provider's own check-in and system-check instructions (ID, room, computer) in good time.",
        ]}
    plans = {
        7: ("Full mock exam #1", minutes + 30, [
            "learning_mock_exam_start() and sit it like the real exam: one go, no notes, timer on.",
            "learning_mock_exam_finish(session_id): note the score and the two weakest domains it shows.",
        ]),
        6: ("Review mock #1", 90, [
            "Go through every wrong or unanswered question from learning_mock_exam_finish; for each, write one line on "
            "why the right answer is right.",
            "learning_exam_review(): clear the spaced-repetition cards that are due.",
            f"learning_drill_start(focus='domain', domain='{first}', count=15).",
        ]),
        5: ("Drill the weak domains", 75, [
            f"learning_drill_start(focus='domain', domain='{second}', count=15).",
            f"learning_drill_start(focus='domain', domain='{third}', count=15).",
            "learning_composition_puzzle(difficulty='medium') x3: Composition is the biggest domain (23%).",
        ]),
        4: ("Full mock exam #2", minutes + 30, [
            "learning_mock_exam_start(): a second full run, same rules.",
            "learning_mock_exam_finish(session_id): compare with mock #1 domain by domain.",
        ]),
        3: ("Review mock #2", 75, [
            "Review every miss from mock #2, as after mock #1.",
            "learning_drill_start(focus='mistakes', count=20): only questions you got wrong.",
            f"learning_drill_start(focus='domain', domain='{first}', count=10) if it is still the weakest.",
        ]),
        2: ("Light mixed practice", 45, [
            "learning_drill_start(focus='weak', count=15).",
            "learning_cheatsheet(): read it once; look up anything fuzzy with learning_glossary(term).",
        ]),
        1: ("Rest and review", 20, [
            "No new topics and no mock. learning_exam_review() for due cards only, 20 minutes at most.",
            "Plan the exam-day logistics (time, ID, quiet room, computer check) and sleep early.",
        ]),
    }
    title, mins, steps = plans.get(offset, ("Follow the daily plan", 60, [
        "learning_exam_day(): today's lesson from your study plan, then learning_today() for reviews and a drill.",
    ]))
    return {"title": title, "minutes": mins, "steps": steps}


def exam_week(data: dict[str, Any], *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """The final `days` (3-14) before the exam date in the saved study plan, one agenda per day."""
    if not isinstance(days, int) or isinstance(days, bool) or not 3 <= days <= 14:
        return {"ok": False, "error": "days must be a whole number from 3 to 14."}
    plan = data.get("plan") or {}
    exam_date = _date(plan.get("exam_date"))
    today = _date(data.get("today")) or date.today()
    if exam_date is None:
        return {"ok": False, "error": "No exam date is saved yet. Set it with learning_exam_set_date(exam_date="
                                      "'YYYY-MM-DD') first."}
    exam = data.get("exam") or {}
    weak = weakest_domains(data.get("domains") or [])
    start = exam_date - timedelta(days=days)
    schedule = []
    for offset in range(days, -1, -1):
        day = exam_date - timedelta(days=offset)
        schedule.append({"date": day.isoformat(), "days_before_exam": offset, "is_today": day == today,
                         "past": day < today, **_agenda(offset, weak, exam)})
    days_left = (exam_date - today).days
    if days_left < 0:
        where = f"The saved exam date {exam_date.isoformat()} has passed; set a new one with learning_exam_set_date."
    elif today < start:
        where = (f"Exam week starts on {start.isoformat()} ({(start - today).days} days from now). Until then, "
                 "follow the daily plan (learning_today).")
    else:
        where = f"You are in exam week: {days_left} day(s) to the exam. Today's agenda is marked is_today."
    return {
        "ok": True, "read_only": True,
        "exam_date": exam_date.isoformat(), "today": today.isoformat(), "days_left": days_left,
        "where_you_are": where,
        "focus_domains": weak,
        "readiness": readiness(data),
        "schedule": schedule,
        "note": "Read-only plan: nothing is started or saved until you call the tools it names.",
    }
