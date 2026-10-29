"""Mistake notebook: wrong answers written out with the right answer and why (read-only)."""
from __future__ import annotations

from omniverse_scene_mcp.exam import bank, notebook
from omniverse_scene_mcp.exam.progress import ProgressStore


def _single_questions():
    return [q for q in bank.load_bank() if q.get("type") == "single" and len(q.get("options") or []) >= 3][:3]


def test_notebook_lists_still_wrong_first_with_your_choice_the_answer_and_why():
    store = ProgressStore(memory_only=True)
    empty = notebook.mistake_notebook(store)
    assert empty["ok"] and not empty["entries"] and "answer some questions" in empty["message"]
    first, second, third = _single_questions()
    right = lambda q: int(q["answer"][0])  # noqa: E731
    wrong = lambda q: next(i for i in range(len(q["options"])) if i != right(q))  # noqa: E731
    for question, choice in ((first, wrong(first)), (first, wrong(first)), (second, wrong(second)),
                             (second, right(second)), (third, right(third))):
        store.record_answer(question["id"], domain=question["domain"], objective_ids=question["objective_ids"],
                            chosen=str(choice), correct=str(right(question)), is_correct=choice == right(question))
    result = notebook.mistake_notebook(store)
    assert result["read_only"] and result["still_wrong"] == 1 and result["fixed"] == 1, result
    top = result["entries"][0]
    assert top["question_id"] == first["id"] and top["status"] == "still_wrong" and top["times_wrong"] == 2
    assert top["you_chose"][0].startswith("ABCDEFGH"[wrong(first)] + ". ")
    assert top["right_answer"][0].startswith("ABCDEFGH"[right(first)] + ". ")
    assert top["why_the_answer_is_right"] and top["why_your_choice_is_wrong"]
    assert result["entries"][1]["status"] == "fixed"
    assert third["id"] not in [e["question_id"] for e in result["entries"]]  # never wrong: not in the notebook
    assert len(notebook.mistake_notebook(store, include_fixed=False)["entries"]) == 1
    assert not notebook.mistake_notebook(store, domain="No Such Domain")["entries"]
    assert not notebook.mistake_notebook(store, limit=0)["ok"]
