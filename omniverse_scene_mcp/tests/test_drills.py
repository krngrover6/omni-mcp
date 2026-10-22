"""Timed drills: focuses, deterministic draws, answer formats, timing, progress (source 'drill') and storage."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import bank as exam_bank
from omniverse_scene_mcp.exam import drills
from omniverse_scene_mcp.exam.mock import MockExamStore
from omniverse_scene_mcp.exam.progress import ENV_DIR, ProgressStore

NOW = datetime(2026, 10, 22, 9, 0, 0, tzinfo=timezone.utc)
SECRET_KEYS = {"answer", "explanations", "citations", "verify", "correct_answer"}


def _items(per_domain: int = 6, domains: tuple[str, ...] = ("Composition", "Data Modeling", "Visualization")) -> list[dict]:
    """Synthetic questions; option (index % 4) is right, every 3rd is multi-select with answers 0 and 1."""
    items = []
    for domain in domains:
        info = exam_bank.DOMAINS[domain]
        for index in range(1, per_domain + 1):
            multi = index % 3 == 0
            items.append(
                {
                    "id": f"{info['prefix']}-{index:03d}",
                    "domain": domain,
                    "objective_ids": [f"{info['number']}.{1 if index % 2 else 2}"],
                    "type": "multi" if multi else "single",
                    "difficulty": "easy",
                    "stem": f"Synthetic {domain} question {index}." + (" Select TWO." if multi else ""),
                    "options": [f"{info['prefix']}{index} first", f"{info['prefix']}{index} second",
                                f"{info['prefix']}{index} third", f"{info['prefix']}{index} fourth"],
                    "answer": [0, 1] if multi else [index % 4],
                    "explanations": ["why first", "why second", "why third", "why fourth"],
                    "citations": ["https://openusd.org/release/glossary.html"],
                }
            )
    return items


def _word_items() -> list[dict]:
    """Single-select items whose right option is one word or a number (flash one-word candidates)."""
    words = ["payload", "0.5", "faceVarying", "over", "Sublayer", "instanceable"]
    items = []
    for index, word in enumerate(words, 1):
        items.append(
            {
                "id": f"comp-{index:03d}",
                "domain": "Composition",
                "objective_ids": ["1.4"],
                "type": "single",
                "stem": f"Word question {index}: name the thing.",
                "options": [word, f"other{index}a", f"other{index}b", f"other{index}c"],
                "answer": [0],
                "explanations": [f"{word} is right", "no", "no", "no"],
            }
        )
    return items


def _key(items: list[dict]) -> dict[str, list[int]]:
    return {item["id"]: sorted(item["answer"]) for item in items}


def _right(items: list[dict], qid: str) -> list[str]:
    return [exam_bank.LETTERS[i] for i in _key(items)[qid]]


def _wrong(items: list[dict], qid: str) -> str:
    answer = _key(items)[qid]
    return exam_bank.LETTERS[(answer[0] + 1) % 4] if len(answer) == 1 else "C"


def _mem() -> ProgressStore:
    return ProgressStore(memory_only=True)


def _keys(value, found=None) -> set[str]:
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(key)
            _keys(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _keys(inner, found)
    return found


@contextlib.contextmanager
def _env_dir(folder: str | None):
    saved = os.environ.get(ENV_DIR)
    try:
        if folder is None:
            os.environ.pop(ENV_DIR, None)
        else:
            os.environ[ENV_DIR] = folder
        yield
    finally:
        if saved is None:
            os.environ.pop(ENV_DIR, None)
        else:
            os.environ[ENV_DIR] = saved


def _temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="drill_test_", ignore_cleanup_errors=True)


# ---------------------------------------------------------------- focuses and drawing


def test_list_drill_focuses_names_every_focus():
    result = drills.list_drill_focuses()
    assert result["ok"] is True
    names = [row["name"] for row in result["focuses"]]
    assert names == ["weak", "mistakes", "mixed", "objective", "domain", "flash"]
    needs = {row["name"]: row["needs"] for row in result["focuses"]}
    assert needs["objective"] == "objective" and needs["domain"] == "domain" and needs["weak"] is None
    assert result["defaults"] == {"count": 10, "seconds_per_question": 45}
    json.dumps(result)


def test_mixed_drill_on_the_real_bank_hides_answers_and_sets_the_time_limit():
    result = drills.start_drill(_mem(), focus="mixed", seed=7, now=NOW)
    assert result["ok"] is True and result["drill_id"].startswith("drill-20261022T090000Z-")
    assert result["count"] == 10 and len(result["questions"]) == 10
    assert result["time_limit_s"] == 450 and result["seconds_per_question"] == 45
    assert result["deadline"] == "2026-10-22T09:07:30Z"
    assert not (_keys(result["questions"]) & SECRET_KEYS)
    assert all(q["options"][0].startswith("A. ") for q in result["questions"])
    assert [q["number"] for q in result["questions"]] == list(range(1, 11))
    assert len({q["id"] for q in result["questions"]}) == 10
    assert result["focus_explained"] and result["storage"]["memory_only"] is True
    json.dumps(result)


def test_same_seed_gives_the_same_drill_and_another_seed_differs():
    for focus, extra in (("mixed", {}), ("objective", {"objective": "1.6"}), ("domain", {"domain": "dm"}),
                         ("flash", {}), ("weak", {})):
        first = drills.start_drill(_mem(), focus=focus, seed=11, now=NOW, **extra)
        second = drills.start_drill(_mem(), focus=focus, seed=11, now=NOW, **extra)
        assert first["ok"] and second["ok"], (focus, first, second)
        assert [q["id"] for q in first["questions"]] == [q["id"] for q in second["questions"]], focus
        if focus == "flash":
            assert [q["prompt"] for q in first["questions"]] == [q["prompt"] for q in second["questions"]]
    a = drills.start_drill(_mem(), focus="mixed", seed=1, now=NOW)
    b = drills.start_drill(_mem(), focus="mixed", seed=2, now=NOW)
    assert [q["id"] for q in a["questions"]] != [q["id"] for q in b["questions"]]


def test_objective_focus_only_serves_that_objective():
    result = drills.start_drill(_mem(), focus="objective", objective="1.6", count=30, seed=3, now=NOW)
    assert result["ok"] is True
    expected = {item["id"] for item in exam_bank.load_bank() if "1.6" in item["objective_ids"]}
    assert {q["id"] for q in result["questions"]} <= expected
    assert result["count"] == min(30, len(expected))
    assert all("1.6" in q["objective_ids"] for q in result["questions"])


def test_domain_focus_accepts_name_prefix_or_number():
    for value in ("Data Modeling", "dm", 5, "data mod"):
        result = drills.start_drill(_mem(), focus="domain", domain=value, seed=4, now=NOW)
        assert result["ok"] is True, value
        assert result["filters"] == {"domain": "Data Modeling"}
        assert {q["domain"] for q in result["questions"]} == {"Data Modeling"}


def test_objective_and_domain_focus_need_their_argument_and_bad_values_fail():
    store = _mem()
    for kwargs in ({"focus": "objective"}, {"focus": "domain"}, {"focus": "objective", "objective": "9.9"},
                   {"focus": "domain", "domain": "Cooking"}, {"focus": "speedrun"}, {"focus": None},
                   {"focus": "mixed", "count": "ten"}, {"focus": "mixed", "seed": "x"},
                   {"focus": "mixed", "seconds_per_question": True}):
        result = drills.start_drill(store, now=NOW, **kwargs)
        assert result["ok"] is False and result["error"], kwargs
    assert drills.start_drill(object(), focus="mixed")["ok"] is False


def test_count_and_seconds_are_clamped_with_notes():
    result = drills.start_drill(_mem(), focus="mixed", count=500, seconds_per_question=1, seed=5, now=NOW)
    assert result["ok"] is True
    assert result["count"] == drills.MAX_COUNT and result["seconds_per_question"] == drills.MIN_SECONDS
    assert result["time_limit_s"] == drills.MAX_COUNT * drills.MIN_SECONDS
    assert any("clamped" in note for note in result["notes"])


def test_small_bank_gives_a_shorter_drill_with_a_note():
    items = _items(per_domain=2, domains=("Composition",))
    result = drills.start_drill(_mem(), focus="mixed", bank=items, count=10, seed=1, now=NOW)
    assert result["ok"] is True and result["count"] == 2 and result["time_limit_s"] == 90
    assert any("Only 2 of the 10" in note for note in result["notes"])


def test_empty_bank_and_fully_filtered_bank_fail_cleanly():
    empty = drills.start_drill(_mem(), focus="mixed", bank=[], now=NOW)
    assert empty["ok"] is False and "empty" in empty["error"]
    items = _items(domains=("Composition",))
    none_left = drills.start_drill(_mem(), focus="domain", domain="Visualization", bank=items, now=NOW)
    assert none_left["ok"] is False and "no questions match" in none_left["error"]
    excluded = drills.start_drill(_mem(), focus="mixed", bank=items, exclude_ids=[i["id"] for i in items], now=NOW)
    assert excluded["ok"] is False
    flash = drills.start_drill(_mem(), focus="flash", bank=[], now=NOW)
    assert flash["ok"] is False


# ---------------------------------------------------------------- grading


def test_grading_accepts_letters_indices_and_option_text():
    items = _items(per_domain=6, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=6, seed=2, now=NOW)
    by_id = {item["id"]: item for item in items}
    forms = []
    answers = {}
    for n, question in enumerate(started["questions"]):
        item = by_id[question["id"]]
        right = sorted(item["answer"])
        letter = exam_bank.LETTERS[right[0]]
        text = item["options"][right[0]]
        if item["type"] == "single":
            choice = [letter.lower(), right[0], str(right[0]), f"{letter}.", f"{letter}. {text}", text.upper(),
                      f"({letter})"][n % 7]
        else:
            choice = ["A,B", "ab", ["a", 1], [item["options"][0], item["options"][1]], {0, 1}, "0 1"][n % 6]
        forms.append(choice)
        answers[question["id"]] = choice
    graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=30, store=store, now=NOW)
    assert graded["ok"] is True, graded
    assert graded["correct"] == graded["total"] == 6 and graded["score"] == 100.0, forms
    assert all(row["right"] for row in graded["per_question"])
    assert graded["encouragement"].startswith("All 6 right.")
    json.dumps(graded)


def test_wrong_answers_show_correct_letters_and_explanations():
    items = _items(per_domain=3, domains=("Visualization",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=3, seed=9, now=NOW)
    answers = {q["id"]: _wrong(items, q["id"]) for q in started["questions"]}
    graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=10, store=store, now=NOW)
    assert graded["ok"] and graded["correct"] == 0 and graded["score"] == 0.0
    for row in graded["per_question"]:
        assert row["right"] is False and row["answered"] is True
        assert row["correct_answer"] == _right(items, row["id"])
        assert row["your_answer"] == [_wrong(items, row["id"])]
        assert row["explanation"].startswith("Correct: ") and "You chose" in row["explanation"]
        assert row["objective"] == row["objective_ids"][0]
    assert graded["weak_spots"] and graded["weak_spots"][0]["missed"] >= 1
    assert graded["next_drill_suggestion"]["focus"] == "mistakes"
    assert "tough round" in graded["encouragement"]


def test_digits_mean_indices_even_when_an_option_text_is_a_number():
    items = [{
        "id": "comp-001", "domain": "Composition", "objective_ids": ["1.6"], "type": "single",
        "stem": "What is the composed value?", "options": ["3", "4", "1", "0"], "answer": [0],
    }]
    for choice, right in (("0", True), (0, True), ("3", False), ("A", True), ("A. 3", True)):
        store = _mem()
        started = drills.start_drill(store, focus="mixed", bank=items, seed=1, now=NOW)
        graded = drills.answer_drill(started["drill_id"], {"comp-001": choice}, elapsed_s=1, store=store, now=NOW)
        assert graded["ok"] and graded["per_question"][0]["right"] is right, choice


def test_unreadable_answers_change_nothing_and_the_drill_stays_open():
    items = _items(per_domain=3, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=3, seed=4, now=NOW)
    ids = [q["id"] for q in started["questions"]]
    for bad in ("Z", "banana", 7, True, 2.5, {"nested": 1}, "B. comp1 third"):
        result = drills.answer_drill(started["drill_id"], {ids[0]: bad}, store=store, now=NOW)
        assert result["ok"] is False and result["problems"], bad
    assert drills.answer_drill(started["drill_id"], {"zzz-999": "A"}, store=store, now=NOW)["ok"] is False
    assert drills.answer_drill(started["drill_id"], ["A"] * 4, store=store, now=NOW)["ok"] is False
    assert drills.answer_drill(started["drill_id"], "A", store=store, now=NOW)["ok"] is False
    assert store.history() == []
    assert drills.drill_status(started["drill_id"], store=store, now=NOW)["status"] == "active"
    good = drills.answer_drill(started["drill_id"], {q: _right(items, q) for q in ids}, store=store, now=NOW)
    assert good["ok"] is True and good["correct"] == 3


def test_letter_that_disagrees_with_its_option_text_is_rejected():
    items = _items(per_domain=1, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, seed=1, now=NOW)
    result = drills.answer_drill(started["drill_id"], {"comp-001": "A. comp1 second"}, store=store, now=NOW)
    assert result["ok"] is False and "does not match" in result["problems"][0]


def test_two_choices_on_a_single_select_question_is_a_format_problem():
    items = _items(per_domain=1, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, seed=1, now=NOW)
    result = drills.answer_drill(started["drill_id"], {"comp-001": "A,B"}, store=store, now=NOW)
    assert result["ok"] is False and "one answer" in result["problems"][0]


def test_list_answers_number_keys_and_skips():
    items = _items(per_domain=3, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=3, seed=6, now=NOW)
    ids = [q["id"] for q in started["questions"]]
    graded = drills.answer_drill(started["drill_id"], [_right(items, ids[0]), None], elapsed_s=20, store=store, now=NOW)
    assert graded["ok"] and graded["correct"] == 1 and graded["answered"] == 1 and graded["skipped"] == 2
    assert graded["per_question"][1]["answered"] is False and graded["per_question"][1]["your_answer"] == []
    assert any("skipped" in note for note in graded["notes"])
    store2 = _mem()
    again = drills.start_drill(store2, focus="mixed", bank=items, count=3, seed=6, now=NOW)
    by_number = {1: _right(items, ids[0]), "2": _right(items, ids[1]), ids[2]: _right(items, ids[2])}
    graded2 = drills.answer_drill(again["drill_id"], by_number, elapsed_s=20, store=store2, now=NOW)
    assert graded2["ok"] and graded2["correct"] == 3
    dup = drills.start_drill(_mem(), focus="mixed", bank=items, count=3, seed=6, now=NOW)
    twice = drills.answer_drill(dup["drill_id"], {1: "A", ids[0]: "B"}, now=NOW)
    assert twice["ok"] is False and "twice" in twice["error"]


# ---------------------------------------------------------------- progress and focuses that read it


def test_answers_are_saved_to_progress_with_source_drill():
    items = _items(per_domain=4, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=4, seed=8, now=NOW)
    ids = [q["id"] for q in started["questions"]]
    answers = {ids[0]: _right(items, ids[0]), ids[1]: _wrong(items, ids[1]), ids[2]: _right(items, ids[2])}
    graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=60, store=store, now=NOW)
    assert graded["progress"]["recorded"] == 3 and graded["progress"]["source"] == "drill"
    assert graded["progress"]["seconds_each"] == 20.0
    history = store.history()
    assert len(history) == 3 and {row["source"] for row in history} == {"drill"}
    assert {row["question_id"]: row["is_correct"] for row in history} == {ids[0]: True, ids[1]: False, ids[2]: True}
    assert all(row["seconds"] == 20.0 for row in history)
    summary = store.summary(now=NOW)
    assert summary["totals"]["answered"] == 3 and summary["totals"]["correct"] == 2


def test_mistakes_focus_picks_recent_wrong_answers_newest_first():
    items = _items(per_domain=6, domains=("Composition",))
    store = _mem()
    for minute, (qid, ok) in enumerate([("comp-001", False), ("comp-002", True), ("comp-003", False),
                                        ("comp-004", False), ("comp-004", True), ("comp-005", False)]):
        item = next(i for i in items if i["id"] == qid)
        store.record_answer(qid, domain=item["domain"], objective_ids=item["objective_ids"], chosen=[0],
                            correct=item["answer"], is_correct=ok, now=NOW + timedelta(minutes=minute))
    result = drills.start_drill(store, focus="mistakes", bank=items, seed=1, now=NOW + timedelta(hours=1))
    assert result["ok"] is True
    assert [q["id"] for q in result["questions"]] == ["comp-005", "comp-003", "comp-001", "comp-004"]
    assert "3 question(s) whose latest saved answer was wrong" in result["focus_explained"]
    assert "plus 1" in result["focus_explained"]
    short = drills.start_drill(store, focus="mistakes", bank=items, count=2, seed=1, now=NOW + timedelta(hours=1))
    assert [q["id"] for q in short["questions"]] == ["comp-005", "comp-003"]


def test_mistakes_focus_without_wrong_answers_explains_what_to_do():
    result = drills.start_drill(_mem(), focus="mistakes", bank=_items(), now=NOW)
    assert result["ok"] is False and "focus='weak'" in result["error"]


def test_weak_focus_without_progress_is_a_weighted_mixed_drill():
    result = drills.start_drill(_mem(), focus="weak", seed=3, now=NOW)
    assert result["ok"] is True and result["count"] == 10
    assert "No saved answers yet" in result["focus_explained"]


def test_weak_focus_targets_wrong_due_and_weak_objectives():
    items = _items(per_domain=6)
    store = _mem()
    # Visualization 8.1 is weak (wrong), Composition answered right, Data Modeling never answered.
    for qid, ok in (("vis-001", False), ("vis-003", False), ("comp-001", True), ("comp-002", True)):
        item = next(i for i in items if i["id"] == qid)
        when = NOW - (timedelta(days=2) if not ok else timedelta(hours=1))  # right answers are not due yet
        store.record_answer(qid, domain=item["domain"], objective_ids=item["objective_ids"], chosen=[0],
                            correct=item["answer"], is_correct=ok, now=when)
    result = drills.start_drill(store, focus="weak", bank=items, count=4, seed=5, now=NOW)
    assert result["ok"] is True
    ids = [q["id"] for q in result["questions"]]
    assert {"vis-001", "vis-003"} <= set(ids)
    assert "comp-001" not in ids and "comp-002" not in ids
    assert all(q["domain"] == "Visualization" for q in result["questions"])
    assert "2 you got wrong last time" in result["focus_explained"]
    big = drills.start_drill(store, focus="weak", bank=items, count=12, seed=5, now=NOW)
    assert big["count"] == 12 and "unseen question(s) were added" in big["focus_explained"]


def test_weak_and_objective_focus_put_due_reviews_first():
    items = _items(per_domain=6, domains=("Composition",))
    store = _mem()
    item = next(i for i in items if i["id"] == "comp-005")
    store.record_answer("comp-005", domain="Composition", objective_ids=item["objective_ids"], chosen=[0],
                        correct=item["answer"], is_correct=False, now=NOW - timedelta(days=3))
    result = drills.start_drill(store, focus="objective", objective="1.1", bank=items, count=1, seed=2, now=NOW)
    assert [q["id"] for q in result["questions"]] == ["comp-005"]


# ---------------------------------------------------------------- timing, status and bad ids


def test_too_slow_flag_uses_elapsed_or_the_clock():
    items = _items(per_domain=2, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=2, seconds_per_question=30, seed=1, now=NOW)
    assert started["time_limit_s"] == 60
    answers = {q["id"]: _right(items, q["id"]) for q in started["questions"]}
    slow = drills.answer_drill(started["drill_id"], answers, elapsed_s=61, store=store, now=NOW)
    assert slow["too_slow"] is True and "time_note" in slow and "Time ran over" in slow["encouragement"]
    assert slow["next_drill_suggestion"]["focus"] == "mixed" and slow["next_drill_suggestion"]["seconds_per_question"] == 30

    store2 = _mem()
    again = drills.start_drill(store2, focus="mixed", bank=items, count=2, seconds_per_question=30, seed=1, now=NOW)
    fast = drills.answer_drill(again["drill_id"], answers, store=store2, now=NOW + timedelta(seconds=40))
    assert fast["too_slow"] is False and fast["elapsed_s"] == 40.0 and "time_note" not in fast
    late = drills.start_drill(_mem(), focus="mixed", bank=items, count=2, seconds_per_question=30, seed=1, now=NOW)
    clock = drills.answer_drill(late["drill_id"], answers, now=NOW + timedelta(seconds=90))
    assert clock["too_slow"] is True and clock["elapsed_s"] == 90.0
    for bad in (-1, True, float("nan"), "fast"):
        fresh = drills.start_drill(_mem(), focus="mixed", bank=items, count=2, seed=1, now=NOW)
        assert drills.answer_drill(fresh["drill_id"], answers, elapsed_s=bad, now=NOW)["ok"] is False, bad


def test_drill_status_while_open_and_after_answering():
    items = _items(per_domain=3, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="mixed", bank=items, count=3, seed=1, now=NOW)
    status = drills.drill_status(started["drill_id"], now=NOW + timedelta(seconds=100))
    assert status["ok"] and status["status"] == "active"
    assert status["elapsed_s"] == 100 and status["remaining_s"] == 35 and status["over_time"] is False
    assert [q["id"] for q in status["questions"]] == [q["id"] for q in started["questions"]]
    assert not (_keys(status["questions"]) & SECRET_KEYS)
    over = drills.drill_status(started["drill_id"], now=NOW + timedelta(seconds=200))
    assert over["over_time"] is True and "Time is up" in over["message"]
    answers = {q["id"]: _right(items, q["id"]) for q in started["questions"]}
    drills.answer_drill(started["drill_id"], answers, elapsed_s=50, store=store, now=NOW)
    done = drills.drill_status(started["drill_id"], store=store)
    assert done["status"] == "finished" and done["result"]["score"] == 100.0
    assert done["result"]["progress_recorded"] == 3 and "questions" not in done
    again = drills.answer_drill(started["drill_id"], answers, store=store, now=NOW)
    assert again["ok"] is False and "already answered" in again["error"]
    assert len(store.history()) == 3


def test_bad_drill_ids_return_errors():
    for bad in ("nope", "", None, 42, "drill-20260101T000000Z-000000"):
        assert drills.answer_drill(bad, {}, store=_mem())["ok"] is False, bad
        assert drills.drill_status(bad, store=_mem())["ok"] is False, bad
    with _temp_dir() as tmp:
        store = ProgressStore(Path(tmp) / "progress.json")
        result = drills.drill_status("drill-nothing", store=store)
        assert result["ok"] is False and "unknown drill id" in result["error"]
        assert list(Path(tmp).iterdir()) == []


# ---------------------------------------------------------------- flash


def _flash_truth(items: list[dict], prompt: dict) -> bool:
    item = next(i for i in items if i["id"] == prompt["question_id"])
    shown = prompt["prompt"].rsplit("\n", 1)[1].split("? ", 1)[-1].replace("Proposed answer: ", "", 1)
    return item["options"].index(shown) in item["answer"]


def test_flash_true_false_prompts_come_from_bank_items_and_are_not_saved():
    items = _items(per_domain=4)
    store = _mem()
    started = drills.start_drill(store, focus="flash", bank=items, count=8, seed=12, now=NOW)
    assert started["ok"] is True and started["count"] == 8
    assert not (_keys(started["questions"]) & SECRET_KEYS)
    bank_ids = {item["id"] for item in items}
    answers = {}
    for prompt in started["questions"]:
        assert prompt["kind"] == "true_false" and prompt["question_id"] in bank_ids
        assert prompt["options"] == ["A. True", "B. False"]
        truth = _flash_truth(items, prompt)
        answers[prompt["id"]] = [truth, "true" if truth else "false", "A" if truth else "b",
                                 "Yes" if truth else "no"][prompt["number"] % 4]
    graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=40, store=store, now=NOW)
    assert graded["ok"] is True and graded["correct"] == graded["total"] == 8
    assert graded["progress"]["recorded"] == 0 and store.history() == []
    assert {row["kind"] for row in graded["per_question"]} == {"true_false"}
    assert all(row["correct_answer"] in ("True", "False") for row in graded["per_question"])


def test_flash_rejects_numbers_and_grades_wrong_truth_values():
    items = _items(per_domain=3, domains=("Composition",))
    store = _mem()
    started = drills.start_drill(store, focus="flash", bank=items, count=3, seed=4, now=NOW)
    first = started["questions"][0]
    bad = drills.answer_drill(started["drill_id"], {first["id"]: 1}, store=store, now=NOW)
    assert bad["ok"] is False and "Numbers are not accepted" in bad["problems"][0]
    answers = {p["id"]: not _flash_truth(items, p) for p in started["questions"]}
    graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=5, store=store, now=NOW)
    assert graded["correct"] == 0 and graded["weak_spots"]
    assert graded["next_drill_suggestion"]["focus"] == "objective"


def test_flash_one_word_prompts_accept_the_word_case_insensitively_and_numbers_by_value():
    items = _word_items()
    seen_word = False
    for seed in range(40):
        store = _mem()
        started = drills.start_drill(store, focus="flash", bank=items, count=6, seed=seed, now=NOW)
        words = [p for p in started["questions"] if p["kind"] == "one_word"]
        if not words:
            continue
        seen_word = True
        answers = {}
        for prompt in started["questions"]:
            item = next(i for i in items if i["id"] == prompt["question_id"])
            if prompt["kind"] == "one_word":
                word = item["options"][0]
                assert "options" not in prompt and prompt["hint"].startswith(f"{len(word)} characters")
                answers[prompt["id"]] = "0.50" if word == "0.5" else f"  `{word.upper()}`. "
            else:
                answers[prompt["question_id"]] = _flash_truth(items, prompt)
        graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=12, store=store, now=NOW)
        assert graded["ok"] is True and graded["correct"] == 6, graded["per_question"]
        break
    assert seen_word


def test_flash_honours_objective_and_domain_filters():
    result = drills.start_drill(_mem(), focus="flash", domain="vis", count=5, seed=1, now=NOW)
    assert result["ok"] and {q["domain"] for q in result["questions"]} == {"Visualization"}
    result = drills.start_drill(_mem(), focus="flash", objective="1.6", count=5, seed=1, now=NOW)
    assert result["ok"] and all("1.6" in q["objective_ids"] for q in result["questions"])


# ---------------------------------------------------------------- storage


def test_file_store_keeps_sessions_beside_progress_and_survives_a_restart():
    items = _items(per_domain=3, domains=("Composition",))
    with _temp_dir() as tmp:
        folder = Path(tmp)
        store = ProgressStore(folder / "progress.json")
        started = drills.start_drill(store, focus="mixed", count=3, seed=1, now=NOW)
        assert started["storage"]["path"] == str(folder / drills.FILE_NAME)
        saved = json.loads((folder / drills.FILE_NAME).read_text(encoding="utf-8"))
        assert saved["sessions"][0]["id"] == started["drill_id"]
        assert not (_keys(saved) & {"answer", "explanations", "correct_answer"})
        # A new store object on the same file (a restarted server) finds and grades the drill.
        restarted = ProgressStore(folder / "progress.json")
        key = {item["id"]: item for item in exam_bank.load_bank()}
        answers = {q["id"]: sorted(key[q["id"]]["answer"]) for q in started["questions"]}
        graded = drills.answer_drill(started["drill_id"], answers, elapsed_s=30, store=restarted, now=NOW)
        assert graded["ok"] and graded["correct"] == 3 and graded["progress"]["persisted"] is True
        assert len(restarted.history()) == 3
        saved = json.loads((folder / drills.FILE_NAME).read_text(encoding="utf-8"))
        assert saved["sessions"][0]["status"] == "finished"
        # Custom banks are kept in this process only; after a restart they must be passed again.
        custom = drills.start_drill(store, focus="mixed", bank=items, count=2, seed=1, now=NOW)
        drills._CUSTOM_BANKS.pop(custom["drill_id"], None)
        answers = {q["id"]: _right(items, q["id"]) for q in custom["questions"]}
        assert drills.answer_drill(custom["drill_id"], answers, store=store, now=NOW)["ok"] is False
        assert drills.answer_drill(custom["drill_id"], answers, store=store, bank=items, now=NOW)["correct"] == 2
        assert sorted(p.name for p in folder.iterdir() if not p.name.endswith(".lock")) == [
            "drill_sessions.json", "progress.json"]


def test_corrupt_session_file_is_kept_as_a_backup():
    with _temp_dir() as tmp:
        folder = Path(tmp)
        (folder / drills.FILE_NAME).write_text("{not json", encoding="utf-8")
        result = drills.start_drill(ProgressStore(folder / "progress.json"), focus="mixed", count=2, seed=1, now=NOW)
        assert result["ok"] is True and result["storage"]["recovered_backup"]
        assert Path(result["storage"]["recovered_backup"]).read_text(encoding="utf-8") == "{not json"


def test_open_mock_exam_questions_are_never_drilled():
    items = _items(per_domain=6, domains=("Composition",))
    with _temp_dir() as tmp:
        folder = Path(tmp)
        mock = MockExamStore(folder / "mock_sessions.json", bank=items)
        opened = mock.start(count=3, seed=1, now=NOW, domain_weights={"Composition": 1})
        held = set(opened["question_ids"])
        result = drills.start_drill(ProgressStore(folder / "progress.json"), focus="mixed", bank=items, count=10,
                                    seed=1, now=NOW)
        assert result["ok"] is True
        assert not ({q["id"] for q in result["questions"]} & held)
        assert result["count"] == len(items) - len(held)
        assert any("open mock" in note for note in result["notes"])


def test_default_store_follows_the_exam_prep_env_folder():
    with _temp_dir() as tmp, _env_dir(tmp):
        result = drills.start_drill(focus="mixed", count=2, seed=1, now=NOW)
        assert result["ok"] is True and result["storage"]["path"] == str(Path(tmp) / drills.FILE_NAME)
        assert drills.drill_status(result["drill_id"])["status"] == "active"
        key = {item["id"]: item for item in exam_bank.load_bank()}
        graded = drills.answer_drill(result["drill_id"], {q["id"]: sorted(key[q["id"]]["answer"])
                                                          for q in result["questions"]}, elapsed_s=5, now=NOW)
        assert graded["ok"] and graded["progress"]["path"] == str(Path(tmp) / "progress.json")
        assert (Path(tmp) / "progress.json").exists()


def test_memory_store_writes_nothing_to_disk():
    with _temp_dir() as tmp, _env_dir(tmp):
        store = _mem()
        started = drills.start_drill(store, focus="mixed", count=2, seed=1, now=NOW)
        drills.answer_drill(started["drill_id"], ["A", "A"], elapsed_s=5, store=store, now=NOW)
        assert list(Path(tmp).iterdir()) == []
