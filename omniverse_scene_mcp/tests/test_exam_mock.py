"""Timed mock exam: weighted draw, answers, flags, deadline, scoring, progress recording and persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import bank as exam_bank
from omniverse_scene_mcp.exam import mock
from omniverse_scene_mcp.exam.mock import ItemBank, MockExamError, MockExamStore
from omniverse_scene_mcp.exam.progress import ProgressStore

NOW = datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)
EXPECTED_65 = {
    "Composition": 15,
    "Data Exchange": 10,
    "Pipeline Development": 9,
    "Data Modeling": 8,
    "Debugging and Troubleshooting": 7,
    "Content Aggregation": 7,
    "Visualization": 5,
    "Customizing USD": 4,
}
SECRET_KEYS = {"answer", "explanations", "citations", "verify", "correct_answer"}


def _items(counts: dict[str, int] | None = None, multi_every: int = 0) -> list[dict]:
    """Synthetic questions: option (index % 4) is right; every ``multi_every``-th is multi-select (answers 0 and 1)."""
    counts = counts if counts is not None else {name: 20 for name in exam_bank.DOMAINS}
    items = []
    for domain, number in counts.items():
        info = exam_bank.DOMAINS[domain]
        for index in range(1, number + 1):
            multi = bool(multi_every) and index % multi_every == 0
            items.append(
                {
                    "id": f"{info['prefix']}-{index:03d}",
                    "domain": domain,
                    "objective_ids": [f"{info['number']}.1"],
                    "type": "multi" if multi else "single",
                    "difficulty": "easy",
                    "stem": f"Synthetic {domain} question {index}." + (" Select TWO." if multi else ""),
                    "options": ["first", "second", "third", "fourth"],
                    "answer": [0, 1] if multi else [index % 4],
                    "explanations": ["why first", "why second", "why third", "why fourth"],
                    "citations": ["https://openusd.org/release/glossary.html"],
                }
            )
    return items


def _key(items: list[dict]) -> dict[str, list[int]]:
    return {item["id"]: item["answer"] for item in items}


def _wrong(answer: list[int]) -> list[int]:
    return [(answer[0] + 1) % 4] if len(answer) == 1 else [2]


def _temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="mock_exam_test_", ignore_cleanup_errors=True)


# ---------------------------------------------------------------- apportioning


def test_apportion_65_matches_official_weights():
    counts = mock.apportion(mock.official_weights(), 65)
    assert counts == EXPECTED_65
    assert sum(counts.values()) == 65


def test_apportion_sums_and_edge_cases():
    weights = mock.official_weights()
    for total in (1, 10, 60, 65, 70, 100):
        assert sum(mock.apportion(weights, total).values()) == total
    assert mock.apportion(weights, 0) == {name: 0 for name in weights}
    assert mock.apportion({"a": 1, "b": 1, "c": 0}, 3) == {"a": 2, "b": 1, "c": 0}  # tie -> earlier
    assert mock.apportion({"a": 0, "b": 0}, 5) == {"a": 0, "b": 0}


def test_allocate_redistributes_shortfall_by_weight():
    weights = {"a": 50, "b": 30, "c": 20, "z": 0}
    allocation, quota = mock.allocate(weights, {"a": 2, "b": 100, "c": 100, "z": 100}, 10)
    assert quota == {"a": 5, "b": 3, "c": 2, "z": 0}
    assert allocation["a"] == 2 and allocation["z"] == 0
    assert sum(allocation.values()) == 10
    assert allocation == {"a": 2, "b": 5, "c": 3, "z": 0}
    allocation, _ = mock.allocate(weights, {"a": 1, "b": 1, "c": 1, "z": 50}, 10)
    assert allocation == {"a": 1, "b": 1, "c": 1, "z": 0}  # zero-weight domains never absorb


# ---------------------------------------------------------------- start


def test_start_draws_weighted_questions_without_answers():
    items = _items()
    store = MockExamStore(memory_only=True)
    session = store.start(seed=1, bank=items, now=NOW)
    assert session["ok"] and session["status"] == "active"
    assert session["count"] == 65 and len(session["question_ids"]) == 65
    assert len(set(session["question_ids"])) == 65
    assert session["allocation"] == {name: EXPECTED_65[name] for name in mock.official_weights()}
    assert session["shortfalls"] == [] and session["short_by"] == 0
    assert session["deadline"] == "2026-10-20T11:00:00Z" and session["time_limit_seconds"] == 7200
    assert session["bank"] == "custom"
    drawn = Counter(question["domain"] for question in session["questions"])
    assert dict(drawn) == {name: count for name, count in EXPECTED_65.items()}
    for number, question in enumerate(session["questions"], 1):
        assert question["number"] == number
        assert not SECRET_KEYS & set(question), question
        assert question["options"][0].startswith("A. ")
    json.dumps(session)


def test_start_is_reproducible_with_seed_and_mixes_domains():
    items = _items()
    first = MockExamStore(memory_only=True).start(seed=42, bank=items, now=NOW, include_questions=False)
    second = MockExamStore(memory_only=True).start(seed=42, bank=list(reversed(items)), now=NOW, include_questions=False)
    other = MockExamStore(memory_only=True).start(seed=43, bank=items, now=NOW, include_questions=False)
    assert first["question_ids"] == second["question_ids"]
    assert first["question_ids"] != other["question_ids"]
    assert "questions" not in first
    domains_in_first_ten = {qid.split("-")[0] for qid in first["question_ids"][:10]}
    assert len(domains_in_first_ten) > 1


def test_start_never_exceeds_bank_and_reports_shortfall():
    counts = {name: 20 for name in exam_bank.DOMAINS}
    counts["Composition"] = 10
    counts["Customizing USD"] = 2
    session = MockExamStore(memory_only=True).start(seed=3, bank=_items(counts), now=NOW)
    allocation = session["allocation"]
    assert allocation["Composition"] == 10 and allocation["Customizing USD"] == 2
    assert session["count"] == 65 and sum(allocation.values()) == 65
    short = {row["domain"]: row for row in session["shortfalls"]}
    assert short["Composition"] == {"domain": "Composition", "wanted": 15, "available": 10, "given": 10}
    assert short["Customizing USD"]["wanted"] == 4
    assert sum(session["redistributed"].values()) == 7
    assert all(allocation[name] <= counts[name] for name in counts)
    assert any("short" in note for note in session["notes"])


def test_start_with_small_bank_gives_fewer_questions():
    counts = {name: 3 for name in exam_bank.DOMAINS}
    session = MockExamStore(memory_only=True).start(seed=1, bank=_items(counts), now=NOW)
    assert session["count"] == 24 and session["short_by"] == 41
    assert all(value == 3 for value in session["allocation"].values())
    assert any("Only 24 of the 65" in note for note in session["notes"])


def test_custom_domain_weights_and_validation():
    store = MockExamStore(memory_only=True)
    session = store.start(count=10, domain_weights={"comp": 1, "Data Exchange": 1}, seed=5, bank=_items(), now=NOW)
    assert session["allocation"]["Composition"] == 5 and session["allocation"]["Data Exchange"] == 5
    assert sum(session["allocation"].values()) == 10
    store.abandon(session["session_id"], now=NOW)
    for bad in ({"Nope": 1}, {"Composition": 0}, {"Composition": -1}, {"c": 1}, {"comp": 1, "Composition": 2}):
        try:
            store.start(count=10, domain_weights=bad, bank=_items(), now=NOW)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    clamped = store.start(count=500, minutes=0, seed=1, bank=_items(), now=NOW, include_questions=False)
    assert clamped["requested_count"] == mock.MAX_COUNT and clamped["minutes"] == mock.MIN_MINUTES
    assert len([note for note in clamped["notes"] if "clamped" in note]) == 2


def test_only_one_open_session_until_abandoned():
    store = MockExamStore(memory_only=True)
    first = store.start(seed=1, bank=_items(), now=NOW, include_questions=False)
    try:
        store.start(seed=2, bank=_items(), now=NOW)
    except MockExamError as exc:
        assert first["session_id"] in str(exc)
    else:
        raise AssertionError("second open session allowed")
    abandoned = store.abandon(first["session_id"], now=NOW + timedelta(minutes=5))
    assert abandoned["status"] == "abandoned"
    second = store.start(seed=2, bank=_items(), now=NOW + timedelta(minutes=6), include_questions=False)
    assert second["session_id"] != first["session_id"]
    assert store.status(first["session_id"], now=NOW)["state"] == "abandoned"
    try:
        store.answer(first["session_id"], first["question_ids"][0], "A", now=NOW)
    except MockExamError:
        pass
    else:
        raise AssertionError("answered an abandoned mock")


# ---------------------------------------------------------------- answer, flag, status


def test_answer_change_clear_and_validation():
    items = _items(multi_every=5)
    key = _key(items)
    store = MockExamStore(memory_only=True)
    session = store.start(seed=9, bank=items, now=NOW, include_questions=False)
    sid = session["session_id"]
    single = next(q for q in session["question_ids"] if len(key[q]) == 1)
    multi = next(q for q in session["question_ids"] if len(key[q]) == 2)

    first = store.answer(sid, single, "b", now=NOW + timedelta(minutes=1))
    assert first["chosen"] == [1] and first["chosen_letters"] == ["B"] and first["answered_count"] == 1
    assert first["remaining_seconds"] == 119 * 60 and not first["changed"]
    changed = store.answer(sid, single, 2, now=NOW + timedelta(minutes=2))
    assert changed["changed"] and changed["chosen"] == [2] and changed["answered_count"] == 1
    cleared = store.answer(sid, single, None, now=NOW + timedelta(minutes=3))
    assert cleared["cleared"] and cleared["answered_count"] == 0

    both = store.answer(sid, multi, "A, B", now=NOW + timedelta(minutes=4))
    assert both["chosen"] == [0, 1] and "note" not in both
    assert store.answer(sid, multi, "AC", now=NOW + timedelta(minutes=4))["chosen"] == [0, 2]
    assert "select 2" in store.answer(sid, multi, ["D"], now=NOW + timedelta(minutes=4))["note"]

    by_number = store.answer(sid, 1, "A", now=NOW + timedelta(minutes=5))
    assert by_number["question_id"] == session["question_ids"][0] and by_number["number"] == 1
    for question, choice in ((single, "Z"), (single, 7), (single, [0, 1]), (single, True), ("comp-999", "A")):
        try:
            store.answer(sid, question, choice, now=NOW + timedelta(minutes=6))
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"accepted {question} {choice!r}")


def test_deadline_rejects_answers_unless_allow_late():
    items = _items()
    store = MockExamStore(memory_only=True)
    session = store.start(count=10, minutes=20, seed=2, bank=items, now=NOW, include_questions=False)
    sid, qid = session["session_id"], session["question_ids"][0]
    late_time = NOW + timedelta(minutes=21)
    try:
        store.answer(sid, qid, "A", now=late_time)
    except MockExamError as exc:
        assert "Time is up" in str(exc)
    else:
        raise AssertionError("late answer accepted")
    status = store.status(sid, now=late_time)
    assert status["state"] == "time_up" and status["remaining_seconds"] == 0 and "message" in status
    late = store.answer(sid, qid, "A", now=late_time, allow_late=True)
    assert late["late"] and late["remaining_seconds"] == 0
    report = store.finish(sid, store=ProgressStore(memory_only=True), now=late_time)
    assert report["score"]["late_answers"] == 1
    assert report["time"]["finished_after_deadline"] and report["time"]["over_time_seconds"] == 60
    assert any("after the deadline" in note for note in report["notes"])


def test_status_remaining_time_counts_and_flags():
    store = MockExamStore(memory_only=True)
    session = store.start(seed=4, bank=_items(), now=NOW, include_questions=False)
    sid, ids = session["session_id"], session["question_ids"]
    store.answer(sid, ids[0], "A", now=NOW + timedelta(minutes=10))
    store.answer(sid, ids[1], "B", now=NOW + timedelta(minutes=12))
    store.flag(sid, ids[5])
    marked = store.flag(sid, ids[2])
    assert marked["flagged_ids"] == [ids[2], ids[5]]  # kept in question order
    assert store.flag(sid, ids[5], flagged=False)["flagged_ids"] == [ids[2]]
    status = store.status(now=NOW + timedelta(minutes=30))  # None -> the open mock
    assert status["session_id"] == sid and status["state"] == "in_progress"
    assert status["remaining_seconds"] == 90 * 60 and status["remaining"] == "1h 30m 00s"
    assert status["elapsed_seconds"] == 30 * 60
    assert status["answered"] == 2 and status["unanswered"] == 63 and status["total"] == 65
    assert status["flagged"] == [ids[2]] and ids[0] not in status["unanswered_ids"]
    assert sum(row["questions"] for row in status["domains"].values()) == 65
    assert store.active(now=NOW)["session_id"] == sid
    json.dumps(status)


# ---------------------------------------------------------------- finish and review


def test_finish_scores_lists_wrong_and_records_progress():
    items = _items()
    key = _key(items)
    store = MockExamStore(memory_only=True)
    session = store.start(seed=11, bank=items, now=NOW, include_questions=False)
    sid, ids = session["session_id"], session["question_ids"]
    right, wrong_ids, skipped = ids[:40], ids[40:55], ids[55:]
    for qid in right:
        store.answer(sid, qid, key[qid], now=NOW + timedelta(minutes=30))
    for qid in wrong_ids:
        store.answer(sid, qid, _wrong(key[qid]), now=NOW + timedelta(minutes=40))
    store.flag(sid, skipped[0])
    progress = ProgressStore(memory_only=True)
    report = store.finish(sid, store=progress, now=NOW + timedelta(minutes=45))

    score = report["score"]
    assert score == {
        "questions": 65, "answered": 55, "unanswered": 10, "correct": 40, "wrong": 25,
        "pct": 61.5, "accuracy_on_answered_pct": 72.7, "late_answers": 0,
    }
    assert report["time"]["used_seconds"] == 45 * 60 and not report["time"]["finished_after_deadline"]
    assert report["time"]["limit_seconds"] == 7200
    assert sum(row["questions"] for row in report["domains"].values()) == 65
    assert sum(row["correct"] for row in report["domains"].values()) == 40
    for name, row in report["domains"].items():
        assert row["questions"] == EXPECTED_65[name]
        assert row["answered"] + row["unanswered"] == row["questions"]

    assert report["wrong_count"] == 25 and {row["question_id"] for row in report["wrong"]} == set(wrong_ids + skipped)
    first_skipped = next(row for row in report["wrong"] if row["question_id"] == skipped[0])
    assert not first_skipped["answered"] and first_skipped["flagged"] and first_skipped["your_answer"] == []
    assert first_skipped["correct_answer"] == [mock.LETTERS[key[skipped[0]][0]]]
    assert first_skipped["correct_options"][0]["why"].startswith("why ")
    assert first_skipped["stem"].startswith("Synthetic")
    answered_wrong = next(row for row in report["wrong"] if row["question_id"] == wrong_ids[0])
    assert answered_wrong["answered"] and len(answered_wrong["your_wrong_choices"]) == 1

    readiness = report["readiness"]
    assert readiness["target_pct"] == 80.0 and readiness["meets_target"] is False
    assert "no passing score" in readiness["note"] and "self-set" in readiness["note"]

    assert report["progress"]["recorded"] == 55 and report["progress"]["not_recorded_unanswered"] == 10
    assert report["progress"]["errors"] == [] and report["progress"]["persisted"] is False
    summary = progress.summary(now=NOW + timedelta(minutes=46))
    assert summary["totals"]["answered"] == 55 and summary["totals"]["correct"] == 40
    history = progress.history(limit=100)
    assert len(history) == 55 and {row["source"] for row in history} == {"mock"}
    assert not {row["question_id"] for row in history} & set(skipped)
    json.dumps(report)

    status = store.status(sid, now=NOW + timedelta(hours=5))
    assert status["state"] == "finished" and status["result"]["score"]["pct"] == 61.5
    assert status["result"]["progress"]["recorded"] == 55
    assert set(status["result"]["wrong_ids"]) == set(wrong_ids + skipped)


def test_finish_twice_rejected_review_rebuilds_without_recording():
    items = _items(multi_every=4)
    key = _key(items)
    store = MockExamStore(memory_only=True)
    session = store.start(count=20, seed=6, bank=items, now=NOW, include_questions=False)
    sid = session["session_id"]
    for qid in session["question_ids"]:
        store.answer(sid, qid, key[qid], now=NOW + timedelta(minutes=5))
    progress = ProgressStore(memory_only=True)
    report = store.finish(sid, store=progress, now=NOW + timedelta(minutes=20))
    assert report["score"]["pct"] == 100.0 and report["wrong"] == []
    assert report["readiness"]["meets_target"] is True and report["readiness"]["domains_below_target"] == []
    try:
        store.finish(sid, store=progress, now=NOW + timedelta(minutes=21))
    except MockExamError as exc:
        assert "finished" in str(exc)
    else:
        raise AssertionError("finished twice")
    review = store.review(sid)
    assert review["score"] == report["score"] and review["progress"]["recorded"] == 20
    assert store.review()["session_id"] == sid  # latest finished mock
    assert progress.summary(now=NOW)["totals"]["answered"] == 20
    rows = store.list_sessions()
    assert rows[0]["session_id"] == sid and rows[0]["score_pct"] == 100.0


def test_ungradable_question_left_out_of_score():
    items = _items({"Composition": 6, "Data Exchange": 4})
    store = MockExamStore(memory_only=True)
    session = store.start(count=10, seed=1, bank=items, now=NOW, include_questions=False)
    missing = session["question_ids"][0]
    smaller = ItemBank([item for item in items if item["id"] != missing])
    report = store.finish(session["session_id"], store=ProgressStore(memory_only=True), bank=smaller, now=NOW)
    assert [row["question_id"] for row in report["ungraded"]] == [missing]
    assert report["score"]["questions"] == 9
    assert any("no longer in the bank" in note for note in report["notes"])


# ---------------------------------------------------------------- persistence


def test_sessions_survive_restart_on_disk():
    items = _items()
    key = _key(items)
    with _temp_dir() as tmp:
        path = Path(tmp) / "prep" / "mock_sessions.json"
        session = MockExamStore(path).start(seed=8, bank=items, now=NOW, include_questions=False)
        sid, ids = session["session_id"], session["question_ids"]
        assert path.exists()
        assert "answer" not in path.read_text(encoding="utf-8").replace('"answers"', "")

        restarted = MockExamStore(path)
        assert restarted.status(now=NOW + timedelta(minutes=1))["session_id"] == sid
        restarted.answer(sid, ids[0], key[ids[0]], now=NOW + timedelta(minutes=2))
        restarted.flag(sid, ids[1])
        again = MockExamStore(path)
        status = again.status(sid, now=NOW + timedelta(minutes=3))
        assert status["answered"] == 1 and status["flagged"] == [ids[1]]
        try:
            again.start(seed=1, bank=items, now=NOW)
        except MockExamError:
            pass
        else:
            raise AssertionError("open session lost after restart")
        try:
            again.finish(sid, store=ProgressStore(memory_only=True), now=NOW + timedelta(minutes=4))
        except MockExamError as exc:
            assert "custom question bank" in str(exc)
        else:
            raise AssertionError("graded a custom-bank mock without its bank")
        assert again.status(sid, now=NOW)["status"] == "active"  # failed finish changed nothing
        shown = again.questions(sid, bank=items, offset=0, limit=2)
        assert [row["id"] for row in shown["questions"]] == ids[:2]
        assert shown["questions"][0]["chosen_letters"] and shown["questions"][1]["flagged"]
        assert not SECRET_KEYS & set(shown["questions"][0])

        report = again.finish(sid, bank=items, now=NOW + timedelta(minutes=4))  # default progress store
        assert report["score"]["correct"] == 1 and report["progress"]["recorded"] == 1
        progress_path = path.with_name("progress.json")
        assert report["progress"]["persisted"] and report["progress"]["path"] == str(progress_path)
        assert ProgressStore(progress_path).history(limit=5)[0]["source"] == "mock"
        assert MockExamStore(path).status(sid, now=NOW)["result"]["score"]["correct"] == 1
        assert not list(path.parent.glob("*.tmp"))


def test_default_bank_session_finishes_after_restart():
    with _temp_dir() as tmp:
        path = Path(tmp) / "mock_sessions.json"
        session = MockExamStore(path).start(count=12, seed=21, now=NOW)
        assert session["bank"] == "default" and session["count"] == 12
        for question in session["questions"]:
            assert not SECRET_KEYS & set(question)
        sid, qid = session["session_id"], session["question_ids"][0]
        correct = exam_bank.grade(qid, [])["correct_answer"]
        MockExamStore(path).answer(sid, qid, correct, now=NOW + timedelta(minutes=1))
        progress = ProgressStore(Path(tmp) / "progress.json")
        report = MockExamStore(path).finish(sid, store=progress, now=NOW + timedelta(minutes=2))
        assert report["score"]["correct"] == 1 and report["score"]["unanswered"] == 11
        assert report["wrong_count"] == 11 and all(row["correct_answer"] for row in report["wrong"])
        assert progress.summary(now=NOW)["totals"]["answered"] == 1


def test_full_mock_from_shipped_bank_follows_weights():
    session = MockExamStore(memory_only=True).start(seed=7, now=NOW, include_questions=False)
    available = exam_bank.stats()["by_domain"]
    if all(available[name] >= count for name, count in EXPECTED_65.items()):
        assert session["allocation"] == {name: EXPECTED_65[name] for name in mock.official_weights()}
        assert session["shortfalls"] == []
    else:
        assert session["shortfalls"]
    assert session["count"] == min(65, sum(available.values()))
    assert all(session["allocation"][name] <= available[name] for name in available)


def test_corrupt_file_is_quarantined_and_env_dir_default():
    with _temp_dir() as tmp:
        path = Path(tmp) / "mock_sessions.json"
        path.write_text("{not json", encoding="utf-8")
        store = MockExamStore(path)
        try:
            store.status(now=NOW)
        except MockExamError:
            pass
        else:
            raise AssertionError("expected no open mock")
        assert store.recovered_backup and Path(store.recovered_backup).exists()
        assert store.start(count=5, seed=1, bank=_items(), now=NOW, include_questions=False)["ok"]

        previous = os.environ.get(mock.ENV_DIR)
        os.environ[mock.ENV_DIR] = tmp
        try:
            assert mock.default_sessions_path() == Path(tmp) / "mock_sessions.json"
            assert MockExamStore().path == Path(tmp) / "mock_sessions.json"
        finally:
            if previous is None:
                os.environ.pop(mock.ENV_DIR, None)
            else:
                os.environ[mock.ENV_DIR] = previous


def test_closed_sessions_are_pruned_but_open_one_kept():
    store = MockExamStore(memory_only=True)
    items = _items({"Composition": 5})
    for index in range(mock.MAX_SESSIONS_KEPT + 3):
        session = store.start(count=2, domain_weights={"Composition": 1}, seed=index, bank=items, now=NOW, include_questions=False)
        store.abandon(session["session_id"], now=NOW)
    last = store.start(count=2, domain_weights={"Composition": 1}, seed=99, bank=items, now=NOW, include_questions=False)
    rows = store.list_sessions(limit=mock.MAX_LIST_LIMIT)
    assert len(store._load()["sessions"]) == mock.MAX_SESSIONS_KEPT + 1
    assert rows[0]["session_id"] == last["session_id"] and rows[0]["status"] == "active"


def test_item_bank_rejects_bad_items():
    good = _items({"Composition": 1})[0]
    for change in ({"answer": [9]}, {"type": "essay"}, {"options": ["only"]}, {"stem": ""}, {"answer": [0, 1]}):
        try:
            ItemBank([{**good, **change}])
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"accepted {change}")
    try:
        ItemBank([good, good])
    except ValueError:
        pass
    else:
        raise AssertionError("accepted duplicate ids")
    graded = ItemBank([good]).grade(good["id"], ["B"])
    assert graded["correct"] is (good["answer"] == [1]) and graded["correct_letters"] == [mock.LETTERS[good["answer"][0]]]
