"""value_story: "why does this value appear?" and the safe what-if, on tiny layered scenes in temp folders."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, nl_tier8, value_story as vs, workflows
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService

ASSET = """#usda 1.0
(
    defaultPrim = "Box"
)
def Cube "Box"
{
    double size = 3
}
"""
WEAK = """#usda 1.0
over "World"
{
    over "Box"
    {
        double size = 4
    }
}
"""
ROOT = """#usda 1.0
(
    subLayers = [@./weak.usda@]
)
def Xform "World"
{
    double3 xformOp:translate = (1, 0, 0)
    uniform token[] xformOpOrder = ["xformOp:translate"]
    token visibility = "invisible"
    rel material:binding = </Looks/Red>

    def "Box" (
        references = @./box.usda@
    )
    {
        double size = 2
        double3 xformOp:translate = (0, 2, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def "Crate" (
        prepend apiSchemas = ["MaterialBindingAPI"]
        variants = {
            string look = "Big"
        }
        prepend variantSets = "look"
        inherits = </_class_Crate>
    )
    {
        variantSet "look" = {
            "Big" {
                double size = 8
            }
            "Small" {
                double size = 1
            }
        }
    }

    def Cube "Sampled"
    {
        double size.timeSamples = {
            1: 10,
            2: 20,
        }
    }

    def Cube "Blocked"
    {
        double size = None
    }

    def Cube "Plain"
    {
    }
}

class "_class_Crate"
{
    double size = 5
    double radius = 0.5
}

def Scope "Looks"
{
    def Material "Red"
    {
    }
}
"""


@contextmanager
def _scene():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in (("box.usda", ASSET), ("weak.usda", WEAK), ("root.usda", ROOT)):
            (root / name).write_text(text, encoding="utf-8")
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        stage = Usd.Stage.Open(str(root / "root.usda"))
        try:
            yield stage, root
        finally:
            assert {p.name: p.read_bytes() for p in root.iterdir()} == before, "a file was changed"
            assert not any(layer.dirty for layer in stage.GetLayerStack()), "a layer was dirtied"
            del stage


def test_local_value_beats_weaker_sublayer_and_the_reference():
    with _scene() as (stage, _):
        story = vs.why_value(stage, "/World/Box.size")
        assert story["ok"] is True and story["value"] == 2.0
        assert story["comes_from"]["liverps_letter"] == "L"
        assert "root layer root.usda" in story["comes_from"]["written_in"]
        losers = {item["value"]: item for item in story["beats"]}
        assert set(losers) == {4.0, 3.0}
        assert "higher in the stack" in losers[4.0]["why_it_loses"]
        assert losers[3.0]["liverps_letter"] == "R" and "(L) beat references (R)" in losers[3.0]["why_it_loses"]
        assert "referenced file box.usda" in losers[3.0]["written_in"]


def test_inherited_class_beats_the_variant_and_names_both():
    # LIVRPS: Inherits (I) are stronger than Variants (V), so the class value 5 beats variant Big's 8.
    with _scene() as (stage, _):
        story = vs.why_value(stage, "/World/Crate", "size")
        assert story["value"] == 5.0
        assert story["comes_from"]["liverps_letter"] == "I"
        assert "inherited class /_class_Crate" in story["comes_from"]["written_in"]
        variant = [b for b in story["beats"] if b["liverps_letter"] == "V"]
        assert variant and variant[0]["value"] == 8.0
        assert "variant 'Big' of the variant set 'look'" in variant[0]["written_in"]
        assert "inherits (I) beat variants (V)" in variant[0]["why_it_loses"]
        assert "Learn OpenUSD: Variant sets" in story["learn_more"]


def test_inherited_class_value_when_nothing_stronger_sets_it():
    with _scene() as (stage, _):
        story = vs.why_value(stage, "/World/Crate.radius")
        assert story["value"] == 0.5 and story["comes_from"]["liverps_letter"] == "I"


def test_time_samples_block_and_fallback():
    with _scene() as (stage, _):
        sampled = vs.why_value(stage, "/World/Sampled.size", time=1)
        assert sampled["value"] == 10.0 and sampled["comes_from"]["opinion_kind"] == "a time sample"
        blocked = vs.why_value(stage, "/World/Blocked.size")
        assert "value block" in blocked["headline"]
        plain = vs.why_value(stage, "/World/Plain.size")
        assert plain["not_authored"] and plain["not_authored"]["fallback"] == 2.0
        assert "fallback" in plain["headline"] and plain["comes_from"] is None


def test_computed_values_are_kept_apart_from_authored_ones():
    with _scene() as (stage, _):
        vis = vs.why_value(stage, "/World/Box.visibility")
        assert vis["value"] == "inherited"
        assert vis["computed"][0]["value"] == "invisible" and "/World is invisible" in vis["computed"][0]["sentence"]
        assert vis["authored_vs_computed"]
        move = vs.why_value(stage, "/World/Box.xformOp:translate")
        assert move["value"] == [0.0, 2.0, 0.0]
        assert move["computed"][0]["value"] == [1.0, 2.0, 0.0] and "/World" in move["computed"][0]["sentence"]


def test_material_bound_on_an_ancestor():
    with _scene() as (stage, _):
        story = vs.why_value(stage, "/World/Box", "material")
        assert story["ok"] is True and story["value"] == "/Looks/Red"
        assert story["binding_authored_on"] == "/World" and "ancestor /World" in story["headline"]
        none = vs.why_value(stage, "/Looks", "material")
        assert none["value"] is None


def test_predictions_point_at_what_if_with_a_different_value():
    with _scene() as (stage, _):
        story = vs.why_value(stage, "/World/Box.size")
        targets = [p["check_with"]["arguments"]["layer"] for p in story["predict"]]
        assert targets == ["root", "weak.usda"]
        assert all(p["check_with"]["tool"] == "scene_what_if" for p in story["predict"])
        assert all(p["check_with"]["arguments"]["value"] == 5.0 for p in story["predict"])


def test_what_if_reports_winners_and_losers_without_touching_anything():
    with _scene() as (stage, _):
        weak = vs.what_if(stage, "/World/Box.size", value=9, layer="weak.usda")
        assert weak["ok"] is True and weak["your_value_would_win"] is False and weak["value_after"] == 2.0
        assert "root layer root.usda" in weak["still_wins"]["written_in"]
        root = vs.what_if(stage, "/World/Box.size", value=9, layer="root")
        assert root["your_value_would_win"] is True and root["value_after"] == 9.0
        session = vs.what_if(stage, "/World/Box.size", value=7, layer="session")
        assert session["your_value_would_win"] is True and session["value_after"] == 7.0
        # a weak local opinion still beats the inherited class and the variant (L > I > V)
        crate = vs.what_if(stage, "/World/Crate.size", value=3, layer="weak.usda")
        assert crate["your_value_would_win"] is True and crate["value_after"] == 3.0
        assert all(r["changed_anything"] is False for r in (weak, root, session, crate))
        assert stage.GetAttributeAtPath("/World/Box.size").Get() == 2.0


def test_what_if_at_a_time_and_with_vector_values():
    with _scene() as (stage, _):
        timed = vs.what_if(stage, "/World/Sampled.size", value=99, layer="weak.usda", time=1)
        assert timed["your_value_would_win"] is False and timed["value_after"] == 10.0
        moved = vs.what_if(stage, "/World/Box.xformOp:translate", value=[5, 5, 5], layer="root")
        assert moved["your_value_would_win"] is True and moved["value_after"] == [5.0, 5.0, 5.0]


def test_bad_inputs_are_refused():
    with _scene() as (stage, _):
        assert vs.why_value(stage, "/World/Nope.size")["ok"] is False
        assert vs.why_value(stage, "/World/Box")["ok"] is False
        assert vs.why_value(None, "/World/Box.size")["ok"] is False
        assert vs.why_value(stage, "")["ok"] is False
        assert "not in this stage's local layer stack" in vs.what_if(stage, "/World/Box.size", value=1, layer="box.usda")["error"]
        assert vs.what_if(stage, "/World/Box.size", value="big", layer="root")["ok"] is False
        assert vs.what_if(stage, "/World/Box.size", layer="root")["ok"] is False
        assert vs.what_if(stage, "/World/Box.nothing", value=1)["ok"] is False


def test_tools_are_registered_read_only_with_scene_file_mode():
    for name in ("scene_why_value", "scene_what_if"):
        spec = features.FEATURES[name]
        assert spec.location == "stage" and spec.mutates is False
        assert "stage" not in features.signature(name).parameters
    with _scene() as (stage, _):
        result = features.invoke("scene_why_value", {"prim_path": "/World/Box.size"}, stage=stage)
        assert result["ok"] is True and result["value"] == 2.0
        result = features.invoke("scene_what_if", {"prim_path": "/World/Box.size", "value": 9, "layer": "root"}, stage=stage)
        assert result["your_value_would_win"] is True


def test_phrases_route_to_the_value_tools():
    cases = {
        "why is /World/Box.size 2": ("scene_why_value", {"prim_path": "/World/Box", "attribute": "size"}),
        "why does /World/Box.primvars:displayColor look red?": (
            "scene_why_value", {"prim_path": "/World/Box", "attribute": "primvars:displayColor"}),
        "why is the size of /World/Box so big": ("scene_why_value", {"prim_path": "/World/Box", "attribute": "size"}),
        "what if I set /World/Box.size to 5 in root": (
            "scene_what_if", {"prim_path": "/World/Box", "attribute": "size", "value": 5, "layer": "root"}),
        "what if I set /World/Box.size to 5": ("scene_what_if", {"prim_path": "/World/Box", "attribute": "size", "value": 5}),
        "what if i set /World/A.xformOp:translate to [1, 2, 3] in weak.usda": (
            "scene_what_if", {"prim_path": "/World/A", "attribute": "xformOp:translate", "value": [1, 2, 3], "layer": "weak.usda"}),
    }
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for phrase, (tool, arguments) in cases.items():
        parsed = nl_tier8.parse(phrase)
        assert parsed == {"tool": tool, "arguments": arguments, "scene": True}, (phrase, parsed)
        assert router.route(phrase).target is RouteTarget.SCENE, phrase
    assert nl_tier8.parse("why is " + "x" * 300 + " /World/A.size") is None


def test_existing_routes_are_unchanged():
    import importlib.util

    spec = importlib.util.spec_from_file_location("round8_corpus", Path(__file__).with_name("test_round8_integration.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    phrases = module._corpus()
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    after = {phrase: router.route(phrase) for phrase in phrases}
    original = nl_tier8._value_route
    nl_tier8._value_route = lambda _clean: None
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier8._value_route = original
    assert not [p for p in phrases if before[p] != after[p]]


def test_why_value_workflow_runs_predict_then_check():
    text = workflows.render_prompt("why_value", prim_path="/World/Box", attribute="size")
    order = [text.index(s) for s in ("scene_why_value(prim_path='/World/Box', attribute='size')", "first 'predict'",
                                      "scene_what_if", "dry_run=True")]
    assert order == sorted(order)
