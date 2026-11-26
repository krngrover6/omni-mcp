"""usda_quiz: questions from the user's own file, answer keys composed by USD, stateless grading."""
from __future__ import annotations

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path

from omniverse_scene_mcp import features
from omniverse_scene_mcp.usda_quiz import _matches, answer_quiz, make_quiz

ROOT = '''#usda 1.0
(
    metersPerUnit = 0.01
    upAxis = "Y"
    subLayers = [@./layout.usda@]
)
def Xform "World"
{
    def Xform "Day04"
    {
        def Sphere "Ball"
        {
            double radius = 30
        }
        def Cube "Box"
        {
        }
    }
}
'''
LAYOUT = '#usda 1.0\nover "World"\n{\n    over "Day04"\n    {\n        over "Ball"\n        {\n            double radius = 90\n        }\n    }\n}\n'


def _scene(tmp):
    (Path(tmp) / "layout.usda").write_text(LAYOUT)
    path = Path(tmp) / "day04.usda"
    path.write_text(ROOT)
    return str(path)


def _find(quiz, kind):
    return next(q for q in quiz["questions"] if q["kind"] == kind)


def test_quiz_asks_about_paths_fallbacks_settings_and_the_winning_layer():
    with tempfile.TemporaryDirectory() as tmp:
        path = _scene(tmp)
        quiz = make_quiz(path, count=10, seed=7)
        kinds = {q["kind"] for q in quiz["questions"]}
        assert {"stage_settings", "full_path", "fallback", "which_layer_wins"} <= kinds
        assert "answer" not in quiz["questions"][0]  # answers withheld
        wins = _find(quiz, "which_layer_wins")
        assert "30" in wins["question"] and "90" in wins["question"]
        graded = answer_quiz(path, wins["id"], "30")
        assert graded["correct"] and "root layer" in graded["explain"]
        assert not answer_quiz(path, wins["id"], "90")["correct"]
        settings = _find(quiz, "stage_settings")
        assert answer_quiz(path, settings["id"], "Y is up, centimetres")["correct"]
        assert not answer_quiz(path, settings["id"], "Z up, 0.01")["correct"]
        assert make_quiz(path, count=10, seed=7)["questions"] == quiz["questions"]  # same seed, same quiz
        assert not answer_quiz(path, "7:99", "x")["ok"] and not answer_quiz(path, "abc", "x")["ok"]
    assert not make_quiz("missing.usda")["ok"]
    assert features.FEATURES["usda_quiz"].mutates is False


def test_answer_matching_is_forgiving_but_exact_on_facts():
    assert _matches("/World/Day04/Ball", "`/World/Day04/Ball`") and not _matches("/World/Day04/Ball", "World/Day04/Ball")
    assert _matches(2.0, "2") and _matches(2.0, "it is 2.0") and not _matches(2.0, "3")
    assert _matches("inherited", "Inherited") and not _matches("inherited", "invisible")
    assert _matches({"upAxis": "Z", "metersPerUnit": 1.0}, "z up, metres")
