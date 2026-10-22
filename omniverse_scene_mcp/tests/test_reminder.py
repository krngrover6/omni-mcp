"""Daily reminder / 'what should I do today': plan maths, streaks, catch-up, text caps, temp-only writes, task text."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import daily, progress, python_practice, reminder
from omniverse_scene_mcp.exam.progress import ProgressStore

TODAY = date(2026, 10, 22)
GUILT_WORDS = ("behind", "failed", "lazy", "should have", "missed", "falling", "guilt", "shame")


def _temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="exam_reminder_test_", ignore_cleanup_errors=True)


@contextlib.contextmanager
def _exam_dir(value: str):
    """Point $OMNIVERSE_EXAM_PREP_DIR at a temp folder (never the real exam-prep) and clear $OMNIVERSE_ROOT."""
    saved = os.environ.get(progress.ENV_DIR)
    saved_root = os.environ.pop("OMNIVERSE_ROOT", None)
    os.environ[progress.ENV_DIR] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(progress.ENV_DIR, None)
        else:
            os.environ[progress.ENV_DIR] = saved
        if saved_root is not None:
            os.environ["OMNIVERSE_ROOT"] = saved_root


def _store(plan_start: str | None = "2026-10-21", exam_date: str | None = "2026-11-20",
           days: int | None = 30, completed: list[int] | None = None) -> ProgressStore:
    store = ProgressStore(memory_only=True)
    if plan_start or exam_date:
        store.update_settings(plan_start=plan_start, exam_date=exam_date, days_until_exam=days,
                              minutes_per_day=60, completed_days=completed or [])
    return store


def _answer(store: ProgressStore, qid: str, ok: bool, *, domain: str = "Composition", objective: str = "1.1",
            when: datetime = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)) -> None:
    store.record_answer(qid, domain=domain, objective_ids=[objective], chosen="A", correct="A" if ok else "B",
                        is_correct=ok, now=when)


def _no_guilt(text: str | None) -> None:
    lowered = (text or "").lower()
    for word in GUILT_WORDS:
        assert word not in lowered, (word, text)


# ---------------------------------------------------------------- day numbering and exam date


def test_day_numbering_and_days_left_with_a_real_frozen_schedule():
    store = ProgressStore(memory_only=True)
    plan = daily.set_exam_date("2026-11-20", 60, store, today="2026-10-21")
    assert plan["ok"] and plan["scheduled_days"] == 30
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["ok"] and brief["mode"] == "study"
    assert brief["day_number"] == 2 and brief["total_days"] == 30
    assert brief["days_left_to_exam"] == 29 and brief["exam_date"] == "2026-11-20"
    assert brief["headline"].startswith("Day 2 of 30, 29 days to the exam")
    lesson = brief["plan"][0]
    assert lesson["block"] == "lesson" and lesson["what"].startswith("Day 1 ")
    assert "learning_exam_day" in lesson["how"]
    for block in brief["plan"]:
        assert set(block) == {"block", "minutes", "what", "why", "how"}
        assert all(isinstance(block[k], str) and block[k] for k in ("block", "what", "why", "how"))
    json.dumps(brief)
    # Reading the brief saved nothing: the settings are exactly what set_exam_date stored.
    assert store.get_settings()["completed_days"] == []


def test_brief_does_not_write_plan_start_like_start_day_would():
    store = ProgressStore(memory_only=True)
    before = json.dumps(store._load(), sort_keys=True)
    reminder.today_brief(store, today=TODAY)
    assert json.dumps(store._load(), sort_keys=True) == before
    assert store.get_settings()["plan_start"] is None


def test_exam_date_unset_asks_for_a_date_first():
    brief = reminder.today_brief(ProgressStore(memory_only=True), today=TODAY)
    assert brief["ok"] and brief["mode"] == "no_plan"
    assert brief["day_number"] is None and brief["days_left_to_exam"] is None and brief["exam_date"] is None
    assert brief["plan"][0]["block"] == "set_plan"
    assert "learning_exam_set_date" in brief["plan"][0]["how"]
    assert brief["missed_days"] == 0 and brief["catch_up"] is None
    assert "exam date" in brief["headline"].lower()


def test_exam_date_passed_is_gentle_and_offers_a_new_date():
    store = _store(plan_start="2026-09-01", exam_date="2026-10-01", days=30)
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["mode"] == "exam_passed" and brief["days_left_to_exam"] == -21
    assert "2026-10-01" in brief["headline"]
    assert brief["plan"][0]["block"] == "set_plan"
    assert brief["missed_days"] == 0 and brief["catch_up"] is None and brief["open_plan_days"] == []
    _no_guilt(brief["headline"])


def test_exam_day_is_a_light_day():
    store = _store(plan_start="2026-09-22", exam_date="2026-10-22", days=30)
    _answer(store, "q-wrong", False)
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["mode"] == "exam_day" and brief["days_left_to_exam"] == 0
    assert [b["block"] for b in brief["plan"]] == ["reviews", "rest"]
    assert "good luck" in brief["headline"].lower()


def test_plan_complete_suggests_a_mock_exam():
    store = _store(plan_start="2026-10-01", exam_date="2026-11-20", days=50, completed=list(range(1, 51)))
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["mode"] == "plan_complete"
    assert brief["plan"][0]["block"] == "mock" and "learning_mock_exam_start" in brief["plan"][0]["how"]


def test_bad_today_and_bad_minutes_are_reported_not_raised():
    store = ProgressStore(memory_only=True)
    assert reminder.today_brief(store, today="22/10/2026")["ok"] is False
    for bad in (5, 1000, True, "lots", 12.5, None):
        result = reminder.today_brief(store, today=TODAY, minutes=bad)
        assert result["ok"] is False and "minutes" in result["message"]
    assert reminder.today_brief(store, today=TODAY, minutes="45")["minutes"] == 45


# ---------------------------------------------------------------- streak and missed days


def test_streak_counts_consecutive_study_days():
    store = _store()
    for day in (18, 20, 21, 22):  # gap on the 19th
        _answer(store, f"q{day}", True, when=datetime(2026, 10, day, 9, 0, tzinfo=timezone.utc))
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["streak"] == 3 and "3 study days in a row" in brief["streak_message"]
    # Nothing yet today still counts yesterday's run.
    assert reminder.today_brief(store, today="2026-10-23")["streak"] == 3
    assert reminder.today_brief(store, today="2026-10-25")["streak"] == 0
    assert reminder.today_brief(ProgressStore(memory_only=True), today=TODAY)["streak"] == 0


def test_missed_days_and_gentle_catch_up():
    on_time = reminder.today_brief(_store(completed=[1]), today=TODAY)
    assert on_time["missed_days"] == 0 and on_time["catch_up"] is None and on_time["open_plan_days"] == []

    one = reminder.today_brief(_store(), today=TODAY)  # day 2, day 1 still open
    assert one["missed_days"] == 1 and one["open_plan_days"] == [1]
    assert "Day 1" in one["catch_up"] and "no need to double up" in one["catch_up"]

    two = reminder.today_brief(_store(plan_start="2026-10-18", completed=[1, 2]), today=TODAY)  # day 5
    assert two["day_number"] == 5 and two["missed_days"] == 2 and two["open_plan_days"] == [3, 4]
    assert "day 3" in two["catch_up"]

    many = reminder.today_brief(_store(plan_start="2026-10-10"), today=TODAY)  # day 13, nothing done
    assert many["missed_days"] == 12 and "learning_exam_set_date" in many["catch_up"]
    for brief in (one, two, many):
        _no_guilt(brief["catch_up"])
        _no_guilt(brief["headline"])
        _no_guilt(brief["message_markdown"])
        _no_guilt(reminder.reminder_text(_store(plan_start="2026-10-10"), today=TODAY))


# ---------------------------------------------------------------- reviews, weak domains, python practice


def test_due_reviews_count_and_top_objectives():
    store = _store()
    _answer(store, "a", False, objective="1.2")
    _answer(store, "b", False, objective="1.2")
    _answer(store, "c", False, objective="3.1", domain="Data Modeling")
    _answer(store, "d", True, objective="2.1", domain="Content Aggregation",
            when=datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc))  # box 2: due tomorrow
    brief = reminder.today_brief(store, today=TODAY)
    due = brief["due_reviews"]
    assert due["count"] == 3 and due["how"] == "learning_exam_review"
    assert due["top_objectives"][0] == {"objective_id": "1.2", "due": 2}
    assert {"objective_id": "3.1", "due": 1} in due["top_objectives"]
    reviews = next(b for b in brief["plan"] if b["block"] == "reviews")
    assert "3 due review questions" in reviews["what"]
    assert "Due reviews: 3" in brief["message_markdown"]
    assert reminder.today_brief(_store(), today=TODAY)["due_reviews"] == {
        "count": 0, "top_objectives": [], "how": "learning_exam_review"}


def test_weak_domains_and_drill_block():
    store = _store()
    for index, ok in enumerate([True, False, False, True, False, True], 1):  # 3/6 = 50% -> weak
        _answer(store, f"comp-{index}", ok, objective="1.1")
    for index in range(5):  # 5/5 -> strong, not listed
        _answer(store, f"vis-{index}", True, domain="Visualization", objective="8.1")
    brief = reminder.today_brief(store, today=TODAY)
    assert [d["name"] for d in brief["weak_domains"]] == ["Composition"]
    assert brief["weak_domains"][0]["accuracy_pct"] == 50.0 and brief["weak_domains"][0]["weight"] == 23
    drill = next(b for b in brief["plan"] if b["block"] == "drill")
    assert reminder.DRILL_PHRASE in drill["how"] and "Composition" in drill["what"]
    assert brief["weakest_objectives"][0]["objective_id"] == "1.1"
    assert "Weak domains: Composition (50%)" in brief["message_markdown"]


def test_python_practice_next_skips_passed_exercises_only():
    store = _store()
    first = python_practice.list_exercises()["exercises"]
    assert reminder.python_practice_next(store)["id"] == first[0]["id"]
    python_practice.record_progress(store, first[0]["id"], True)
    python_practice.record_progress(store, first[1]["id"], False)  # a failed attempt is not a pass
    nxt = reminder.python_practice_next(store)
    assert nxt["id"] == first[1]["id"] and nxt["passed"] == 1 and nxt["total"] == len(first)
    brief = reminder.today_brief(store, today=TODAY)
    assert brief["python_practice_next"]["id"] == first[1]["id"]
    block = next(b for b in brief["plan"] if b["block"] == "python")
    assert first[1]["id"] in block["how"] and "python_practice_get" in block["how"]
    for row in first:
        python_practice.record_progress(store, row["id"], True)
    assert reminder.python_practice_next(store) is None


# ---------------------------------------------------------------- minutes scaling


def test_minutes_scale_to_30_60_90_and_always_add_up():
    store = _store()
    _answer(store, "wrong-1", False)
    for index, ok in enumerate([False, False, True, False, True], 2):
        _answer(store, f"wrong-{index}", ok)
    counts = {}
    for minutes in (10, 20, 30, 45, 60, 90, 120, 480):
        brief = reminder.today_brief(store, today=TODAY, minutes=minutes)
        blocks = brief["plan"]
        assert brief["minutes"] == minutes
        assert sum(b["minutes"] for b in blocks) == minutes, (minutes, blocks)
        assert all(b["minutes"] >= reminder.MIN_BLOCK_MINUTES for b in blocks)
        assert blocks[0]["block"] == "lesson" and blocks[0]["minutes"] == max(b["minutes"] for b in blocks)
        assert len(blocks) == len({b["block"] for b in blocks})
        counts[minutes] = len(blocks)
    assert counts[10] == 1 and counts[30] == 3 and counts[60] == 5 and counts[90] == 5
    assert counts[30] < counts[60]
    lesson = {m: reminder.today_brief(store, today=TODAY, minutes=m)["plan"][0]["minutes"] for m in (30, 60, 90)}
    assert lesson[30] < lesson[60] < lesson[90]
    assert "Plan for 90 minutes" in reminder.today_brief(store, today=TODAY, minutes=90)["message_markdown"]


def test_apportion_is_exact_and_keeps_a_minimum():
    assert reminder._apportion([], 30) == []
    assert reminder._apportion([5, 2, 2], 30) == [17, 7, 6]  # leftover minutes go to the biggest remainders
    assert sum(reminder._apportion([5, 3, 2, 2, 2], 60)) == 60
    assert min(reminder._apportion([20, 1], 20)) == 5


# ---------------------------------------------------------------- text output


def test_reminder_text_is_short_plain_and_capped():
    store = _store(plan_start="2026-10-10")
    for index in range(8):
        _answer(store, f"x{index}", index % 3 == 0)
    _answer(store, "today", True, when=datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc))
    for minutes in (10, 60, 480):
        text = reminder.reminder_text(store, today=TODAY, minutes=minutes)
        lines = text.split("\n")
        assert 1 <= len(lines) <= reminder.MAX_TEXT_LINES
        assert all(len(line) <= reminder.MAX_LINE_CHARS for line in lines)
        assert text.isascii() and "**" not in text and "`" not in text
    assert reminder._cap_line("x" * 500).endswith("...") and len(reminder._cap_line("x" * 500)) == 110
    bad = reminder.reminder_text(ProgressStore(memory_only=True), today="not a date")
    assert "\n" not in bad and bad.startswith("NCP-OUSD study:")


def test_markdown_is_short_and_readable():
    brief = reminder.today_brief(_store(), today=TODAY)
    md = brief["message_markdown"]
    assert md.startswith("**Day 2 of 30") and "## Plan for 60 minutes" in md and "## Status" in md
    assert len(md.splitlines()) <= 30 and len(md) < 3000


# ---------------------------------------------------------------- file writes (temp only)


def test_write_today_file_only_to_an_explicit_temp_path():
    with _temp_dir() as folder:
        target = Path(folder) / "TODAY.md"
        result = reminder.write_today_file(target, _store(), today=TODAY)
        assert result["ok"] and result["path"] == str(target) and result["date"] == "2026-10-22"
        text = target.read_text(encoding="utf-8")
        assert text.startswith("# Today: 2026-10-22") and "Plan for 60 minutes" in text
        assert result["bytes"] == len(text.encode("utf-8"))
        assert sorted(p.name for p in Path(folder).iterdir()) == ["TODAY.md"]  # no temp files left behind

        again = reminder.write_today_file(str(target), _store(completed=[1]), today=TODAY)
        assert again["ok"] and "still open" not in target.read_text(encoding="utf-8")

        assert reminder.write_today_file(Path(folder) / "today.txt", _store(), today=TODAY)["ok"] is False
        missing = Path(folder) / "nope" / "TODAY.md"
        assert reminder.write_today_file(missing, _store(), today=TODAY)["ok"] is False
        assert not missing.parent.exists()  # never creates folders
        (Path(folder) / "dir.md").mkdir()
        assert reminder.write_today_file(Path(folder) / "dir.md", _store(), today=TODAY)["ok"] is False
        assert sorted(p.name for p in Path(folder).iterdir()) == ["TODAY.md", "dir.md"]


def test_write_today_file_needs_a_path():
    for bad in (None, "", "   "):
        try:
            reminder.write_today_file(bad, _store(), today=TODAY)
        except TypeError as exc:
            assert "explicit path" in str(exc)
        else:
            raise AssertionError("expected TypeError")


# ---------------------------------------------------------------- read-only default store


def test_store_none_reads_the_progress_file_without_writing_it():
    with _temp_dir() as folder, _exam_dir(folder):
        real = ProgressStore(Path(folder) / "progress.json")
        real.update_settings(plan_start="2026-10-21", exam_date="2026-11-20", days_until_exam=30)
        _answer(real, "q1", False)
        path = Path(folder) / "progress.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in Path(folder).iterdir()))
        brief = reminder.today_brief(today=TODAY)
        assert brief["day_number"] == 2 and brief["due_reviews"]["count"] == 1 and brief["notes"] == []
        reminder.reminder_text(today=TODAY)
        after = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in Path(folder).iterdir()))
        assert before == after


def test_corrupt_or_missing_progress_file_is_left_untouched():
    with _temp_dir() as folder:
        path = Path(folder) / "progress.json"
        notes: list[str] = []
        empty = reminder.read_only_store(path, notes=notes)
        assert empty.memory_only and empty.summary()["totals"]["answered"] == 0
        assert notes and "No saved progress" in notes[0] and not path.exists()

        path.write_text("{ not json", encoding="utf-8")
        notes = []
        store = reminder.read_only_store(path, notes=notes)
        assert store.summary()["totals"]["answered"] == 0 and "left untouched" in notes[0]
        assert path.read_text(encoding="utf-8") == "{ not json"
        assert sorted(p.name for p in Path(folder).iterdir()) == ["progress.json"]  # no .corrupt backup made
        with _exam_dir(folder):
            brief = reminder.today_brief(today=TODAY)
        assert brief["ok"] and any("left untouched" in n for n in brief["notes"])
        assert sorted(p.name for p in Path(folder).iterdir()) == ["progress.json"]


def test_main_prints_the_reminder_from_a_temp_store():
    with _temp_dir() as folder, _exam_dir(folder):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert reminder.main([]) == 0
            assert reminder.main(["--markdown"]) == 0
        text = out.getvalue()
        assert "exam date" in text.lower() and "## Plan for" in text
        assert list(Path(folder).iterdir()) == []


# ---------------------------------------------------------------- scheduled task text (never executed)


def test_scheduled_task_commands_are_text_only_and_never_run():
    calls = []
    real_popen, real_system = subprocess.Popen, os.system

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("scheduled_task_commands must not run anything")

    subprocess.Popen, os.system = refuse, refuse
    try:
        result = reminder.scheduled_task_commands("7:05", python_exe=r"C:\Program Files\Python312\pythonw.exe",
                                                   src_dir=r"C:\Users\someone\proj\src")
    finally:
        subprocess.Popen, os.system = real_popen, real_system
    assert calls == []
    assert result["ok"] and result["executed"] is False and result["time"] == "07:05"
    create, remove = result["create_command"], result["remove_command"]
    assert "Register-ScheduledTask" in create and "New-ScheduledTaskTrigger -Daily -At '07:05'" in create
    assert "-WorkingDirectory 'C:\\Users\\someone\\proj\\src'" in create
    assert "-m omniverse_scene_mcp.exam.reminder --popup" in create
    assert remove == "Unregister-ScheduledTask -TaskName 'NCP-OUSD daily study reminder' -Confirm:$false"
    assert "Get-ScheduledTask" in result["check_command"] and "Start-ScheduledTask" in result["run_now_command"]
    assert "Nothing was created" in result["note"] and "YOU" in result["note"]
    everything = json.dumps(result).lower()
    for secret in ("password", "token", "apikey", "api_key", "secret", "-user ", "credential", "runlevel highest",
                   "invoke-webrequest", "http://", "https://", "reg add", "set-itemproperty"):
        assert secret not in everything, secret


def test_scheduled_task_commands_validate_and_quote():
    for bad in ("25:00", "7pm", "", None, "12:60"):
        assert reminder.scheduled_task_commands(bad)["ok"] is False
    assert reminder.scheduled_task_commands("08:00", task_name="bad;name")["ok"] is False
    assert reminder.scheduled_task_commands("08:00", task_name="x" * 81)["ok"] is False
    quoted = reminder.scheduled_task_commands("23:59", task_name="My reminder", src_dir=r"C:\O'Brien\src",
                                              python_exe=r"C:\Py\pythonw.exe")
    assert "-WorkingDirectory 'C:\\O''Brien\\src'" in quoted["create_command"]
    assert "-TaskName 'My reminder'" in quoted["remove_command"]
    assert "python.exe" in quoted["preview_command"] and "--popup" not in quoted["preview_command"]
    default = reminder.scheduled_task_commands()
    assert default["ok"] and default["time"] == "19:00"
    assert Path(default["create_command"].split("-WorkingDirectory '")[1].split("'")[0]).name == "src"
