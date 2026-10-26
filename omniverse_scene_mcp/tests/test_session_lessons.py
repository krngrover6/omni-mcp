"""session_lessons: class transcripts -> short lessons, accuracy flags and quizzes (read-only, temp folders only)."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

from omniverse_scene_mcp import features, nl_tier8, session_lessons as sl, workflows
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService

TESTS = Path(__file__).resolve().parent


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{ms:03d}"


def _srt(lines: list[tuple[float, float, str]]) -> str:
    return "\n".join(f"{i}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n" for i, (a, b, text) in enumerate(lines, start=1))


def _class(minutes: float = 40.0) -> str:
    """A made-up 40 minute class: one sentence every 10 seconds, with key moments at known times."""
    special = {
        60: "Today is all about assets and environment creation in Isaac Sim.",
        120: "Go to the create menu and add a ground plane to the stage.",
        300: "A reference brings another asset file into your stage.",
        1170: "While in the references, You cannot do those kind of changes directly.",
        1200: "Only the payload lets you edit and modify the asset.",
        1260: "The USD a file is binary and fast to load.",
        1500: "Select the Xform in the stage window and set the translate value.",
        1800: "Click the rigid body preset in the property panel.",
        2100: "Apply a material to the floor so the scene looks good.",
    }
    lines, t = [], 0.0
    while t < minutes * 60:
        text = special.get(int(t), f"So here we keep talking about the scene and the x form transform number {int(t)}.")
        lines.append((t, t + 8.0, text))
        t += 10.0
    return _srt(lines)


class _Folder:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "Session3(Assets & Environment Creation)_zoomed_HD.srt").write_text(_class(), encoding="utf-8")
        sub = root / "Issac Sim Classes" / "Session 2"
        sub.mkdir(parents=True)
        (sub / "session-2(Basics of Isaac sim).srt").write_text(_class(20), encoding="utf-8")
        # an identical copy, as browsers create with "(1)"
        text = (root / "Session3(Assets & Environment Creation)_zoomed_HD.srt").read_text(encoding="utf-8")
        (root / "Session3(Assets & Environment Creation)_zoomed_HD (1).srt").write_text(text, encoding="utf-8")
        (root / "notes.txt").write_text("not a transcript", encoding="utf-8")
        self.root = root
        return root

    def __exit__(self, *exc):
        self._tmp.cleanup()


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_find_transcripts_reads_sessions_titles_and_skips_identical_copies():
    with _Folder() as root:
        found = sl.find_transcripts([root])
        assert found["ok"] is True
        by_session = {t["session"]: t for t in found["transcripts"]}
        assert sorted(by_session) == [2, 3]
        assert by_session[3]["title"] == "Assets & Environment Creation"
        assert by_session[3]["duplicates"] == ["Session3(Assets & Environment Creation)_zoomed_HD (1).srt"]
        assert by_session[2]["title"] == "Basics of Isaac sim"


def test_session_plan_splits_at_about_the_requested_length():
    with _Folder() as root:
        plan = sl.session_plan(session=3, search_dirs=[root])
        assert plan["ok"] is True and plan["session"] == 3
        assert plan["lesson_count"] == 3  # 40 minutes in ~15 minute lessons, the short tail joins the last
        assert all(13 <= item["minutes"] <= 18 for item in plan["lessons"][:2])
        assert plan["lessons"][0]["start"] == "00:00:00"
        assert plan["lessons"][1]["likely_mistakes"] >= 2
        shorter = sl.session_plan(session=3, minutes=10, search_dirs=[root])
        assert shorter["lesson_count"] == 4


def test_lesson_has_terms_points_steps_flags_and_questions():
    with _Folder() as root:
        one = sl.lesson(1, session=3, search_dirs=[root])
        assert one["ok"] is True and one["of"] == 3
        terms = [t["term"] for t in one["key_terms"]]
        assert "Xform" in terms and "ground plane" in terms
        assert all(t["meaning"] for t in one["key_terms"])
        steps = " ".join(s["step"] for s in one["do_in_app"])
        assert "create menu" in steps and "ground plane" in steps
        assert one["check_questions"] and one["check_questions"][0]["check_against"]
        assert one["next"].startswith("class_lesson(session=3, number=2)")
        assert len(one["excerpt"]) <= sl.EXCERPT_CHARS
        # "x form" was cleaned to "Xform"
        assert "x form" not in one["excerpt"].lower()


def test_accuracy_flags_catch_the_payload_and_format_claims_with_timestamps():
    with _Folder() as root:
        claims = sl.check_claims(session=3, search_dirs=[root])
        assert claims["ok"] is True
        topics = {(f["topic"], f["at"]) for f in claims["flags"]}
        assert ("references vs payloads", "00:19:30") in topics
        assert ("references vs payloads", "00:20:00") in topics
        assert ("file formats", "00:21:00") in topics  # "USD a ... binary" (cleaned to USDA)
        assert all(f["level"] == "likely" for f in claims["flags"])
        assert "load or unload" in next(f for f in claims["flags"] if f["topic"] == "references vs payloads")["correct_idea"]
        with_reminders = sl.check_claims(session=3, include_reminders=True, search_dirs=[root])
        assert with_reminders["count"] >= claims["count"]


def test_format_swap_split_over_two_subtitle_lines_is_caught():
    lines = ["For the USDA or normally called as USD, it's a Machine Format.", "The USDC.",
             "is text format where humans can read it.", "And the USDZ is a combined format."]
    sentences = sl._sentences([sl.Cue(i * 5.0, i * 5.0 + 4, text) for i, text in enumerate(lines)])
    flags = [f for f in sl._flags(sentences) if f["topic"] == "file formats"]
    assert [f["at"] for f in flags] == ["00:00:00", "00:00:05"], flags


def test_claim_patterns_do_not_flag_correct_statements():
    good = [
        "References and payloads can both be overridden locally.",
        "USDA is text and USDC is binary.",
        "A prim is an object in the stage.",
        "Scaling the Xform does not change the points.",
    ]
    cue_text = sl._sentences([sl.Cue(i * 5.0, i * 5.0 + 4, text) for i, text in enumerate(good)])
    assert [f for f in sl._flags(cue_text) if f["level"] == "likely"] == []


def test_bad_inputs_are_refused_without_touching_files():
    with _Folder() as root:
        before = _bytes(root)
        assert sl.session_plan(search_dirs=[root])["ok"] is False
        assert "No transcript for session 9" in sl.session_plan(session=9, search_dirs=[root])["error"]
        assert sl.session_plan(session=3, minutes=2, search_dirs=[root])["ok"] is False
        assert sl.session_plan(session=3, minutes=True, search_dirs=[root])["ok"] is False
        assert sl.lesson(0, session=3, search_dirs=[root])["ok"] is False
        assert sl.lesson(9, session=3, search_dirs=[root])["ok"] is False
        assert sl.session_plan(path=str(root / "notes.txt"), search_dirs=[root])["ok"] is False
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "Session7(x).srt"
            outside.write_text(_class(10), encoding="utf-8")
            refused = sl.session_plan(path=str(outside), search_dirs=[root])
            assert refused["ok"] is False and "outside" in refused["error"]
        by_path = sl.session_plan(path=str(root / "Session3(Assets & Environment Creation)_zoomed_HD.srt"),
                                  search_dirs=[root])
        assert by_path["ok"] is True
        assert _bytes(root) == before


def test_oversized_and_empty_transcripts():
    with _Folder() as root:
        empty = root / "Session5(empty).srt"
        empty.write_text("", encoding="utf-8")
        assert "no readable" in sl.session_plan(session=5, search_dirs=[root])["error"]
        original = sl.MAX_TRANSCRIPT_BYTES
        sl.MAX_TRANSCRIPT_BYTES = 100
        try:
            assert "limit" in sl.session_plan(session=3, search_dirs=[root])["error"]
        finally:
            sl.MAX_TRANSCRIPT_BYTES = original


def test_env_override_sets_the_search_folders():
    with _Folder() as root:
        saved = os.environ.get(sl.ENV_DIRS)
        os.environ[sl.ENV_DIRS] = str(root)
        try:
            assert sl.default_dirs() == [root]
            assert sl.session_plan(session=2)["ok"] is True
        finally:
            if saved is None:
                os.environ.pop(sl.ENV_DIRS, None)
            else:
                os.environ[sl.ENV_DIRS] = saved


def test_feature_registration_hides_the_folder_parameter():
    for name in ("class_transcripts", "class_session_plan", "class_lesson", "class_check_claims"):
        spec = features.FEATURES[name]
        assert spec.location == "local" and spec.mutates is False
        assert "search_dirs" not in features.signature(name).parameters, name
    refused = features.invoke("class_session_plan", {"session": 3, "search_dirs": ["C:/"]})
    assert refused["ok"] is False
    refused = features.invoke("class_session_plan", {"session": 3, "options": {"search_dirs": ["C:/"]}})
    assert refused["ok"] is False


def test_phrases_route_to_the_lesson_tools():
    cases = {
        "teach me session 3": ("class_session_plan", {"session": 3}),
        "study session 3 in 10 minutes": ("class_session_plan", {"session": 3, "minutes": 10}),
        "session 3 lesson 2": ("class_lesson", {"session": 3, "number": 2}),
        "lesson 2 of session 3": ("class_lesson", {"session": 3, "number": 2}),
        "check session 3 for mistakes": ("class_check_claims", {"session": 3}),
        "list class transcripts": ("class_transcripts", {}),
    }
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for phrase, (tool, arguments) in cases.items():
        parsed = nl_tier8.parse(phrase)
        assert parsed == {"tool": tool, "arguments": arguments, "scene": False}, (phrase, parsed)
        assert router.route(phrase).target is RouteTarget.LEARN, phrase
    for phrase in ("what did we learn in session 2", "how do references work", "x" * 500 + " session 3 lesson 2"):
        assert nl_tier8.parse(phrase) is None or nl_tier8.parse(phrase)["tool"] not in {
            "class_lesson", "class_session_plan", "class_check_claims"}, phrase


def test_existing_routes_are_unchanged():
    spec = importlib.util.spec_from_file_location("round8_corpus", TESTS / "test_round8_integration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    phrases = module._corpus()
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    after = {phrase: router.route(phrase) for phrase in phrases}
    original = nl_tier8._session_route
    nl_tier8._session_route = lambda _clean: None
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier8._session_route = original
    changed = [phrase for phrase in phrases if before[phrase] != after[phrase]]
    assert not changed, changed


def test_study_session_workflow_names_the_tools_in_order():
    text = workflows.render_prompt("study_session", session=3, minutes=15)
    order = [text.index(name) for name in ("class_transcripts", "class_session_plan(session=3, minutes=15)",
                                            "class_lesson(session=3, number=1)", "learning_exam_answer")]
    assert order == sorted(order)


def test_real_session3_smoke_if_present():
    real = Path.home() / "Downloads" / "Session3(Assets & Environment Creation)_zoomed_HD.srt"
    if not real.is_file():
        return
    before = real.stat().st_mtime_ns, real.stat().st_size
    plan = sl.session_plan(session=3)
    assert plan["ok"] is True and plan["lesson_count"] >= 2
    assert (real.stat().st_mtime_ns, real.stat().st_size) == before
