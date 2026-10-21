from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from omniverse_scene_mcp.exam import bank

WORKSPACE = Path(bank.__file__).resolve().parents[4]
GUIDE = Path(
    r"C:\Users\krngr\AppData\Local\Temp\claude\C--Users-krngr-Omniverse"
    r"\95cdf3be-6b57-4a88-a9a9-b1b598787514\scratchpad\mcp-expansion\cert-research\guide.txt"
)
WEIGHT_TOLERANCE_PP = 3.0  # each domain's share of the bank stays within this many points of its exam weight
MIN_BANK_SIZE = 200
HIDDEN_KEYS = {"answer", "explanations", "verify", "citations"}


def _items() -> list[dict]:
    return bank.load_bank()


def _layer_texts(item: dict) -> list[tuple[str, str]]:
    texts = list((item.get("usda") or {}).items())
    verify = item.get("verify") or {}
    texts += list((verify.get("usda") or {}).items())
    for scenario in verify.get("scenarios", []):
        texts += list((scenario.get("usda") or {}).items())
    return texts


def _first(predicate) -> dict:
    return next(item for item in _items() if predicate(item))


def test_every_item_matches_schema():
    items = _items()
    assert len(items) >= 90
    problems = [p for item in items for p in bank.validate_item(item)]
    assert problems == [], problems[:10]


def test_ids_are_unique_and_prefixed_by_domain():
    ids = [item["id"] for item in _items()]
    assert len(ids) == len(set(ids))
    for item in _items():
        assert item["id"].startswith(bank.DOMAINS[item["domain"]]["prefix"] + "-")


def test_answer_indices_are_valid_and_multi_has_several():
    for item in _items():
        answer = item["answer"]
        assert answer and all(0 <= a < len(item["options"]) for a in answer), item["id"]
        assert len(set(answer)) == len(answer), item["id"]
        assert len(item["explanations"]) == len(item["options"]), item["id"]
        assert 4 <= len(item["options"]) <= 6, item["id"]
        if item["type"] == "multi":
            assert len(answer) >= 2, item["id"]
            assert f"Select {bank.SELECT_WORDS[len(answer)]}" in item["stem"], item["id"]
        else:
            assert len(answer) == 1, item["id"]


def test_objective_ids_come_from_the_study_guide():
    for item in _items():
        assert item["objective_ids"], item["id"]
        assert set(item["objective_ids"]) <= bank.OBJECTIVE_IDS, item["id"]
        number = str(bank.DOMAINS[item["domain"]]["number"])
        assert any(o.split(".")[0] == number for o in item["objective_ids"]), item["id"]


def test_distribution_tracks_exam_weights():
    summary = bank.stats()
    counts, total = summary["by_domain"], summary["total"]
    assert total >= MIN_BANK_SIZE, total
    assert sum(info["weight"] for info in bank.DOMAINS.values()) == 100
    for domain, info in bank.DOMAINS.items():
        share = 100.0 * counts[domain] / total
        detail = (domain, counts[domain], round(share, 1), info["weight"])
        assert abs(share - info["weight"]) <= WEIGHT_TOLERANCE_PP, detail
    items = _items()
    assert sum(1 for item in items if item.get("usda")) >= 30
    assert sum(1 for item in items if item["type"] == "multi") >= 20
    assert any("Select THREE" in item["stem"] for item in items)


def test_usda_questions_always_carry_verify_blocks():
    for item in _items():
        if item.get("usda"):
            assert item.get("verify", {}).get("checks"), item["id"]


def test_every_usda_snippet_parses():
    from pxr import Sdf

    count = 0
    for item in _items():
        for name, text in _layer_texts(item):
            layer = Sdf.Layer.CreateAnonymous(".usda")
            assert layer.ImportFromString(text), f"{item['id']}: {name} does not parse"
            count += 1
    assert count >= 40


def test_every_verify_block_passes_with_pxr():
    from pxr import Usd

    assert Usd.GetVersion() >= (0, 25, 0)
    failures = []
    for item in _items():
        if not item.get("verify"):
            continue
        result = bank.verify_question(item)
        assert result["checks"], item["id"]
        if result["passed"] is not True:
            failures.append((item["id"], result["errors"], [c for c in result["checks"] if not c["passed"]]))
    assert failures == [], failures[:3]


def test_verify_rejects_a_wrong_expectation():
    item = copy.deepcopy(_first(lambda i: i["id"] == "comp-001"))
    assert bank.verify_question(item)["passed"] is True
    item["verify"]["checks"][0]["expect"] = 1
    result = bank.verify_question(item)
    assert result["passed"] is False
    assert result["checks"][0]["actual"] == 3


def test_verify_scenarios_and_load_options_run():
    result = bank.verify_question("comp-019")
    labels = {check["scenario"] for check in result["checks"]}
    assert result["passed"] is True
    assert labels == {"main", "payload loaded"}
    muted = bank.verify_question("dbg-004")
    assert muted["passed"] is True
    assert [c["actual"] for c in muted["checks"]] == [1000, 5000]


def test_verify_question_without_block_is_skipped():
    item = _first(lambda i: "verify" not in i)
    result = bank.verify_question(item)
    assert result["passed"] is None and "skipped" in result


def test_bank_works_without_pxr():
    import importlib
    import sys

    saved = sys.modules.get("pxr")
    sys.modules["pxr"] = None  # makes `import pxr` raise ImportError
    try:
        importlib.reload(bank)
        result = bank.verify_question("comp-001")
        assert result["passed"] is None and "pxr" in result["skipped"]
        assert bank.quiz(count=2, seed=3)["count"] == 2
        key = next(i for i in bank.load_bank() if i["id"] == "comp-001")["answer"]
        assert bank.grade("comp-001", key)["correct"] is True
    finally:
        if saved is None:
            sys.modules.pop("pxr", None)
        else:
            sys.modules["pxr"] = saved
        importlib.reload(bank)


def test_verify_results_are_json_serializable():
    for qid in ("comp-005", "dx-005", "vis-002", "ca-004", "pipe-010"):
        json.dumps(bank.verify_question(qid))


def test_plain_converts_usd_values():
    from pxr import Gf, Sdf, Vt

    assert bank._plain(Gf.Vec3f(1, 2, 3)) == [1.0, 2.0, 3.0]
    assert bank._plain(Gf.Matrix4d(1))[3] == [0.0, 0.0, 0.0, 1.0]
    assert bank._plain(Sdf.Path("/A/B")) == "/A/B"
    assert bank._plain(Sdf.AssetPath("./a.usda")) == "./a.usda"
    assert bank._plain(Gf.Quatf(1, 0, 0, 0)) == [1.0, 0.0, 0.0, 0.0]
    assert len(bank._plain(Vt.IntArray(list(range(5000))))) == bank.MAX_ARRAY
    json.dumps(bank._plain(Vt.Vec3fArray([Gf.Vec3f(0, 1, 2)])))


def test_grade_single_answer():
    item = _first(lambda i: i["type"] == "single")
    right = item["answer"][0]
    wrong = next(i for i in range(len(item["options"])) if i != right)
    result = bank.grade(item["id"], [right])
    assert result["correct"] is True
    assert result["correct_answer"] == [right]
    assert result["domain"] == item["domain"] and result["objective_ids"] == item["objective_ids"]
    assert len(result["explanations"]) == len(item["options"]) and result["citations"]
    assert bank.grade(item["id"], [wrong])["correct"] is False
    assert bank.grade(item["id"], [bank.LETTERS[right].lower()])["correct"] is True
    json.dumps(result)


def test_grade_multi_needs_the_exact_set():
    item = _first(lambda i: i["type"] == "multi")
    answer = item["answer"]
    assert bank.grade(item["id"], list(reversed(answer)))["correct"] is True
    assert bank.grade(item["id"], answer[:1])["correct"] is False
    extra = next(i for i in range(len(item["options"])) if i not in answer)
    assert bank.grade(item["id"], answer + [extra])["correct"] is False
    explained = bank.grade(item["id"], answer[:1])["explanations"]
    assert [e["is_correct"] for e in explained] == [i in answer for i in range(len(item["options"]))]


def test_answer_keys_are_spread_over_option_positions():
    """'Pick the first N options' must not pass: no position may hold most of the keys."""
    items = _items()
    positions = [a for item in items for a in item["answer"]]
    assert max(positions.count(p) for p in set(positions)) <= 0.4 * len(positions)
    assert sum(item["answer"] == list(range(len(item["answer"]))) for item in items) < 0.4 * len(items)


def test_grade_rejects_bad_input():
    for args in (("no-such-id", [0]), ("comp-001", [9]), ("comp-001", ["Z"]), ("comp-001", [True])):
        try:
            bank.grade(*args)
        except ValueError:
            continue
        raise AssertionError(f"grade{args} should raise ValueError")


def _quiz_pages(domain: str) -> list[dict]:
    """Every question of a domain, fetched in MAX_QUIZ-sized pages with exclude_ids (domains can exceed one page)."""
    expected = bank.stats()["by_domain"][domain]
    questions: list[dict] = []
    while True:
        result = bank.quiz(domain=domain, count=bank.MAX_QUIZ, seed=1, exclude_ids=[q["id"] for q in questions])
        assert result["available"] == expected - len(questions), (domain, result["available"])
        assert result["count"] == min(bank.MAX_QUIZ, result["available"]), (domain, result["count"])
        questions += result["questions"]
        if result["available"] <= bank.MAX_QUIZ:
            return questions


def test_quiz_never_leaks_answers():
    by_id = {item["id"]: item for item in _items()}
    seen = set()
    for domain in bank.DOMAINS:
        questions = _quiz_pages(domain)
        assert len(questions) == len({q["id"] for q in questions}) == bank.stats()["by_domain"][domain]
        for question in questions:
            assert not HIDDEN_KEYS & set(question), question["id"]
            source = by_id[question["id"]]
            dumped = json.dumps(question)
            for why in source["explanations"]:
                assert why not in dumped, question["id"]
            assert question["options"][0].startswith("A. ")
            if source["type"] == "multi":
                assert question["select"] == len(source["answer"])
            seen.add(question["id"])
    assert seen == set(by_id)


def test_quiz_filters_seed_exclude_and_prefer():
    first = bank.quiz(count=5, seed=42)
    assert first == bank.quiz(count=5, seed=42)
    assert first["count"] == 5
    by_objective = bank.quiz(objective="1.4", count=50)
    assert by_objective["count"] >= 1
    assert all("1.4" in q["objective_ids"] for q in by_objective["questions"])
    assert all(q["domain"] == "Visualization" for q in bank.quiz(domain="vis", count=50)["questions"])
    excluded = [q["id"] for q in bank.quiz(domain="Customizing USD", count=50)["questions"]][:-1]
    remaining = bank.quiz(domain="Customizing USD", exclude_ids=excluded, count=50)
    assert remaining["count"] == 1 and remaining["questions"][0]["id"] not in excluded
    preferred = bank.quiz(count=3, seed=7, prefer_ids=["vis-003", "missing-id", "comp-020"])
    assert [q["id"] for q in preferred["questions"]][:2] == ["vis-003", "comp-020"]
    usda_question = bank.quiz(count=1, prefer_ids=["comp-009"])["questions"][0]
    assert usda_question["root"] == "shot.usda" and "layout.usda" in usda_question["usda"]
    for bad in ({"domain": "Astronomy"}, {"objective": "9.9"}):
        try:
            bank.quiz(**bad)
        except ValueError:
            continue
        raise AssertionError(f"quiz({bad}) should raise ValueError")


def test_stats_are_consistent():
    summary = bank.stats()
    assert summary["total"] == len(_items()) == sum(summary["by_domain"].values())
    assert sum(summary["by_type"].values()) == summary["total"]
    assert summary["with_verify"] >= summary["with_usda"] >= 30
    assert summary["weights"]["Composition"] == 23
    json.dumps(summary)


def test_validate_item_reports_problems():
    good = copy.deepcopy(_first(lambda i: i["type"] == "multi"))
    assert bank.validate_item(good) == []
    broken = copy.deepcopy(good)
    broken["stem"] = broken["stem"].replace("Select", "Pick")
    broken["objective_ids"] = ["9.1"]
    broken["citations"] = ["https://example.com/dump"]
    problems = " ".join(bank.validate_item(broken))
    assert "Select" in problems and "objective" in problems and "citations" in problems
    usda_item = copy.deepcopy(_first(lambda i: bool(i.get("usda"))))
    usda_item.pop("verify")
    assert any("verify" in p for p in bank.validate_item(usda_item))
    usda_item["verify"] = {"checks": [{"path": "/A", "expect": {"colour": 1}}]}
    assert any("unknown key" in p for p in bank.validate_item(usda_item))


def test_local_citations_exist():
    docs = WORKSPACE / "LearnOpenUSD-main" / "docs"
    if not docs.is_dir():
        print("SKIP local citation check: LearnOpenUSD-main is missing")
        return
    missing = sorted({
        c for item in _items() for c in item["citations"]
        if c.startswith("LearnOpenUSD-main/") and not (WORKSPACE / c).is_file()
    })
    assert missing == [], missing


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("\u2019", "'").replace("'", ""))


def test_no_ten_word_sequence_copied_from_study_guide():
    if not GUIDE.is_file():
        print("SKIP copyright guard: study guide text not found")
        return
    guide_words = _words(GUIDE.read_text(encoding="utf-8", errors="ignore"))
    shingles = {tuple(guide_words[i:i + 10]) for i in range(len(guide_words) - 9)}

    def copied(text: str) -> list[str]:
        words = _words(text)
        return [" ".join(words[i:i + 10]) for i in range(len(words) - 9) if tuple(words[i:i + 10]) in shingles]

    probe = "Quick check: " + " ".join(guide_words[200:212]).upper() + "!"
    assert copied(probe), "guard must flag a copied guide sentence"
    hits = [
        (item["id"], hit)
        for item in _items()
        for text in [item["stem"], *item["options"], *item["explanations"]]
        for hit in copied(text)
    ]
    assert hits == [], hits[:5]


def test_instance_mask_and_surface_source_pin_the_keyed_results():
    """ca-014/ca-015 check PointInstancer visibility and vis-016 the per-context surface, so a wrong key fails."""
    for qid in ("ca-014", "ca-015", "vis-016"):
        assert bank.verify_question(qid)["passed"] is True, qid
    lanterns = copy.deepcopy(_first(lambda i: i["id"] == "ca-014"))
    mask = next(c for c in lanterns["verify"]["checks"] if "instance_mask" in c.get("expect", {}))
    assert mask["expect"]["instance_mask"] == [True, True, False, True]
    mask["expect"]["instance_mask"] = [True, False, True, True]  # 'hides the second entry' (ids ignored)
    assert bank.verify_question(lanterns)["passed"] is False
    painted = copy.deepcopy(_first(lambda i: i["id"] == "vis-016"))
    source = next(c for c in painted["verify"]["checks"] if "surface_source" in c.get("expect", {}))
    source["expect"]["surface_source"]["ri"] = None  # 'ri gets no shader'
    assert bank.verify_question(painted)["passed"] is False
    assert bank.validate_item(painted) == []
