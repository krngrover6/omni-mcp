from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import progress, srs
from omniverse_scene_mcp.exam.progress import ProgressStore

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@contextlib.contextmanager
def _env(value: str | None):
    saved = os.environ.get(progress.ENV_DIR)
    if value is None:
        os.environ.pop(progress.ENV_DIR, None)
    else:
        os.environ[progress.ENV_DIR] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(progress.ENV_DIR, None)
        else:
            os.environ[progress.ENV_DIR] = saved


def _answer(store, qid, ok, *, domain="Composition", objectives=("1.1",), now=T0, **extra):
    return store.record_answer(
        qid,
        domain=domain,
        objective_ids=list(objectives),
        chosen="A" if ok else "B",
        correct="A",
        is_correct=ok,
        now=now,
        **extra,
    )


def _files(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir())


def _data_files(folder: Path) -> list[str]:
    """Folder listing without the writers' lock sidecar file (progress.json.lock)."""
    return [name for name in _files(folder) if name != "progress.json.lock"]


def test_srs_new_card_is_due_now_in_box_one():
    card = srs.new_card(T0)
    assert card["box"] == 1
    assert card["due_at"] == "2026-10-20T09:00:00Z"
    assert srs.is_due(card, T0)
    assert card["last_result"] is None


def test_srs_correct_answers_climb_boxes_with_leitner_intervals():
    assert srs.BOX_INTERVALS_DAYS == (0, 1, 3, 7, 21)
    card = None
    moment = T0
    for expected_box, days in ((2, 1), (3, 3), (4, 7), (5, 21), (5, 21)):
        card = srs.review(card, True, moment)
        assert card["box"] == expected_box
        assert srs.parse_iso(card["due_at"]) == moment + timedelta(days=days)
        assert card["last_result"] == "correct"
        moment = srs.parse_iso(card["due_at"])
    assert card["reviews"] == 5
    assert card["times_correct"] == 5
    assert card["times_wrong"] == 0


def test_srs_wrong_answer_returns_to_box_one_due_immediately():
    card = {**srs.new_card(T0), "box": 4, "domain": "Composition"}
    later = T0 + timedelta(days=7)
    updated = srs.review(card, False, later)
    assert updated["box"] == 1
    assert srs.parse_iso(updated["due_at"]) == later
    assert updated["last_result"] == "wrong"
    assert updated["times_wrong"] == 1
    assert updated["domain"] == "Composition"
    assert card["box"] == 4, "review must not modify its input"


def test_srs_due_boundaries_naive_and_offset_datetimes():
    card = srs.review(None, True, T0)
    assert not srs.is_due(card, T0 + DAY - timedelta(seconds=1))
    assert srs.is_due(card, T0 + DAY)
    assert srs.is_due(card, datetime(2026, 10, 21, 9, 0))
    ist = timezone(timedelta(hours=5, minutes=30))
    assert srs.is_due(card, datetime(2026, 10, 21, 14, 30, tzinfo=ist))
    assert not srs.is_due(card, datetime(2026, 10, 21, 14, 29, tzinfo=ist))
    assert srs.parse_iso(srs.to_iso(T0)) == T0
    assert srs.next_box(5, True) == 5
    assert srs.next_box(3, False) == 1


def test_srs_due_cards_orders_most_overdue_first_and_limits():
    cards = {
        "late": {"box": 2, "due_at": srs.to_iso(T0 - 2 * DAY)},
        "now-b3": {"box": 3, "due_at": srs.to_iso(T0)},
        "now-b1": {"box": 1, "due_at": srs.to_iso(T0)},
        "future": {"box": 4, "due_at": srs.to_iso(T0 + DAY)},
    }
    assert srs.due_cards(cards, T0) == ["late", "now-b1", "now-b3"]
    assert srs.due_cards(cards, T0, limit=1) == ["late"]
    assert srs.due_cards(cards, T0, limit=0) == []


def test_default_path_resolves_to_workspace_exam_prep_without_creating_it():
    workspace = Path(__file__).resolve().parents[2]
    expected = workspace / "exam-prep" / "progress.json"
    file_existed = expected.exists()
    folder_existed = expected.parent.exists()
    with _env(None):
        assert progress.workspace_root() == workspace
        assert (workspace / "AGENTS.md").exists()
        assert progress.default_progress_path() == expected
        store = ProgressStore()
        assert store.path == expected
    assert expected.exists() == file_existed
    assert expected.parent.exists() == folder_existed


def test_env_dir_is_used_and_created_only_on_first_write():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "nested" / "prep"
        with _env(str(folder)):
            store = ProgressStore()
            assert store.path == folder / "progress.json"
            assert store.summary(now=T0)["totals"]["answered"] == 0
            assert store.due_reviews(now=T0) == []
            assert not folder.exists()
            _answer(store, "q1", True)
        saved = json.loads((folder / "progress.json").read_text(encoding="utf-8"))
        assert saved["schema_version"] == progress.SCHEMA_VERSION
        assert saved["cards"]["q1"]["box"] == 2
        assert _data_files(folder) == ["progress.json"]


def test_record_answer_updates_card_and_stats():
    with tempfile.TemporaryDirectory() as tmp:
        store = ProgressStore(Path(tmp) / "progress.json")
        first = _answer(store, "comp-01", False, objectives=["1.3", "1.3", "1.8"], seconds=41.26)
        assert first["box_before"] is None
        assert first["box"] == 1
        assert first["due_at"] == "2026-10-20T09:00:00Z"
        assert first["domain"] == {"name": "Composition", "answered": 1, "correct": 0, "accuracy": 0.0}
        assert [row["objective_id"] for row in first["objectives"]] == ["1.3", "1.8"]
        assert first["persisted"] is True

        second = _answer(store, "comp-01", True, objectives=["1.3"], now=T0 + timedelta(minutes=10))
        assert second["box_before"] == 1
        assert second["box"] == 2
        assert second["due_at"] == "2026-10-21T09:10:00Z"
        assert second["domain"]["accuracy"] == 0.5
        assert second["objectives"] == [
            {"objective_id": "1.3", "domain": "Composition", "answered": 2, "correct": 1, "accuracy": 0.5}
        ]
        saved = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
        assert saved["cards"]["comp-01"]["objective_ids"] == ["1.3"]
        assert saved["history"][0]["seconds"] == 41.3
        assert saved["totals"] == {"answered": 2, "correct": 1, "seconds": 41.3, "timed": 1}


def test_summary_math_domains_weakest_and_due_count():
    store = ProgressStore(memory_only=True)
    _answer(store, "q1", True, objectives=["1.1", "1.3"])
    _answer(store, "q2", False, objectives=["1.3"])
    _answer(store, "q3", False, domain="Data Exchange", objectives=["4.1"])
    _answer(store, "q4", True, domain="Data Exchange", objectives=["4.1", "4.2"], seconds=30)
    _answer(store, "q5", True, domain="Visualization", objectives=["7.1"], seconds=12)
    _answer(store, "q6", False, objectives=["1.8"])

    summary = store.summary(now=T0, known_domains=["Customizing USD", "Composition"])
    totals = summary["totals"]
    assert (totals["answered"], totals["correct"], totals["wrong"], totals["accuracy"]) == (6, 3, 3, 0.5)
    assert totals["unique_questions"] == 6
    assert totals["boxes"] == {"1": 3, "2": 3, "3": 0, "4": 0, "5": 0}
    assert totals["avg_seconds"] == 21.0
    assert list(summary["domains"]) == ["Customizing USD", "Composition", "Data Exchange", "Visualization"]
    assert summary["domains"]["Customizing USD"] == {"answered": 0, "correct": 0, "accuracy": None}
    assert summary["domains"]["Composition"] == {"answered": 3, "correct": 1, "accuracy": 0.333}
    assert summary["domains"]["Data Exchange"]["accuracy"] == 0.5
    assert summary["domains"]["Visualization"]["accuracy"] == 1.0
    weakest = [(row["objective_id"], row["accuracy"], row["domain"]) for row in summary["weakest_objectives"]]
    assert weakest == [("1.8", 0.0, "Composition"), ("1.3", 0.5, "Composition"), ("4.1", 0.5, "Data Exchange")]
    assert summary["due_count"] == 3
    assert store.summary(now=T0 + DAY)["due_count"] == 6
    assert summary["streak_days"] == 1
    assert summary["last_answered_at"] == "2026-10-20T09:00:00Z"
    assert summary["storage"]["memory_only"] is True


def test_weakest_prefers_more_evidence_at_equal_accuracy():
    store = ProgressStore(memory_only=True)
    _answer(store, "a1", True, objectives=["2.1"])
    _answer(store, "a2", False, objectives=["2.1"])
    for index, ok in enumerate((True, True, False, False)):
        _answer(store, f"b{index}", ok, objectives=["9.9"])
    weakest = store.summary(now=T0)["weakest_objectives"]
    assert [(row["objective_id"], row["answered"]) for row in weakest] == [("9.9", 4), ("2.1", 2)]


def test_streak_days_counts_consecutive_utc_days():
    store = ProgressStore(memory_only=True)
    for offset in range(3):
        _answer(store, f"q{offset}", True, now=T0 + offset * DAY)
    assert store.summary(now=T0 + 2 * DAY)["streak_days"] == 3
    assert store.summary(now=T0 + 3 * DAY)["streak_days"] == 3
    assert store.summary(now=T0 + 4 * DAY)["streak_days"] == 0
    _answer(store, "q9", True, now=T0 + 5 * DAY)
    assert store.summary(now=T0 + 5 * DAY)["streak_days"] == 1


def test_due_reviews_oldest_first_with_limit():
    store = ProgressStore(memory_only=True)
    _answer(store, "q-a", False, objectives=["1.6"])
    _answer(store, "q-b", False, domain="Debugging and Troubleshooting", objectives=["5.2"], now=T0 + timedelta(hours=1))
    _answer(store, "q-c", True)
    due = store.due_reviews(now=T0 + timedelta(hours=2))
    assert [row["question_id"] for row in due] == ["q-a", "q-b"]
    assert due[0] == {
        "question_id": "q-a",
        "box": 1,
        "due_at": "2026-10-20T09:00:00Z",
        "last_result": "wrong",
        "domain": "Composition",
        "objective_ids": ["1.6"],
    }
    assert [row["question_id"] for row in store.due_reviews(limit=1, now=T0 + timedelta(hours=2))] == ["q-a"]
    assert [row["question_id"] for row in store.due_reviews(now=T0 + DAY)] == ["q-a", "q-b", "q-c"]
    assert store.due_reviews(limit=0, now=T0 + DAY) == []


def test_history_newest_first_limited_and_multiselect_choices():
    store = ProgressStore(memory_only=True)
    _answer(store, "q1", True)
    _answer(store, "q2", False, now=T0 + timedelta(minutes=1))
    store.record_answer(
        "q3",
        domain="Data Modeling",
        objective_ids="6.1",
        chosen={"C", "A"},
        correct=["A", "C"],
        is_correct=True,
        source="drill",
        now=T0 + timedelta(minutes=2),
    )
    recent = store.history(limit=2)
    assert [row["question_id"] for row in recent] == ["q3", "q2"]
    assert recent[0]["chosen"] == ["A", "C"]
    assert recent[0]["objective_ids"] == ["6.1"]
    assert recent[0]["source"] == "drill"
    assert store.history(limit=0) == []
    assert len(store.history()) == 3


def test_outputs_are_json_serializable():
    with tempfile.TemporaryDirectory() as tmp:
        store = ProgressStore(Path(tmp) / "progress.json")
        result = _answer(store, "q1", False, seconds=5)
        for value in (result, store.summary(now=T0), store.due_reviews(now=T0), store.history(), store.reset()):
            json.dumps(value)


def test_atomic_write_leaves_no_tmp_file_and_survives_failed_replace():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = ProgressStore(folder / "progress.json")
        for index in range(3):
            _answer(store, f"q{index}", index % 2 == 0)
        assert _data_files(folder) == ["progress.json"]
        before = (folder / "progress.json").read_bytes()

        original_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("simulated disk failure")

        progress.os.replace = failing_replace
        try:
            try:
                _answer(store, "q-new", True)
            except OSError as exc:
                assert "simulated" in str(exc)
            else:
                raise AssertionError("expected the simulated failure")
        finally:
            progress.os.replace = original_replace
        assert _data_files(folder) == ["progress.json"]
        assert (folder / "progress.json").read_bytes() == before


def test_concurrent_writers_on_one_file_keep_every_answer():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path = folder / "progress.json"
        errors: list[BaseException] = []

        def worker(tag: str) -> None:
            store = ProgressStore(path)  # one store per writer, like two router processes
            try:
                for index in range(25):
                    _answer(store, f"{tag}-{index}", index % 2 == 0)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"w{n}",)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        summary = ProgressStore(path).summary(now=T0)
        assert summary["totals"]["answered"] == 100
        assert summary["totals"]["unique_questions"] == 100
        assert summary["storage"]["recovered_backup"] is None
        assert _data_files(folder) == ["progress.json"]


def test_atomic_write_uses_a_unique_temp_file_per_write():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        seen: list[str] = []
        original_replace = os.replace

        def spying_replace(src, dst):
            seen.append(Path(src).name)
            return original_replace(src, dst)

        progress.os.replace = spying_replace
        try:
            progress._atomic_write_json(folder / "progress.json", {"a": 1})
            progress._atomic_write_json(folder / "progress.json", {"a": 2})
        finally:
            progress.os.replace = original_replace
        assert len(seen) == 2 and seen[0] != seen[1]
        assert all(name.startswith("progress.json.") and name.endswith(".tmp") for name in seen)
        assert "progress.json.tmp" not in seen
        assert json.loads((folder / "progress.json").read_text(encoding="utf-8")) == {"a": 2}


def test_utf8_bom_file_is_read_not_quarantined():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path = folder / "progress.json"
        store = ProgressStore(path)
        for index in range(5):
            _answer(store, f"q{index}", True)
        text = path.read_text(encoding="utf-8")
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))  # as PowerShell 5.1 -Encoding utf8 saves it
        summary = ProgressStore(path).summary(now=T0)
        assert summary["totals"]["answered"] == 5
        assert summary["storage"]["recovered_backup"] is None
        assert list(folder.glob("progress.json.corrupt-*")) == []
        assert path.exists()


def test_corrupt_file_is_backed_up_not_deleted():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path = folder / "progress.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = ProgressStore(path)
        summary = store.summary(now=T0)
        assert summary["totals"]["answered"] == 0
        backups = list(folder.glob("progress.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{not valid json"
        assert summary["storage"]["recovered_backup"] == str(backups[0])
        assert not path.exists()

        _answer(store, "q1", True)
        assert json.loads(path.read_text(encoding="utf-8"))["totals"]["answered"] == 1
        assert backups[0].exists()


def test_unreadable_shapes_and_newer_schema_are_backed_up():
    for content in ("[]", '{"schema_version": 99, "cards": {}}', '{"schema_version": 1, "cards": {"q": {"box": 2}}}', ""):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "progress.json").write_text(content, encoding="utf-8")
            store = ProgressStore(folder / "progress.json")
            assert store.history() == []
            backups = list(folder.glob("progress.json.corrupt-*"))
            assert len(backups) == 1, content
            assert backups[0].read_text(encoding="utf-8") == content
            assert store.recovery_reason


def test_unknown_top_level_keys_are_kept_on_rewrite():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        path.write_text(json.dumps({"schema_version": 1, "notes": "keep me"}), encoding="utf-8")
        _answer(ProgressStore(path), "q1", True)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["notes"] == "keep me"
        assert saved["totals"]["answered"] == 1
        assert ProgressStore(path).summary(now=T0)["totals"]["answered"] == 1


def test_reset_requires_confirm_and_keeps_backup():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path = folder / "progress.json"
        store = ProgressStore(path)
        _answer(store, "q1", True)
        _answer(store, "q2", False)
        before = path.read_bytes()

        refused = store.reset()
        assert refused["ok"] is False and refused["reset"] is False
        assert store.reset(confirm="yes")["reset"] is False
        assert path.read_bytes() == before
        assert _data_files(folder) == ["progress.json"]

        done = store.reset(confirm=True)
        assert done["ok"] is True and done["reset"] is True
        backup = Path(done["backup"])
        assert backup.name.startswith("progress.json.bak-")
        assert backup.read_bytes() == before
        assert store.summary(now=T0)["totals"]["answered"] == 0
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == progress.SCHEMA_VERSION

        again = store.reset(confirm=True)
        assert again["backup"] != done["backup"]
        assert backup.exists() and Path(again["backup"]).exists()

    with tempfile.TemporaryDirectory() as tmp:
        fresh = ProgressStore(Path(tmp) / "sub" / "progress.json").reset(confirm=True)
        assert fresh["ok"] is True and fresh["backup"] is None


def test_memory_only_never_touches_disk():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)

        def forbidden(*args, **kwargs):
            raise AssertionError("memory_only store touched the disk")

        patched = [(builtins, "open"), (io, "open"), (os, "replace"), (os, "mkdir"), (os, "makedirs"), (shutil, "copy2")]
        saved = [(owner, name, getattr(owner, name)) for owner, name in patched]
        with _env(str(folder)):
            for owner, name in patched:
                setattr(owner, name, forbidden)
            try:
                store = ProgressStore(folder / "progress.json", memory_only=True)
                default_store = ProgressStore(memory_only=True)
                result = _answer(store, "q1", False)
                _answer(default_store, "q1", True)
                summary = store.summary(now=T0)
                due = store.due_reviews(now=T0)
                recent = store.history()
                reset = store.reset(confirm=True)
            finally:
                for owner, name, original in saved:
                    setattr(owner, name, original)
        assert store.path is None and default_store.path is None
        assert result["persisted"] is False and result["path"] is None
        assert summary["totals"]["answered"] == 1
        assert summary["storage"] == {
            "path": None,
            "memory_only": True,
            "exists": False,
            "recovered_backup": None,
            "recovery_reason": None,
        }
        assert [row["question_id"] for row in due] == ["q1"]
        assert len(recent) == 1
        assert reset == {"ok": True, "reset": True, "backup": None, "path": None}
        assert store.summary(now=T0)["totals"]["answered"] == 0
        assert _files(folder) == []


def test_invalid_answers_are_rejected_before_any_write():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = ProgressStore(folder / "progress.json")
        bad_calls = [
            dict(question_id="q1", is_correct="yes"),
            dict(question_id="", is_correct=True),
            dict(question_id="q1", is_correct=True, seconds=-1),
            dict(question_id="q1", is_correct=True, seconds=float("nan")),
            dict(question_id="q1", is_correct=True, domain=""),
            dict(question_id="q1", is_correct=True, chosen={"bad": 1}),
        ]
        for call in bad_calls:
            arguments = {"domain": "Composition", "objective_ids": ["1.1"], "chosen": "A", "correct": "A", "now": T0}
            arguments.update(call)
            question_id = arguments.pop("question_id")
            try:
                store.record_answer(question_id, **arguments)
            except (TypeError, ValueError):
                pass
            else:
                raise AssertionError(f"accepted invalid answer {call}")
        assert _files(folder) == []


def test_separate_stores_on_one_file_see_each_other():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        writer, reader = ProgressStore(path), ProgressStore(path)
        _answer(writer, "q1", False)
        assert reader.summary(now=T0)["due_count"] == 1
        _answer(reader, "q2", True)
        assert writer.summary(now=T0)["totals"]["answered"] == 2


# ---------------------------------------------------------------- study-plan settings


def test_settings_default_on_old_file_without_settings():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        _answer(ProgressStore(path), "q1", True)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("settings", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        store = ProgressStore(path)
        assert store.get_settings() == {
            "exam_date": None,
            "plan_start": None,
            "days_until_exam": None,
            "minutes_per_day": None,
            "completed_days": [],
            "schedule": None,
        }
        assert store.summary(now=T0)["totals"]["answered"] == 1
        assert store.recovered_backup is None
        assert _data_files(Path(tmp)) == ["progress.json"]


def test_update_settings_validates_normalizes_and_persists():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        store = ProgressStore(path)
        saved = store.update_settings(
            exam_date="2026-11-30",
            plan_start=datetime(2026, 10, 20, 8, 0),
            days_until_exam=41.0,
            minutes_per_day=45,
            completed_days=(3, 1, 3, 2),
        )
        assert saved == {
            "exam_date": "2026-11-30",
            "plan_start": "2026-10-20",
            "days_until_exam": 41,
            "minutes_per_day": 45,
            "completed_days": [1, 2, 3],
            "schedule": None,
        }
        assert ProgressStore(path).get_settings() == saved
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == progress.SCHEMA_VERSION
        before = path.read_text(encoding="utf-8")
        bad = [
            {"exam_date": "30/11/2026"},
            {"exam_date": "2026-02-30"},
            {"days_until_exam": 0},
            {"days_until_exam": True},
            {"days_until_exam": 2.5},
            {"minutes_per_day": "60"},
            {"minutes_per_day": progress.MAX_MINUTES_PER_DAY + 1},
            {"completed_days": [0]},
            {"completed_days": "1,2"},
            {"completed_days": [1, True]},
            {"colour": "blue"},
        ]
        for values in bad:
            try:
                store.update_settings(**values)
            except (TypeError, ValueError):
                pass
            else:
                raise AssertionError(f"accepted invalid settings {values}")
        assert path.read_text(encoding="utf-8") == before
        cleared = store.update_settings(exam_date=None, completed_days=None)
        assert cleared["exam_date"] is None and cleared["completed_days"] == []
        assert cleared["minutes_per_day"] == 45
        assert store.update_settings() == cleared


def test_settings_survive_answers_and_bad_saved_values_fall_back():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        store = ProgressStore(path)
        store.update_settings(plan_start="2026-10-20", completed_days=[1, 2])
        _answer(store, "q1", False)
        assert store.get_settings()["completed_days"] == [1, 2]
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["settings"] = {
            "exam_date": "soon",
            "minutes_per_day": -5,
            "completed_days": [1, "two", 3, 0],
            "future_key": {"kept": True},
        }
        path.write_text(json.dumps(raw), encoding="utf-8")
        view = store.get_settings()
        assert view["exam_date"] is None and view["minutes_per_day"] is None
        assert view["completed_days"] == [1, 3]
        assert store.recovered_backup is None
        assert store.summary(now=T0)["totals"]["answered"] == 1
        store.update_settings(minutes_per_day=30)
        rewritten = json.loads(path.read_text(encoding="utf-8"))["settings"]
        assert rewritten["future_key"] == {"kept": True}
        assert rewritten["minutes_per_day"] == 30 and rewritten["completed_days"] == [1, 3]
        not_a_dict = json.loads(path.read_text(encoding="utf-8"))
        not_a_dict["settings"] = [1, 2]
        path.write_text(json.dumps(not_a_dict), encoding="utf-8")
        assert store.get_settings()["completed_days"] == []
        assert store.recovered_backup is None


def test_settings_memory_only_and_returned_copies():
    store = ProgressStore(memory_only=True)
    first = store.update_settings(completed_days=[2])
    first["completed_days"].append(99)
    assert store.get_settings()["completed_days"] == [2]
    store.get_settings()["completed_days"].append(5)
    assert store.get_settings()["completed_days"] == [2]
    assert store.summary(now=T0)["storage"]["path"] is None


def test_schedule_setting_is_validated_and_cleared_when_the_plan_changes():
    store = ProgressStore(memory_only=True)
    block = {"objective": "1.6", "also": ["6.2"], "domain": "Composition", "minutes": 20, "pass": "learn", "extra": 1}
    saved = store.update_settings(schedule=[{"day": 1, "type": "study", "minutes": 40, "blocks": [block]}])
    assert saved["schedule"] == [{"day": 1, "type": "study", "minutes": 40, "blocks": [
        {"objective": "1.6", "also": ["6.2"], "domain": "Composition", "minutes": 20, "pass": "learn"}]}]
    day = saved["schedule"][0]
    for bad in ("plan", [{"type": "holiday", "blocks": []}], [{"type": "study", "blocks": [{"objective": 5}]}],
                [{"type": "study", "minutes": True, "blocks": []}], [day] * (progress.MAX_SCHEDULE_DAYS + 1)):
        try:
            store.update_settings(schedule=bad)
        except (TypeError, ValueError):
            continue
        raise AssertionError(f"accepted invalid schedule {bad!r}")
    assert store.update_settings(completed_days=[1])["schedule"] == saved["schedule"]  # marking days keeps it
    assert store.update_settings(minutes_per_day=30)["schedule"] is None  # a changed plan makes it stale
