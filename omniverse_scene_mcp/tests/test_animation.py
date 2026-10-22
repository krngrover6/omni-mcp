"""Simple animation helper (time samples) on temporary in-memory stages.

Z-up meters and Y-up centimeters, Isaac-style translate + orient (quatf/quatd) + scale stacks, Composer-style
rotateXYZ stacks with an up-axis correction, nested rotated/scaled parents, exact sampled values at and between
keys, turntable continuity, conveyor rides, door swings, clearing, refusals, byte-identical dry runs and rollback.
"""

import json
import math
import os

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import animation

DOUBLE = UsdGeom.XformOp.PrecisionDouble
FLOAT = UsdGeom.XformOp.PrecisionFloat


# --- helpers ----------------------------------------------------------------------------------------------


def _stage(up="Z", meters_per_unit=1.0, start=None, end=None):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    if start is not None:
        stage.SetStartTimeCode(start)
    if end is not None:
        stage.SetEndTimeCode(end)
    return stage


def _quat_about(axis, degrees):
    half = math.radians(degrees) / 2
    s = math.sin(half)
    return (math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _isaac(stage, path, translate=(0, 0, 0), quat=(1, 0, 0, 0), scale=(1, 1, 1), orient_precision=FLOAT):
    """Isaac Sim style: xformOpOrder = [translate (double), orient (quatf/quatd), scale]."""
    cube = UsdGeom.Cube.Define(stage, path)
    xformable = UsdGeom.Xformable(cube)
    xformable.AddTranslateOp(DOUBLE).Set(Gf.Vec3d(*translate))
    w, x, y, z = quat
    op = xformable.AddOrientOp(orient_precision)
    op.Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)) if orient_precision == FLOAT else Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    xformable.AddScaleOp(FLOAT).Set(Gf.Vec3f(*scale))
    return cube.GetPrim()


def _composer(stage, path, translate=(0, 0, 0), rotate=(0, 0, 0), scale=(1, 1, 1)):
    """Composer / XformCommonAPI style: [translate, rotateXYZ, scale]."""
    cube = UsdGeom.Cube.Define(stage, path)
    xformable = UsdGeom.Xformable(cube)
    xformable.AddTranslateOp(DOUBLE).Set(Gf.Vec3d(*translate))
    xformable.AddRotateXYZOp(FLOAT).Set(Gf.Vec3f(*rotate))
    xformable.AddScaleOp(FLOAT).Set(Gf.Vec3f(*scale))
    return cube.GetPrim()


def _world(prim, t):
    return UsdGeom.XformCache(Usd.TimeCode(t)).GetLocalToWorldTransform(prim)


def _pos(prim, t):
    return list(_world(prim, t).ExtractTranslation())


def _heading(prim, t, up="Z"):
    m = Gf.Matrix4d(_world(prim, t)).RemoveScaleShear()
    if up == "Z":
        return math.degrees(math.atan2(m[0][1], m[0][0]))
    return math.degrees(math.atan2(-m[0][2], m[0][0]))


def _close(first, second, tol=1e-9):
    first, second = list(first), list(second)
    assert len(first) == len(second), (first, second)
    assert all(abs(a - b) <= tol for a, b in zip(first, second)), (first, second)


def _quat(value):
    return [value.GetReal(), *value.GetImaginary()]


def _layers(stage):
    return [layer.ExportToString() for layer in stage.GetLayerStack(True)]


def _op_names(prim):
    return [str(op.GetOpName()) for op in UsdGeom.Xformable(prim).GetOrderedXformOps()]


def _ok(result):
    json.dumps(result, allow_nan=False)
    assert result["ok"], result.get("error")
    return result


def _applied(result):
    _ok(result)
    assert result["applied"] is True and result["written"] is True and result["dry_run"] is False, result
    return result


def _refused(result, *words):
    json.dumps(result, allow_nan=False)
    assert result["ok"] is False and result.get("written", False) is False, result
    text = result["error"].lower()
    for word in words:
        assert word.lower() in text, (word, result["error"])
    assert result["explanation"].startswith("Nothing was")
    return result


def _unwrap_deltas(headings):
    deltas = []
    for a, b in zip(headings, headings[1:]):
        d = (b - a + 180.0) % 360.0 - 180.0
        deltas.append(d)
    return deltas


# --- timeline ---------------------------------------------------------------------------------------------


def test_timeline_info_reports_time_codes_seconds_and_animated_attributes():
    stage = _stage(start=0, end=240)
    prim = _composer(stage, "/World/Box")
    attr = prim.GetAttribute("xformOp:translate")
    attr.Set(Gf.Vec3d(0, 0, 0), 0)
    attr.Set(Gf.Vec3d(1, 0, 0), 24)
    before = _layers(stage)
    info = _ok(animation.timeline_info(stage))
    assert _layers(stage) == before
    assert info["read_only"] is True and info["has_authored_time_range"] is True
    assert info["start_time_code"] == 0 and info["end_time_code"] == 240
    assert info["time_codes_per_second"] == 24 and info["frames_per_second"] == 24
    assert info["duration_seconds"] == 10 and info["frame_count"] == 241
    assert info["animated_attributes_count"] == 1 and info["animated_prims_count"] == 1
    assert info["animated_attributes"][0]["attribute"] == "/World/Box.xformOp:translate"
    assert info["sample_time_range"] == [0, 24]
    assert "seconds = time code / timeCodesPerSecond" in info["explanation"]
    assert "time samples beat the default" in info["explanation"]

    empty = _ok(animation.timeline_info(_stage()))
    assert empty["has_authored_time_range"] is False and empty["animated_attributes_count"] == 0
    assert any("startTimeCode" in w for w in empty["warnings"])
    odd = _stage(start=0, end=10)
    odd.SetTimeCodesPerSecond(60)
    odd.SetFramesPerSecond(30)
    assert any("differs from framesPerSecond" in w for w in _ok(animation.timeline_info(odd))["warnings"])
    assert animation.timeline_info(None)["ok"] is False


def test_set_timeline_dry_run_then_root_layer_and_tcps_follows_fps():
    stage = _stage()
    prim = _composer(stage, "/World/Box")
    prim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(1, 0, 0), 24)
    before = _layers(stage)
    dry = _ok(animation.set_timeline(stage, start=0, end=120, fps=30))
    assert dry["dry_run"] is True and dry["applied"] is False and _layers(stage) == before
    assert dry["layer_role"] == "root layer"
    assert {c["field"] for c in dry["changes"]} == {"startTimeCode", "endTimeCode", "framesPerSecond", "timeCodesPerSecond"}
    assert any("play" in w and "faster" in w for w in dry["warnings"])  # the key at 24 now lasts 0.8 s, not 1 s
    done = _applied(animation.set_timeline(stage, start=0, end=120, fps=30, dry_run=False))
    root = stage.GetRootLayer()
    assert root.startTimeCode == 0 and root.endTimeCode == 120 and root.framesPerSecond == 30 and root.timeCodesPerSecond == 30
    assert stage.GetTimeCodesPerSecond() == 30 and done["timeline"]["duration_seconds"] == 4
    assert done["readback"]["end_time_code"] == 120

    # tcps differing from fps already: fps alone does not touch tcps; an explicit value does.
    stage.SetTimeCodesPerSecond(24)
    keep = _applied(animation.set_timeline(stage, fps=25, dry_run=False))
    assert stage.GetTimeCodesPerSecond() == 24 and stage.GetFramesPerSecond() == 25
    assert any("stays 24" in w for w in keep["warnings"])
    _applied(animation.set_timeline(stage, time_codes_per_second=25, dry_run=False))
    assert stage.GetTimeCodesPerSecond() == 25

    _refused(animation.set_timeline(stage, start=50, end=10), "before start")
    _refused(animation.set_timeline(stage), "nothing to change")
    _refused(animation.set_timeline(stage, fps=0), "fps")
    _refused(animation.set_timeline(stage, start=float("nan")), "start")


def test_set_timeline_from_sublayer_edit_target_writes_root_and_session_opinion_is_refused():
    stage = _stage()
    sub = Sdf.Layer.CreateAnonymous("sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    stage.SetEditTarget(Usd.EditTarget(sub))
    sub_before = sub.ExportToString()
    done = _applied(animation.set_timeline(stage, start=10, end=20, dry_run=False))
    assert sub.ExportToString() == sub_before and stage.GetRootLayer().endTimeCode == 20
    assert any("root layer" in w for w in done["warnings"])

    session = stage.GetSessionLayer()
    session.startTimeCode = 5
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    before = _layers(stage)
    _refused(animation.set_timeline(stage, start=0, dry_run=False), "session layer")
    assert _layers(stage) == before
    stage.SetEditTarget(Usd.EditTarget(session))
    on_session = _applied(animation.set_timeline(stage, start=0, dry_run=False))
    assert on_session["layer_role"] == "session layer" and session.startTimeCode == 0 and stage.GetStartTimeCode() == 0


# --- add_keyframes ----------------------------------------------------------------------------------------


def test_add_keyframes_translate_exact_at_between_and_outside_keys():
    stage = _stage(start=0, end=48)
    prim = _composer(stage, "/World/Box")
    attr = prim.GetAttribute("xformOp:translate")
    attr.Set(Gf.Vec3d(9, 9, 9), 48)  # an existing key at another time is kept
    result = _applied(animation.add_keyframes(stage, "/World/Box", translate={"0": [0, 0, 0], "24": [2, 4, 6]}, dry_run=False))
    assert _op_names(prim) == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
    assert list(attr.GetTimeSamples()) == [0, 24, 48]
    _close(attr.Get(12), (1, 2, 3))  # halfway between keys: exactly halfway (linear interpolation)
    _close(attr.Get(6), (0.5, 1, 1.5))
    _close(attr.Get(-10), (0, 0, 0))  # before the first key: held
    _close(attr.Get(36), (5.5, 6.5, 7.5))
    _close(attr.Get(Usd.TimeCode.Default()), (0, 0, 0))  # the default value is untouched
    _close(_pos(prim, 12), (1, 2, 3))
    write = result["writes"][0]
    assert write["attribute"] == "xformOp:translate" and write["type"] == "double3" and write["op_added"] is False
    assert write["existing_samples_in_edit_target"] == 1 and result["readback"]["max_value_error"] == 0
    assert "kept" in result["explanation"] and result["preview"].splitlines()[0].startswith("time")

    # a float translate op stays float
    cube = UsdGeom.Cube.Define(stage, "/World/F")
    UsdGeom.Xformable(cube).AddTranslateOp(FLOAT).Set(Gf.Vec3f(0, 0, 0))
    _applied(animation.add_keyframes(stage, "/World/F", translate=[[0, [0, 0, 0]], [10, [1.5, 0, 0]]], dry_run=False))
    float_attr = cube.GetPrim().GetAttribute("xformOp:translate")
    assert str(float_attr.GetTypeName()) == "float3" and isinstance(float_attr.Get(5), Gf.Vec3f)
    _close(float_attr.Get(5), (0.75, 0, 0), 1e-6)


def test_add_keyframes_isaac_orient_quatf_and_quatd_from_numbers_and_euler():
    stage = _stage()
    rest = _quat_about((0, 0, 1), 30)
    prim = _isaac(stage, "/World/Robot", translate=(1, 2, 0), quat=rest)
    result = _applied(animation.add_keyframes(stage, "/World/Robot", rotate={0: 0.0, 24: 90.0}, dry_run=False))
    orient = prim.GetAttribute("xformOp:orient")
    assert str(orient.GetTypeName()) == "quatf" and isinstance(orient.Get(12), Gf.Quatf)
    assert _op_names(prim) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]
    assert abs(_heading(prim, 0) - 30) < 1e-4 and abs(_heading(prim, 24) - 120) < 1e-4
    assert abs(_heading(prim, 12) - 75) < 1e-4  # slerp about one axis is exact between keys
    _close(_pos(prim, 12), (1, 2, 0))
    _close(_quat(orient.Get(Usd.TimeCode.Default())), rest, 1e-6)
    assert result["writes"][0]["op_kind"] == "orient" and result["writes"][0]["precision"] == "float"

    prim_d = _isaac(stage, "/World/RobotD", orient_precision=DOUBLE)
    euler = _applied(animation.add_keyframes(stage, "/World/RobotD", rotate={0: (0, 0, 0), 10: (90, 0, 0)}, dry_run=False))
    orient_d = prim_d.GetAttribute("xformOp:orient")
    assert str(orient_d.GetTypeName()) == "quatd"
    _close(_quat(orient_d.Get(10)), _quat_about((1, 0, 0), 90), 1e-12)
    _close(_quat(orient_d.Get(5)), _quat_about((1, 0, 0), 45), 1e-12)
    assert any("rotateXYZ order" in w for w in euler["warnings"])

    _refused(animation.add_keyframes(stage, "/World/Robot", rotate={0: 1.0, 5: (0, 0, 1)}), "all")
    flip = _ok(animation.add_keyframes(stage, "/World/RobotD", rotate={0: (0, 0, 0), 10: (0, 0, 200)}))
    assert any("shortest way" in w for w in flip["warnings"])
    # numbers on an orient op get in-between keys, so a 270 degree key still turns the long way
    spin = _isaac(stage, "/World/Spin")
    long_way = _applied(animation.add_keyframes(stage, "/World/Spin", rotate={0: 0.0, 9: 270.0}, dry_run=False))
    assert "in-between" in long_way["explanation"]
    spin_orient = spin.GetAttribute("xformOp:orient")
    assert len(spin_orient.GetTimeSamples()) == 10 and list(spin_orient.GetTimeSamples())[-1] == 9
    deltas = _unwrap_deltas([_heading(spin, t / 2) for t in range(0, 19)])
    assert all(14.9 < d < 15.1 for d in deltas), deltas


def test_add_keyframes_adds_missing_ops_in_standard_place_without_reordering():
    stage = _stage()
    cube = UsdGeom.Cube.Define(stage, "/World/A")
    xformable = UsdGeom.Xformable(cube)
    xformable.AddTranslateOp().Set(Gf.Vec3d(1, 0, 0))
    xformable.AddScaleOp().Set(Gf.Vec3f(2, 2, 2))
    default_before = _world(cube.GetPrim(), Usd.TimeCode.Default())
    result = _applied(animation.add_keyframes(stage, "/World/A", rotate={0: 0.0, 24: 90.0}, dry_run=False))
    assert _op_names(cube.GetPrim()) == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
    assert result["writes"][0]["op_added"] is True and result["xform_op_order_before"] == ["xformOp:translate", "xformOp:scale"]
    rotate = cube.GetPrim().GetAttribute("xformOp:rotateXYZ")
    assert str(rotate.GetTypeName()) == "float3"
    _close(rotate.Get(Usd.TimeCode.Default()), (0, 0, 0))
    _close(rotate.Get(12), (0, 0, 45))
    assert _world(cube.GetPrim(), Usd.TimeCode.Default()) == default_before  # the rest pose did not change

    pivot = UsdGeom.Cube.Define(stage, "/World/P")
    pxf = UsdGeom.Xformable(pivot)
    pxf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
    pivot_op = pxf.AddTranslateOp(DOUBLE, "pivot")
    pivot_op.Set(Gf.Vec3d(1, 0, 0))
    pxf.AddTranslateOp(DOUBLE, "pivot", isInverseOp=True)
    _applied(animation.add_keyframes(stage, "/World/P", rotate={0: 0.0, 10: 90.0}, dry_run=False))
    assert _op_names(pivot.GetPrim()) == [
        "xformOp:translate", "xformOp:translate:pivot", "xformOp:rotateXYZ", "!invert!xformOp:translate:pivot"
    ]
    _close(_pos(pivot.GetPrim(), 10), (1, -1, 0))  # turns about the pivot at (1, 0, 0)

    bare = UsdGeom.Cube.Define(stage, "/World/Bare")
    _applied(animation.add_keyframes(stage, "/World/Bare", translate={0: (0, 0, 0), 4: (4, 0, 0)},
                                     rotate={0: 0.0, 4: 40.0}, scale={0: 1.0, 4: (2, 2, 2)}, dry_run=False))
    assert _op_names(bare.GetPrim()) == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
    assert str(bare.GetPrim().GetAttribute("xformOp:translate").GetTypeName()) == "double3"
    _close(bare.GetPrim().GetAttribute("xformOp:scale").Get(2), (1.5, 1.5, 1.5), 1e-6)
    _close(bare.GetPrim().GetAttribute("xformOp:scale").Get(Usd.TimeCode.Default()), (1, 1, 1))


# --- turntable --------------------------------------------------------------------------------------------


def test_turntable_isaac_z_up_is_continuous_never_reverses_and_ends_on_full_turns():
    stage = _stage(start=0, end=240)
    rest = _quat_about((0, 0, 1), 30)
    prim = _isaac(stage, "/World/Product", translate=(0, 0, 0.5), quat=rest)
    result = _applied(animation.turntable(stage, "/World/Product", seconds=10, turns=2, dry_run=False))
    orient = prim.GetAttribute("xformOp:orient")
    times = list(orient.GetTimeSamples())
    assert times[0] == 0 and times[-1] == 240 and all(float(t).is_integer() for t in times)
    assert result["key_every_degrees"] <= animation.MAX_STEP_DEGREES and result["axis"] == "Z"
    quats = [_quat(orient.Get(t)) for t in times]
    for a, b in zip(quats, quats[1:]):  # neighbors on the same side and well under 180 degrees apart
        dot = sum(x * y for x, y in zip(a, b))
        assert dot > math.cos(math.radians(animation.MAX_STEP_DEGREES / 2)) - 1e-6
    headings = [_heading(prim, t) for t in range(0, 241)]
    deltas = _unwrap_deltas(headings)
    assert all(2.9 < d < 3.1 for d in deltas), (min(deltas), max(deltas))  # 720 degrees / 240 frames, never backwards
    assert abs(sum(deltas) - 720.0) < 1e-3
    assert abs(_heading(prim, 0) - 30) < 1e-4 and abs(_heading(prim, 7) - (30 + 21)) < 1e-3  # between keys
    _close(_pos(prim, 100), (0, 0, 0.5))
    assert "360 degrees equals the one for 0" in result["explanation"]

    # Why the in-between keys matter: a bare 0 / 360 pair resolves to no turn at all.
    other = _isaac(stage, "/World/Naive")
    naive = other.GetAttribute("xformOp:orient")
    naive.Set(Gf.Quatf(1, Gf.Vec3f(0, 0, 0)), 0)
    naive.Set(Gf.Quatf(-1, Gf.Vec3f(0, 0, 0)), 240)
    assert abs(_heading(other, 120)) < 1e-6


def test_turntable_y_up_centimeters_rotate_xyz_with_up_correction_and_reverse_turns():
    stage = _stage(up="Y", meters_per_unit=0.01, start=0, end=96)
    prim = _composer(stage, "/World/Car", translate=(100, 0, 50), rotate=(-90, 0, 0))  # Z-up asset on a Y-up stage
    result = _applied(animation.turntable(stage, "/World/Car", seconds=4, turns=-1, dry_run=False))
    rotate = prim.GetAttribute("xformOp:rotateXYZ")
    assert result["axis"] == "Y" and str(rotate.GetTypeName()) == "float3"
    _close(rotate.Get(48), (-90, -180, 0))
    _close(rotate.Get(96), (-90, -360, 0))  # linear numbers: 360 stays 360, never wraps to 0
    _close(rotate.Get(Usd.TimeCode.Default()), (-90, 0, 0))
    deltas = _unwrap_deltas([_heading(prim, t, "Y") for t in range(0, 97)])
    assert all(-3.9 < d < -3.6 for d in deltas)
    _close(_pos(prim, 30), (100, 0, 50))

    _composer(stage, "/World/Tilted", rotate=(-90, 0, 15))
    _refused(animation.turntable(stage, "/World/Tilted"), "xform")
    _refused(animation.turntable(stage, "/World/Car", turns=0), "turns")
    _refused(animation.turntable(stage, "/World/Car", seconds=-1), "seconds")
    _refused(animation.turntable(stage, "/World/Car", axis="w"), "axis")


# --- move_along -------------------------------------------------------------------------------------------


def test_move_along_world_points_under_nested_rotated_scaled_parents():
    stage = _stage(start=0, end=168)
    line = UsdGeom.Xform.Define(stage, "/World/Line")
    lxf = UsdGeom.Xformable(line)
    lxf.AddTranslateOp().Set(Gf.Vec3d(10, 0, 0))
    lxf.AddRotateXYZOp().Set(Gf.Vec3f(0, 0, 90))
    lxf.AddScaleOp().Set(Gf.Vec3f(2, 2, 2))
    belt = UsdGeom.Xform.Define(stage, "/World/Line/Belt")
    UsdGeom.Xformable(belt).AddTranslateOp().Set(Gf.Vec3d(0, 1, 0))
    box = _isaac(stage, "/World/Line/Belt/Box", translate=(0, 0, 0), quat=_quat_about((0, 0, 1), 30), scale=(0.5, 0.5, 0.5))
    points = [(0, 0, 1), (4, 0, 1), (4, 3, 1)]
    result = _applied(animation.move_along(stage, "/World/Line/Belt/Box", points, seconds=7, dry_run=False))
    translate = box.GetAttribute("xformOp:translate")
    assert list(translate.GetTimeSamples()) == [0, 96, 168]  # timed by distance: 4 of 7 units at 1 unit/s
    for t, point in zip((0, 96, 168), points):
        _close(_pos(box, t), point, 1e-9)
    _close(_pos(box, 48), (2, 0, 1), 1e-9)  # between keys, still on the straight segment
    _close(_pos(box, 132), (4, 1.5, 1), 1e-9)
    assert result["speed_units_per_second"] == 1 and result["path_length"] == 7
    assert result["readback"]["max_world_position_error"] < 1e-9
    assert _op_names(box) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]

    cm = _stage(up="Y", meters_per_unit=0.01, start=0, end=48)
    crate = _isaac(cm, "/World/Crate", translate=(0, 10, 0))
    run = _applied(animation.move_along(cm, "/World/Crate", [(0, 10, 0), (200, 10, 0)], seconds=2, dry_run=False))
    assert run["speed_meters_per_second"] == 1 and "centimeters" in run["explanation"]
    _close(_pos(crate, 24), (100, 10, 0), 1e-9)


def test_move_along_loop_new_translate_op_and_refusals():
    stage = _stage(start=0, end=96)
    cube = UsdGeom.Cube.Define(stage, "/World/Tote")
    UsdGeom.Xformable(cube).AddOrientOp(FLOAT).Set(Gf.Quatf(1, Gf.Vec3f(0, 0, 0)))
    default_before = _world(cube.GetPrim(), Usd.TimeCode.Default())
    result = _applied(animation.move_along(stage, "/World/Tote", [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)],
                                           seconds=4, loop=True, dry_run=False))
    assert _op_names(cube.GetPrim()) == ["xformOp:translate", "xformOp:orient"]
    assert result["loop"] is True and result["writes"][0]["op_added"] is True
    attr = cube.GetPrim().GetAttribute("xformOp:translate")
    assert list(attr.GetTimeSamples()) == [0, 24, 48, 72, 96]
    _close(_pos(cube.GetPrim(), 96), (0, 0, 0))
    _close(_pos(cube.GetPrim(), 84), (0, 1, 0))
    assert _world(cube.GetPrim(), Usd.TimeCode.Default()) == default_before

    before = _layers(stage)
    _refused(animation.move_along(stage, "/World/Tote", [(0, 0, 0), (0, 0, 0), (1, 0, 0)], seconds=2, dry_run=False), "same")
    _refused(animation.move_along(stage, "/World/Tote", [(0, 0, 0)], seconds=2, dry_run=False), "two")
    _refused(animation.move_along(stage, "/World/Tote", [(0, 0, 0), (1, 0, 0)], seconds=0, dry_run=False), "seconds")
    _refused(animation.move_along(stage, "/World/Tote", "nope", seconds=1, dry_run=False), "points")
    _refused(animation.move_along(stage, "/World/Tote", [(0, 0), (1, 0)], seconds=1, dry_run=False), "3 numbers")
    assert _layers(stage) == before


# --- swing ------------------------------------------------------------------------------------------------


def test_swing_door_opens_round_trip_and_single_axis_op():
    stage = _stage(start=0, end=96)
    door = _composer(stage, "/World/Door", translate=(1, 0, 0))
    result = _applied(animation.swing(stage, "/World/Door", axis="z", angle_deg=90, seconds=2, round_trip=True, dry_run=False))
    assert abs(_heading(door, 24) - 45) < 1e-4 and abs(_heading(door, 48) - 90) < 1e-4
    assert abs(_heading(door, 72) - 45) < 1e-4 and abs(_heading(door, 96)) < 1e-4
    assert result["hinge_world_point"] == [1, 0, 0] and result["hinge_world_direction"] == [0, 0, 1]
    assert "hinge" in result["explanation"]
    _close(_pos(door, 50), (1, 0, 0))

    gate = UsdGeom.Xform.Define(stage, "/World/Gate")
    gxf = UsdGeom.Xformable(gate)
    gxf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
    gxf.AddRotateZOp().Set(10.0)
    _applied(animation.swing(stage, "/World/Gate", axis="up", angle_deg=-80, seconds=1, dry_run=False))
    rz = gate.GetPrim().GetAttribute("xformOp:rotateZ")
    assert abs(rz.Get(24) - (-70)) < 1e-6 and abs(rz.Get(12) - (-30)) < 1e-6
    _refused(animation.swing(stage, "/World/Gate", axis="x", angle_deg=30, seconds=1), "rotatez")
    _refused(animation.swing(stage, "/World/Gate", axis=None, angle_deg=30, seconds=1), "axis is required")
    _refused(animation.swing(stage, "/World/Gate", axis="z", angle_deg=0, seconds=1), "angle_deg")


# --- clear_animation --------------------------------------------------------------------------------------


def test_clear_animation_keeps_value_at_time_and_reports_other_layers():
    stage = _stage(start=0, end=24)
    prim = _composer(stage, "/World/Box")
    _applied(animation.add_keyframes(stage, "/World/Box", translate={0: (0, 0, 0), 24: (4, 0, 0)},
                                     rotate={0: 0.0, 24: 90.0}, dry_run=False))
    before = _layers(stage)
    dry = _ok(animation.clear_animation(stage, "/World/Box", attributes=["xformOp:translate"], keep_value_at=12))
    assert _layers(stage) == before and dry["clears"][0]["samples_removed"] == 2
    assert dry["clears"][0]["default_kept"] == [2, 0, 0]
    _applied(animation.clear_animation(stage, "/World/Box", attributes=["xformOp:translate"], keep_value_at=12, dry_run=False))
    translate = prim.GetAttribute("xformOp:translate")
    assert translate.GetNumTimeSamples() == 0
    _close(translate.Get(Usd.TimeCode.Default()), (2, 0, 0))
    _close(_pos(prim, 0), (2, 0, 0))
    assert prim.GetAttribute("xformOp:rotateXYZ").GetNumTimeSamples() == 2  # not listed, so kept
    _applied(animation.clear_animation(stage, "/World/Box", dry_run=False))  # None = everything in the edit target
    assert prim.GetAttribute("xformOp:rotateXYZ").GetNumTimeSamples() == 0
    _close(prim.GetAttribute("xformOp:rotateXYZ").Get(), (0, 0, 0))
    nothing = _ok(animation.clear_animation(stage, "/World/Box", attributes=["xformOp:scale"], dry_run=False))
    assert nothing["applied"] is False and any("nothing to clear" in w for w in nothing["warnings"])

    # samples that live in another layer are reported, not cleared
    layered = _stage()
    sub = Sdf.Layer.CreateAnonymous("anim.usda")
    layered.GetRootLayer().subLayerPaths.append(sub.identifier)
    layered.SetEditTarget(Usd.EditTarget(sub))
    cube = _composer(layered, "/World/Box")
    cube.GetAttribute("xformOp:translate").Set(Gf.Vec3d(1, 0, 0), 5)
    layered.SetEditTarget(Usd.EditTarget(layered.GetRootLayer()))
    report = _ok(animation.clear_animation(layered, "/World/Box", attributes=["xformOp:translate"], dry_run=False))
    assert report["applied"] is False and any("another layer" in w for w in report["warnings"])
    assert cube.GetAttribute("xformOp:translate").GetNumTimeSamples() == 1
    _refused(animation.clear_animation(stage, "/World/Box", attributes=["nope"]), "no attribute")
    _refused(animation.clear_animation(stage, "/World/Box", keep_value_at="x"), "keep_value_at")


# --- refusals, dry runs, rollback -------------------------------------------------------------------------


def test_refusals_instance_proxy_stronger_opinion_matrix_and_ambiguous_stacks():
    stage = _stage()
    proto = UsdGeom.Xform.Define(stage, "/Proto")
    _composer(stage, "/Proto/Part")
    inst = UsdGeom.Xform.Define(stage, "/World/Inst").GetPrim()
    inst.GetReferences().AddInternalReference(proto.GetPath())
    inst.SetInstanceable(True)
    assert stage.GetPrimAtPath("/World/Inst/Part").IsInstanceProxy()
    before = _layers(stage)
    _refused(animation.turntable(stage, "/World/Inst/Part", dry_run=False), "instance")

    matrix = UsdGeom.Xform.Define(stage, "/World/M")
    UsdGeom.Xformable(matrix).AddTransformOp().Set(Gf.Matrix4d(1.0))
    _refused(animation.turntable(stage, "/World/M", dry_run=False), "matrix")
    before = _layers(stage)
    two = UsdGeom.Xform.Define(stage, "/World/Two")
    UsdGeom.Xformable(two).AddRotateXOp().Set(1.0)
    UsdGeom.Xformable(two).AddRotateZOp().Set(1.0)
    before = _layers(stage)
    _refused(animation.turntable(stage, "/World/Two", dry_run=False), "more than one rotation")
    _refused(animation.turntable(stage, "/World/Missing", dry_run=False), "does not exist")
    UsdGeom.Scope.Define(stage, "/World/Scope")
    before = _layers(stage)
    _refused(animation.turntable(stage, "/World/Scope", dry_run=False), "not transformable")
    _refused(animation.add_keyframes(stage, "/World/Two", translate={"abc": (0, 0, 0)}), "time code")
    _refused(animation.add_keyframes(stage, "/World/Two", translate={"0": (0, 0, 0), "0.0": (1, 0, 0)}), "two keys")
    _refused(animation.add_keyframes(stage, "/World/Two"), "at least one")
    _refused(animation.add_keyframes(stage, "World/Two", translate={0: (0, 0, 0)}), "absolute prim path")
    assert _layers(stage) == before

    # a stronger layer's opinion would hide samples written to a weaker sublayer
    layered = _stage()
    sub = Sdf.Layer.CreateAnonymous("weak.usda")
    layered.GetRootLayer().subLayerPaths.append(sub.identifier)
    _composer(layered, "/World/Box")  # authored in the (stronger) root layer
    layered.SetEditTarget(Usd.EditTarget(sub))
    sub_before = sub.ExportToString()
    _refused(animation.add_keyframes(layered, "/World/Box", translate={0: (0, 0, 0), 5: (1, 0, 0)}, dry_run=False), "stronger")
    assert sub.ExportToString() == sub_before

    locked = _stage()
    _composer(locked, "/World/Box")
    locked.GetRootLayer().SetPermissionToEdit(False)
    _refused(animation.turntable(locked, "/World/Box", dry_run=False), "read-only")


def test_dry_runs_are_default_and_byte_identical_for_every_writer():
    stage = _stage(start=0, end=48)
    _isaac(stage, "/World/A")
    _composer(stage, "/World/B")
    UsdGeom.Cube.Define(stage, "/World/Bare")
    _composer(stage, "/World/C").GetAttribute("xformOp:translate").Set(Gf.Vec3d(1, 0, 0), 3)
    before = _layers(stage)
    calls = [
        lambda **kw: animation.set_timeline(stage, start=0, end=100, fps=30, **kw),
        lambda **kw: animation.add_keyframes(stage, "/World/Bare", translate={0: (0, 0, 0), 10: (1, 0, 0)}, rotate={0: 0.0, 10: 5.0}, **kw),
        lambda **kw: animation.turntable(stage, "/World/A", **kw),
        lambda **kw: animation.move_along(stage, "/World/B", [(0, 0, 0), (1, 0, 0)], seconds=1, **kw),
        lambda **kw: animation.swing(stage, "/World/B", axis="z", angle_deg=45, seconds=1, **kw),
        lambda **kw: animation.clear_animation(stage, "/World/C", keep_value_at=0, **kw),
    ]
    for call in calls:
        for flags in ({}, {"dry_run": True}, {"dry_run": None}, {"dry_run": "yes"}, {"dry_run": 1}):
            result = _ok(call(**flags))
            assert result["dry_run"] is True and result["applied"] is False and result["written"] is False
            assert result["next_step"].startswith("Dry run") and result["explanation"].startswith("Dry run")
            assert not any("Read-only violation" in w for w in result["warnings"])
    assert _layers(stage) == before
    _applied(animation.turntable(stage, "/World/A", dry_run="false"))


def test_failed_write_rolls_back_every_spec_change():
    stage = _stage(start=0, end=24)
    bare = UsdGeom.Cube.Define(stage, "/World/Bare")
    _composer(stage, "/World/Box")
    before = _layers(stage)
    original = animation._write_one
    calls = []

    def flaky(prim, write, layer, edit_target):
        calls.append(write["attribute"])
        if len(calls) == 2:
            raise ValueError("simulated failure")
        original(prim, write, layer, edit_target)

    animation._write_one = flaky
    try:
        result = animation.add_keyframes(stage, "/World/Bare", translate={0: (0, 0, 0), 5: (1, 0, 0)},
                                         rotate={0: 0.0, 5: 10.0}, dry_run=False)
    finally:
        animation._write_one = original
    assert result["ok"] is False and result["written"] is False and "put back" in result["error"], result
    assert _layers(stage) == before and not bare.GetPrim().GetAttribute("xformOp:translate")

    original_order = animation._write_order

    def broken_order(prim, order):
        raise ValueError("order failure")

    animation._write_order = broken_order
    try:
        result = animation.add_keyframes(stage, "/World/Bare", rotate={0: 0.0, 5: 10.0}, dry_run=False)
    finally:
        animation._write_order = original_order
    assert result["ok"] is False and result["written"] is False and "xformOpOrder" in result["error"]
    assert _layers(stage) == before

    original_clear = animation._write_clear
    stage.GetPrimAtPath("/World/Box").GetAttribute("xformOp:translate").Set(Gf.Vec3d(1, 0, 0), 2)
    stage.GetPrimAtPath("/World/Box").GetAttribute("xformOp:scale").Set(Gf.Vec3f(2, 2, 2), 2)
    before = _layers(stage)
    seen = []

    def flaky_clear(layer, clear):
        seen.append(clear["attribute"])
        original_clear(layer, clear)
        if len(seen) == 2:
            raise ValueError("clear failure")

    animation._write_clear = flaky_clear
    try:
        result = animation.clear_animation(stage, "/World/Box", keep_value_at=2, dry_run=False)
    finally:
        animation._write_clear = original_clear
    assert result["ok"] is False and result["written"] is False and _layers(stage) == before


# --- read-only: sample and conveyor_plan ------------------------------------------------------------------


def test_sample_reports_world_pose_at_times_and_refuses_bad_input():
    stage = _stage(start=0, end=48)
    parent = UsdGeom.Xform.Define(stage, "/World/Table")
    UsdGeom.Xformable(parent).AddTranslateOp().Set(Gf.Vec3d(0, 0, 1))
    prim = _isaac(stage, "/World/Table/Item")
    _applied(animation.turntable(stage, "/World/Table/Item", seconds=2, dry_run=False))
    _applied(animation.add_keyframes(stage, "/World/Table/Item", translate={0: (0, 0, 0), 48: (2, 0, 0)}, dry_run=False))
    before = _layers(stage)
    result = _ok(animation.sample(stage, "/World/Table/Item", [0, 12, 24, 48]))
    assert _layers(stage) == before and result["read_only"] is True
    rows = result["samples"]
    assert [r["time"] for r in rows] == [0, 12, 24, 48] and rows[1]["seconds"] == 0.5
    _close(rows[2]["world_position"], (1, 0, 1), 1e-9)
    assert abs(rows[1]["heading_about_up_deg"] - 90) < 1e-4 and abs(abs(rows[2]["heading_about_up_deg"]) - 180) < 1e-4
    assert set(result["animated_ops"]) == {"xformOp:translate", "xformOp:orient"}
    assert len(rows[0]["ops"]) == 3 and len(rows[0]["world_rotation_quat"]) == 4
    single = _ok(animation.sample(stage, "/World/Table/Item", 6))
    assert len(single["samples"]) == 1
    still = _ok(animation.sample(stage, "/World/Table", [0]))
    assert any("no animated" in w for w in still["warnings"])
    assert animation.sample(stage, "/World/Table/Item", [])["ok"] is False
    assert animation.sample(stage, "/World/Table/Item", ["x"])["ok"] is False
    assert animation.sample(stage, "/World/Nope", [0])["ok"] is False
    assert prim


def test_conveyor_plan_cites_installed_physx_schema_and_writes_nothing():
    stage = _stage(up="Z", meters_per_unit=0.01)
    belt = UsdGeom.Cube.Define(stage, "/World/Conveyor").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(belt)
    UsdPhysics.CollisionAPI.Apply(belt)
    belt.AddAppliedSchema("PhysxSurfaceVelocityAPI")
    before = _layers(stage)
    plan = _ok(animation.conveyor_plan(stage, "/World/Conveyor", speed=0.5, direction="-y", boxes=["/World/Box_01"]))
    assert _layers(stage) == before
    assert plan["plan_only"] is True and plan["written"] is False and plan["surface_velocity"] == [0, -50, 0]
    schema = plan["schema"]
    assert schema["api"] == "PhysxSurfaceVelocityAPI"
    assert "physxSurfaceVelocity:surfaceVelocity" in schema["attributes"]
    assert schema["schema_source"].endswith("PhysxSchema/resources/generatedSchema.usda")
    status = plan["belt_status"]
    assert status["rigid_body"] and status["collider"] and status["surface_velocity_api"] and not status["kinematic"]
    assert any("kinematic" in w for w in plan["warnings"]) and any("PhysicsScene" in w for w in plan["warnings"])
    snippet = plan["python_snippet"]
    assert "PhysxSchema.PhysxSurfaceVelocityAPI.Apply(belt)" in snippet and "CreateKinematicEnabledAttr" in snippet
    assert "'/World/Box_01'" in snippet and "Gf.Vec3f(0.0, -50.0, 0.0)" in snippet
    compile(snippet, "<conveyor snippet>", "exec")  # valid Python (not run: it needs Kit)
    assert len(plan["steps"]) == 5 and len(plan["comparison"]) == 2

    if os.path.exists(schema["schema_source"]):  # read-only check against the installed Isaac Sim 6.1
        with open(schema["schema_source"], encoding="utf-8") as handle:
            text = handle.read()
        assert 'class "PhysxSurfaceVelocityAPI"' in text
        for name in schema["attributes"]:
            assert name in text, name

    bare = _ok(animation.conveyor_plan())
    assert bare["surface_velocity"] == [0.5, 0, 0] and bare["belt_status"] is None
    assert animation.conveyor_plan(speed=-1)["ok"] is False
    assert animation.conveyor_plan(direction="+w")["ok"] is False


def test_public_api_lists_and_json_safety():
    for name in animation.PUBLIC_FUNCTIONS:
        assert callable(getattr(animation, name))
    assert set(animation.READ_ONLY_FUNCTIONS) <= set(animation.PUBLIC_FUNCTIONS)
    stage = _stage()
    _isaac(stage, "/World/A")
    for result in (
        animation.timeline_info(stage),
        animation.turntable(stage, "/World/A"),
        animation.sample(stage, "/World/A", [0, 1.5]),
        animation.conveyor_plan(stage),
        animation.turntable(None, "/World/A"),
    ):
        json.dumps(result, allow_nan=False)
        assert "explanation" in result and "warnings" in result
