"""NCP-OUSD exam blueprint: domains, objectives, readings, and a weighted study plan."""

from __future__ import annotations

import copy
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

BLUEPRINT_PATH = Path(__file__).resolve().parent / "data" / "blueprint.json"
MAX_SCHEDULE_DAYS = 60
MAX_MATCH_LIMIT = 50
DEFAULT_TARGET_PCT = 80.0
MIN_EVIDENCE = 5
BLOCK_MINUTES = 20
MAX_BLOCKS_PER_DAY = 6
NEEDS_PRACTICE = ("untested", "low_evidence")

_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in into is it its my of on or so that the "
    "this to via what when where which why with without you your".split()
)


@lru_cache(maxsize=1)
def load_blueprint() -> dict[str, Any]:
    """Parsed blueprint.json (cached). Treat as read-only; the helpers below return copies."""
    with BLUEPRINT_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _index() -> dict[str, Any]:
    data = load_blueprint()
    objectives: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for dom in data["domains"]:
        for obj in dom["objectives"]:
            objectives[obj["id"]] = (obj, dom)
    shared: dict[str, list[str]] = {}
    for group in data.get("shared_objectives", []):
        for oid in group:
            shared[oid] = [other for other in group if other != oid]
    return {"objectives": objectives, "shared": shared, "high_yield": set(data.get("high_yield", []))}


def _per_objective_pct(dom: dict[str, Any]) -> float:
    return dom["weight"] / max(1, len(dom["objectives"]))


def _estimated_pct(objective_id: str) -> float:
    idx = _index()
    ids = [objective_id, *idx["shared"].get(objective_id, [])]
    return round(sum(_per_objective_pct(idx["objectives"][oid][1]) for oid in ids), 2)


def _find_domain(value: Any) -> dict[str, Any] | None:
    domains = load_blueprint()["domains"]
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return next((d for d in domains if d["id"] == value), None)
    text = re.sub(r"[_\-\s]+", " ", str(value)).strip().casefold()
    if text.isdigit():
        return next((d for d in domains if d["id"] == int(text)), None)
    exact = [d for d in domains if d["name"].casefold() == text]
    if exact:
        return exact[0]
    if len(text) < 3:
        return None
    partial = [d for d in domains if text in d["name"].casefold()]
    return partial[0] if len(partial) == 1 else None


def blueprint(include_objectives: bool = True) -> dict[str, Any]:
    """Exam facts plus all domains; include_objectives=False gives a compact overview."""
    data = copy.deepcopy(load_blueprint())
    for dom in data["domains"]:
        dom["objective_count"] = len(dom["objectives"])
        if not include_objectives:
            dom["objective_ids"] = [obj["id"] for obj in dom.pop("objectives")]
            dom["reading_count"] = len(dom.pop("readings"))
    data["domain_count"] = len(data["domains"])
    data["objective_count"] = len(_index()["objectives"])
    data["distinct_topic_count"] = len(_topics())
    return data


def objective(objective_id: Any) -> dict[str, Any] | None:
    """One objective with its domain, shared ids, high-yield flag and estimated exam share."""
    key = "" if objective_id is None else str(objective_id).strip()
    idx = _index()
    hit = idx["objectives"].get(key)
    if hit is None:
        return None
    obj, dom = hit
    result = copy.deepcopy(obj)
    result.update(
        domain_id=dom["id"],
        domain_name=dom["name"],
        domain_weight=dom["weight"],
        shared_with=list(idx["shared"].get(key, [])),
        high_yield=key in idx["high_yield"],
        estimated_exam_pct=_estimated_pct(key),
    )
    return result


def domain(domain_id_or_name: Any) -> dict[str, Any] | None:
    """A domain by id (1-8) or name; unique partial names like 'debugging' also work."""
    dom = _find_domain(domain_id_or_name)
    if dom is None:
        return None
    result = copy.deepcopy(dom)
    high_yield = _index()["high_yield"]
    result["objective_count"] = len(dom["objectives"])
    result["high_yield_ids"] = [obj["id"] for obj in dom["objectives"] if obj["id"] in high_yield]
    return result


def _singular(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _keyword_score(keyword: str, words: set[str], phrase_text: str, squashed: str) -> float:
    parts = [_singular(t) for t in _tokens(keyword)]
    if not parts:
        return 0.0
    joined = "".join(_tokens(keyword))
    if len(parts) > 1:
        if f" {' '.join(parts)} " in phrase_text:
            return 3.0
        return 2.5 if len(joined) >= 6 and joined in squashed else 0.0
    if parts[0] in words:
        return 2.0
    return 1.5 if len(joined) >= 6 and joined in squashed else 0.0


def match_objectives(text: str, limit: int = 5) -> list[str]:
    """Objective ids ranked by keyword overlap with free text (best first)."""
    raw = _tokens(str(text or ""))
    if not raw:
        return []
    singular = [_singular(t) for t in raw]
    words = set(raw) | set(singular)
    phrase_text = f" {' '.join(singular)} "
    squashed = "".join(raw)
    try:
        limit = max(1, min(int(limit), MAX_MATCH_LIMIT))
    except (TypeError, ValueError):
        limit = 5
    ranked = []
    for order, (oid, (obj, dom)) in enumerate(_index()["objectives"].items()):
        score = sum(_keyword_score(kw, words, phrase_text, squashed) for kw in obj["keywords"])
        text_words = {_singular(w) for w in _tokens(obj["text"]) if len(w) > 3 and w not in _STOPWORDS}
        score += 0.5 * len(text_words & words)
        if score > 0:
            ranked.append((-score, -dom["weight"], order, oid))
    ranked.sort()
    return [oid for *_, oid in ranked[:limit]]


def _as_int(value: Any, default: int | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if math.isfinite(number) else default


def _apportion(weights: list[float], total: int) -> list[int]:
    """Largest-remainder split of an integer total in proportion to weights."""
    weight_sum = sum(weights)
    if total <= 0 or weight_sum <= 0:
        return [0] * len(weights)
    exact = [w * total / weight_sum for w in weights]
    base = [int(x) for x in exact]
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[: total - sum(base)]:
        base[i] += 1
    return base


def _parse_progress(summary: Any, notes: list[str]) -> dict[int, tuple[int, int]] | None:
    if summary is None:
        return None
    raw = summary.get("domains") if isinstance(summary, dict) else None
    if isinstance(raw, list):
        items = [
            (item.get("id") or item.get("name") or item.get("domain"), item)
            for item in raw
            if isinstance(item, dict)
        ]
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        notes.append("progress_summary had no per-domain data; minutes follow exam weights only.")
        return None
    progress: dict[int, tuple[int, int]] = {}
    ignored: list[str] = []
    for key, stats in items:
        dom = _find_domain(key)
        if dom is None or not isinstance(stats, dict):
            ignored.append(str(key))
            continue
        answered = max(0, _as_int(stats.get("answered"), 0) or 0)
        correct = min(answered, max(0, _as_int(stats.get("correct"), 0) or 0))
        prev_answered, prev_correct = progress.get(dom["id"], (0, 0))
        progress[dom["id"]] = (prev_answered + answered, prev_correct + correct)
    if ignored:
        notes.append(f"Ignored unrecognized progress entries: {', '.join(ignored[:5])}.")
    return progress


def _domain_status(answered: int, correct: int, target: float, has_progress: bool) -> tuple[str, float]:
    if not has_progress:
        return "no_data", 1.0
    if answered == 0:
        return "untested", 1.25
    if answered < MIN_EVIDENCE:
        return "low_evidence", 1.15
    accuracy = 100.0 * correct / answered
    if accuracy < target:
        return "weak", min(2.0, 1.0 + (target - accuracy) / 50.0)
    if accuracy >= min(100.0, target + 10.0):
        return "strong", 0.75
    return "on_track", 1.0


@lru_cache(maxsize=1)
def _topics() -> tuple[tuple[str, ...], ...]:
    """Distinct topics: each shared objective group collapses to one entry (first id leads)."""
    idx = _index()
    seen: set[str] = set()
    topics = []
    for oid in idx["objectives"]:
        if oid in seen:
            continue
        ids = (oid, *idx["shared"].get(oid, []))
        seen.update(ids)
        topics.append(ids)
    return tuple(topics)


def _focus(rows: dict[int, dict[str, Any]], weak_objectives: set[str] | None = None) -> list[dict[str, Any]]:
    idx = _index()
    weak_objectives = weak_objectives or set()
    focus = []
    for order, ids in enumerate(_topics()):
        doms = [idx["objectives"][oid][1] for oid in ids]
        statuses = [rows[d["id"]]["status"] for d in doms]
        priority = sum(_per_objective_pct(d) * rows[d["id"]]["boost"] for d in doms)
        # Shared ids are in the same topic, so a weak 6.2 also lifts the 1.8 topic.
        weak_obj = [oid for oid in ids if oid in weak_objectives]
        if weak_obj:
            priority *= 2.0
        high_yield = len(ids) > 1 or ids[0] in idx["high_yield"]
        weak = "weak" in statuses
        unsure = any(s in NEEDS_PRACTICE for s in statuses)
        tier = (
            0 if high_yield and (weak or weak_obj)
            else 1 if high_yield or weak_obj
            else 2 if weak
            else 3 if unsure
            else 4
        )
        reasons = ["high_yield"] if high_yield else []
        reasons += [f"{s}:{d['name']}" for s, d in zip(statuses, doms) if s == "weak" or s in NEEDS_PRACTICE]
        reasons += [f"weak_objective:{oid}" for oid in weak_obj]
        focus.append(
            {
                "id": ids[0],
                "also": list(ids[1:]),
                "text": idx["objectives"][ids[0]][0]["text"],
                "domains": [d["name"] for d in doms],
                "estimated_exam_pct": _estimated_pct(ids[0]),
                "priority": round(priority, 3),
                "reasons": reasons,
                "_sort": (tier, -priority, order),
            }
        )
    focus.sort(key=lambda item: item["_sort"])
    for item in focus:
        del item["_sort"]
    return focus


def _schedule(days: int, mpd: int, focus: list[dict[str, Any]], domain_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    final_day = days if days >= 3 else None
    kinds = []
    for day in range(1, days + 1):
        if day == final_day:
            kinds.append("final_review")
        elif day % 7 == 0:
            kinds.append("review")
        else:
            kinds.append("study")
    block_count = max(1, min(MAX_BLOCKS_PER_DAY, mpd // BLOCK_MINUTES))
    block_lengths = _apportion([1.0] * block_count, mpd)
    total_blocks = kinds.count("study") * block_count

    counts = [0] * len(focus)
    for i in range(min(len(focus), total_blocks)):
        counts[i] = 1
    # Hand out the remaining blocks by each topic's shortfall against its priority share (not by raw priority),
    # so the one-block floor does not leave low-weight domains over-scheduled.
    priority_sum = sum(item["priority"] for item in focus) or 1.0
    extra = _apportion(
        [max(0.0, item["priority"] * total_blocks / priority_sum - c) for item, c in zip(focus, counts)],
        total_blocks - sum(counts),
    )
    counts = [c + e for c, e in zip(counts, extra)]
    sequence = []
    for pass_no in range(1, max(counts, default=0) + 1):
        sequence += [(item, pass_no) for item, count in zip(focus, counts) if count >= pass_no]

    high_yield_ids = [item["id"] for item in focus if "high_yield" in item["reasons"]]
    top_domains = [row["name"] for row in domain_rows[:3]]
    schedule = []
    cursor = 0
    for day, kind in enumerate(kinds, 1):
        if kind == "study":
            blocks = []
            for minutes in block_lengths:
                if cursor >= len(sequence):
                    break
                item, pass_no = sequence[cursor]
                cursor += 1
                blocks.append(
                    {
                        "objective": item["id"],
                        "also": item["also"],
                        "domain": item["domains"][0],
                        "minutes": minutes,
                        "pass": "learn" if pass_no == 1 else "revisit",
                    }
                )
        elif kind == "review":
            practice, revisit = _apportion([2.0, 1.0], mpd)
            blocks = [
                {"activity": "mixed timed practice", "domains": top_domains, "minutes": practice},
                {"activity": "revisit high-yield objectives", "objectives": high_yield_ids[:6], "minutes": revisit},
            ]
        else:
            logistics = min(15, mpd // 3)
            blocks = [
                {"activity": "light review of high-yield objectives", "objectives": high_yield_ids[:8], "minutes": mpd - logistics},
                {"activity": "exam logistics: ID, Certiverse system check, time limit", "minutes": logistics},
            ]
        schedule.append({"day": day, "type": kind, "minutes": sum(b["minutes"] for b in blocks), "blocks": blocks})
    unscheduled = [item["id"] for item, count in zip(focus, counts) if count == 0]
    return schedule, unscheduled


def study_plan(
    progress_summary: dict[str, Any] | None = None,
    days_until_exam: int | None = None,
    minutes_per_day: int = 60,
    *,
    target_pct: float = DEFAULT_TARGET_PCT,
) -> dict[str, Any]:
    """Weight-based study plan, boosted for weak domains, with an optional day-by-day schedule."""
    data = load_blueprint()
    notes: list[str] = []
    mpd = _as_int(minutes_per_day, 60) or 60
    if not 10 <= mpd <= 480:
        notes.append(f"minutes_per_day {mpd} is outside 10-480; clamped.")
        mpd = max(10, min(480, mpd))
    try:
        target = float(target_pct)
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_PCT
    target = max(50.0, min(100.0, target)) if math.isfinite(target) else DEFAULT_TARGET_PCT

    progress = _parse_progress(progress_summary, notes)
    has_progress = progress is not None
    days: int | None = None
    if days_until_exam is not None:
        requested = _as_int(days_until_exam, None)
        if requested is None:
            notes.append("days_until_exam was not a number; no schedule was built.")
        else:
            days = max(1, min(requested, MAX_SCHEDULE_DAYS))
            if requested > MAX_SCHEDULE_DAYS:
                notes.append(
                    f"Exam is {requested} days away; the schedule covers the next {MAX_SCHEDULE_DAYS} days. "
                    "Re-run the plan later with fresh progress."
                )
            elif requested < 1:
                notes.append("days_until_exam was below 1; built a single-day plan.")

    rows: dict[int, dict[str, Any]] = {}
    for dom in data["domains"]:
        answered, correct = (progress or {}).get(dom["id"], (0, 0))
        status, boost = _domain_status(answered, correct, target, has_progress)
        rows[dom["id"]] = {
            "id": dom["id"],
            "name": dom["name"],
            "weight": dom["weight"],
            "status": status,
            "answered": answered,
            "correct": correct,
            "accuracy_pct": round(100.0 * correct / answered, 1) if answered else None,
            "boost": round(boost, 3),
        }
    ordered = list(rows.values())
    raw = [row["weight"] * row["boost"] for row in ordered]
    raw_sum = sum(raw)
    per_day = _apportion(raw, mpd)
    totals = _apportion(raw, mpd * days) if days else [None] * len(ordered)
    for row, value, daily, total in zip(ordered, raw, per_day, totals):
        row["share_pct"] = round(100.0 * value / raw_sum, 1)
        row["minutes_per_day"] = daily
        row["total_minutes"] = total
    domain_rows = sorted(ordered, key=lambda r: (-r["share_pct"], r["id"]))

    # Objective-level weakness from the progress summary (answered/correct counts, not the 0-1 accuracy field).
    weak_objectives: set[str] = set()
    weakest = progress_summary.get("weakest_objectives") if isinstance(progress_summary, dict) else None
    for item in weakest if isinstance(weakest, list) else []:
        if not isinstance(item, dict):
            continue
        answered = max(0, _as_int(item.get("answered"), 0) or 0)
        correct = min(answered, max(0, _as_int(item.get("correct"), 0) or 0))
        oid = str(item.get("objective_id") or "")
        if oid in _index()["objectives"] and answered >= 2 and 100.0 * correct / answered < target:
            weak_objectives.add(oid)

    focus = _focus(rows, weak_objectives)
    plan: dict[str, Any] = {
        "exam": {
            "code": data["exam"]["code"],
            "name": data["exam"]["name"],
            "time_limit_minutes": data["exam"]["time_limit_minutes"],
            "questions": data["exam"]["format"]["questions"],
        },
        "minutes_per_day": mpd,
        "target_accuracy_pct": target,
        "progress_used": has_progress,
        "domains": domain_rows,
        "focus_objectives": focus,
        "days_until_exam": _as_int(days_until_exam, None),
        "scheduled_days": days or 0,
    }
    if days:
        plan["schedule"], plan["unscheduled_objectives"] = _schedule(days, mpd, focus, domain_rows)
        if plan["unscheduled_objectives"]:
            notes.append(
                f"{len(plan['unscheduled_objectives'])} lower-priority topics did not fit; "
                "add days or minutes to cover them."
            )
    notes += [
        "NVIDIA publishes no passing score. target_accuracy_pct is a self-set readiness goal, not an official pass mark.",
        "Minutes follow the official domain weights, boosted for weak or untested domains and trimmed for strong ones; "
        "per-domain daily minutes are averages, while the schedule works in blocks of about 20 minutes.",
        "Focus order: high-yield objectives in weak domains (or weak themselves), other high-yield and weak "
        "objectives, weak-domain objectives, untested domains, then the rest; ties go to the larger estimated exam share.",
        "estimated_exam_pct assumes questions spread evenly over a domain's objectives; it is an estimate, not an NVIDIA figure.",
        data["exam"]["scope_note"],
    ]
    plan["notes"] = notes
    return plan
