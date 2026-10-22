"""Local progress dashboard: data maths, plan and mock history, safe self-contained HTML, atomic writes in temp dirs."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import blueprint as real_blueprint
from omniverse_scene_mcp.exam import dashboard, progress
from omniverse_scene_mcp.exam.mock import MockExamStore
from omniverse_scene_mcp.exam.progress import ProgressStore

TODAY = date(2026, 10, 22)
T0 = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)  # yesterday: wrong answers (box 1) are due today
DOMAIN_NAMES = [d["name"] for d in real_blueprint.load_blueprint()["domains"]]
WEIGHTS = {d["name"]: d["weight"] for d in real_blueprint.load_blueprint()["domains"]}


def _temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="exam_dashboard_test_", ignore_cleanup_errors=True)


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


def _bank(per_domain: int = 4) -> list[dict]:
    """Synthetic bank: per_domain questions in each blueprint domain, objective '<n>.1'."""
    items = []
    for number, name in enumerate(DOMAIN_NAMES, 1):
        for index in range(1, per_domain + 1):
            items.append({"id": f"d{number}-{index}", "domain": name, "objective_ids": [f"{number}.1"]})
    return items


def _answer(store: ProgressStore, qid: str, domain: str, ok: bool, objective: str | None = None, now=T0) -> None:
    store.record_answer(qid, domain=domain, objective_ids=[objective] if objective else [], chosen="A",
                        correct="A" if ok else "B", is_correct=ok, now=now)


def _row(data: dict, name: str) -> dict:
    return next(row for row in data["domains"] if row["name"] == name)


# ---------------------------------------------------------------- data


def test_empty_progress_gives_every_domain_with_zero_counts():
    store = ProgressStore(memory_only=True)
    data = dashboard.dashboard_data(store, today=TODAY)
    assert [row["name"] for row in data["domains"]] == DOMAIN_NAMES
    assert sum(row["weight"] for row in data["domains"]) == 100
    for row in data["domains"]:
        assert row["answered"] == 0 and row["accuracy_pct"] is None and row["status"] == "untested"
        assert row["weight"] == WEIGHTS[row["name"]]
    assert data["totals"]["answered"] == 0 and data["totals"]["accuracy_pct"] is None
    assert data["totals"]["weighted_accuracy_pct"] is None
    assert data["due_reviews"] == 0 and data["streak_days"] == 0 and data["weakest_objectives"] == []
    assert data["plan"]["configured"] is False and data["plan"]["days_left"] is None
    assert data["mocks"]["available"] is False and data["mocks"]["finished"] == []
    assert data["coverage"]["answered"] == 0 and data["coverage"]["total"] == 233  # the shipped bank
    kinds = [item["kind"] for item in data["focus_next"]]
    assert "plan" in kinds and "untested_domain" in kinds and "reviews" not in kinds
    assert data["today"] == "2026-10-22" and data["generated_at"].startswith("2026-10-22T12:00:00")
    json.dumps(data)
    # Reading the dashboard saved nothing (no plan_start was written, unlike 'start day').
    assert store.get_settings()["plan_start"] is None


def test_domain_accuracy_coverage_status_and_weighted_math():
    store = ProgressStore(memory_only=True)
    comp, vis, dm = "Composition", "Visualization", "Data Modeling"
    for index, ok in enumerate([True, True, True, False, False, True], 1):  # 4/6 = 66.7% -> weak
        _answer(store, f"d1-{index}" if index <= 4 else f"extra-{index}", comp, ok, "1.1")
    for index in range(1, 6):  # 5/5 -> strong
        _answer(store, f"d8-{(index - 1) % 4 + 1}", vis, True, "8.1")
    _answer(store, "d5-1", dm, False, "5.1")  # 1 answer -> too few to judge
    data = dashboard.dashboard_data(store, bank=_bank(), today=TODAY)

    c, v, d = _row(data, comp), _row(data, vis), _row(data, dm)
    assert (c["answered"], c["correct"], c["wrong"], c["accuracy_pct"], c["status"]) == (6, 4, 2, 66.7, "weak")
    assert (v["answered"], v["correct"], v["accuracy_pct"], v["status"]) == (5, 5, 100.0, "strong")
    assert (d["answered"], d["accuracy_pct"], d["status"]) == (1, 0.0, "low_evidence")
    assert _row(data, "Data Exchange")["status"] == "untested"
    # Coverage counts distinct bank questions tried: extra-* ids are not in the bank; d8-1 was answered twice.
    assert (c["bank_answered"], c["bank_total"], c["coverage_pct"]) == (4, 4, 100.0)
    assert (v["bank_answered"], v["coverage_pct"]) == (4, 100.0)
    assert (d["bank_answered"], d["coverage_pct"]) == (1, 25.0)
    assert data["coverage"]["answered"] == 9 and data["coverage"]["total"] == 32 and data["coverage"]["pct"] == 28.1
    assert len(data["coverage"]["by_domain"]) == 8

    totals = data["totals"]
    assert (totals["answered"], totals["correct"], totals["wrong"], totals["accuracy_pct"]) == (12, 9, 3, 75.0)
    expected = round((23 * 66.7 + 8 * 100.0 + 13 * 0.0) / (23 + 8 + 13), 1)
    assert totals["weighted_accuracy_pct"] == expected
    assert totals["tested_weight_pct"] == 44.0

    weakest = data["weakest_objectives"]
    assert [row["objective_id"] for row in weakest] == ["5.1", "1.1"]  # 0% first, then 66.7%
    assert weakest[0]["text"].startswith("Create a primvar") and weakest[1]["accuracy_pct"] == 66.7
    # 3 wrong cards (box 1, due at once) + 7 cards right once (box 2, due a day later) are due at noon today;
    # d8-1 was right twice (box 3) and is not due yet.
    noon = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    assert data["due_reviews"] == store.summary(now=noon)["due_count"] == 10
    assert data["streak_days"] == 1  # answers yesterday only

    focus = data["focus_next"]
    assert focus[0]["kind"] == "reviews" and "10 due review questions" in focus[0]["text"]
    assert any(item["kind"] == "weak_domain" and item["domain"] == comp for item in focus)
    assert any(item["kind"] == "weak_objective" and item["objective_id"] == "5.1" for item in focus)
    assert any(item["kind"] == "untested_domain" and item["domain"] == "Data Exchange" for item in focus)
    assert len(focus) <= dashboard.MAX_FOCUS
    json.dumps(data)


def test_dashboard_with_a_fixed_today_ignores_the_wall_clock():
    """Regression: due_reviews/streak once came from the real clock (summary(now=None)), so the weighted-math test
    above failed when the suite ran late in the UTC day (only the 3 box-1 cards were due). With today given, every
    time-dependent number is computed at noon UTC of that day, whatever the wall clock says."""
    from omniverse_scene_mcp.exam import srs

    def build() -> dict:
        store = ProgressStore(memory_only=True)
        for index, ok in enumerate([True, False, False, True], 1):
            _answer(store, f"d1-{index}", "Composition", ok, "1.1")
        return dashboard.dashboard_data(store, bank=_bank(), today=TODAY)

    real_now = srs.utc_now
    seen = set()
    try:
        for moment in (datetime(2026, 10, 21, 21, 36, tzinfo=timezone.utc), datetime(2026, 10, 22, 23, 59, tzinfo=timezone.utc),
                       datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)):
            srs.utc_now = lambda moment=moment: moment
            data = build()
            seen.add((data["due_reviews"], data["streak_days"], data["generated_at"], json.dumps(data["plan"], sort_keys=True)))
    finally:
        srs.utc_now = real_now
    assert len(seen) == 1, seen
    due, streak, generated, _plan = seen.pop()
    assert (due, streak) == (4, 1) and generated.startswith("2026-10-22T12:00:00")


def test_plan_days_left_today_topics_and_behind():
    store = ProgressStore(memory_only=True)
    schedule = [
        {"day": 1, "type": "study", "minutes": 40,
         "blocks": [{"objective": "1.1", "domain": "Composition", "minutes": 20, "pass": "learn"},
                    {"objective": "2.1", "domain": "Content Aggregation", "minutes": 20, "pass": "learn"}]},
        {"day": 2, "type": "study", "minutes": 20,
         "blocks": [{"objective": "5.1", "domain": "Data Modeling", "minutes": 20, "pass": "learn"}]},
        {"day": 3, "type": "review", "minutes": 30,
         "blocks": [{"activity": "mixed timed practice", "domains": ["Composition"], "minutes": 30}]},
        {"day": 4, "type": "study", "minutes": 20,
         "blocks": [{"objective": "4.1", "domain": "Data Exchange", "minutes": 20, "pass": "learn"}]},
        {"day": 5, "type": "final_review", "minutes": 20,
         "blocks": [{"activity": "light review", "objectives": ["5.1"], "minutes": 20}]},
    ]
    store.update_settings(plan_start="2026-10-20", exam_date="2026-10-25", days_until_exam=5, minutes_per_day=40,
                          schedule=schedule)
    store.update_settings(completed_days=[1])
    data = dashboard.dashboard_data(store, bank=_bank(), today=TODAY)
    plan = data["plan"]
    assert plan["configured"] and plan["plan_start"] == "2026-10-20" and plan["exam_date"] == "2026-10-25"
    assert plan["days_left"] == 3 and plan["total_days"] == 5 and plan["minutes_per_day"] == 40
    assert plan["completed_days"] == [1] and plan["completed_count"] == 1
    assert plan["calendar_day"] == 3 and plan["today_day"] == 3
    today = plan["today"]
    assert today["day"] == 3 and today["type"] == "review" and today["date"] == "2026-10-22"
    assert today["topics"][0]["label"] == "mixed timed practice: Composition" and today["completed"] is False
    assert plan["next_open_day"] == 2 and plan["next"]["topics"][0]["objective_id"] == "5.1"
    assert plan["next"]["topics"][0]["label"].startswith("Create a primvar")
    assert plan["behind_days"] == 1 and plan["plan_complete"] is False
    plan_focus = next(item for item in data["focus_next"] if item["kind"] == "plan")
    assert plan_focus["text"].startswith("Do day 2 of your study plan")

    page = dashboard.render_dashboard_html(data)
    assert "Today: day 3" in page and "Next open day: day 2" in page and "1 / 5 days" in page

    # Exam date derived from plan_start + days when only days were saved; all days done -> plan complete.
    store.update_settings(exam_date=None, schedule=schedule)
    store.update_settings(completed_days=[1, 2, 3, 4, 5])
    done = dashboard.dashboard_data(store, bank=_bank(), today="2026-10-22")["plan"]
    assert done["exam_date"] == "2026-10-25" and done["plan_complete"] is True and done["next"] is None
    assert done["behind_days"] == 0


def test_mock_history_newest_first_with_domain_scores():
    bank = []
    for number, name in enumerate(DOMAIN_NAMES, 1):
        for index in range(1, 11):
            bank.append({"id": f"m{number}-{index:02d}", "domain": name, "objective_ids": [f"{number}.1"],
                         "type": "single", "difficulty": "easy", "stem": f"{name} question {index}.",
                         "options": ["a", "b", "c", "d"], "answer": [0], "explanations": ["w", "x", "y", "z"]})
    mocks = MockExamStore(memory_only=True, bank=bank)
    store = ProgressStore(memory_only=True)
    first = mocks.start(count=20, minutes=30, seed=1, now=T0, include_questions=False)
    for qid in first["question_ids"]:
        mocks.answer(first["session_id"], qid, [1], now=T0 + timedelta(minutes=5))  # all wrong
    mocks.finish(first["session_id"], store=store, now=T0 + timedelta(minutes=10))
    second_at = T0 + timedelta(hours=2)
    second = mocks.start(count=20, minutes=30, seed=2, now=second_at, include_questions=False)
    for qid in second["question_ids"]:
        mocks.answer(second["session_id"], qid, [0], now=second_at + timedelta(minutes=5))  # all right
    mocks.finish(second["session_id"], store=store, now=second_at + timedelta(minutes=10))
    mocks.start(count=10, minutes=30, seed=3, now=second_at + timedelta(hours=1), include_questions=False)

    data = dashboard.dashboard_data(store, mock_store=mocks, bank=bank, today=TODAY)
    history = data["mocks"]
    assert history["available"] and history["active"] is True and history["finished_count"] == 2
    assert [m["session_id"] for m in history["finished"]] == [second["session_id"], first["session_id"]]
    assert history["latest_pct"] == 100.0 and history["best_pct"] == 100.0
    newest, oldest = history["finished"]
    assert newest["score_pct"] == 100.0 and newest["correct"] == 20 and newest["meets_target"] is True
    assert oldest["score_pct"] == 0.0 and oldest["meets_target"] is False
    assert sum(row["questions"] for row in newest["domains"].values()) == 20
    assert newest["domains"]["Composition"]["pct"] == 100.0 and oldest["domains"]["Composition"]["pct"] == 0.0
    assert data["totals"]["answered"] == 40 and data["coverage"]["answered"] > 0

    page = dashboard.render_dashboard_html(data)
    assert "Finished: 2." in page and "100%" in page and "A mock exam is open right now" in page
    json.dumps(data)


def test_mock_store_without_private_loader_uses_list_and_review():
    class FakeMocks:
        def list_sessions(self, limit=10):
            return [{"session_id": "s2", "status": "abandoned", "finished_at": None},
                    {"session_id": "s1", "status": "finished", "finished_at": "2026-10-21T10:00:00Z"}]

        def review(self, session_id):
            assert session_id == "s1"
            return {"score": {"pct": 61.5, "correct": 40, "questions": 65, "answered": 55},
                    "domains": {"Composition": {"pct": 50.0, "correct": 5, "questions": 10}},
                    "readiness": {"meets_target": False}}

    data = dashboard.dashboard_data(ProgressStore(memory_only=True), mock_store=FakeMocks(), bank=_bank(),
                                    today=TODAY)
    assert data["mocks"]["finished_count"] == 1 and data["mocks"]["active"] is False
    only = data["mocks"]["finished"][0]
    assert only["session_id"] == "s1" and only["score_pct"] == 61.5 and only["domains"]["Composition"]["pct"] == 50.0

    class BrokenMocks:
        def _load(self):
            raise ValueError("damaged <file>")

    broken = dashboard.dashboard_data(ProgressStore(memory_only=True), mock_store=BrokenMocks(), bank=_bank(),
                                      today=TODAY)
    assert broken["mocks"]["finished"] == [] and any("could not be read" in n for n in broken["notes"])
    assert "damaged &lt;file&gt;" in dashboard.render_dashboard_html(broken)


# ---------------------------------------------------------------- HTML


def test_html_escapes_text_and_lists_every_domain():
    store = ProgressStore(memory_only=True)
    evil_domain = "<script>alert('x')</script> & Co"
    evil_objective = '"><img src=x onerror=alert(1)>'
    _answer(store, "q-evil", evil_domain, False, evil_objective)
    _answer(store, "d1-1", "Composition", True, "1.1")
    data = dashboard.dashboard_data(store, bank=_bank(), today=TODAY)
    assert _row(data, evil_domain)["in_blueprint"] is False
    page = dashboard.render_dashboard_html(data)

    assert "<script" not in page.lower() and "<img" not in page.lower() and "onerror=alert(1)>" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; Co" in page
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    for name in DOMAIN_NAMES:
        assert name in page
    for heading in ("What to do next", "Your study plan", "How you are doing in each exam domain",
                    "Objectives to revisit", "Mock exams", "Question bank coverage", "How to read this page"):
        assert heading in page
    assert page.count("<svg") >= 8 + 1 + 8  # accuracy + share bars per domain, coverage bars


def test_html_is_self_contained_with_no_urls_or_scripts():
    store = ProgressStore(memory_only=True)
    for index in range(1, 4):
        _answer(store, f"d4-{index}", "Data Exchange", index != 2, "4.1")
    store.update_settings(plan_start="2026-10-20", days_until_exam=30)
    page = dashboard.render_dashboard_html(dashboard.dashboard_data(store, today=TODAY))
    lowered = page.lower()
    assert page.startswith("<!doctype html>") and lowered.rstrip().endswith("</html>")
    for banned in ("http", "<script", "src=", "href=", "<link", "@import", "url(", "//cdn", "@font-face",
                   "<iframe", "<object", "<embed", "javascript:"):
        assert banned not in lowered, banned
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "prefers-color-scheme:dark" in page and ':root[data-theme="dark"]' in page
    assert "max-width:640px" in page  # phone layout
    assert re.search(r"<title>NCP-OUSD Study Dashboard</title>", page)
    assert '<meta charset="utf-8">' in page


def test_render_tolerates_minimal_data():
    page = dashboard.render_dashboard_html({})
    assert "No plan yet" in page and "No finished mock exams yet" in page and "No missed objectives" in page


# ---------------------------------------------------------------- writing


def test_write_dashboard_default_path_is_exam_prep_dir_and_atomic():
    with _temp_dir() as tmp, _exam_dir(tmp):
        assert dashboard.default_dashboard_path() == Path(tmp) / "dashboard.html"
        store = ProgressStore(memory_only=True)
        _answer(store, "d1-1", "Composition", True, "1.1")
        result = dashboard.write_dashboard(store, bank=_bank(), today=TODAY)
        target = Path(tmp) / "dashboard.html"
        assert result["ok"] is True and Path(result["path"]) == target.resolve()
        assert result["bytes"] == target.stat().st_size > 1000
        assert "Composition" in target.read_text(encoding="utf-8")
        again = dashboard.write_dashboard(store, bank=_bank(), today="2026-10-23")
        assert again["ok"] and "2026-10-23" in target.read_text(encoding="utf-8")
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["dashboard.html"]  # no temp files left behind


def test_write_dashboard_refuses_paths_outside_allowed_folders():
    with _temp_dir() as tmp, _temp_dir() as other, _exam_dir(tmp):
        store = ProgressStore(memory_only=True)
        outside = Path(other) / "dashboard.html"
        refused = dashboard.write_dashboard(store, outside, bank=_bank(), today=TODAY)
        assert refused["ok"] is False and refused["bytes"] == 0 and "exam-prep folder" in refused["message"]
        assert not outside.exists()
        for bad in ("notes.txt", "sub/dashboard.html", "../dashboard.html", str(Path(tmp) / "dash.json")):
            result = dashboard.write_dashboard(store, bad, bank=_bank(), today=TODAY)
            assert result["ok"] is False and result["bytes"] == 0, bad
        (Path(tmp) / "folder.html").mkdir()
        assert dashboard.write_dashboard(store, "folder.html", bank=_bank(), today=TODAY)["ok"] is False
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["folder.html"]
        assert list(Path(other).iterdir()) == []

        named = dashboard.write_dashboard(store, "my-progress.htm", bank=_bank(), today=TODAY)
        assert named["ok"] and (Path(tmp) / "my-progress.htm").is_file()


def test_write_dashboard_into_progress_store_folder_reads_only():
    with _temp_dir() as tmp, _temp_dir() as prep, _exam_dir(prep):
        progress_file = Path(tmp) / "progress.json"
        store = ProgressStore(progress_file)
        _answer(store, "d1-1", "Composition", False, "1.1")
        before = progress_file.read_bytes()
        names_before = {p.name for p in Path(tmp).iterdir()}
        result = dashboard.write_dashboard(store, progress_file.with_name("dashboard.html"), bank=_bank(),
                                           today=TODAY)
        assert result["ok"] and Path(result["path"]).parent == Path(tmp).resolve()
        assert progress_file.read_bytes() == before  # the dashboard never changes progress
        assert {p.name for p in Path(tmp).iterdir()} == names_before | {"dashboard.html"}
        assert list(Path(prep).iterdir()) == []


def test_write_dashboard_failed_replace_keeps_old_file_and_cleans_temp():
    with _temp_dir() as tmp, _exam_dir(tmp):
        target = Path(tmp) / "dashboard.html"
        target.write_text("old page", encoding="utf-8")
        original = dashboard.os.replace

        def failing_replace(src, dst):
            raise OSError("disk full")

        dashboard.os.replace = failing_replace
        try:
            result = dashboard.write_dashboard(ProgressStore(memory_only=True), bank=_bank(), today=TODAY)
        finally:
            dashboard.os.replace = original
        assert result["ok"] is False and "disk full" in result["message"]
        assert target.read_text(encoding="utf-8") == "old page"
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["dashboard.html"]
