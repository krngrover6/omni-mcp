"""Live-challenge trainer (class session 15): picks tasks that fit the scene, checks them by reading it again."""
from __future__ import annotations

import json

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, live_challenge as lc


def _node(stage, path, kind):
    prim = stage.DefinePrim(path, "OmniGraphNode")
    prim.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(kind)
    return prim


def _cell():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    graph = stage.DefinePrim("/World/Graph", "OmniGraph")
    graph.CreateAttribute("graph:variable:Velocity", Sdf.ValueTypeNames.Float).Set(1.0)
    _node(stage, "/World/Graph/ConveyorNode", "isaacsim.asset.gen.conveyor.IsaacConveyor")
    _node(stage, "/World/Graph/Wait", "omni.graph.action.Delay").CreateAttribute(
        "inputs:duration", Sdf.ValueTypeNames.Double).Set(5.0)
    compare = _node(stage, "/World/Graph/TooClose", "omni.graph.nodes.Compare")
    compare.CreateAttribute("inputs:operation", Sdf.ValueTypeNames.Token).Set("<")
    compare.CreateAttribute("inputs:b", Sdf.ValueTypeNames.Double).Set(0.5)
    go = _node(stage, "/World/Graph/Go", "omni.graph.core.WriteVariable")
    go.CreateAttribute("inputs:variableName", Sdf.ValueTypeNames.Token).Set("linear")
    go.CreateAttribute("inputs:value", Sdf.ValueTypeNames.Double).Set(0.2)
    sorter = _node(stage, "/World/Graph/Sorter", "omni.graph.scriptnode.ScriptNode")
    sorter.CreateAttribute("inputs:routes", Sdf.ValueTypeNames.String).Set(
        json.dumps({"red": "/World/BinA", "blue": "/World/BinB"}))
    UsdGeom.Xform.Define(stage, "/World/Packages")
    for i in range(2):
        box = UsdGeom.Cube.Define(stage, f"/World/Packages/Package_{i}")
        UsdPhysics.RigidBodyAPI.Apply(box.GetPrim())
    return stage


def test_start_picks_only_challenges_the_scene_can_host_and_is_repeatable():
    empty = lc.challenge_start(Usd.Stage.CreateInMemory())
    assert empty["ok"] and empty["challenge"] is None and "Build something" in empty["message"]
    stage = _cell()
    kinds = {lc.challenge_start(stage, seed=seed)["id"].split(":")[0] for seed in range(80)}
    assert kinds == {"belt_speed", "wait", "stop_distance", "drive_speed", "route", "packages", "mass", "camera_on"}
    first = lc.challenge_start(stage, level="easy", seed=7)
    assert first == lc.challenge_start(stage, level="easy", seed=7) and first["minutes"] == 3 and first["read_only"]
    assert first["id"].split(":")[0] in ("belt_speed", "wait", "stop_distance", "drive_speed")
    route = next(lc.challenge_start(stage, level="medium", seed=s) for s in range(50)
                 if lc.challenge_start(stage, level="medium", seed=s)["id"].startswith("route:"))
    colour, _, target = route["id"][len("route:"):].partition("=")
    assert target != {"red": "/World/BinA", "blue": "/World/BinB"}[colour]  # always asks for a CHANGE
    assert not lc.challenge_start(stage, level="impossible")["ok"]
    assert features.FEATURES["live_challenge_start"].mutates is False
    assert features.FEATURES["live_challenge_check"].mutates is False


def test_check_reads_the_scene_again_hints_on_request_never_the_answer():
    stage = _cell()
    graph = stage.GetPrimAtPath("/World/Graph")
    cases = [
        ("belt_speed:0.5", lambda: graph.GetAttribute("graph:variable:Velocity").Set(0.5)),
        ("wait:3", lambda: stage.GetPrimAtPath("/World/Graph/Wait").GetAttribute("inputs:duration").Set(3.0)),
        ("stop_distance:0.8", lambda: stage.GetPrimAtPath("/World/Graph/TooClose").GetAttribute("inputs:b").Set(0.8)),
        ("drive_speed:0.1", lambda: stage.GetPrimAtPath("/World/Graph/Go").GetAttribute("inputs:value").Set(0.1)),
        ("route:red=/World/BinB", lambda: stage.GetPrimAtPath("/World/Graph/Sorter").GetAttribute("inputs:routes").Set(
            json.dumps({"red": "/World/BinB", "blue": "/World/BinB"}))),
        ("packages:3", lambda: UsdPhysics.RigidBodyAPI.Apply(
            UsdGeom.Cube.Define(stage, "/World/Packages/Package_2").GetPrim())),
        ("mass:/World/Packages/Package_0=2.0", lambda: UsdPhysics.MassAPI.Apply(
            stage.GetPrimAtPath("/World/Packages/Package_0")).CreateMassAttr(2.0)),
    ]
    for challenge_id, solve in cases:
        before = lc.challenge_check(stage, challenge_id, hint=True)
        assert before["ok"] and not before["done"] and len(before["hints"]) == 2, (challenge_id, before)
        solve()
        after = lc.challenge_check(stage, challenge_id, hint=True)
        assert after["done"] and "hints" not in after, (challenge_id, after)
    assert not lc.challenge_check(stage, "nonsense")["ok"]
    assert not lc.challenge_check(stage, "belt_speed:fast")["ok"]
    assert not lc.challenge_check(stage, "route:red")["ok"]


def test_camera_must_really_look_at_the_object():
    stage = _cell()
    box = "/World/Packages/Package_0"
    UsdGeom.XformCommonAPI(stage.GetPrimAtPath(box)).SetTranslate(Gf.Vec3d(0, 0, 0.5))
    camera = UsdGeom.Camera.Define(stage, "/World/Cam")
    UsdGeom.XformCommonAPI(camera).SetTranslate(Gf.Vec3d(3, 0, 0.5))  # default view: -Z, down at the floor
    sideways = lc.challenge_check(stage, f"camera_on:{box}")
    assert not sideways["done"] and sideways["evidence"]["cameras"][0]["off_by_deg"] > 10, sideways
    UsdGeom.XformCommonAPI(camera).SetRotate(Gf.Vec3f(0, 90, 0))  # -Z now points along -X, at the box
    looking = lc.challenge_check(stage, f"camera_on:{box}")
    assert looking["done"] and looking["evidence"]["cameras"][0]["distance_m"] == 3.0, looking
    UsdGeom.XformCommonAPI(camera).SetTranslate(Gf.Vec3d(0.5, 0, 0.5))  # too close
    assert not lc.challenge_check(stage, f"camera_on:{box}")["done"]
