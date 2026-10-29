"""Factory cell builder: preview, build in order, homework 14 sees a complete cell, and no half-built cells."""
from __future__ import annotations

try:
    from pxr import Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import factory_cell as fc, features, homework, omnigraph_helper, robot_import


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def _fake_kit_parts(test):
    """No Kit here: stand in for the graph build and the Franka asset, keeping the real checks elsewhere."""
    saved = (omnigraph_helper.apply_recipe, robot_import.add_ready_robot)

    def apply(stage, name, *, dry_run=True, graph_path=None, **params):
        stage.DefinePrim(graph_path, "OmniGraph")
        node = stage.DefinePrim(f"{graph_path}/ConveyorNode", "OmniGraphNode")
        node.CreateAttribute("node:type", __import__("pxr").Sdf.ValueTypeNames.Token).Set(
            "isaacsim.asset.gen.conveyor.IsaacConveyor")
        node.CreateRelationship("inputs:conveyorPrim").SetTargets([params["belt"]])
        return {"ok": True}

    def robot(stage, name, prim_path=None, *, position=(0, 0, 0), dry_run=True, **_):
        if dry_run:
            return {"ok": True, "dry_run": True}
        arm = UsdGeom.Xform.Define(stage, prim_path)
        UsdGeom.XformCommonAPI(arm).SetTranslate(position)
        UsdPhysics.ArticulationRootAPI.Apply(arm.GetPrim())
        previous = None
        for i in range(7):
            link = UsdGeom.Xform.Define(stage, f"{prim_path}/link{i}").GetPrim()
            UsdPhysics.RigidBodyAPI.Apply(link)
            joint = UsdPhysics.RevoluteJoint.Define(stage, f"{prim_path}/joint{i}")
            if previous is not None:
                joint.CreateBody0Rel().SetTargets([previous.GetPath()])
            joint.CreateBody1Rel().SetTargets([link.GetPath()])
            previous = link
        UsdPhysics.PrismaticJoint.Define(stage, f"{prim_path}/panda_finger_joint1")
        return {"ok": True}

    omnigraph_helper.apply_recipe, robot_import.add_ready_robot = apply, robot
    try:
        test()
    finally:
        omnigraph_helper.apply_recipe, robot_import.add_ready_robot = saved


def test_preview_then_build_a_cell_homework_14_accepts():
    def run():
        stage = _stage()
        preview = fc.build_cell(stage)
        assert preview["ok"] and preview["dry_run"] and not stage.GetPrimAtPath("/World/FactoryCell"), preview
        assert [p["part"] for p in preview["parts"]][:2] == ["belt", "stopper"]
        built = fc.build_cell(stage, dry_run=False)
        assert built["ok"] and built["built"], built
        belt = stage.GetPrimAtPath("/World/FactoryCell/Belt")
        assert belt.GetAttribute("physics:kinematicEnabled").Get() is True
        assert len(stage.GetPrimAtPath("/World/FactoryCell/Packages").GetChildren()) == 1  # one at a time
        UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
        node = stage.DefinePrim("/World/FactoryCell/BeltGraph/Arm", "OmniGraphNode")
        node.CreateAttribute("node:type", __import__("pxr").Sdf.ValueTypeNames.Token).Set(
            "isaacsim.core.nodes.IsaacArticulationController")
        node.CreateRelationship("inputs:targetPrim").SetTargets(["/World/FactoryCell/Franka"])
        grade = homework.check_homework(stage, 14)
        status = {r["rule"]: r["status"] for r in grade["results"]}
        assert all(v == "pass" for v in status.values()), status  # the layout really is reachable and graspable
        assert not fc.build_cell(stage)["ok"]  # never builds over an existing cell
    _fake_kit_parts(run)


def test_a_failing_part_removes_the_whole_cell_and_inputs_are_checked():
    def run():
        stage = _stage()
        saved = omnigraph_helper.apply_recipe
        omnigraph_helper.apply_recipe = lambda *a, **k: {"ok": False, "error": "no Kit"}
        try:
            failed = fc.build_cell(stage, dry_run=False)
        finally:
            omnigraph_helper.apply_recipe = saved
        assert not failed["ok"] and failed["failed_at"] == "belt_graph" and not stage.GetPrimAtPath("/World/FactoryCell")
        for bad in ({"colours": ["pink"]}, {"packages": 0}, {"belt_speed": 5}, {"origin": [1, 2]}, {"root": "x"}):
            assert not fc.build_cell(stage, **bad)["ok"], bad
    _fake_kit_parts(run)
    assert features.FEATURES["scene_build_factory_cell"].mutates
