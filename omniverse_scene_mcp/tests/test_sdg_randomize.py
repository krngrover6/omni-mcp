"""Domain randomization for SDG: per-frame changes in the session layer, same seed same sequence, exact restore."""
from __future__ import annotations

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import sdg, sdg_randomize as dr


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    for i in range(3):
        cube = UsdGeom.Cube.Define(stage, f"/World/Box_{i}")
        UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(i, 0, 0.5))
        UsdGeom.XformCommonAPI(cube).SetRotate(Gf.Vec3f(0, 0, 0))
        cube.GetDisplayColorAttr().Set([Gf.Vec3f(0.5, 0.5, 0.5)])
    UsdLux.DomeLight.Define(stage, "/World/Dome").CreateIntensityAttr(1000.0)
    return stage


SPEC = {"prims": ["/World/Box_0", "/World/Box_1", "/World/Box_2"], "position": {"min": [-1, -1, 0.5],
        "max": [1, 1, 0.5]}, "rotate_z": True, "colors": "any", "lights": ["/World/Dome"], "intensity": [200, 3000]}


def test_each_frame_varies_inside_the_limits_and_restore_puts_everything_back():
    stage = _stage()
    root_before = stage.GetRootLayer().ExportToString()
    session_before = stage.GetSessionLayer().ExportToString()
    randomizer = dr.Randomizer(stage, dr.validate(SPEC), seed=7)
    frames = [randomizer.apply() for _ in range(5)]
    positions = [tuple(c["position"]) for frame in frames for c in frame if "position" in c]
    assert len(set(positions)) == len(positions) == 15  # every frame, every box, a new place
    assert all(-1 <= x <= 1 and -1 <= y <= 1 and z == 0.5 for x, y, z in positions)
    box = stage.GetPrimAtPath("/World/Box_0")
    world = UsdGeom.Xformable(box).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()
    assert [round(v, 3) for v in world] == list(frames[-1][0]["position"])  # the change really shows
    assert 200 <= stage.GetPrimAtPath("/World/Dome").GetAttribute("inputs:intensity").Get() <= 3000
    assert stage.GetRootLayer().ExportToString() == root_before  # the file's layer is never touched
    assert randomizer.restore() > 0
    assert stage.GetSessionLayer().ExportToString() == session_before, stage.GetSessionLayer().ExportToString()
    assert UsdGeom.Xformable(box).ComputeLocalToWorldTransform(0).ExtractTranslation() == Gf.Vec3d(0, 0, 0.5)
    again = dr.Randomizer(_stage(), dr.validate(SPEC), seed=7)
    assert [again.apply() for _ in range(5)] == frames  # same seed, same sequence


def test_validation_and_prims_that_cannot_be_changed():
    for bad in ({}, {"prims": ["World"]}, {"prims": ["/A"]}, {"lights": ["/L"]}, {"prims": ["/A"], "colors": "red"},
                {"prims": ["/A"], "position": {"min": [1, 1, 1], "max": [0, 0, 0]}}, {"oops": 1}):
        try:
            dr.validate(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert dr.validate(None) is None
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/World/Bare")
    try:
        dr.Randomizer(stage, dr.validate({"prims": ["/World/Bare"], "position": {"min": [0, 0, 0], "max": [1, 1, 1]}}), 1)
    except ValueError as exc:
        assert "translate" in str(exc)
    else:
        raise AssertionError("moved a prim without a translate op")
    plan = sdg.plan_sdg(__import__("tempfile").mkdtemp(), "dr_run", randomize=SPEC)
    assert plan["ok"] and plan["config"]["randomize"] == SPEC and "randomization" in plan["note"], plan
    assert not sdg.plan_sdg(__import__("tempfile").mkdtemp(), "dr_bad", randomize={"prims": []})["ok"]
