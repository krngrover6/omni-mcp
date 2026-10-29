"""Class quiz: blanks from the teacher's own transcript sentences, graded, saved, and shown in the notebook."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from omniverse_scene_mcp.exam import class_quiz as cq, notebook
from omniverse_scene_mcp.exam.progress import ProgressStore

SRT = """1
00:00:01,000 --> 00:00:05,000
A payload can be switched off, so heavy files only load when they are needed.

2
00:00:06,000 --> 00:00:10,000
Every layer is one file, and the stronger one will win here.

3
00:00:11,000 --> 00:00:15,000
The joint is what lets the door swing around its hinge nicely.

4
00:00:16,000 --> 00:00:20,000
Blah Blah Foo Bar Kolitis Digit Body prim stuff Wobble Yes.
"""


def _with_transcript(test):
    folder = tempfile.mkdtemp()
    Path(folder, "Session 9 class.srt").write_text(SRT, encoding="utf-8")
    saved = os.environ.get("OMNIVERSE_CLASS_TRANSCRIPTS")
    os.environ["OMNIVERSE_CLASS_TRANSCRIPTS"] = folder
    try:
        test()
    finally:
        if saved is None:
            os.environ.pop("OMNIVERSE_CLASS_TRANSCRIPTS", None)
        else:
            os.environ["OMNIVERSE_CLASS_TRANSCRIPTS"] = saved


def test_quiz_answer_and_notebook():
    def run():
        quiz = cq.class_quiz(9, count=5, seed=1)
        assert quiz["ok"] and quiz["read_only"], quiz
        stems = " ".join(q["question"] for q in quiz["questions"])
        assert "Kolitis" not in stems  # misheard subtitle lines are skipped
        assert all("____" in q["question"] and len(q["options"]) == 4 for q in quiz["questions"])
        assert cq.class_quiz(9, count=5, seed=1) == quiz  # same seed, same quiz
        question = next(q for q in quiz["questions"] if q["id"].endswith(":payload"))
        right = next(o[0] for o in question["options"] if o.endswith(". payload"))
        wrong = next(o[0] for o in question["options"] if not o.endswith(". payload"))
        store = ProgressStore(memory_only=True)
        miss = cq.class_quiz_answer(question["id"], wrong, store)
        assert miss["ok"] and not miss["correct"] and miss["rewatch_at"] == "00:00:01" and "unload" in miss["meaning"]
        assert cq.class_quiz_answer(question["id"], right, store)["correct"]
        cq.class_quiz_answer(question["id"], wrong, store)
        book = notebook.mistake_notebook(store)
        entry = book["entries"][0]
        assert entry["question_id"] == question["id"] and entry["status"] == "still_wrong", book
        assert entry["right_answer"][0].endswith("payload") and "unload" in entry["why_the_answer_is_right"][0]
        assert not cq.class_quiz_answer("class:9:999:payload", "A", store)["ok"]
        assert not cq.class_quiz_answer(question["id"], "Z", store)["ok"]
        assert not cq.class_quiz(9, count=0)["ok"] and not cq.class_quiz(42)["ok"]
    _with_transcript(run)
