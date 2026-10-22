"""Smart layout (snap, stack rows, distribute on a shelf, space evenly, align) on temporary in-memory stages.

Z-up and Y-up, meters and centimeters, nested rotated/scaled parents, Isaac-style translate + orient + scale stacks,
float versus double translate ops, exact positions after apply, byte-identical layers after dry runs, and refusals.
"""

import json
import math

try:
    from pxr import Gf, Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import layout

TOL = 1e-6
DOUBLE = UsdGeom.XformOp.PrecisionDouble
FLOAT = UsdGeom.XformOp.PrecisionFloat


# --- helpers ----------------------------------------------------------------------------------------------


def _stage(up="Z", meters_per_unit=1.0):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def _cube(stage, path, translate=(0.0, 0.0, 0.0), size=1.0, scale=None, precision=DOUBLE):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(float(size))
    xformable = UsdGeom.Xformable(cube)
    op = xformable.AddTranslateOp(precision)
    op.Set(Gf.Vec3f(*translate) if precision == FLOAT else Gf.Vec3d(*translate))
    if scale is not None:
        xformable.AddScaleOp().Set(Gf.Vec3f(*scale))
    return cube.GetPrim()


def _floor(stage, path="/World/Floor", up="Z", top=0.0, half=10.0):
    """A flat cube (thickness 0.125, exact in float) whose top is at `top` along the up axis."""
    center = [0.0, 0.0, 0.0]
    scale = [2 * half, 2 * half, 2 * half]
    index = 2 if up == "Z" else 1
    center[index] = top - 0.0625
    scale[index] = 0.125
    return _cube(stage, path, center, size=1.0, scale=scale)


def _isaac(stage, path, translate, quat=(1.0, 0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), translate_precision=DOUBLE, orient_precision=FLOAT):
    """Isaac Sim style: xformOpOrder = [translate, orient, scale] (XformCommonAPI cannot edit it)."""
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    xformable = UsdGeom.Xformable(cube)
    t = xformable.AddTranslateOp(translate_precision)
    t.Set(Gf.Vec3f(*translate) if translate_precision == FLOAT else Gf.Vec3d(*translate))
    o = xformable.AddOrientOp(orient_precision)
    w, x, y, z = quat
    o.Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)) if orient_precision == FLOAT else Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    xformable.AddScaleOp(FLOAT).Set(Gf.Vec3f(*scale))
    return cube.GetPrim()


def _xform(stage, path, translate=(0, 0, 0), rotate=None, quat=None, scale=None):
    xform = UsdGeom.Xform.Define(stage, path)
    xformable = UsdGeom.Xformable(xform)
    xformable.AddTranslateOp().Set(Gf.Vec3d(*translate))
    if rotate is not None:
        xformable.AddRotateXYZOp().Set(Gf.Vec3f(*rotate))
    if quat is not None:
        w, x, y, z = quat
        xformable.AddOrientOp(DOUBLE).Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    if scale is not None:
        xformable.AddScaleOp().Set(Gf.Vec3f(*scale))
    return xform.GetPrim()


def _box(prim):
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    return [float(v) for v in box.GetMin()], [float(v) for v in box.GetMax()]


def _center(prim):
    low, high = _box(prim)
    return [(a + b) / 2 for a, b in zip(low, high)]


def _world(prim):
    return UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)


def _close(first, second, tol=TOL):
    assert len(first) == len(second), (first, second)
    assert all(abs(a - b) <= tol for a, b in zip(first, second)), (first, second)


def _layers(stage):
    return [layer.ExportToString() for layer in stage.GetLayerStack(True)]


def _linear(matrix):
    return [matrix[r][c] for r in range(3) for c in range(3)]


def _op_names(prim):
    return [str(op.GetOpName()) for op in UsdGeom.Xformable(prim).GetOrderedXformOps()]


def _ok(result):
    json.dumps(result, allow_nan=False)
    assert result["ok"], result.get("error")
    return result


def _refused(result, *words):
    json.dumps(result, allow_nan=False)
    assert result["ok"] is False and result["applied"] is False and result["written"] is False, result
    text = result["error"].lower()
    for word in words:
        assert word.lower() in text, (word, result["error"])
    assert result["explanation"].startswith("Nothing was moved")
    return result


def _quat_about(axis, degrees):
    half = math.radians(degrees) / 2
    s = math.sin(half)
    return (math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


# --- snap_to_surface --------------------------------------------------------------------------------------


def test_snap_dry_run_is_default_and_leaves_layers_byte_identical():
    stage = _stage()
    _floor(stage)
    a = _cube(stage, "/World/A", (1, 2, 3.5))
    before = _layers(stage)
    dirty = stage.GetRootLayer().dirty
    result = _ok(layout.snap_to_surface(stage, ["/World/A"]))
    assert result["dry_run"] is True and result["applied"] is False and result["written"] is False
    assert _layers(stage) == before and stage.GetRootLayer().dirty == dirty
    assert result["surface"] == "/World/Floor"
    move = result["moves"][0]
    assert move["moved"] and move["translate_kind"] == "translate_op"
    _close(move["from_world"], [1, 2, 3.5])
    _close(move["to_world"], [1, 2, 0.5])
    _close(move["from_local"], [1, 2, 3.5])
    _close(move["to_local"], [1, 2, 0.5])
    assert "Dry run" in result["explanation"] and "meters" in result["explanation"]
    assert "/World/A" in result["preview"] and "Z -3" in result["preview"]
    assert "dry_run=False" in result["next_step"]
    _close(_center(a), [1, 2, 3.5])


def test_snap_apply_z_up_meters_rests_exactly_on_auto_floor():
    stage = _stage()
    _floor(stage, top=0.25)
    a = _cube(stage, "/World/A", (1, 2, 3.5))
    b = _cube(stage, "/World/B", (-3, 0, 0.1), size=0.4)  # sunk into the floor: lifted up
    c = _cube(stage, "/World/C", (5, 5, 0.75))  # already resting: unchanged
    result = _ok(layout.snap_to_surface(stage, ["/World/A", "/World/B", "/World/C"], dry_run=False))
    assert result["applied"] and result["written"] and result["surface"] == "/World/Floor"
    assert [m["moved"] for m in result["moves"]] == [True, True, False]
    for prim in (a, b, c):
        assert abs(_box(prim)[0][2] - 0.25) < TOL
    _close(_center(a)[:2], [1, 2])
    _close(_center(b)[:2], [-3, 0])
    for move, prim in zip(result["moves"], (a, b, c)):
        _close(move["to_world"], list(_world(prim).ExtractTranslation()))
        _close(move["to_local"], list(prim.GetAttribute("xformOp:translate").Get()))


def test_snap_y_up_centimeters_uses_y_and_floor_search_skips_walls_and_higher_surfaces():
    stage = _stage(up="Y", meters_per_unit=0.01)
    _floor(stage, up="Y", top=0.0, half=500.0)
    _cube(stage, "/World/Wall", (0, 150, -400), size=1.0, scale=(800, 300, 10))  # tall, not flat
    _cube(stage, "/World/HighShelf", (300, 400, 0), size=1.0, scale=(2000, 5, 2000))  # flat but above the prim
    box = _cube(stage, "/World/Box", (20, 80, 30), size=40)
    result = _ok(layout.snap_to_surface(stage, "/World/Box", dry_run=False))
    assert result["up_axis"] == "Y" and result["meters_per_unit"] == 0.01
    assert result["surface"] == "/World/Floor"
    assert "centimeters" in result["explanation"]
    low, _high = _box(box)
    assert abs(low[1] - 0.0) < TOL
    _close(_center(box), [20, 20, 30])


def test_snap_onto_named_surface_and_height_number_and_footprint_warning():
    stage = _stage()
    _floor(stage)
    _cube(stage, "/World/Table", (0, 0, 0.75), size=1.0, scale=(2, 1, 0.1))  # top at 0.8
    cup = _cube(stage, "/World/Cup", (0.5, 0.2, 2), size=0.1)
    far = _cube(stage, "/World/Far", (9, 9, 2), size=0.1)
    result = _ok(layout.snap_to_surface(stage, ["/World/Cup", "/World/Far"], surface="/World/Table", dry_run=False))
    assert abs(result["surface_top"] - 0.8) < TOL
    assert abs(_box(cup)[0][2] - 0.8) < TOL and abs(_box(far)[0][2] - 0.8) < TOL
    assert any("/World/Far is not above /World/Table" in w for w in result["warnings"])
    result = _ok(layout.snap_to_surface(stage, ["/World/Cup"], surface=-2.5, dry_run=False))
    assert result["surface"] is None and abs(_box(cup)[0][2] + 2.5) < TOL


def test_snap_under_rotated_and_scaled_parents_keeps_rotation_and_scale():
    stage = _stage()
    _floor(stage, top=1.0)
    _xform(stage, "/World/Rig", translate=(5, 2, 4), rotate=(90, 0, 30), scale=(2, 3, 0.5))
    _xform(stage, "/World/Rig/Inner", translate=(1, -1, 2), quat=_quat_about((0.6, 0.0, 0.8), 40), scale=(1.5, 1.5, 1.5))
    item = _isaac(stage, "/World/Rig/Inner/Item", (0.3, 0.2, -0.4), quat=_quat_about((1, 0, 0), 25), scale=(0.5, 0.7, 0.9))
    linear_before = _linear(_world(item))
    rig_world = _world(stage.GetPrimAtPath("/World/Rig"))
    center_before = _center(item)
    result = _ok(layout.snap_to_surface(stage, ["/World/Rig/Inner/Item"], dry_run=False))
    assert result["surface"] == "/World/Floor"
    low, _high = _box(item)
    assert abs(low[2] - 1.0) < 1e-6
    _close(_center(item)[:2], center_before[:2])
    _close(_linear(_world(item)), linear_before, 1e-9)
    assert _world(stage.GetPrimAtPath("/World/Rig")) == rig_world
    assert _op_names(item) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]
    assert item.GetAttribute("xformOp:orient").GetTypeName() == Sdf.ValueTypeNames.Quatf
    move = result["moves"][0]
    _close(move["to_local"], list(item.GetAttribute("xformOp:translate").Get()))
    _close(move["to_world"], list(_world(item).ExtractTranslation()))


def test_float_translate_stays_float_and_double_stays_double():
    stage = _stage()
    _floor(stage)
    f = _cube(stage, "/World/F", (1.1, 2.2, 3.3), precision=FLOAT)
    d = _cube(stage, "/World/D", (1.1, 2.2, 3.3), precision=DOUBLE)
    result = _ok(layout.snap_to_surface(stage, ["/World/F", "/World/D"], dry_run=False))
    assert [m["precision"] for m in result["moves"]] == ["float", "double"]
    assert f.GetAttribute("xformOp:translate").GetTypeName() == Sdf.ValueTypeNames.Float3
    assert d.GetAttribute("xformOp:translate").GetTypeName() == Sdf.ValueTypeNames.Double3
    assert isinstance(f.GetAttribute("xformOp:translate").Get(), Gf.Vec3f)
    assert abs(_box(f)[0][2]) < 1e-5 and abs(_box(d)[0][2]) < TOL
    _close(result["moves"][0]["to_local"], list(f.GetAttribute("xformOp:translate").Get()), 1e-9)


def test_translate_after_rotate_op_and_matrix_op_and_missing_translate_op():
    stage = _stage()
    _floor(stage)
    # translate listed after a rotate: the change is mapped through that rotation
    spun = UsdGeom.Cube.Define(stage, "/World/Spun")
    spun.CreateSizeAttr(1.0)
    spun_x = UsdGeom.Xformable(spun)
    spun_x.AddRotateXYZOp().Set(Gf.Vec3f(90, 0, 0))
    spun_x.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    # one matrix op
    mat = UsdGeom.Cube.Define(stage, "/World/Mat")
    mat.CreateSizeAttr(1.0)
    matrix = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 30)) * Gf.Matrix4d().SetScale(Gf.Vec3d(2, 2, 2))
    matrix.SetTranslateOnly(Gf.Vec3d(4, 4, 5))
    UsdGeom.Xformable(mat).AddTransformOp().Set(matrix)
    # orient + scale only (no translate op yet)
    bare = UsdGeom.Cube.Define(stage, "/World/Bare")
    bare.CreateSizeAttr(1.0)
    bare_x = UsdGeom.Xformable(bare)
    bare_x.AddOrientOp(FLOAT).Set(Gf.Quatf(1, Gf.Vec3f(0, 0, 0)))
    bare_x.AddScaleOp().Set(Gf.Vec3f(1, 1, 2))
    bare_before_z = _box(bare.GetPrim())[0][2]
    assert bare_before_z == -1.0
    paths = ["/World/Spun", "/World/Mat", "/World/Bare"]
    spun_linear = _linear(_world(spun.GetPrim()))
    mat_linear = _linear(_world(mat.GetPrim()))
    lifted = _ok(layout.snap_to_surface(stage, paths, dry_run=False))
    assert [m["translate_kind"] for m in lifted["moves"]] == ["translate_op", "transform_matrix", "new_translate_op"]
    for path in paths:
        assert abs(_box(stage.GetPrimAtPath(path))[0][2]) < TOL, path
    _close(_linear(_world(spun.GetPrim())), spun_linear, 1e-9)
    _close(_linear(_world(mat.GetPrim())), mat_linear, 1e-9)
    assert _op_names(spun.GetPrim()) == ["xformOp:rotateXYZ", "xformOp:translate"]
    assert _op_names(mat.GetPrim()) == ["xformOp:transform"]
    assert _op_names(bare.GetPrim()) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]
    assert bare.GetPrim().GetAttribute("xformOp:translate").GetTypeName() == Sdf.ValueTypeNames.Double3
    _close(list(bare.GetPrim().GetAttribute("xformOp:translate").Get()), [0, 0, 1])


def test_reset_xform_stack_prim_ignores_parent_transform():
    stage = _stage()
    _floor(stage)
    _xform(stage, "/World/Moved", translate=(100, 100, 100), rotate=(0, 45, 0))
    cube = UsdGeom.Cube.Define(stage, "/World/Moved/Free")
    cube.CreateSizeAttr(2.0)
    xformable = UsdGeom.Xformable(cube)
    xformable.SetResetXformStack(True)
    xformable.AddTranslateOp().Set(Gf.Vec3d(3, 3, 7))
    _ok(layout.snap_to_surface(stage, ["/World/Moved/Free"], dry_run=False))
    _close(_center(cube.GetPrim()), [3, 3, 1])
    assert UsdGeom.Xformable(cube).GetResetXformStack()
    _close(list(cube.GetPrim().GetAttribute("xformOp:translate").Get()), [3, 3, 1])


# --- stack_rows -------------------------------------------------------------------------------------------


def _boxes(stage, count, size=1.0, start=(0, 0, 5), make=None):
    paths = []
    for index in range(count):
        path = f"/World/Box_{index:02d}"
        position = (start[0] + 3 * index, start[1] + index, start[2] + index)
        if make is None:
            _cube(stage, path, position, size=size)
        else:
            make(stage, path, position)
        paths.append(path)
    return paths


def test_stack_rows_pyramid_on_point_anchor_z_up_facing_minus_y():
    stage = _stage()
    paths = _boxes(stage, 10)
    result = _ok(layout.stack_rows(stage, paths, anchor=(10, 20, 0), dry_run=False))
    assert result["rows"] == [4, 3, 2, 1] and result["facing"] == "-Y" and result["row_axis"] == "X"
    expected = [
        (8.5, 0.5), (9.5, 0.5), (10.5, 0.5), (11.5, 0.5),
        (9.0, 1.5), (10.0, 1.5), (11.0, 1.5),
        (9.5, 2.5), (10.5, 2.5),
        (10.0, 3.5),
    ]
    for path, (x, z) in zip(paths, expected):
        _close(_center(stage.GetPrimAtPath(path)), [x, 20, z])
    assert result["stack_height"] == 4.0
    assert result["placements"][4] == {"path": paths[4], "row": 2, "position_in_row": 1}


def test_stack_rows_facing_plus_y_runs_right_to_left_in_x_and_gap_is_used():
    stage = _stage()
    paths = _boxes(stage, 3)
    _ok(layout.stack_rows(stage, paths, rows=[2, 1], anchor=(0, 0, 0), gap=0.5, facing="+Y", dry_run=False))
    # Seen from +Y the viewer's right is -X, so the first prim gets the larger X.
    _close(_center(stage.GetPrimAtPath(paths[0])), [0.75, 0, 0.5])
    _close(_center(stage.GetPrimAtPath(paths[1])), [-0.75, 0, 0.5])
    _close(_center(stage.GetPrimAtPath(paths[2])), [0, 0, 1.5])


def test_stack_rows_y_up_centimeters_default_facing_plus_z_and_prim_anchor():
    stage = _stage(up="Y", meters_per_unit=0.01)
    _cube(stage, "/World/Pallet", (100, 7.5, -50), size=1.0, scale=(120, 15, 80))  # top at y = 15
    paths = _boxes(stage, 3, size=40)
    result = _ok(layout.stack_rows(stage, paths, rows=(2, 1), anchor="/World/Pallet", dry_run=False))
    assert result["facing"] == "+Z" and result["row_axis"] == "X" and result["anchor"] == "/World/Pallet"
    _close(_center(stage.GetPrimAtPath(paths[0])), [80, 35, -50])
    _close(_center(stage.GetPrimAtPath(paths[1])), [120, 35, -50])
    _close(_center(stage.GetPrimAtPath(paths[2])), [100, 75, -50])


def test_stack_rows_isaac_stacks_under_rotated_parent_with_mixed_sizes():
    stage = _stage()
    _xform(stage, "/World/Env", translate=(2, -3, 0.5), quat=_quat_about((0, 0, 1), 37), scale=(1.2, 1.2, 1.2))

    def make(stage_, path, position):
        child = path.replace("/World/", "/World/Env/")
        _isaac(stage_, child, position, quat=_quat_about((0, 0, 1), 15), scale=(0.5, 0.5, 0.5),
               translate_precision=DOUBLE, orient_precision=FLOAT)

    _boxes(stage, 4, make=make)
    paths = [f"/World/Env/Box_{i:02d}" for i in range(4)]
    sizes_before = [[h - l for l, h in zip(*_box(stage.GetPrimAtPath(p)))] for p in paths]
    linear_before = [_linear(_world(stage.GetPrimAtPath(p))) for p in paths]
    result = _ok(layout.stack_rows(stage, paths, rows=[3, 1], anchor=(0, 0, 0), dry_run=False))
    width = sizes_before[0][0]
    height = sizes_before[0][2]
    depth_center = 0.0
    boxes = [_box(stage.GetPrimAtPath(p)) for p in paths]
    for index in range(3):
        low, high = boxes[index]
        assert abs(low[2]) < 1e-6
        assert abs((low[0] + high[0]) / 2 - (index - 1) * width) < 1e-6
        assert abs((low[1] + high[1]) / 2 - depth_center) < 1e-6
    low, high = boxes[3]
    assert abs(low[2] - height) < 1e-6 and abs((low[0] + high[0]) / 2) < 1e-6
    for path, before in zip(paths, linear_before):
        _close(_linear(_world(stage.GetPrimAtPath(path))), before, 1e-9)
        assert _op_names(stage.GetPrimAtPath(path)) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]
    assert all(m["precision"] == "double" for m in result["moves"])


def test_stack_rows_default_anchor_keeps_combined_center_and_lowest_bottom():
    stage = _stage()
    paths = _boxes(stage, 3, start=(0, 0, 5))  # centers x 0, 3, 6; y 0, 1, 2; z 5, 6, 7
    _ok(layout.stack_rows(stage, paths, rows=[3], dry_run=False))
    for index, path in enumerate(paths):
        _close(_center(stage.GetPrimAtPath(path)), [2 + index, 1, 5])


def test_stack_rows_refusals():
    stage = _stage()
    paths = _boxes(stage, 3)
    _refused(layout.stack_rows(stage, paths), "hold 10", "3 path")
    _refused(layout.stack_rows(stage, paths, rows=[2, 0, 1]), "whole number")
    _refused(layout.stack_rows(stage, paths, rows=[2.0, 1]), "whole number")
    _refused(layout.stack_rows(stage, paths, rows="3"), "list of whole numbers")
    _refused(layout.stack_rows(stage, paths, rows=[3], facing="+Z"), "up axis")
    _refused(layout.stack_rows(stage, paths, rows=[3], facing="sideways"), "facing")
    _refused(layout.stack_rows(stage, paths, rows=[3], gap=-1), "gap")
    _refused(layout.stack_rows(stage, paths, rows=[3], anchor=(1, 2)), "anchor")
    _refused(layout.stack_rows(stage, paths, rows=[3], anchor="/World/Box_00"), "being moved")
    _refused(layout.stack_rows(stage, paths, rows=[3], anchor="/World/Nope"), "does not exist")


# --- distribute_on ----------------------------------------------------------------------------------------


def test_distribute_on_shelf_long_axis_y_with_margin_and_equal_gaps():
    stage = _stage()
    _cube(stage, "/World/Shelf", (1, 2, 1.0), size=1.0, scale=(0.8, 4.0, 0.1))  # long along Y, top z = 1.05
    _cube(stage, "/World/A", (9, 9, 9), size=0.5)
    _cube(stage, "/World/B", (8, 8, 8), size=0.3)
    _cube(stage, "/World/C", (7, 7, 7), size=0.5)
    result = _ok(layout.distribute_on(stage, ["/World/A", "/World/B", "/World/C"], "/World/Shelf", margin=0.1, dry_run=False))
    assert result["long_axis"] == "Y" and result["margin"] == 0.1
    # usable 4 - 0.2 = 3.8; widths 1.3; gaps (3.8 - 1.3) / 2 = 1.25
    assert abs(result["spacing"] - 1.25) < TOL
    _close(_center(stage.GetPrimAtPath("/World/A")), [1, 0.1 + 0.25, 1.3])
    _close(_center(stage.GetPrimAtPath("/World/B")), [1, 0.1 + 0.5 + 1.25 + 0.15, 1.2])
    _close(_center(stage.GetPrimAtPath("/World/C")), [1, 3.9 - 0.25, 1.3])


def test_distribute_on_default_margin_is_five_centimeters_in_stage_units():
    stage = _stage(meters_per_unit=0.01)
    _cube(stage, "/World/Shelf", (0, 0, 100), size=1.0, scale=(200, 50, 4))
    _cube(stage, "/World/A", (0, 0, 300), size=20)
    _cube(stage, "/World/B", (0, 0, 300), size=20)
    result = _ok(layout.distribute_on(stage, ["/World/A", "/World/B"], "/World/Shelf", dry_run=False))
    assert result["margin"] == 5.0 and result["long_axis"] == "X"
    _close(_center(stage.GetPrimAtPath("/World/A")), [-85, 0, 112])
    _close(_center(stage.GetPrimAtPath("/World/B")), [85, 0, 112])


def test_distribute_on_single_item_is_centered_and_refuses_when_too_long():
    stage = _stage()
    _cube(stage, "/World/Shelf", (0, 0, 0), size=1.0, scale=(2, 0.5, 0.1))
    _cube(stage, "/World/A", (3, 3, 3), size=0.6)
    _ok(layout.distribute_on(stage, ["/World/A"], "/World/Shelf", dry_run=False))
    _close(_center(stage.GetPrimAtPath("/World/A")), [0, 0, 0.35])
    assert any("overhangs" in w for w in layout.distribute_on(stage, ["/World/A"], "/World/Shelf")["warnings"])
    for name in ("B", "C", "D"):
        _cube(stage, f"/World/{name}", (3, 3, 3), size=0.6)
    before = _layers(stage)
    _refused(layout.distribute_on(stage, ["/World/A", "/World/B", "/World/C", "/World/D"], "/World/Shelf", dry_run=False), "do not fit")
    assert _layers(stage) == before
    _refused(layout.distribute_on(stage, ["/World/A"], "/World/Shelf", margin=-1), "margin")
    _refused(layout.distribute_on(stage, ["/World/A"], "/World/A"), "being moved")


def test_distribute_on_rotated_surface_warns():
    stage = _stage()
    shelf = _cube(stage, "/World/Shelf", (0, 0, 0), size=1.0, scale=(4, 0.5, 0.1))
    UsdGeom.Xformable(shelf).AddRotateZOp().Set(30.0)
    UsdGeom.Xformable(shelf).SetXformOpOrder(
        [UsdGeom.XformOp(shelf.GetAttribute(n)) for n in ("xformOp:translate", "xformOp:rotateZ", "xformOp:scale")]
    )
    _cube(stage, "/World/A", (3, 3, 3), size=0.2)
    result = _ok(layout.distribute_on(stage, ["/World/A"], "/World/Shelf"))
    assert any("at an angle" in w for w in result["warnings"])


# --- space_evenly and align -------------------------------------------------------------------------------


def test_space_evenly_keeps_ends_or_uses_spacing_in_current_order():
    stage = _stage()
    _cube(stage, "/World/A", (0, 0, 0), size=1)
    _cube(stage, "/World/B", (10, 5, 1), size=2)
    _cube(stage, "/World/C", (2, -1, 2), size=1)
    result = _ok(layout.space_evenly(stage, ["/World/A", "/World/B", "/World/C"], axis="x", dry_run=False))
    # order along x: A [-0.5, 0.5], C [1.5, 2.5], B [9, 11]; span 11.5, widths 4, gap 3.75
    assert result["order"] == ["/World/A", "/World/C", "/World/B"] and abs(result["spacing"] - 3.75) < 1e-9
    _close(_center(stage.GetPrimAtPath("/World/A")), [0, 0, 0])
    _close(_center(stage.GetPrimAtPath("/World/C")), [4.75, -1, 2])
    _close(_center(stage.GetPrimAtPath("/World/B")), [10, 5, 1])
    _ok(layout.space_evenly(stage, ["/World/A", "/World/B", "/World/C"], axis="X", spacing=0.5, dry_run=False))
    _close(_center(stage.GetPrimAtPath("/World/C")), [1.5, -1, 2])
    _close(_center(stage.GetPrimAtPath("/World/B")), [3.5, 5, 1])


def test_space_evenly_up_axis_and_two_prims_warning():
    stage = _stage(up="Y")
    _cube(stage, "/World/A", (0, 0, 0), size=1)
    _cube(stage, "/World/B", (0, 4, 0), size=1)
    result = _ok(layout.space_evenly(stage, ["/World/A", "/World/B"], axis="up"))
    assert result["axis"] == "Y" and not any(m["moved"] for m in result["moves"])
    assert any("two prims" in w for w in result["warnings"])
    result = _ok(layout.space_evenly(stage, ["/World/A", "/World/B"], axis="up", spacing=1, dry_run=False))
    assert result["applied"]
    _close(_center(stage.GetPrimAtPath("/World/B")), [0, 2, 0])


def test_align_min_center_max():
    stage = _stage()
    _cube(stage, "/World/A", (0, 0, 0), size=1)
    _cube(stage, "/World/B", (5, 3, 1), size=2)
    _cube(stage, "/World/C", (-2, 7, 4), size=4)
    paths = ["/World/A", "/World/B", "/World/C"]
    _ok(layout.align(stage, paths, axis="y", mode="min", dry_run=False))
    assert all(abs(_box(stage.GetPrimAtPath(p))[0][1] + 0.5) < TOL for p in paths)
    _ok(layout.align(stage, paths, axis="z", mode="max", dry_run=False))
    assert all(abs(_box(stage.GetPrimAtPath(p))[1][2] - 6) < TOL for p in paths)
    result = _ok(layout.align(stage, paths, axis="x", mode="center", dry_run=False))
    assert abs(result["target"] - 1.0) < TOL  # union of x ranges [-4, 6]
    assert all(abs(_center(stage.GetPrimAtPath(p))[0] - 1.0) < TOL for p in paths)
    _refused(layout.align(stage, paths, axis="w"), "axis")
    _refused(layout.align(stage, paths, mode="middle"), "mode")
    _refused(layout.align(stage, ["/World/A"]), "at least two")


def test_nothing_to_do_writes_nothing():
    stage = _stage()
    _cube(stage, "/World/A", (0, 0, 0), size=1)
    _cube(stage, "/World/B", (3, 0, 0), size=1)
    before = _layers(stage)
    result = _ok(layout.align(stage, ["/World/A", "/World/B"], axis="z", dry_run=False))
    assert result["applied"] is False and result["written"] is False and "already in place" in result["explanation"]
    assert _layers(stage) == before


# --- refusals, layers and safety --------------------------------------------------------------------------


def test_refuses_bad_inputs_without_writing():
    stage = _stage()
    _floor(stage)
    _cube(stage, "/World/A", (0, 0, 3))
    UsdGeom.Xform.Define(stage, "/World/Empty")
    stage.DefinePrim("/World/Group", "Scope")
    _cube(stage, "/World/Group/Child", (0, 0, 3))
    _xform(stage, "/World/Parent")
    _cube(stage, "/World/Parent/Kid", (0, 0, 3))
    before = _layers(stage)
    _refused(layout.snap_to_surface(stage, [], dry_run=False), "list")
    _refused(layout.snap_to_surface(stage, ["/World/A", "/World/A"], dry_run=False), "twice")
    _refused(layout.snap_to_surface(stage, ["/World/Parent", "/World/Parent/Kid"], dry_run=False), "inside")
    _refused(layout.snap_to_surface(stage, ["/World/Missing"], dry_run=False), "does not exist")
    _refused(layout.snap_to_surface(stage, ["/World/Empty"], dry_run=False), "no bounds")
    _refused(layout.snap_to_surface(stage, ["/World/Group"], dry_run=False), "not transformable")
    _refused(layout.snap_to_surface(stage, ["World/A"], dry_run=False), "absolute")
    _refused(layout.snap_to_surface(stage, ["/World/A"], surface="/World/Nope", dry_run=False), "does not exist")
    _refused(layout.snap_to_surface(stage, ["/World/Parent/Kid"], surface="/World/Parent", dry_run=False), "contains")
    _refused(layout.snap_to_surface(stage, ["/World/A"], surface=float("nan"), dry_run=False), "finite")
    _refused(layout.snap_to_surface(None, ["/World/A"]), "no usd stage")
    assert _layers(stage) == before


def test_refuses_instance_proxy_animated_inverted_and_inactive():
    stage = _stage()
    _floor(stage)
    _xform(stage, "/World/Proto")
    _cube(stage, "/World/Proto/Part", (0, 0, 2))
    holder = _xform(stage, "/World/Inst", translate=(5, 0, 3))
    holder.GetReferences().AddInternalReference("/World/Proto")
    holder.SetInstanceable(True)
    anim = _cube(stage, "/World/Anim", (0, 0, 3))
    anim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(0, 0, 4), 10.0)
    inv = UsdGeom.Cube.Define(stage, "/World/Inv")
    inv_x = UsdGeom.Xformable(inv)
    inv_x.AddTranslateOp().Set(Gf.Vec3d(1, 1, 1))
    inv_x.AddTranslateOp(DOUBLE, "", True)
    off = _cube(stage, "/World/Off", (0, 0, 3))
    off.SetActive(False)
    before = _layers(stage)
    _refused(layout.snap_to_surface(stage, ["/World/Inst/Part"], dry_run=False), "instance")
    _refused(layout.snap_to_surface(stage, ["/World/Anim"], dry_run=False), "animated")
    _refused(layout.snap_to_surface(stage, ["/World/Inv"], dry_run=False), "invert")
    _refused(layout.snap_to_surface(stage, ["/World/Off"], dry_run=False), "inactive")
    assert _layers(stage) == before
    # The instance prim itself can move; its proxies come along.
    _ok(layout.snap_to_surface(stage, ["/World/Inst"], dry_run=False))
    assert abs(_box(stage.GetPrimAtPath("/World/Inst"))[0][2]) < TOL


def test_floor_missing_and_read_only_edit_target_refused():
    stage = _stage()
    _cube(stage, "/World/A", (0, 0, 3))
    _refused(layout.snap_to_surface(stage, ["/World/A"]), "no floor")
    _floor(stage)
    stage.GetRootLayer().SetPermissionToEdit(False)
    try:
        _refused(layout.snap_to_surface(stage, ["/World/A"], dry_run=False), "read-only")
    finally:
        stage.GetRootLayer().SetPermissionToEdit(True)


def test_writes_go_to_edit_target_sublayer_and_stronger_opinion_is_reported():
    stage = _stage()
    sub = Sdf.Layer.CreateAnonymous("layout-sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    _floor(stage)
    _cube(stage, "/World/A", (0, 0, 3))
    root_text = stage.GetRootLayer().ExportToString()
    stage.SetEditTarget(Usd.EditTarget(sub))
    result = _ok(layout.snap_to_surface(stage, ["/World/A"], dry_run=False))
    assert result["edit_target_layer"] == sub.identifier
    assert stage.GetRootLayer().ExportToString() == root_text
    _close(list(sub.GetAttributeAtPath("/World/A.xformOp:translate").default), [0, 0, 0.5])
    assert any("stronger opinion" in w for w in result["warnings"])
    assert any("away from the planned spot" in w for w in result["warnings"])
    assert abs(_box(stage.GetPrimAtPath("/World/A"))[0][2] - 2.5) < TOL  # the root layer still wins


def test_failed_write_rolls_back_earlier_moves():
    stage = _stage()
    _floor(stage)
    _cube(stage, "/World/A", (0, 0, 3))
    _cube(stage, "/World/B", (2, 0, 3))
    before = _layers(stage)
    original = layout._author
    calls = []

    def flaky(item):
        calls.append(item.path)
        if len(calls) == 2:
            raise ValueError("simulated failure")
        original(item)

    layout._author = flaky
    try:
        result = layout.snap_to_surface(stage, ["/World/A", "/World/B"], dry_run=False)
    finally:
        layout._author = original
    assert result["ok"] is False and result["written"] is False and "put back" in result["error"]
    assert _layers(stage) == before


def test_dry_run_flag_parsing_and_json_results():
    stage = _stage()
    _floor(stage)
    _cube(stage, "/World/A", (0, 0, 3))
    before = _layers(stage)
    for flag in (True, None, "true", "yes", 1):
        result = _ok(layout.snap_to_surface(stage, ["/World/A"], dry_run=flag))
        assert result["dry_run"] is True
    assert _layers(stage) == before
    result = _ok(layout.snap_to_surface(stage, ["/World/A"], dry_run="false"))
    assert result["applied"] is True
    for name in layout.PUBLIC_FUNCTIONS:
        assert callable(getattr(layout, name))


def test_preview_is_short_table_and_truncates():
    stage = _stage()
    _floor(stage, half=500)
    paths = [f"/World/B{i}" for i in range(30)]
    for index, path in enumerate(paths):
        _cube(stage, path, (index * 2, 0, 3))
    result = _ok(layout.snap_to_surface(stage, paths))
    lines = result["preview"].splitlines()
    assert lines[0].startswith("prim") and "from (world)" in lines[0]
    assert len(lines) == 1 + layout.MAX_PREVIEW_ROWS + 1 and lines[-1] == "... and 5 more"
    _refused(layout.snap_to_surface(stage, ["/World/B0"] * 2), "twice")
    _refused(layout.snap_to_surface(stage, [f"/World/X{i}" for i in range(layout.MAX_PRIMS + 1)]), "at most")
