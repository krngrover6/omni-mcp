"""Mistake notebook: every exam question you got wrong, written out to revise from (read-only).

Spaced repetition (learning_exam_review) brings wrong questions back on a schedule, and learning_drill_start
(focus='mistakes') re-tests them. This is the third piece: a page you can READ, showing for each mistake the
question, what you chose, the right answer and why. A question stays "still wrong" until your latest answer to it
is right; then it moves to "fixed", which is worth one last look before the exam.
"""

from __future__ import annotations

import string
from typing import Any

from . import bank as exam_bank

HISTORY_LOOKBACK = 200
MAX_LIMIT = 50


def _option_index(choice: Any, options: list[str]) -> int | None:
    text = str(choice).strip()
    if text.isdigit() and int(text) < len(options):
        return int(text)
    if len(text) == 1 and text.upper() in string.ascii_uppercase[:len(options)]:
        return string.ascii_uppercase.index(text.upper())
    for index, option in enumerate(options):
        if text == option:
            return index
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _label(index: int, options: list[str]) -> str:
    return f"{string.ascii_uppercase[index]}. {options[index]}"


def _entry(question: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    options = [str(o) for o in question.get("options") or []]
    explanations = list(question.get("explanations") or [])
    latest = rows[0]
    wrong = [r for r in rows if r.get("is_correct") is not True]
    chosen = [_option_index(c, options) for c in _as_list(wrong[0].get("chosen"))]
    correct = [i for i in (_option_index(a, options) for a in _as_list(question.get("answer"))) if i is not None]
    why_wrong = [explanations[i] for i in chosen if i is not None and i < len(explanations) and i not in correct]
    why_right = [explanations[i] for i in correct if i < len(explanations)]
    return {
        "question_id": question["id"],
        "status": "fixed" if latest.get("is_correct") is True else "still_wrong",
        "domain": question.get("domain"),
        "objective_ids": list(question.get("objective_ids") or []),
        "question": question.get("stem"),
        "you_chose": [(_label(i, options) if i is not None else str(c))
                      for i, c in zip(chosen, _as_list(wrong[0].get("chosen")))] or ["(skipped)"],
        "right_answer": [_label(i, options) for i in correct],
        "why_your_choice_is_wrong": why_wrong,
        "why_the_answer_is_right": why_right,
        "times_wrong": len(wrong),
        "times_answered": len(rows),
        "last_answered": latest.get("answered_at"),
        "read_more": list(question.get("citations") or [])[:2],
    }


def _meaning(built: dict[str, Any]) -> str:
    from .. import session_lessons as sl

    term = built["_term"]
    return f"{term}: {sl._TERMS[term][1]} (the teacher said: {sl.clean_text(built['_sentence'])})"


def mistake_notebook(store: Any = None, include_fixed: bool = True, domain: str | None = None,
                     limit: int = 20) -> dict[str, Any]:
    """Read-only: your exam mistakes to revise from, still-wrong first (most often missed first), then fixed ones.
    Each entry has the question, your choice, the right answer and the explanations. domain filters by exam domain."""
    if store is None:
        from .progress import ProgressStore

        store = ProgressStore()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return {"ok": False, "error": "limit must be a whole number of at least 1."}
    limit = min(limit, MAX_LIMIT)
    rows_by_question: dict[str, list[dict[str, Any]]] = {}
    for row in store.history(limit=HISTORY_LOOKBACK):  # newest first
        if isinstance(row, dict) and row.get("question_id"):
            rows_by_question.setdefault(str(row["question_id"]), []).append(row)
    questions = {str(q["id"]): q for q in exam_bank.load_bank()}
    entries = []
    for qid, rows in rows_by_question.items():
        question = questions.get(qid)
        if question is None and qid.startswith("class:"):  # a class quiz question: rebuilt from its transcript
            from .class_quiz import rebuild

            built = rebuild(qid)
            question = built and {"id": qid, "domain": f"Class session {built['session']}", "stem": built["question"],
                                  "options": [o.split(". ", 1)[1] for o in built["options"]],
                                  "answer": [built["_answer"]], "objective_ids": [],
                                  "explanations": [_meaning(built) if i == built["_answer"] else ""
                                                   for i in range(len(built["options"]))],
                                  "citations": [f"Rewatch the class at {built['rewatch_at']}"]}
        if question is None or all(r.get("is_correct") is True for r in rows):
            continue  # not a bank question (e.g. a practice exercise), or never wrong
        if domain and str(question.get("domain", "")).lower() != domain.lower():
            continue
        entries.append(_entry(question, rows))
    still = sorted((e for e in entries if e["status"] == "still_wrong"), key=lambda e: -e["times_wrong"])
    fixed = [e for e in entries if e["status"] == "fixed"] if include_fixed else []
    shown = (still + fixed)[:limit]
    answered = sum(len(r) for r in rows_by_question.values())
    return {
        "ok": True, "read_only": True,
        "still_wrong": len(still), "fixed": len([e for e in entries if e["status"] == "fixed"]),
        "entries": shown,
        "message": ("No mistakes yet: " + ("answer some questions first (learning_exam_quiz)." if not answered else
                                           "every question you answered was right.")) if not entries else
                   f"{len(still)} still wrong, {len(entries) - len(still)} fixed. Read each 'why', then re-test with "
                   "learning_drill_start(focus='mistakes').",
        "looked_at": f"your last {HISTORY_LOOKBACK} saved answers",
    }
