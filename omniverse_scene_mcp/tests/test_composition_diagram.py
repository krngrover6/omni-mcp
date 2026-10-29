"""Composition picture: the prim index drawn as Mermaid and a text tree, strongest first, winner marked."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import composition_diagram as cd, features


def _stage():
    asset = Sdf.Layer.CreateAnonymous("lamp.usda")
    asset.ImportFromString('''#usda 1.0
def Xform "Lamp" (
    variants = {
        string size = "big"
    }
    prepend variantSets = "size"
)
{
    double intensity = 1
    variantSet "size" = {
        "big" {
            double intensity = 5
        }
        "small" {
            double intensity = 2
        }
    }
}
''')
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Lamp")
    prim.GetReferences().AddReference(asset.identifier, "/Lamp")
    return stage


def test_reference_and_variant_are_drawn_in_strength_order_and_the_winner_is_marked():
    stage = _stage()
    picture = cd.diagram(stage, "/World/Lamp", "intensity")
    assert picture["ok"] and picture["read_only"], picture
    arcs = [n["arc"] for n in picture["nodes"]]
    assert arcs[0] == "local" and "reference" in arcs and "variant" in arcs, arcs
    assert [n["strength"] for n in picture["nodes"]] == list(range(1, len(arcs) + 1))
    winner = [n for n in picture["nodes"] if n["wins"]]
    # Exam trap: inside the referenced Lamp, its own intensity = 1 is a LOCAL opinion there, and L beats V.
    assert len(winner) == 1 and winner[0]["arc"] == "reference", picture["nodes"]
    assert stage.GetPrimAtPath("/World/Lamp").GetAttribute("intensity").Get() == 1
    assert picture["mermaid"].startswith("flowchart TD") and "WINS for intensity" in picture["mermaid"]
    assert "-->|reference|" in picture["mermaid"] and "<- WINS" in picture["text_tree"]
    stage.GetPrimAtPath("/World/Lamp").GetAttribute("intensity").Set(9.0)  # a local opinion now wins
    assert cd.diagram(stage, "/World/Lamp", "intensity")["nodes"][0]["wins"]


def test_bad_input_and_fallback_values():
    stage = _stage()
    assert not cd.diagram(stage, "/Nope")["ok"]
    assert not cd.diagram(stage, "not a path")["ok"]
    assert not cd.diagram(stage, "/World/Lamp", "noSuchAttr")["ok"]
    plain = cd.diagram(stage, "/World", None)
    assert plain["ok"] and plain["arcs_used"] == ["none: everything is authored locally"]
    assert features.FEATURES["scene_composition_diagram"].mutates is False
