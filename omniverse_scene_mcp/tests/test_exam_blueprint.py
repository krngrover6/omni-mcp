from __future__ import annotations

import json
import os
import re
from pathlib import Path

from omniverse_scene_mcp.exam import blueprint as bp

GUIDE_TXT = Path(
    os.environ.get(
        "NCP_OUSD_GUIDE_TXT",
        r"C:/Users/krngr/AppData/Local/Temp/claude/C--Users-krngr-Omniverse/"
        r"95cdf3be-6b57-4a88-a9a9-b1b598787514/scratchpad/mcp-expansion/cert-research/guide.txt",
    )
)
OBJECTIVE_LINE = r"^[ \t]*([1-8]\.\d{1,2})[ \t]+(\S.*)$"
GUIDE_ORDER = [
    "Composition",
    "Content Aggregation",
    "Customizing USD",
    "Data Exchange",
    "Data Modeling",
    "Debugging and Troubleshooting",
    "Pipeline Development",
    "Visualization",
]


def _guide_text() -> str | None:
    try:
        return GUIDE_TXT.read_text(encoding="utf-8")
    except OSError:
        return None


def _words(text: str) -> list[str]:
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.findall(r"[a-z0-9]+", text.lower())


def _all_objectives() -> list[dict]:
    return [obj for dom in bp.load_blueprint()["domains"] for obj in dom["objectives"]]


def _plan_progress(**domains: tuple[int, int]) -> dict:
    return {"domains": {name.replace("_", " "): {"answered": a, "correct": c} for name, (a, c) in domains.items()}}


def test_weights_sum_to_100() -> None:
    assert sum(dom["weight"] for dom in bp.load_blueprint()["domains"]) == 100


def test_domains_follow_guide_order_and_weights() -> None:
    domains = bp.load_blueprint()["domains"]
    assert [d["id"] for d in domains] == list(range(1, 9))
    assert [d["name"] for d in domains] == GUIDE_ORDER
    weights = {d["name"]: d["weight"] for d in domains}
    assert weights["Composition"] == 23 and weights["Customizing USD"] == 6
    guide = _guide_text()
    if guide is None:
        return
    found = dict(re.findall(r"^([A-Za-z ]+):\s*\n\s*Exam Weight (\d+)%", guide, re.M))
    assert {name: int(w) for name, w in found.items()} == weights


def test_objective_count_and_numbering_match_guide() -> None:
    ids = [obj["id"] for obj in _all_objectives()]
    assert len(ids) == 55
    assert len(set(ids)) == len(ids)
    for dom in bp.load_blueprint()["domains"]:
        numbers = [obj["id"].split(".") for obj in dom["objectives"]]
        assert all(major == str(dom["id"]) for major, _ in numbers)
        assert [int(minor) for _, minor in numbers] == list(range(1, len(numbers) + 1))
    guide = _guide_text()
    if guide is not None:
        guide_ids = [oid for oid, _ in re.findall(OBJECTIVE_LINE, guide, re.M)]
        assert sorted(guide_ids) == sorted(ids)
    assert bp.blueprint()["distinct_topic_count"] == 44


def test_every_domain_has_objectives_and_readings() -> None:
    for dom in bp.load_blueprint()["domains"]:
        assert dom["objectives"], dom["name"]
        assert len(dom["description"]) > 40
        assert dom["readings"], dom["name"]
        for reading in dom["readings"]:
            assert reading["title"].strip()
            assert reading["url"].startswith("https://") and " " not in reading["url"]
            assert reading["kind"]


def test_objective_texts_and_keywords() -> None:
    for obj in _all_objectives():
        assert len(obj["text"]) > 20
        assert 3 <= len(obj["keywords"]) <= 8, obj["id"]
        assert all(kw == kw.lower() and kw.strip() for kw in obj["keywords"]), obj["id"]
        assert len(set(obj["keywords"])) == len(obj["keywords"])


def test_high_yield_ids_repeat_across_domains() -> None:
    data = bp.load_blueprint()
    known = {obj["id"] for obj in _all_objectives()}
    high_yield = data["high_yield"]
    assert high_yield and len(set(high_yield)) == len(high_yield)
    assert set(high_yield) <= known
    for group in data["shared_objectives"]:
        assert set(group) <= set(high_yield)
        assert len({oid.split(".")[0] for oid in group}) == len(group)
    for oid in ("1.8", "6.2", "4.3", "7.3", "5.3", "7.6", "5.1", "8.1", "5.5", "6.4"):
        info = bp.objective(oid)
        assert info["high_yield"] and info["shared_with"]
        assert oid in bp.objective(info["shared_with"][0])["shared_with"]


def test_exam_facts() -> None:
    exam = bp.load_blueprint()["exam"]
    assert exam["code"] == "NCP-OUSD"
    assert exam["format"]["questions"] == "60-70"
    assert exam["price_usd"] == 200
    assert exam["time_limit_minutes"] == 120 and "90" in exam["time_limit_notes"]
    assert exam["retake_policy"] == {"wait_days": 14, "max_attempts_per_12_months": 5}
    assert exam["validity_years"] == 2
    assert exam["scoring"]["passing_score_published"] is False
    assert "Certiverse" in exam["delivery"]
    assert "USD Composer is not examined" in exam["scope_note"]
    assert exam["study_guide"]["url"].startswith("https://dam-cdn.nvd.orangelogic.com/")


def test_objective_and_domain_lookup() -> None:
    info = bp.objective("1.8")
    assert info["domain_name"] == "Composition" and info["domain_weight"] == 23
    assert info["shared_with"] == ["6.2"]
    assert info["estimated_exam_pct"] == round(23 / 11 + 11 / 5, 2)
    assert bp.objective(" 2.3 ")["id"] == "2.3"
    assert bp.objective("9.1") is None and bp.objective(None) is None
    assert bp.domain(4)["name"] == "Data Exchange"
    assert bp.domain("4")["name"] == "Data Exchange"
    assert bp.domain("customizing usd")["id"] == 3
    assert bp.domain("debugging")["id"] == 6
    assert bp.domain("data_modeling")["id"] == 5
    assert bp.domain("data") is None
    assert bp.domain("nonsense") is None and bp.domain(True) is None
    assert set(bp.domain(1)["high_yield_ids"]) == {"1.8", "1.10"}


def test_returned_dicts_are_copies() -> None:
    bp.objective("1.1")["text"] = "changed"
    bp.domain(1)["objectives"].clear()
    bp.blueprint()["domains"].clear()
    assert bp.objective("1.1")["text"] != "changed"
    assert len(bp.domain(1)["objectives"]) == 11
    compact = bp.blueprint(include_objectives=False)
    assert "objectives" not in compact["domains"][0]
    assert compact["domains"][0]["objective_ids"][0] == "1.1"
    assert compact["objective_count"] == 55


def test_match_objectives_ranking() -> None:
    assert bp.match_objectives("How do I hide instances of a PointInstancer?")[0] == "2.3"
    assert bp.match_objectives("usda vs usdc trade-offs")[:2] == ["4.3", "7.3"]
    assert set(bp.match_objectives("add a displayColor primvar to a mesh")[:2]) == {"5.1", "8.1"}
    assert bp.match_objectives("TfDebug and Trace profiling")[0] == "6.5"
    assert bp.match_objectives("LIVRPS")[0] == "1.6"
    assert bp.match_objectives("Sdf ChangeBlock")[0] == "6.1"
    assert len(bp.match_objectives("instance prototype", limit=2)) == 2
    assert bp.match_objectives("") == [] and bp.match_objectives("zzzz qqqq") == []


def test_study_plan_without_progress_follows_weights() -> None:
    plan = bp.study_plan()
    rows = {row["name"]: row for row in plan["domains"]}
    assert plan["progress_used"] is False and "schedule" not in plan
    assert sum(row["minutes_per_day"] for row in plan["domains"]) == 60
    for dom in bp.load_blueprint()["domains"]:
        row = rows[dom["name"]]
        assert row["share_pct"] == dom["weight"]
        assert abs(row["minutes_per_day"] - dom["weight"] * 0.6) < 1
        assert row["boost"] == 1.0 and row["total_minutes"] is None
    assert [row["name"] for row in plan["domains"]][0] == "Composition"
    focus = plan["focus_objectives"]
    assert len(focus) == 44
    assert all("high_yield" in item["reasons"] for item in focus[:11])
    assert not any("high_yield" in item["reasons"] for item in focus[11:])


def test_study_plan_boosts_weak_domains() -> None:
    progress = _plan_progress(Composition=(10, 3), Visualization=(10, 10), Data_Exchange=(10, 8))
    plan = bp.study_plan(progress, minutes_per_day=90)
    rows = {row["name"]: row for row in plan["domains"]}
    assert plan["progress_used"] is True
    assert rows["Composition"]["status"] == "weak" and rows["Composition"]["boost"] == 2.0
    assert rows["Visualization"]["status"] == "strong" and rows["Visualization"]["boost"] == 0.75
    assert rows["Data Exchange"]["status"] == "on_track" and rows["Data Exchange"]["boost"] == 1.0
    assert rows["Customizing USD"]["status"] == "untested"
    raw = {name: row["weight"] * row["boost"] for name, row in rows.items()}
    for name, row in rows.items():
        assert abs(row["share_pct"] - 100 * raw[name] / sum(raw.values())) < 0.06
    assert rows["Composition"]["share_pct"] > 23 and rows["Visualization"]["share_pct"] < 8
    assert sum(row["minutes_per_day"] for row in rows.values()) == 90
    assert abs(sum(row["share_pct"] for row in rows.values()) - 100) < 0.5
    assert rows["Composition"]["accuracy_pct"] == 30.0


def test_study_plan_focus_puts_weak_and_high_yield_first() -> None:
    focus = bp.study_plan(_plan_progress(Customizing_USD=(10, 2), Composition=(10, 9)))["focus_objectives"]
    order = [item["id"] for item in focus]
    assert order[0] in {"3.6", "3.5"} and order[1] in {"3.6", "3.5"}
    high_yield_end = max(i for i, item in enumerate(focus) if "high_yield" in item["reasons"])
    customizing = [i for i, item in enumerate(focus) if item["domains"] == ["Customizing USD"]]
    assert len(customizing) == 6 and min(customizing) == high_yield_end + 1
    assert max(customizing) < order.index("1.1")


def test_study_plan_schedule_math() -> None:
    plan = bp.study_plan(days_until_exam=30, minutes_per_day=60)
    schedule = plan["schedule"]
    assert plan["scheduled_days"] == 30 and len(schedule) == 30
    assert [day["day"] for day in schedule] == list(range(1, 31))
    for day in schedule:
        assert day["minutes"] == 60 == sum(block["minutes"] for block in day["blocks"])
    assert schedule[-1]["type"] == "final_review"
    assert [day["day"] for day in schedule if day["type"] == "review"] == [7, 14, 21, 28]
    assert sum(row["total_minutes"] for row in plan["domains"]) == 30 * 60
    study_blocks = [b for day in schedule if day["type"] == "study" for b in day["blocks"]]
    assert len(study_blocks) == 25 * 3
    first_pass = [b["objective"] for b in study_blocks if b["pass"] == "learn"]
    assert first_pass == [item["id"] for item in plan["focus_objectives"]]
    assert plan["unscheduled_objectives"] == []


def test_study_plan_caps_and_edge_days() -> None:
    long_plan = bp.study_plan(days_until_exam=200)
    assert long_plan["scheduled_days"] == 60 and len(long_plan["schedule"]) == 60
    assert long_plan["days_until_exam"] == 200
    assert any("60 days" in note for note in long_plan["notes"])
    short = bp.study_plan(days_until_exam=1, minutes_per_day=45)
    assert len(short["schedule"]) == 1 and short["schedule"][0]["type"] == "study"
    assert short["schedule"][0]["minutes"] == 45
    assert len(short["unscheduled_objectives"]) == 44 - len(short["schedule"][0]["blocks"])
    assert bp.study_plan(days_until_exam=0)["scheduled_days"] == 1
    assert "schedule" not in bp.study_plan(days_until_exam="soon")
    clamped = bp.study_plan(minutes_per_day=5000)
    assert clamped["minutes_per_day"] == 480
    assert sum(row["minutes_per_day"] for row in clamped["domains"]) == 480


def test_study_plan_tolerates_bad_progress() -> None:
    for summary in ("garbage", {"domains": None}, {"answered": 3}, {"domains": {"Composition": "x"}}):
        plan = bp.study_plan(summary)
        assert sum(row["minutes_per_day"] for row in plan["domains"]) == 60
    plan = bp.study_plan({"domains": {"Composition": {"answered": "ten"}, "Unknown": {"answered": 4}}})
    assert any("Unknown" in note for note in plan["notes"])
    rows = {row["name"]: row for row in bp.study_plan({"domains": {"composition": {"answered": 5, "correct": 99}}})["domains"]}
    assert rows["Composition"]["correct"] == 5 and rows["Composition"]["status"] == "strong"
    rows = {row["id"]: row for row in bp.study_plan({"domains": [{"id": 6, "answered": 8, "correct": 2}]})["domains"]}
    assert rows[6]["status"] == "weak"


def test_study_plan_notes_say_target_is_self_set() -> None:
    notes = " ".join(bp.study_plan(target_pct=70)["notes"]).lower()
    assert "no passing score" in notes and "self-set" in notes
    assert bp.study_plan(target_pct="bad")["target_accuracy_pct"] == 80.0


def test_outputs_are_json_serializable() -> None:
    json.dumps(bp.blueprint())
    json.dumps(bp.blueprint(include_objectives=False))
    json.dumps(bp.domain(1))
    json.dumps(bp.objective("8.4"))
    big = json.dumps(bp.study_plan(_plan_progress(Composition=(20, 5)), days_until_exam=60, minutes_per_day=480))
    assert len(big) < 100_000


def test_blueprint_text_is_not_copied_from_guide() -> None:
    guide = _guide_text()
    if guide is None:
        return
    guide_words = _words(guide)
    grams = {tuple(guide_words[i : i + 12]) for i in range(len(guide_words) - 11)}
    guide_lines = [_words(text) for _, text in re.findall(OBJECTIVE_LINE, guide, re.M)]
    objective_lines = [" ".join(words) for words in guide_lines if len(words) >= 4]
    assert len(guide_lines) == 55
    data = bp.load_blueprint()

    def strings(value):
        if isinstance(value, str):
            if not value.startswith("http"):
                yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                if key != "readings":
                    yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    checked = 0
    for text in strings(data):
        words = _words(text)
        for i in range(len(words) - 11):
            assert tuple(words[i : i + 12]) not in grams, text
        joined = " ".join(words)
        for line in objective_lines:
            assert line not in joined, text
        checked += 1
    assert checked > 70


def _scheduled_share(plan: dict, domain: str) -> float:
    """Percent of scheduled study minutes for one domain; a shared topic's minutes are split across its ids."""
    owner = {obj["id"]: dom["name"] for dom in bp.load_blueprint()["domains"] for obj in dom["objectives"]}
    total = mine = 0.0
    for day in plan["schedule"]:
        for block in day["blocks"]:
            if "objective" not in block:
                continue
            ids = [block["objective"], *block["also"]]
            for oid in ids:
                share = block["minutes"] / len(ids)
                total += share
                mine += share if owner[oid] == domain else 0.0
    return 100.0 * mine / total


def test_study_plan_schedule_follows_domain_weights() -> None:
    plan = bp.study_plan(None, 45, 60)
    assert plan["unscheduled_objectives"] == []
    assert _scheduled_share(plan, "Customizing USD") < 9.0  # weight 6
    assert _scheduled_share(plan, "Composition") > 20.0  # weight 23


def test_study_plan_uses_weak_objectives_from_progress() -> None:
    domains = {dom["name"]: {"answered": 20, "correct": 17} for dom in bp.load_blueprint()["domains"]}
    baseline = bp.study_plan({"domains": domains}, 3, 60)
    assert "1.6" in baseline["unscheduled_objectives"]
    weak = {"objective_id": "1.6", "domain": "Composition", "answered": 6, "correct": 0, "accuracy": 0.0}
    plan = bp.study_plan({"domains": domains, "weakest_objectives": [weak]}, 3, 60)
    order = [item["id"] for item in plan["focus_objectives"]]
    assert order.index("1.6") < 5, order[:8]
    assert "1.6" not in plan["unscheduled_objectives"]
    item = next(item for item in plan["focus_objectives"] if item["id"] == "1.6")
    assert "weak_objective:1.6" in item["reasons"]
    shared = bp.study_plan({"domains": domains, "weakest_objectives": [{"objective_id": "6.2", "answered": 4, "correct": 1}]}, 3, 60)
    top = shared["focus_objectives"][0]
    assert "6.2" in [top["id"], *top["also"]], top
    one_try = bp.study_plan({"domains": domains, "weakest_objectives": [{"objective_id": "1.6", "answered": 1, "correct": 0}]}, 3, 60)
    assert "1.6" in one_try["unscheduled_objectives"], "a single answer is not evidence"


def test_change_block_and_high_yield_wording_are_accurate() -> None:
    data = bp.load_blueprint()
    text = next(obj["text"] for obj in _all_objectives() if obj["id"] == "6.1")
    assert "removes notification overhead" not in text
    assert "batching" in text and "queued" in text and "outermost block" in text
    assert "double-weighted" not in data["high_yield_notes"]
    assert "estimate" in data["high_yield_notes"]
