"""scene_diagnose focus='animation': lessons from the Isaac camera tour (seen live 2026-11-13)."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp.debug_trainer import diagnose


def _codes(result):
    return {(f["code"], f["prim"]) for f in result["findings"]}


def test_camera_rig_orient_keys_outside_timeline_and_session_override():
    stage = Usd.Stage.CreateInMemory()
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(100)
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    UsdGeom.Camera.Define(stage, "/Rig/Cam")
    rig.AddOrientOp()
    move = rig.AddTranslateOp()
    move.Set((0, 0, 0), 0)
    move.Set((1, 0, 0), 150)
    prop = UsdGeom.Xform.Define(stage, "/Prop")
    prop.AddOrientOp()  # no camera: Orient is fine here
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        rig.GetPrim().CreateAttribute("visibility", Sdf.ValueTypeNames.Token).Set("invisible")
    result = diagnose(stage, "animation")
    assert _codes(result) == {("orient_not_keyable", "/Rig"), ("keys_outside_timeline", "/Rig"),
                              ("session_layer_override", None)}
    session = next(f for f in result["findings"] if f["code"] == "session_layer_override")
    assert "/Rig.visibility" in session["cause"]
    assert ("orient_not_keyable", "/Rig") in _codes(diagnose(stage))  # included when no focus is given


def test_clean_animation_and_bad_focus():
    stage = Usd.Stage.CreateInMemory()
    stage.SetEndTimeCode(100)
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    UsdGeom.Camera.Define(stage, "/Rig/Cam")
    spin = rig.AddRotateXYZOp()
    spin.Set((0, 0, 0), 0)
    spin.Set((0, 0, 90), 60)
    assert diagnose(stage, "animation")["findings"] == []
    assert "animation" in diagnose(stage, "nope")["error"]
