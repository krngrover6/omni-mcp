"""Five-box Leitner spaced repetition: pure functions on JSON-ready card dicts, times in UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

BOX_INTERVALS_DAYS: tuple[int, ...] = (0, 1, 3, 7, 21)
FIRST_BOX = 1
LAST_BOX = len(BOX_INTERVALS_DAYS)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None = None) -> datetime:
    """Return ``value`` (default: now) as an aware UTC datetime; naive values are read as UTC."""
    if value is None:
        return utc_now()
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: datetime) -> str:
    """UTC timestamp to the second, e.g. ``2026-10-20T09:30:00Z``."""
    return as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    if not isinstance(text, str) or not text:
        raise ValueError(f"not an ISO timestamp: {text!r}")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return as_utc(datetime.fromisoformat(text))


def clamp_box(box: Any) -> int:
    return max(FIRST_BOX, min(LAST_BOX, int(box)))


def interval_days(box: int) -> int:
    return BOX_INTERVALS_DAYS[clamp_box(box) - 1]


def next_box(box: int, is_correct: bool) -> int:
    """Correct moves up one box (capped at the last); wrong goes back to box 1."""
    return min(clamp_box(box) + 1, LAST_BOX) if is_correct else FIRST_BOX


def due_at(box: int, now: datetime | None = None) -> datetime:
    return as_utc(now) + timedelta(days=interval_days(box))


def new_card(now: datetime | None = None) -> dict[str, Any]:
    """An unseen card: box 1, due immediately."""
    return {
        "box": FIRST_BOX,
        "due_at": to_iso(as_utc(now)),
        "last_result": None,
        "last_reviewed_at": None,
        "reviews": 0,
        "times_correct": 0,
        "times_wrong": 0,
    }


def review(card: Mapping[str, Any] | None, is_correct: bool, now: datetime | None = None) -> dict[str, Any]:
    """Return a new card after one answer; the input is not modified and extra keys are kept."""
    moment = as_utc(now)
    updated = dict(card) if card else new_card(moment)
    box = next_box(updated.get("box", FIRST_BOX), is_correct)
    updated.update(
        box=box,
        due_at=to_iso(due_at(box, moment)),
        last_result="correct" if is_correct else "wrong",
        last_reviewed_at=to_iso(moment),
        reviews=int(updated.get("reviews", 0)) + 1,
        times_correct=int(updated.get("times_correct", 0)) + (1 if is_correct else 0),
        times_wrong=int(updated.get("times_wrong", 0)) + (0 if is_correct else 1),
    )
    return updated


def is_due(card: Mapping[str, Any], now: datetime | None = None) -> bool:
    return parse_iso(card["due_at"]) <= as_utc(now)


def due_cards(
    cards: Mapping[str, Mapping[str, Any]], now: datetime | None = None, limit: int | None = None
) -> list[str]:
    """Ids of due cards, most overdue first, then lower box, then id."""
    moment = as_utc(now)
    due = []
    for card_id, card in cards.items():
        when = parse_iso(card["due_at"])
        if when <= moment:
            due.append((when, clamp_box(card.get("box", FIRST_BOX)), card_id))
    due.sort()
    ids = [card_id for _, _, card_id in due]
    return ids if limit is None else ids[: max(0, limit)]
