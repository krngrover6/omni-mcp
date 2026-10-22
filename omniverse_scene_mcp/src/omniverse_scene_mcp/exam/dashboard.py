"""A local, self-contained NCP-OUSD progress dashboard: one HTML page built from saved progress.

``dashboard_data`` only reads (progress, settings, mock sessions, blueprint, bank) and never changes them.
``render_dashboard_html`` turns that data into one HTML page with inline CSS and inline SVG bars: no scripts,
no fonts, no links, no network. ``write_dashboard`` saves the page atomically as ``dashboard.html`` in the
exam-prep folder (``$OMNIVERSE_EXAM_PREP_DIR``, else ``<workspace>/exam-prep``, as for progress.json) or in the
progress store's own folder, and nowhere else.
"""

from __future__ import annotations

import contextlib
import html
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import srs
from .progress import default_progress_path

FILE_NAME = "dashboard.html"
DEFAULT_TARGET_PCT = 80.0
MIN_EVIDENCE = 5
STRONG_MARGIN = 10.0
MAX_WEAKEST = 5
MAX_MOCKS = 10
MAX_FOCUS = 6
MAX_TOPICS = 8
MOCK_SUGGEST_AFTER = 30
TEXT_CAP = 200
HTML_SUFFIXES = (".html", ".htm")

STATUS_LABELS = {
    "untested": ("○", "Not started"),
    "low_evidence": ("◔", "Too few answers to judge"),
    "weak": ("▲", "Needs practice"),
    "on_track": ("●", "On track"),
    "strong": ("✓", "Strong"),
}


# ---------------------------------------------------------------- data


def dashboard_data(
    progress_store: Any,
    *,
    mock_store: Any = None,
    blueprint: Any = None,
    bank: Any = None,
    today: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Everything the dashboard shows, as plain JSON-ready data. Reads only; nothing is saved.

    ``progress_store`` needs ``summary()`` and ``get_settings()``; ``mock_store`` (optional) is a MockExamStore;
    ``blueprint`` is the blueprint module (default) or its parsed dict; ``bank`` is the question-bank module
    (default) or a list of question dicts. ``today`` (ISO date, date or datetime) fixes the day used for the plan,
    due reviews and the streak; None means now.
    """
    if progress_store is None:
        raise TypeError("dashboard_data needs a progress store")
    day_today = _as_date(today)
    moment = None if today is None else datetime.combine(day_today, dtime(12, 0), tzinfo=timezone.utc)
    bp = _blueprint_data(blueprint)
    bp_domains = [d for d in bp.get("domains") or [] if isinstance(d, dict) and d.get("name")]
    target = DEFAULT_TARGET_PCT
    objective_texts = {
        str(obj.get("id")): str(obj.get("text") or "")
        for dom in bp_domains
        for obj in dom.get("objectives") or []
        if isinstance(obj, dict) and obj.get("id")
    }

    summary = progress_store.summary(now=moment, known_domains=[d["name"] for d in bp_domains])
    settings = progress_store.get_settings()
    cards = _cards(progress_store)
    items = _bank_items(bank)
    notes: list[str] = []

    # Per-domain accuracy against exam weight, plus question-bank coverage.
    bank_total: dict[str, int] = {}
    bank_answered: dict[str, int] = {}
    for item in items:
        name = str(item.get("domain") or "")
        bank_total[name] = bank_total.get(name, 0) + 1
        if str(item.get("id")) in cards:
            bank_answered[name] = bank_answered.get(name, 0) + 1
    stats = summary.get("domains") or {}
    names = [d["name"] for d in bp_domains] + sorted(n for n in stats if n not in {d["name"] for d in bp_domains})
    by_name = {d["name"]: d for d in bp_domains}
    domains = []
    for name in names:
        dom = by_name.get(name, {})
        row = stats.get(name) or {}
        answered = _int(row.get("answered"))
        correct = min(answered, _int(row.get("correct")))
        accuracy = _pct(correct, answered)
        status = _status(answered, accuracy, target)
        weight = dom.get("weight")
        total = bank_total.get(name, 0)
        done = bank_answered.get(name, 0)
        domains.append(
            {
                "id": dom.get("id"),
                "name": name,
                "weight": weight if isinstance(weight, (int, float)) else None,
                "answered": answered,
                "correct": correct,
                "wrong": answered - correct,
                "accuracy_pct": accuracy,
                "status": status,
                "status_label": STATUS_LABELS[status][1],
                "bank_total": total,
                "bank_answered": done,
                "coverage_pct": _pct(done, total),
                "in_blueprint": bool(dom),
            }
        )
    tested = [d for d in domains if d["answered"] and d["weight"]]
    tested_weight = sum(d["weight"] for d in tested)
    weighted = (
        round(sum(d["weight"] * d["accuracy_pct"] for d in tested) / tested_weight, 1) if tested_weight else None
    )
    all_weight = sum(d["weight"] for d in domains if d["weight"])

    totals = summary.get("totals") or {}
    answered_total = _int(totals.get("answered"))
    correct_total = min(answered_total, _int(totals.get("correct")))
    weakest = []
    for row in (summary.get("weakest_objectives") or [])[:MAX_WEAKEST]:
        oid = str(row.get("objective_id") or "")
        a = _int(row.get("answered"))
        c = min(a, _int(row.get("correct")))
        weakest.append(
            {
                "objective_id": oid,
                "domain": row.get("domain"),
                "text": objective_texts.get(oid, ""),
                "answered": a,
                "correct": c,
                "accuracy_pct": _pct(c, a),
            }
        )

    coverage_total = sum(bank_total.values())
    coverage_done = sum(bank_answered.values())
    coverage = {
        "answered": coverage_done,
        "total": coverage_total,
        "pct": _pct(coverage_done, coverage_total),
        "by_domain": [
            {"name": d["name"], "answered": d["bank_answered"], "total": d["bank_total"], "pct": d["coverage_pct"]}
            for d in domains
            if d["bank_total"] or d["in_blueprint"]
        ],
    }
    if not items:
        notes.append("The question bank could not be read, so coverage shows 0 questions.")

    plan = _plan(settings, day_today, objective_texts)
    mocks = _mocks(mock_store, notes)
    exam = bp.get("exam") or {}
    data = {
        "ok": True,
        "generated_at": srs.to_iso(moment or srs.utc_now()),
        "today": day_today.isoformat(),
        "target_pct": target,
        "exam": {
            "code": exam.get("code"),
            "name": exam.get("name"),
            "time_limit_minutes": exam.get("time_limit_minutes"),
            "questions": (exam.get("format") or {}).get("questions"),
        },
        "totals": {
            "answered": answered_total,
            "correct": correct_total,
            "wrong": answered_total - correct_total,
            "accuracy_pct": _pct(correct_total, answered_total),
            "unique_questions": _int(totals.get("unique_questions")),
            "weighted_accuracy_pct": weighted,
            "tested_weight_pct": round(100.0 * tested_weight / all_weight, 1) if all_weight else None,
        },
        "domains": domains,
        "weakest_objectives": weakest,
        "due_reviews": _int(summary.get("due_count")),
        "streak_days": _int(summary.get("streak_days")),
        "last_answered_at": summary.get("last_answered_at"),
        "plan": plan,
        "mocks": mocks,
        "coverage": coverage,
        "notes": notes
        + [
            "NVIDIA publishes no passing score; the target here is a self-set readiness goal.",
            "Weighted accuracy counts each domain by its share of the exam, using only domains you have tried.",
        ],
    }
    data["focus_next"] = _focus_next(data)
    return data


def _plan(settings: dict[str, Any], today: date, objective_texts: dict[str, str]) -> dict[str, Any]:
    start = _maybe_date(settings.get("plan_start"))
    exam_date = _maybe_date(settings.get("exam_date"))
    days_until = settings.get("days_until_exam")
    schedule = [e for e in settings.get("schedule") or [] if isinstance(e, dict)]
    completed = sorted({d for d in settings.get("completed_days") or [] if isinstance(d, int) and d > 0})
    total = len(schedule) or (min(int(days_until), 60) if isinstance(days_until, int) else 0)
    if exam_date is None and start and isinstance(days_until, int):
        exam_date = start + timedelta(days=days_until)
    days_left = (exam_date - today).days if exam_date else None
    calendar_day = (today - start).days + 1 if start else None
    today_day = calendar_day if calendar_day and total and 1 <= calendar_day <= total else None
    done = set(completed)
    next_open = next((n for n in range(1, total + 1) if n not in done), None) if total else None
    behind = 0
    if calendar_day and total:
        behind = sum(1 for n in range(1, min(calendar_day, total + 1)) if n not in done)
    return {
        "configured": bool(start),
        "plan_start": start.isoformat() if start else None,
        "exam_date": exam_date.isoformat() if exam_date else None,
        "days_left": days_left,
        "minutes_per_day": settings.get("minutes_per_day"),
        "total_days": total,
        "completed_days": completed,
        "completed_count": len([n for n in completed if not total or n <= total]),
        "calendar_day": calendar_day,
        "today_day": today_day,
        "today": _day_entry(schedule, today_day, done, start, objective_texts),
        "next_open_day": next_open,
        "next": _day_entry(schedule, next_open, done, start, objective_texts),
        "behind_days": behind,
        "plan_complete": bool(total) and next_open is None,
    }


def _day_entry(
    schedule: list[dict[str, Any]], day: int | None, done: set[int], start: date | None, texts: dict[str, str]
) -> dict[str, Any] | None:
    if not day:
        return None
    entry = schedule[day - 1] if day <= len(schedule) else {}
    topics = []
    for block in (entry.get("blocks") or [])[:MAX_TOPICS]:
        if not isinstance(block, dict):
            continue
        oid = block.get("objective")
        if oid:
            label = texts.get(str(oid)) or f"Objective {oid}"
            topics.append({"objective_id": str(oid), "domain": block.get("domain"), "label": label,
                           "minutes": block.get("minutes"), "pass": block.get("pass")})
        else:
            extra = block.get("objectives") or block.get("domains") or []
            label = str(block.get("activity") or "practice")
            if extra:
                label += ": " + ", ".join(str(x) for x in extra[:8])
            topics.append({"objective_id": None, "domain": None, "label": label, "minutes": block.get("minutes"),
                           "pass": None})
    return {
        "day": day,
        "date": (start + timedelta(days=day - 1)).isoformat() if start else None,
        "type": entry.get("type"),
        "minutes": entry.get("minutes"),
        "completed": day in done,
        "topics": topics,
    }


def _mocks(mock_store: Any, notes: list[str]) -> dict[str, Any]:
    result = {"available": mock_store is not None, "finished": [], "finished_count": 0, "active": False,
              "best_pct": None, "latest_pct": None}
    if mock_store is None:
        return result
    records: list[dict[str, Any]] = []
    loader = getattr(mock_store, "_load", None)
    try:
        if callable(loader):
            records = [r for r in (loader().get("sessions") or []) if isinstance(r, dict)]
        else:
            for row in reversed(mock_store.list_sessions(limit=50)):
                rec = {"id": row.get("session_id"), "status": row.get("status"), "finished_at": row.get("finished_at")}
                if row.get("status") == "finished":
                    with contextlib.suppress(Exception):
                        report = mock_store.review(row.get("session_id"))
                        rec["result"] = {"score": report.get("score"), "domains": report.get("domains"),
                                         "readiness": report.get("readiness")}
                records.append(rec)
    except Exception as exc:  # a damaged mock file must not break the dashboard
        notes.append(f"Mock exam history could not be read: {_cap(exc)}")
        return result
    result["active"] = any(r.get("status") == "active" for r in records)
    finished = []
    for rec in reversed(records):
        res = rec.get("result")
        if rec.get("status") != "finished" or not isinstance(res, dict):
            continue
        score = res.get("score") or {}
        domains = {}
        for name, row in (res.get("domains") or {}).items():
            if isinstance(row, dict):
                domains[str(name)] = {"pct": _num(row.get("pct")), "correct": _int(row.get("correct")),
                                      "questions": _int(row.get("questions"))}
        finished.append(
            {
                "session_id": rec.get("id"),
                "finished_at": rec.get("finished_at"),
                "score_pct": _num(score.get("pct")),
                "correct": _int(score.get("correct")),
                "questions": _int(score.get("questions")),
                "answered": _int(score.get("answered")),
                "meets_target": bool((res.get("readiness") or {}).get("meets_target")),
                "domains": domains,
            }
        )
    result["finished_count"] = len(finished)
    result["finished"] = finished[:MAX_MOCKS]
    scores = [m["score_pct"] for m in finished if m["score_pct"] is not None]
    result["best_pct"] = max(scores) if scores else None
    result["latest_pct"] = scores[0] if scores else None
    return result


def _focus_next(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A short, plain-language 'what to do next' list, most useful first."""
    focus: list[dict[str, Any]] = []
    target = data["target_pct"]
    due = data["due_reviews"]
    if due:
        focus.append({"kind": "reviews", "text": f"Answer your {due} due review question{'s' if due != 1 else ''}. "
                      "These are questions coming back so you remember them."})
    plan = data["plan"]
    if not plan["configured"]:
        focus.append({"kind": "plan", "text": "Set your exam date to get a day-by-day study plan."})
    elif plan["plan_complete"]:
        focus.append({"kind": "plan", "text": "Every plan day is done. Set a new exam date to plan more days."})
    else:
        entry = plan["next"]  # like "start day" with no number: the first day not yet marked complete
        if entry:
            topics = [t["label"] for t in entry["topics"][:2]]
            what = f": {'; '.join(_short(t, 70) for t in topics)}" if topics else ""
            text = f"Do day {entry['day']} of your study plan{what}."
            if plan["behind_days"] > 1:
                text += f" You are {plan['behind_days']} days behind, so take it one day at a time."
            focus.append({"kind": "plan", "text": text})

    domains = [d for d in data["domains"] if d["in_blueprint"]]
    weak = [d for d in domains if d["status"] == "weak"]
    weak.sort(key=lambda d: (-(d["weight"] or 0) * (target - d["accuracy_pct"]), d["name"]))
    for d in weak[:2]:
        focus.append({"kind": "weak_domain", "domain": d["name"],
                      "text": f"Practise {d['name']}: you got {_fmt_pct(d['accuracy_pct'])} right so far "
                      f"(goal {target:g}%). It is {d['weight']}% of the exam."})
    for row in data["weakest_objectives"][:1]:
        label = f" ({_short(row['text'], 70)})" if row["text"] else ""
        focus.append({"kind": "weak_objective", "objective_id": row["objective_id"],
                      "text": f"Revisit objective {row['objective_id']}{label}: {row['correct']} of "
                      f"{row['answered']} right."})
    untested = sorted((d for d in domains if d["status"] == "untested"), key=lambda d: (-(d["weight"] or 0), d["name"]))
    for d in untested[:1]:
        focus.append({"kind": "untested_domain", "domain": d["name"],
                      "text": f"Try your first {d['name']} questions. It is {d['weight']}% of the exam."})
    thin = sorted((d for d in domains if d["status"] == "low_evidence"), key=lambda d: (-(d["weight"] or 0), d["name"]))
    for d in thin[:1]:
        focus.append({"kind": "low_evidence", "domain": d["name"],
                      "text": f"Answer a few more {d['name']} questions; {d['answered']} "
                      f"answer{'s are' if d['answered'] != 1 else ' is'} not enough to judge."})
    mocks = data["mocks"]
    if mocks["available"] and not mocks["finished_count"] and data["totals"]["answered"] >= MOCK_SUGGEST_AFTER:
        focus.append({"kind": "mock", "text": "Take a timed mock exam to practise under real exam conditions."})
    if not focus:
        focus.append({"kind": "keep_going", "text": "Keep going: answer a short mixed quiz to keep your streak."})
    return focus[:MAX_FOCUS]


# ---------------------------------------------------------------- HTML


_CSS = """
:root{color-scheme:light;--bg:#f6f5f2;--card:#fcfcfb;--line:#e2e1dc;--ink:#0b0b0b;--ink2:#52514e;--ink3:#6e6d68;
--bar:#2a78d6;--bar2:#86b6ef;--track:#e9e8e4;--mark:#0b0b0b;--good:#0a7f0a;--warn:#a86a00;--bad:#c03030;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--card:#1a1a19;
--line:#33332f;--ink:#f0efec;--ink2:#c3c2b7;--ink3:#9e9d95;--bar:#3987e5;--bar2:#184f95;--track:#2c2c2a;
--mark:#f0efec;--good:#0ca30c;--warn:#fab219;--bad:#e66767;}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--card:#1a1a19;--line:#33332f;--ink:#f0efec;--ink2:#c3c2b7;
--ink3:#9e9d95;--bar:#3987e5;--bar2:#184f95;--track:#2c2c2a;--mark:#f0efec;--good:#0ca30c;--warn:#fab219;--bad:#e66767;}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.6rem;line-height:1.2;margin:0 0 4px}
h2{font-size:1.15rem;margin:0 0 12px}
p{margin:0 0 8px}
.sub,.muted{color:var(--ink2)}
.small{font-size:.875rem}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.tile .v{font-size:1.6rem;font-weight:650;line-height:1.2;font-variant-numeric:tabular-nums}
.tile .k{color:var(--ink2);font-size:.875rem}
ol.focus{margin:0;padding-left:1.4em}
ol.focus li{margin:0 0 6px}
.dom{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,2fr);gap:4px 16px;padding:12px 0;
border-top:1px solid var(--line)}
.doms .dom:first-child{border-top:0}
.dom .name{font-weight:600}
.dom .meta{color:var(--ink2);font-size:.875rem}
.barrow{display:flex;align-items:center;gap:8px;font-size:.875rem;color:var(--ink2)}
.barrow svg{flex:1 1 auto;min-width:60px;height:14px;display:block}
.barrow .lab{flex:0 0 7.5em;text-align:right;font-variant-numeric:tabular-nums;color:var(--ink)}
.st{font-size:.875rem;white-space:nowrap}
.st-strong,.st-on_track{color:var(--good)} .st-weak{color:var(--bad)} .st-low_evidence{color:var(--warn)}
.st-untested{color:var(--ink3)}
.st span{color:var(--ink)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.9rem;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink2);font-weight:600}
td.n,th.n{text-align:right}
ul.topics{margin:4px 0 0;padding-left:1.2em}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.85rem;color:var(--ink2);margin:0 0 8px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px}
.tick{display:inline-block;width:2px;height:12px;background:var(--mark);vertical-align:-1px;margin-right:6px}
@media (max-width:640px){.dom{grid-template-columns:1fr}.barrow .lab{flex-basis:6.5em}h1{font-size:1.35rem}}
"""


def render_dashboard_html(data: dict[str, Any]) -> str:
    """One self-contained HTML page (inline CSS and SVG only; no scripts, fonts, links or URLs). All text is escaped."""
    exam = data.get("exam") or {}
    totals = data.get("totals") or {}
    plan = data.get("plan") or {}
    mocks = data.get("mocks") or {}
    coverage = data.get("coverage") or {}
    target = _num(data.get("target_pct")) or DEFAULT_TARGET_PCT
    code = exam.get("code") or "NCP-OUSD"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_e(code)} Study Dashboard</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        f"<h1>{_e(code)} study dashboard</h1>",
        f'<p class="sub">{_e(exam.get("name") or "")}</p>',
        f'<p class="muted small">Made on {_e(data.get("today") or "")} from your saved answers. '
        "Run the dashboard again to refresh it.</p>",
    ]

    days_left = plan.get("days_left")
    tiles = [
        (str(totals.get("answered", 0)), "questions answered"),
        (_fmt_pct(totals.get("accuracy_pct")), "answered correctly"),
        (str(data.get("due_reviews", 0)), "reviews due now"),
        (f"{data.get('streak_days', 0)} day{'s' if data.get('streak_days') != 1 else ''}", "study streak"),
        ("not set" if days_left is None else str(days_left), "days until the exam"),
    ]
    parts.append('<div class="tiles">')
    for value, key in tiles:
        parts.append(f'<div class="tile"><div class="v">{_e(value)}</div><div class="k">{_e(key)}</div></div>')
    parts.append("</div>")

    parts.append('<section aria-labelledby="h-next"><h2 id="h-next">What to do next</h2><ol class="focus">')
    for item in data.get("focus_next") or []:
        parts.append(f"<li>{_e(item.get('text', ''))}</li>")
    parts.append("</ol></section>")

    parts.append(_plan_html(plan))
    parts.append(_domains_html(data.get("domains") or [], target, totals))
    parts.append(_weakest_html(data.get("weakest_objectives") or []))
    parts.append(_mocks_html(mocks, data.get("domains") or [], target))
    parts.append(_coverage_html(coverage))

    parts.append('<section aria-labelledby="h-help"><h2 id="h-help">How to read this page</h2><ul class="small">')
    for line in [
        "Accuracy is the share of your answers that were right. The thin dark line on each bar is your goal of "
        f"{target:g}%.",
        "Exam share is how much of the real exam a domain covers, so a weak domain with a big share matters most.",
        f"A domain needs at least {MIN_EVIDENCE} answers before the dashboard calls it weak or strong.",
        "Reviews due are questions the spaced-repetition system brings back: missed ones come back soon, "
        "right ones later.",
        *[str(n) for n in data.get("notes") or []],
    ]:
        parts.append(f"<li>{_e(line)}</li>")
    parts.append("</ul></section>")
    parts += ["</main>", "</body>", "</html>", ""]
    return "\n".join(parts)


def _plan_html(plan: dict[str, Any]) -> str:
    out = ['<section aria-labelledby="h-plan"><h2 id="h-plan">Your study plan</h2>']
    if not plan.get("configured"):
        out.append("<p>No plan yet. Set your exam date (or a number of days) and a plan appears here.</p></section>")
        return "".join(out)
    rows = [
        ("Plan started", plan.get("plan_start") or "-"),
        ("Exam date", plan.get("exam_date") or "not set"),
        ("Days left", "-" if plan.get("days_left") is None else str(plan["days_left"])),
        ("Days completed", f"{plan.get('completed_count', 0)} of {plan.get('total_days', 0)}"),
        ("Minutes per day", str(plan.get("minutes_per_day") or "-")),
    ]
    if plan.get("today_day"):
        rows.append(("Today is plan day", str(plan["today_day"])))
    if plan.get("behind_days"):
        rows.append(("Days not done yet before today", str(plan["behind_days"])))
    out.append('<table><tbody>')
    for key, value in rows:
        out.append(f"<tr><th scope=\"row\">{_e(key)}</th><td>{_e(value)}</td></tr>")
    out.append("</tbody></table>")
    total = plan.get("total_days") or 0
    if total:
        out.append(f'<div class="barrow" style="margin-top:10px">{_bar(plan.get("completed_count", 0) / total * 100)}'
                   f'<span class="lab">{_e(plan.get("completed_count", 0))} / {_e(total)} days</span></div>')
    shown = []
    today = plan.get("today")
    if today:
        shown.append((f"Today: day {today['day']}" + (" (done)" if today.get("completed") else ""), today))
    nxt = plan.get("next")
    if nxt and (not today or nxt["day"] != today["day"]):
        shown.append((f"Next open day: day {nxt['day']}", nxt))
    for title, entry in shown:
        kind = {"study": "study day", "review": "review day", "final_review": "final review day"}.get(
            entry.get("type"), "plan day")
        minutes = f", about {entry['minutes']} minutes" if entry.get("minutes") else ""
        out.append(f'<p style="margin-top:12px"><strong>{_e(title)}</strong> '
                   f'<span class="muted">({_e(kind)}{_e(minutes)})</span></p><ul class="topics">')
        for topic in entry.get("topics") or []:
            prefix = f"{topic['objective_id']}: " if topic.get("objective_id") else ""
            out.append(f"<li>{_e(prefix + str(topic.get('label') or ''))}</li>")
        if not entry.get("topics"):
            out.append("<li>Due reviews and a mixed quiz.</li>")
        out.append("</ul>")
    if plan.get("plan_complete"):
        out.append("<p>All plan days are complete.</p>")
    out.append("</section>")
    return "".join(out)


def _domains_html(domains: list[dict[str, Any]], target: float, totals: dict[str, Any]) -> str:
    out = ['<section aria-labelledby="h-dom"><h2 id="h-dom">How you are doing in each exam domain</h2>']
    weighted = totals.get("weighted_accuracy_pct")
    if weighted is not None:
        out.append(f"<p>Weighted by exam share, you get about <strong>{_e(_fmt_pct(weighted))}</strong> right "
                   f"in the domains you have tried ({_e(_fmt_pct(totals.get('tested_weight_pct')))} of the exam).</p>")
    else:
        out.append("<p>No answers yet. Each domain fills in as you answer questions.</p>")
    out.append('<div class="legend"><span><span class="sw" style="background:var(--bar)"></span>accuracy</span>'
               f'<span><span class="tick"></span>your goal ({_e(f"{target:g}")}%)</span></div>')
    out.append('<div class="doms">')
    for d in domains:
        icon, label = STATUS_LABELS.get(d.get("status"), ("", d.get("status_label") or ""))
        number = f"{d['id']}. " if d.get("id") is not None else ""
        share = f"{d['weight']}% of the exam" if d.get("weight") is not None else "not an exam domain"
        acc = d.get("accuracy_pct")
        counts = (f"{d.get('correct', 0)} of {d.get('answered', 0)} right" if d.get("answered")
                  else "no answers yet")
        tip = f"{d['name']}: {counts}" + (f" ({_fmt_pct(acc)})" if acc is not None else "")
        out.append(
            '<div class="dom">'
            f'<div><div class="name">{_e(number + str(d["name"]))}</div>'
            f'<div class="meta">{_e(share)} &middot; {_e(counts)}</div>'
            f'<div class="st st-{_e(d.get("status") or "")}">{_e(icon)} <span>{_e(label)}</span></div></div>'
            '<div>'
            f'<div class="barrow"><span>Accuracy</span>{_bar(acc, target=target, title=tip)}'
            f'<span class="lab">{_e(_fmt_pct(acc))}</span></div>'
            f'<div class="barrow"><span>Share</span>{_bar(d.get("weight"), scale=25, light=True, title=share)}'
            f'<span class="lab">{_e(share.split(" ")[0] if d.get("weight") is not None else "-")}</span></div>'
            "</div></div>"
        )
    out.append("</div>")
    out.append('<p class="muted small">The share bar is drawn against 25%, the size of the biggest domain.</p>')
    out.append("</section>")
    return "".join(out)


def _weakest_html(rows: list[dict[str, Any]]) -> str:
    out = ['<section aria-labelledby="h-weak"><h2 id="h-weak">Objectives to revisit</h2>']
    if not rows:
        out.append("<p>No missed objectives yet. Objectives you get wrong show up here.</p></section>")
        return "".join(out)
    out.append('<div class="scroll"><table><thead><tr><th>Objective</th><th>What it is about</th>'
               '<th class="n">Right</th></tr></thead><tbody>')
    for row in rows:
        out.append(f"<tr><td>{_e(row.get('objective_id'))}</td>"
                   f"<td>{_e(row.get('text') or row.get('domain') or '')}</td>"
                   f"<td class=\"n\">{_e(row.get('correct', 0))} of {_e(row.get('answered', 0))}</td></tr>")
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _mocks_html(mocks: dict[str, Any], domains: list[dict[str, Any]], target: float) -> str:
    out = ['<section aria-labelledby="h-mock"><h2 id="h-mock">Mock exams</h2>']
    finished = mocks.get("finished") or []
    if mocks.get("active"):
        out.append("<p>A mock exam is open right now; finish it to see its score here.</p>")
    if not finished:
        out.append("<p>No finished mock exams yet. A mock is a timed practice exam with questions spread like the "
                   "real one.</p></section>")
        return "".join(out)
    out.append(f"<p>Finished: {_e(mocks.get('finished_count', 0))}. Best score {_e(_fmt_pct(mocks.get('best_pct')))}, "
               f"latest {_e(_fmt_pct(mocks.get('latest_pct')))}. Goal {_e(f'{target:g}')}%.</p>")
    names = [d["name"] for d in domains if d.get("in_blueprint")]
    numbered = {d["name"]: d.get("id") for d in domains}
    out.append('<div class="scroll"><table><thead><tr><th>Finished</th><th class="n">Score</th>')
    for name in names:
        out.append(f'<th class="n" title="{_e(name)}">{_e(numbered.get(name) or name)}</th>')
    out.append("</tr></thead><tbody>")
    for m in finished:
        out.append(f"<tr><td>{_e(str(m.get('finished_at') or '')[:10])}</td>"
                   f"<td class=\"n\">{_e(_fmt_pct(m.get('score_pct')))} "
                   f"<span class=\"muted\">({_e(m.get('correct', 0))}/{_e(m.get('questions', 0))})</span></td>")
        for name in names:
            cell = (m.get("domains") or {}).get(name)
            out.append(f"<td class=\"n\">{_e(_fmt_pct(cell.get('pct')) if cell else '-')}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    out.append('<p class="muted small">Numbered columns are the exam domains listed above; "-" means that mock '
               "had no questions from the domain.</p></section>")
    return "".join(out)


def _coverage_html(coverage: dict[str, Any]) -> str:
    out = ['<section aria-labelledby="h-cov"><h2 id="h-cov">Question bank coverage</h2>',
           f"<p>You have tried {_e(coverage.get('answered', 0))} of the {_e(coverage.get('total', 0))} practice "
           "questions at least once.</p>",
           f'<div class="barrow">{_bar(coverage.get("pct"), title="all questions")}'
           f'<span class="lab">{_e(_fmt_pct(coverage.get("pct")))}</span></div>']
    out.append('<div class="scroll"><table><thead><tr><th>Domain</th><th class="n">Tried</th>'
               '<th style="width:40%">Progress</th></tr></thead><tbody>')
    for row in coverage.get("by_domain") or []:
        out.append(f"<tr><td>{_e(row.get('name'))}</td><td class=\"n\">{_e(row.get('answered', 0))} / "
                   f"{_e(row.get('total', 0))}</td><td><div class=\"barrow\">"
                   f"{_bar(row.get('pct'), title=str(row.get('name')))}</div></td></tr>")
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _bar(value: Any, *, target: float | None = None, scale: float = 100.0, light: bool = False,
         title: str | None = None) -> str:
    """A horizontal inline-SVG bar from 0 to ``scale``, with an optional goal tick. No xmlns needed inline."""
    number = _num(value)
    fill = 0.0 if number is None else max(0.0, min(100.0, 100.0 * number / scale))
    width = round(fill * 2, 1)
    label = title if title is not None else ("no data" if number is None else _fmt_pct(number))
    parts = [f'<svg viewBox="0 0 200 14" preserveAspectRatio="none" role="img" aria-label="{_e(label)}">',
             f"<title>{_e(label)}</title>",
             '<rect x="0" y="2" width="200" height="10" rx="2" fill="var(--track)"/>']
    if width > 0:
        parts.append(f'<rect x="0" y="2" width="{width}" height="10" rx="2" '
                     f'fill="{"var(--bar2)" if light else "var(--bar)"}"/>')
    if target is not None:
        x = round(max(0.0, min(100.0, target)) * 2, 1)
        parts.append(f'<rect x="{max(0.0, x - 1)}" y="0" width="2" height="14" fill="var(--mark)"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- writing


def default_dashboard_path() -> Path:
    """``<exam-prep dir>/dashboard.html`` with the same folder rules as progress.json. Creates nothing."""
    return default_progress_path().parent / FILE_NAME


def write_dashboard(progress_store: Any, path: str | os.PathLike[str] | None = None, **kw: Any) -> dict[str, Any]:
    """Build the dashboard and save it atomically (temp file + ``os.replace``); returns ``{ok, path, bytes}``.

    The file must be an ``.html``/``.htm`` file directly inside the exam-prep folder or the progress store's own
    folder; any other path is refused (``ok`` False, nothing written). ``kw`` goes to ``dashboard_data``.
    """
    target, problem = _resolve_target(progress_store, path)
    if problem:
        return {"ok": False, "path": str(target) if target else None, "bytes": 0, "message": problem}
    assert target is not None
    page = render_dashboard_html(dashboard_data(progress_store, **kw)).encode("utf-8")
    try:
        _atomic_write(target, page)
    except OSError as exc:
        return {"ok": False, "path": str(target), "bytes": 0, "message": f"could not write the dashboard: {_cap(exc)}"}
    return {"ok": True, "path": str(target), "bytes": len(page)}


def _allowed_dirs(progress_store: Any) -> list[Path]:
    dirs = [default_progress_path().parent]
    store_path = getattr(progress_store, "path", None)
    if store_path and not getattr(progress_store, "memory_only", False):
        dirs.append(Path(store_path).expanduser().parent)
    return [_resolve(d) for d in dirs]


def _resolve_target(progress_store: Any, path: Any) -> tuple[Path | None, str | None]:
    allowed = _allowed_dirs(progress_store)
    if path is None:
        candidate = allowed[0] / FILE_NAME
    else:
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else allowed[0] / raw
    target = _resolve(candidate)
    if target.suffix.lower() not in HTML_SUFFIXES:
        return target, "the dashboard file name must end in .html or .htm"
    if target.parent not in allowed:
        return target, ("the dashboard can only be written to the exam-prep folder or the progress folder: "
                        + ", ".join(str(d) for d in dict.fromkeys(allowed)))
    if candidate.is_symlink() or target.is_dir():
        return target, "the dashboard path is a folder or a link; choose a plain .html file name"
    return target, None


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(path))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(payload)
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


# ---------------------------------------------------------------- helpers


def _blueprint_data(blueprint: Any) -> dict[str, Any]:
    if blueprint is None:
        from . import blueprint as blueprint_module

        blueprint = blueprint_module
    if isinstance(blueprint, dict):
        return blueprint
    for name in ("load_blueprint", "blueprint"):
        fn = getattr(blueprint, name, None)
        if callable(fn):
            data = fn()
            if isinstance(data, dict) and "domains" in data:
                return data
    raise TypeError("blueprint must be the blueprint module or a dict with 'domains'")


def _bank_items(bank: Any) -> list[dict[str, Any]]:
    if bank is None:
        from . import bank as bank_module

        bank = bank_module
    try:
        if isinstance(bank, (list, tuple)):
            items = list(bank)
        else:
            loader = getattr(bank, "load_bank", None)
            items = list(loader()) if callable(loader) else []
    except (OSError, ValueError, TypeError):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def _cards(store: Any) -> set[str]:
    """Ids of every question answered at least once (the store's cards; recent history as a fallback)."""
    loader = getattr(store, "_load", None)
    if callable(loader):
        with contextlib.suppress(Exception):
            cards = loader().get("cards")
            if isinstance(cards, dict):
                return {str(key) for key in cards}
    return {str(row.get("question_id")) for row in store.history(limit=200) if isinstance(row, dict)}


def _status(answered: int, accuracy: float | None, target: float) -> str:
    if not answered or accuracy is None:
        return "untested"
    if answered < MIN_EVIDENCE:
        return "low_evidence"
    if accuracy < target:
        return "weak"
    if accuracy >= min(100.0, target + STRONG_MARGIN):
        return "strong"
    return "on_track"


def _as_date(value: Any) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", value):
        return date.fromisoformat(value.strip())
    raise ValueError(f"today must be an ISO date such as '2026-11-30', got {value!r}")


def _maybe_date(value: Any) -> date | None:
    try:
        return _as_date(value) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, int(number)) if math.isfinite(number) else 0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number:.0f}%" if float(number).is_integer() else f"{number:.1f}%"


def _short(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _cap(value: Any) -> str:
    return _short(value, TEXT_CAP)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


__all__ = ["dashboard_data", "default_dashboard_path", "render_dashboard_html", "write_dashboard"]
