from __future__ import annotations

import copy
import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from omniverse_scene_mcp.exam import daily, puzzles
from omniverse_scene_mcp.exam import bank as real_bank
from omniverse_scene_mcp.exam import blueprint as real_blueprint
from omniverse_scene_mcp.exam.progress import ProgressStore

TODAY = date(2026, 10, 21)
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)  # before TODAY noon UTC, so wrong answers are due
LEAK_KEYS = {"answer", "answers", "correct_answer", "explanations", "verify", "citations", "expect", "expected"}
TOOLS = ("learning_exam_answer", "learning_composition_puzzle_answer", "usd_playground_compose",
         "learning_exam_day_complete")

OBJECTIVES = {
    "1.6": {"text": "Describe the LIVERPS strength order in plain language.", "domain_name": "Composition",
            "domain_weight": 23, "keywords": ["liverps", "strength ordering", "inherits"], "high_yield": False,
            "estimated_exam_pct": 2.09},
    "2.1": {"text": "Register an extra prototype on a point instancer.", "domain_name": "Content Aggregation",
            "domain_weight": 10, "keywords": ["pointinstancer", "prototype", "protoindices"], "high_yield": False,
            "estimated_exam_pct": 2.0},
    "5.1": {"text": "Create a primvar on a mesh with a suitable interpolation.", "domain_name": "Data Modeling",
            "domain_weight": 13, "keywords": ["primvar", "interpolation", "displaycolor"], "high_yield": True,
            "estimated_exam_pct": 4.17},
    "8.1": {"text": "Create a primvar on a mesh with a suitable interpolation.", "domain_name": "Visualization",
            "domain_weight": 8, "keywords": ["primvar", "interpolation"], "high_yield": True,
            "estimated_exam_pct": 4.17},
    "4.3": {"text": "Weigh usdc against usda for speed and size.", "domain_name": "Data Exchange",
            "domain_weight": 15, "keywords": ["usda", "usdc", "crate"], "high_yield": True,
            "estimated_exam_pct": 3.6},
}


class FakeBlueprint:
    """study_plan/objective/domain stand-in: study days cycle 1.6, 2.1 and 5.1(+8.1); weekly reviews."""

    MAX_SCHEDULE_DAYS = 60

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def study_plan(self, summary, days, minutes):
        self.calls.append((copy.deepcopy(summary), days, minutes))
        days = max(1, min(int(days), self.MAX_SCHEDULE_DAYS))
        schedule = []
        for number in range(1, days + 1):
            if number == days and days >= 3:
                kind = "final_review"
                blocks = [{"activity": "light review", "objectives": ["5.1", "4.3"], "minutes": minutes - 15},
                          {"activity": "exam logistics", "minutes": 15}]
            elif number % 7 == 0:
                kind = "review"
                blocks = [{"activity": "mixed timed practice", "domains": ["Composition", "Data Modeling"],
                           "minutes": 40},
                          {"activity": "revisit high-yield objectives", "objectives": ["5.1", "4.3"], "minutes": 20}]
            else:
                kind = "study"
                pass_name = "learn" if number < 4 else "revisit"
                blocks = [
                    {"objective": "1.6", "also": [], "domain": "Composition", "minutes": 20, "pass": pass_name},
                    {"objective": "2.1", "also": [], "domain": "Content Aggregation", "minutes": 20, "pass": "learn"},
                    {"objective": "5.1", "also": ["8.1"], "domain": "Data Modeling", "minutes": 20, "pass": "learn"},
                ]
            schedule.append({"day": number, "type": kind, "minutes": sum(b["minutes"] for b in blocks),
                             "blocks": blocks})
        return {"minutes_per_day": minutes, "scheduled_days": days, "schedule": schedule,
                "focus_objectives": [{"id": oid, "text": o["text"], "domains": [o["domain_name"]], "reasons": []}
                                     for oid, o in OBJECTIVES.items()],
                "notes": ["Fake plan note.", "Focus order: standard text that is filtered out."]}

    def objective(self, oid):
        found = OBJECTIVES.get(oid)
        return dict(found, id=oid) if found else None

    def domain(self, name):
        return {"name": name, "weight": 10, "readings": [
            {"title": f"{name} reading {i}", "url": f"https://openusd.org/release/{i}.html"} for i in range(3)]}


def _question(qid, domain, objectives, **extra):
    item = {"id": qid, "domain": domain, "objective_ids": list(objectives), "type": "single", "difficulty": "easy",
            "stem": f"Stem of {qid}?", "options": ["w", "x", "y", "z"], "answer": [1],
            "explanations": ["a", "b", "c", "d"], "citations": ["https://openusd.org/release/x.html"]}
    item.update(extra)
    return item


FOREST = "#usda 1.0\ndef PointInstancer \"Rocks\" {}\n"
SHOT = "#usda 1.0\ndef Xform \"World\" {}\n"
ITEMS = [
    _question("comp-101", "Composition", ["1.6"], usda={"shot.usda": SHOT, "anim.usda": SHOT}, root="shot.usda",
              verify={"checks": [{"path": "/World/Drone.xformOp:translate", "time": 15, "expect": [50, 0, 0]}]}),
    _question("comp-102", "Composition", ["1.6"], usda={"shot.usda": SHOT},
              verify={"checks": [{"path": "/World", "expect": {"type": "Xform"}}]}),
    _question("comp-103", "Composition", ["1.6"], usda={"shot.usda": SHOT, "x.usda": SHOT}, root="shot.usda",
              verify={"mute": ["x.usda"], "checks": [{"path": "/World.size", "expect": 1}]}),
    _question("comp-104", "Composition", ["1.6"]),
    _question("comp-105", "Composition", ["1.6", "1.8"]),
    _question("ca-101", "Content Aggregation", ["2.1"], usda={"forest.usda": FOREST},
              verify={"checks": [{"path": "/Forest/Rocks.prototypes", "expect": ["/Forest/Rocks/Prototypes/Boulder"]}]}),
    _question("ca-102", "Content Aggregation", ["2.1"]),
    _question("dm-101", "Data Modeling", ["5.1"]),
    _question("dm-102", "Data Modeling", ["5.1"]),
    _question("vis-101", "Visualization", ["8.1"]),
    _question("vis-102", "Visualization", ["8.1"]),
    _question("dx-101", "Data Exchange", ["4.3"]),
]


class FakeBank:
    """quiz/load_bank stand-in; quiz deliberately returns answers so tests prove daily strips them."""

    DOMAINS = {"Composition": {}, "Content Aggregation": {}, "Data Modeling": {}, "Visualization": {},
               "Data Exchange": {}}

    def __init__(self, items=None) -> None:
        self.items = copy.deepcopy(items if items is not None else ITEMS)
        self.calls: list[dict] = []

    def load_bank(self):
        return copy.deepcopy(self.items)

    def quiz(self, domain=None, objective=None, count=5, seed=None, exclude_ids=None, prefer_ids=None):
        self.calls.append({"domain": domain, "objective": objective, "count": count, "prefer_ids": prefer_ids})
        if objective == "9.9":
            raise ValueError("unknown objective '9.9'")
        excluded = set(exclude_ids or [])
        preferred = list(prefer_ids or [])
        pool = [it for it in self.items if it["id"] not in excluded
                and (domain is None or it["domain"] == domain)
                and (objective is None or objective in it["objective_ids"])]
        pool.sort(key=lambda it: (it["id"] not in preferred, it["id"]))
        return {"questions": [dict(it) for it in pool[:count]]}


class FakeKnowledge:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[tuple[str, int]] = []

    def search(self, query, top_k=5):
        self.queries.append((query, top_k))
        if self.fail:
            raise RuntimeError("index missing")
        return [{"title": f"Hit {i}", "source": "usd_docs", "kind": "doc", "url_or_path": f"https://openusd.org/{i}",
                 "snippet": "s" * 1000, "score": 1.0} for i in range(5)]


def _fake(**overrides):
    parts = {"bank": FakeBank(), "blueprint": FakeBlueprint(), "knowledge": FakeKnowledge(), "today": TODAY}
    parts.update(overrides)
    return parts


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def _pxr_available() -> bool:
    try:
        from pxr import Usd  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------- plan, settings and day selection


def test_first_use_sets_plan_start_and_uses_defaults():
    store = ProgressStore(memory_only=True)
    parts = _fake()
    lesson = daily.build_day(None, store=store, **parts)
    assert lesson["ok"] is True and lesson["day"] == 1 and lesson["date"] == TODAY.isoformat()
    assert lesson["total_days"] == daily.DEFAULT_DAYS and lesson["minutes_per_day"] == daily.DEFAULT_MINUTES
    assert parts["blueprint"].calls[-1] == (None, daily.DEFAULT_DAYS, daily.DEFAULT_MINUTES)
    settings = store.get_settings()
    assert settings["plan_start"] == TODAY.isoformat()
    assert settings["days_until_exam"] is None and settings["minutes_per_day"] is None
    later = daily.build_day(None, store=store, **_fake(today=TODAY + timedelta(days=3)))
    assert later["plan_start"] == TODAY.isoformat() and later["calendar_day"] == 4
    assert later["day"] == 1 and later["behind_by_days"] == 3


def test_saved_settings_and_progress_feed_the_plan():
    store = ProgressStore(memory_only=True)
    store.update_settings(plan_start=TODAY.isoformat(), days_until_exam=10, minutes_per_day=40)
    parts = _fake()
    lesson = daily.build_day(2, store=store, **parts)
    assert lesson["total_days"] == 10 and lesson["minutes_per_day"] == 40
    assert parts["blueprint"].calls[-1][1:] == (10, 40)
    store.record_answer("dm-101", domain="Data Modeling", objective_ids=["5.1"], chosen="A", correct="B",
                        is_correct=False)
    daily.build_day(2, store=store, **parts)
    summary = parts["blueprint"].calls[-1][0]
    assert summary["totals"]["answered"] == 1 and "Data Modeling" in summary["domains"]
    store.update_settings(days_until_exam=None, exam_date=(TODAY + timedelta(days=12)).isoformat())
    assert daily.build_day(1, store=store, **_fake())["total_days"] == 12
    assert "Fake plan note." in lesson["notes"]
    assert not any(note.startswith("Focus order") for note in lesson["notes"])


def test_day_none_picks_next_uncompleted_and_invalid_days_are_refused():
    store = ProgressStore(memory_only=True)
    store.update_settings(plan_start=TODAY.isoformat(), days_until_exam=5, completed_days=[1, 2, 4])
    assert daily.build_day(None, store=store, **_fake())["day"] == 3
    assert daily.build_day(4, store=store, **_fake())["completed"] is True
    assert daily.build_day("2", store=store, **_fake())["day"] == 2
    for bad in (0, 6, -1, True, "two", 2.5):
        result = daily.build_day(bad, store=store, **_fake())
        assert result["ok"] is False and "1 to 5" in result["message"], bad
    store.update_settings(completed_days=[1, 2, 3, 4, 5])
    done = daily.build_day(None, store=store, **_fake())
    assert done["ok"] is True and done["plan_complete"] is True and "blocks" not in done
    try:
        daily.build_day(1, store=None, **_fake())
    except TypeError:
        pass
    else:
        raise AssertionError("build_day accepted a missing store")


def test_complete_day_tracks_streak_next_day_and_is_idempotent():
    store = ProgressStore(memory_only=True)
    store.update_settings(plan_start=TODAY.isoformat(), days_until_exam=4)
    first = daily.complete_day(1, store, today=TODAY, blueprint=FakeBlueprint())
    assert first["ok"] and first["day_streak"] == 1 and first["next_day"] == 2 and first["remaining_days"] == 3
    skip = daily.complete_day(3, store, today=TODAY, blueprint=FakeBlueprint())
    assert skip["day_streak"] == 1 and skip["next_day"] == 2
    nxt = daily.complete_day(None, store, today=TODAY, blueprint=FakeBlueprint())
    assert nxt["day"] == 2 and nxt["day_streak"] == 2 and nxt["next_day"] == 4
    again = daily.complete_day(2, store, today=TODAY, blueprint=FakeBlueprint())
    assert again["already_completed"] is True and again["completed_days"] == [1, 2, 3]
    last = daily.complete_day(4, store, today=TODAY, blueprint=FakeBlueprint())
    assert last["plan_complete"] is True and last["next_day"] is None and last["day_streak"] == 4
    assert daily.complete_day(None, store, today=TODAY, blueprint=FakeBlueprint())["ok"] is False
    assert daily.complete_day(9, store, today=TODAY, blueprint=FakeBlueprint())["ok"] is False
    assert store.get_settings()["completed_days"] == [1, 2, 3, 4]
    store.record_answer("dm-101", domain="Data Modeling", objective_ids=["5.1"], chosen="A", correct="A",
                        is_correct=True)
    assert daily.complete_day(1, store, blueprint=FakeBlueprint())["study_streak_days"] == 1


def test_set_exam_date_replans_from_today():
    store = ProgressStore(memory_only=True)
    store.update_settings(plan_start="2026-10-01", days_until_exam=30, minutes_per_day=90, completed_days=[1, 2])
    result = daily.set_exam_date("2026-11-20", 45, store, today=TODAY, blueprint=FakeBlueprint())
    assert result["ok"] is True and result["days_until_exam"] == 30 and result["scheduled_days"] == 30
    assert result["cleared_completed_days"] == 2 and result["previous"]["minutes_per_day"] == 90
    assert result["settings"] == {"exam_date": "2026-11-20", "plan_start": TODAY.isoformat(), "days_until_exam": 30,
                                  "minutes_per_day": 45, "completed_days": []}
    assert len(result["outline"]) == 30 and result["outline"][0]["date"] == TODAY.isoformat()
    assert result["outline"][0]["objectives"] == ["1.6", "2.1", "5.1"]
    by_days = daily.set_exam_date(14, None, store, today=TODAY, blueprint=FakeBlueprint())
    assert by_days["settings"]["exam_date"] == (TODAY + timedelta(days=14)).isoformat()
    assert by_days["settings"]["minutes_per_day"] == 45 and by_days["scheduled_days"] == 14
    far = daily.set_exam_date(90, None, store, today=TODAY, blueprint=FakeBlueprint())
    assert far["days_until_exam"] == 90 and far["scheduled_days"] == 60
    assert daily.build_day(None, store=store, **_fake())["total_days"] == 60
    plain = daily.set_exam_date(None, None, store, today=TODAY, blueprint=FakeBlueprint())
    assert plain["settings"]["exam_date"] is None and plain["days_until_exam"] == daily.DEFAULT_DAYS
    before = store.get_settings()
    for when, minutes in (("2026-10-21", None), ("2026-10-01", None), ("soon", None), (0, None), (10, 5),
                          (10, "lots"), (10, 481)):
        refused = daily.set_exam_date(when, minutes, store, today=TODAY, blueprint=FakeBlueprint())
        assert refused["ok"] is False and refused["message"], (when, minutes)
    assert store.get_settings() == before


# ---------------------------------------------------------------- study-day content


def test_study_blocks_have_objective_explanation_docs_exercise_and_quiz():
    store = ProgressStore(memory_only=True)
    parts = _fake()
    lesson = daily.build_day(1, store=store, **parts)
    assert lesson["type"] == "study" and len(lesson["blocks"]) == 3
    first, second, third = lesson["blocks"]
    assert first["objective"]["id"] == "1.6" and first["objective"]["domain"] == "Composition"
    assert first["objective"]["text"] == OBJECTIVES["1.6"]["text"]
    assert first["explanation"].startswith("Goal: be able to describe the LIVERPS strength order")
    assert "Composition is 23% of the exam" in first["explanation"] and "liverps" in first["explanation"]
    assert len(first["docs"]) == 3 and all(len(hit["snippet"]) <= daily.SNIPPET_CAP for hit in first["docs"])
    assert parts["knowledge"].queries[0][1] == daily.DOC_HITS and "liverps" in parts["knowledge"].queries[0][0]
    assert len(first["readings"]) == daily.READINGS_PER_BLOCK
    assert first["exercise"]["kind"] == "puzzle"
    assert second["exercise"]["kind"] == "predict" and second["exercise"]["question_id"] == "ca-101"
    assert third["exercise"]["kind"] == "try_it" and third["exercise"]["tool"] == "usd_playground_compose"
    assert third["exercise"]["topic"] == "primvars and interpolation"
    assert [a["id"] for a in third["objective"]["also"]] == ["8.1"]
    assert first["quiz"]["question_ids"] == ["comp-101", "comp-102", "comp-103"]
    assert "ca-101" not in second["quiz"]["question_ids"] and second["quiz"]["question_ids"] == ["ca-102"]
    assert third["quiz"]["question_ids"] == ["dm-101", "dm-102", "vis-101"]
    all_ids = [qid for block in lesson["blocks"] for qid in block["quiz"]["question_ids"]]
    assert len(all_ids) == len(set(all_ids))
    assert all(tool in " ".join(lesson["how_to_continue"]) for tool in TOOLS)
    assert "learning_exam_day_complete(day=1)" in " ".join(lesson["how_to_continue"])
    assert not (_keys(lesson) & LEAK_KEYS)
    json.dumps(lesson)


def test_revisit_pass_uses_predict_and_answer_stays_server_side():
    store = ProgressStore(memory_only=True)
    lesson = daily.build_day(4, store=store, **_fake())
    item = lesson["blocks"][0]["exercise"]
    assert item["kind"] == "predict" and item["question_id"] == "comp-101"
    assert item["queries"] == ["/World/Drone.xformOp:translate@15"] and item["root"] == "shot.usda"
    assert "at time code 15" in item["prompt"] and set(item["layers"]) == {"shot.usda", "anim.usda"}
    assert "50" not in json.dumps(item)
    assert "comp-101" not in lesson["blocks"][0]["quiz"]["question_ids"]
    fake_bank = FakeBank()
    assert daily.check_prediction("comp-101", "(50, 0, 0)", bank=fake_bank)["correct"] is True
    assert daily.check_prediction("comp-101", [50.0, 0, 0], bank=fake_bank)["correct"] is True
    wrong = daily.check_prediction("comp-101", "[100, 0, 0]", bank=fake_bank)
    assert wrong["ok"] is True and wrong["correct"] is False and wrong["expected"] == [50, 0, 0]
    targets = daily.check_prediction("ca-101", '["/Forest/Rocks/Prototypes/Boulder"]', bank=fake_bank)
    assert targets["correct"] is True
    assert daily.check_prediction("comp-102", "Xform", bank=fake_bank)["ok"] is False
    assert daily.check_prediction("comp-103", "1", bank=fake_bank)["ok"] is False
    assert daily.check_prediction("nope", "1", bank=fake_bank)["ok"] is False


def test_puzzles_are_deterministic_and_hide_the_answer():
    store = ProgressStore(memory_only=True)
    one = daily.build_day(1, store=store, **_fake())["blocks"][0]["exercise"]
    again = daily.build_day(1, store=store, **_fake())["blocks"][0]["exercise"]
    other = daily.build_day(2, store=store, **_fake())["blocks"][0]["exercise"]
    assert one == again and one["puzzle"]["puzzle_id"] != other["puzzle"]["puzzle_id"]
    puzzle = one["puzzle"]
    assert puzzle["difficulty"] == "easy" and puzzle == puzzles.generate_puzzle(puzzle["seed"], "easy")
    assert "learning_composition_puzzle_answer" in one["how_to_answer"]
    assert not (_keys(one) & LEAK_KEYS)
    late = daily.build_day(20, store=store, **_fake())["blocks"]
    assert late[0]["exercise"]["kind"] == "predict"
    store.update_settings(days_until_exam=6)
    no_predict = FakeBank([i for i in ITEMS if i["id"] != "comp-101"])
    hard_day = daily.build_day(5, store=store, **_fake(bank=no_predict))
    assert hard_day["blocks"][0]["exercise"]["puzzle"]["difficulty"] == "hard"


def test_missing_or_failing_knowledge_and_bad_quiz_ids_are_skipped():
    store = ProgressStore(memory_only=True)
    no_docs = daily.build_day(1, store=store, **_fake(knowledge=None))
    assert all(block["docs"] == [] for block in no_docs["blocks"])
    assert any("doc hits were skipped" in note for note in no_docs["notes"])
    failing = daily.build_day(1, store=store, **_fake(knowledge=FakeKnowledge(fail=True)))
    assert failing["ok"] is True and all(block["docs"] == [] for block in failing["blocks"])
    assert any("RuntimeError" in note for note in failing["notes"])

    class OddBlueprint(FakeBlueprint):
        def study_plan(self, summary, days, minutes):
            plan = super().study_plan(summary, days, minutes)
            plan["schedule"][0]["blocks"] = [{"objective": "9.9", "also": [], "domain": "Mystery", "minutes": 20,
                                              "pass": "learn"}]
            return plan

        def objective(self, oid):
            raise KeyError(oid)

    # A fresh store: the schedule is frozen when a plan is first built, so the odd plan must be the first one.
    odd = daily.build_day(1, store=ProgressStore(memory_only=True), **_fake(blueprint=OddBlueprint()))
    block = odd["blocks"][0]
    assert block["objective"]["domain"] == "Mystery" and block["quiz"]["question_ids"] == []
    assert block["exercise"]["kind"] == "try_it" and block["exercise"]["topic"] == "layer stack basics"
    assert any("unknown objective" in note for note in odd["notes"])


def test_quiz_puts_due_cards_first():
    store = ProgressStore(memory_only=True)
    store.record_answer("dm-102", domain="Data Modeling", objective_ids=["5.1"], chosen="A", correct="B",
                        is_correct=False, now=T0)
    lesson = daily.build_day(1, store=store, **_fake())
    assert lesson["blocks"][2]["quiz"]["question_ids"][0] == "dm-102"
    assert lesson["progress"]["due_count"] == 1 and lesson["progress"]["answered"] == 1


# ---------------------------------------------------------------- review days


def test_review_day_returns_mixed_practice_and_revisit_lists():
    store = ProgressStore(memory_only=True)
    store.record_answer("dx-101", domain="Data Exchange", objective_ids=["4.3"], chosen="A", correct="B",
                        is_correct=False, now=T0)
    store.record_answer("dx-101", domain="Data Exchange", objective_ids=["4.3"], chosen="A", correct="B",
                        is_correct=False, now=T0)
    store.record_answer("liverps-easy-42", domain="Composition", objective_ids=["1.6"], chosen="3", correct="4",
                        is_correct=False, source="puzzle", now=T0)
    lesson = daily.build_day(7, store=store, **_fake())
    assert lesson["type"] == "review" and "blocks" in lesson
    practice = lesson["mixed_practice"]
    assert practice["domains"] == ["Composition", "Data Modeling"] and practice["minutes"] == 40
    assert practice["question_ids"][0] == "dx-101"
    assert len(practice["question_ids"]) == len(set(practice["question_ids"])) <= daily.MAX_MIXED
    assert set(practice["due_review_ids"]) == {"dx-101", "liverps-easy-42"}
    assert practice["due_puzzles"] == [puzzles.generate_puzzle(42, "easy")]
    revisit = {row["id"]: row for row in lesson["revisit"]}
    assert revisit["4.3"]["reasons"] == ["high_yield", "weak"] and revisit["4.3"]["accuracy"] == 0.0
    assert revisit["5.1"]["text"] == OBJECTIVES["5.1"]["text"]
    assert all(tool in " ".join(lesson["how_to_continue"]) for tool in TOOLS)
    assert not (_keys(lesson) & LEAK_KEYS)

    final = daily.build_day(30, store=store, **_fake())
    assert final["type"] == "final_review" and final["logistics"]["activity"] == "exam logistics"
    assert [row["id"] for row in final["revisit"]] == ["5.1", "4.3", "1.6"]
    assert final["mixed_practice"]["question_ids"][0] == "dx-101"
    assert len(final["mixed_practice"]["question_ids"]) <= daily.MAX_FINAL_QUESTIONS


# ---------------------------------------------------------------- real bank and blueprint (read-only)


def test_real_bank_and_blueprint_plan_never_leaks_answers():
    store = ProgressStore(memory_only=True)
    kinds = set()
    for number in range(1, 31):
        lesson = daily.build_day(number, store=store, today=TODAY)
        assert lesson["ok"] is True and lesson["day"] == number
        assert not (_keys(lesson) & LEAK_KEYS), number
        assert len(json.dumps(lesson)) <= daily.MAX_OUTPUT_CHARS
        for block in lesson.get("blocks") or []:
            if "exercise" in block:
                kinds.add(block["exercise"]["kind"])
                assert len(block["quiz"]["question_ids"]) <= daily.QUIZ_PER_BLOCK
        if lesson["type"] != "study":
            assert lesson["mixed_practice"]["question_ids"] and lesson["revisit"]
    assert kinds == {"puzzle", "predict", "try_it"}
    assert store.get_settings()["plan_start"] == TODAY.isoformat()
    big = ProgressStore(memory_only=True)
    big.update_settings(minutes_per_day=480)
    heavy = daily.build_day(1, store=big, today=TODAY)
    assert len(heavy["blocks"]) == 6 and len(json.dumps(heavy)) <= daily.MAX_OUTPUT_CHARS


def test_real_predict_items_match_the_composed_value():
    if not _pxr_available():
        return
    from omniverse_scene_mcp.exam import sandbox

    checked = 0
    for item in real_bank.load_bank():
        check = daily._predict_check(item)
        if check is None:
            continue
        composed = sandbox.compose(item["usda"], check["root"], [check["query"]])
        query = composed["queries"][0]
        actual = query.get("targets") if check["targets"] else query.get("value")
        assert query["ok"] and daily._same_value(actual, check["expect"]), (item["id"], actual, check["expect"])
        checked += 1
    assert checked >= 5


def test_try_it_starters_compose_in_the_sandbox():
    starters = [*daily._STARTERS, daily._GENERIC_STARTER]
    for starter in starters:
        assert sum(len(text) for text in starter["layers"].values()) < 2000, starter["topic"]
    if not _pxr_available():
        return
    from omniverse_scene_mcp.exam import sandbox

    for starter in starters:
        root = starter.get("root") or next(iter(starter["layers"]))
        composed = sandbox.compose(starter["layers"], root, starter["queries"])
        assert composed["stage_opened"], starter["topic"]
        assert all(q["ok"] for q in composed["queries"]), (starter["topic"], composed["queries"])
        if starter["topic"] == "asset paths and resolution":
            assert any(e["kind"] == "composition" for e in composed["errors"])  # broken on purpose
        else:
            assert composed["ok"], (starter["topic"], composed["errors"])


def test_starter_matching_follows_objective_keywords():
    picks = {}
    for objective_id in ("5.1", "5.2", "5.3", "5.4", "5.6", "6.3", "7.2", "3.4", "2.1", "2.4", "8.3", "8.4", "4.3"):
        found = real_blueprint.objective(objective_id)
        starter = daily._match_starter({"keywords": found["keywords"], "text": found["text"]})
        picks[objective_id] = starter["topic"] if starter else None
    assert picks["5.1"] == "primvars and interpolation"
    assert picks["5.2"] == "value types" and picks["5.3"] == "metadata"
    assert picks["5.4"].startswith("properties") and picks["5.6"].startswith("extents")
    assert picks["6.3"].startswith("asset paths") and picks["7.2"].startswith("model kinds")
    assert picks["3.4"].startswith("schemas") and picks["2.1"] == "point instancing"
    assert picks["2.4"] == "scenegraph instancing"
    assert picks["8.3"].startswith("UsdPreviewSurface") and picks["8.4"].startswith("UsdPreviewSurface")
    assert picks["4.3"] is None


def test_file_store_only_writes_its_own_file():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = ProgressStore(folder / "prep" / "progress.json")
        daily.build_day(None, store=store, today=TODAY)
        daily.complete_day(1, store, today=TODAY)
        daily.set_exam_date("2026-12-01", 30, store, today=TODAY)
        daily.build_day(None, store=store, today=TODAY)
        assert sorted(p.name for p in folder.iterdir()) == ["prep"]
        assert sorted(p.name for p in (folder / "prep").iterdir()) == ["progress.json", "progress.json.lock"]
        saved = json.loads((folder / "prep" / "progress.json").read_text(encoding="utf-8"))
        assert saved["settings"]["exam_date"] == "2026-12-01" and saved["settings"]["completed_days"] == []
        assert ProgressStore(folder / "prep" / "progress.json").get_settings()["minutes_per_day"] == 30


def test_old_progress_file_without_settings_still_builds_a_day():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "progress.json"
        old = {"schema_version": 1, "totals": {"answered": 1, "correct": 0, "seconds": 0.0, "timed": 0},
               "cards": {"dm-101": {"box": 1, "due_at": "2026-10-20T09:00:00Z", "domain": "Data Modeling",
                                    "objective_ids": ["5.1"]}},
               "domains": {"Data Modeling": {"answered": 1, "correct": 0}},
               "objectives": {"5.1": {"answered": 1, "correct": 0, "domain": "Data Modeling"}},
               "history": [], "active_days": ["2026-10-20"]}
        path.write_text(json.dumps(old), encoding="utf-8")
        store = ProgressStore(path)
        lesson = daily.build_day(None, store=store, **_fake())
        assert lesson["ok"] is True and lesson["progress"]["answered"] == 1
        assert store.recovered_backup is None
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["cards"]["dm-101"]["box"] == 1 and saved["settings"]["plan_start"] == TODAY.isoformat()


def test_output_cap_drops_snippets_then_question_bodies():
    store = ProgressStore(memory_only=True)
    saved = daily.MAX_OUTPUT_CHARS
    try:
        daily.MAX_OUTPUT_CHARS = 1000
        lesson = daily.build_day(1, store=store, **_fake())
    finally:
        daily.MAX_OUTPUT_CHARS = saved
    assert all("snippet" not in hit for block in lesson["blocks"] for hit in block["docs"])
    assert all(block["quiz"]["questions"] == [] and block["quiz"]["questions_omitted"] for block in lesson["blocks"])
    assert lesson["blocks"][0]["quiz"]["question_ids"]
    assert any("left out" in note for note in lesson["notes"])


def test_explain_objective_is_plain_and_bounded():
    text = daily.explain_objective("Pick a value type.", "Data Modeling", ["a", "b", "c", "d", "e", "f", "g"],
                                   "revisit", 13)
    assert text.startswith("Goal: be able to pick a value type.")
    assert "Data Modeling is 13% of the exam." in text and "g" not in text.split("Words to know:")[1].split(".")[0]
    assert daily.PASS_TIPS["revisit"] in text
    assert daily.explain_objective("", None).startswith("First pass")


# ---------------------------------------------------------------- regressions from the exam-tools review


def test_frozen_schedule_studies_every_planned_topic_as_progress_arrives():
    """Day N keeps its topics: a learner who follows the plan (and answers everything right) meets every topic."""
    store = ProgressStore(memory_only=True)
    plan = daily.set_exam_date(30, 40, store, today=TODAY)
    planned = {oid for row in plan["outline"] if row["type"] == "study" for oid in row.get("objectives", [])}
    assert len(planned) == len(real_blueprint.study_plan(None, 30, 40)["focus_objectives"])  # every topic fits
    items = {item["id"]: item for item in real_bank.load_bank()}
    studied = set()
    for number in range(1, 31):
        lesson = daily.build_day(None, store=store, today=TODAY)
        assert lesson["ok"] is True and lesson["day"] == number
        asked = list((lesson.get("mixed_practice") or {}).get("question_ids") or [])
        for block in lesson["blocks"] if lesson["type"] == "study" else []:
            studied.add(block["objective"]["id"])
            asked += block["quiz"]["question_ids"]
        for qid in asked:
            item = items[qid]
            store.record_answer(qid, domain=item["domain"], objective_ids=item["objective_ids"], chosen=item["answer"],
                                correct=item["answer"], is_correct=True, now=T0)
        daily.complete_day(number, store, today=TODAY)
    assert store.summary(now=T0)["totals"]["answered"] > 0
    assert studied == planned, (sorted(planned - studied), sorted(studied - planned))
    assert any("fixed when the plan was made" in note for note in lesson["notes"])


def test_excluded_ids_never_reach_quizzes_or_predict_exercises():
    store = ProgressStore(memory_only=True)
    blocked = {"comp-101", "comp-102", "ca-101", "dm-101"}
    lesson = daily.build_day(1, store=store, exclude_ids=blocked, **_fake())
    served = {qid for block in lesson["blocks"] for qid in block["quiz"]["question_ids"]}
    assert served and not served & blocked
    assert lesson["blocks"][0]["quiz"]["question_ids"] == ["comp-103", "comp-104", "comp-105"]
    assert lesson["blocks"][1]["exercise"]["kind"] != "predict"  # ca-101 was the only predict item for 2.1
    review = daily.build_day(7, store=store, exclude_ids=blocked, **_fake())
    assert review["mixed_practice"]["question_ids"] and not set(review["mixed_practice"]["question_ids"]) & blocked


def test_plan_shorter_than_the_exam_ends_without_exam_eve_logistics():
    store = ProgressStore(memory_only=True)
    far = daily.set_exam_date("2027-01-19", 60, store, today=TODAY)
    assert far["days_until_exam"] == 90 and far["scheduled_days"] == 60
    assert far["outline"][-1]["type"] != "final_review"
    store.update_settings(completed_days=list(range(1, 60)))
    done = daily.complete_day(60, store, today=TODAY)
    assert done["plan_complete"] is True and "Good luck" not in done["message"], done["message"]
    assert "re-plan with learning_exam_set_date" in done["message"] and "2027-01-19" in done["message"]
    near = daily.set_exam_date(30, 60, store, today=TODAY)
    assert near["outline"][-1]["type"] == "final_review"
    store.update_settings(completed_days=list(range(1, 30)))
    assert "Good luck in the exam!" in daily.complete_day(30, store, today=TODAY)["message"]


def test_check_prediction_accepts_relationship_targets_in_common_spellings():
    expected = "/Looks/Glaze/Surface"
    for answer in (expected, f"[{expected}]", f"[<{expected}>]", f"<{expected}>", f'["{expected}"]',
                   [f"<{expected}>"], [expected]):
        assert daily.check_prediction("vis-020", answer)["correct"] is True, answer
    for answer in ("/Looks/Glaze", f"[{expected}, /Looks/Glaze]", [], ""):
        assert daily.check_prediction("vis-020", answer)["correct"] is False, answer
