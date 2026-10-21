"""Light and camera authoring plus look-at math, run on temporary in-memory and temp-file stages (Z-up and Y-up)."""

import ast
import json
import math
import random
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux
except ImportError:  # pragma: no cover - depends on the local environment
    import pytest

    pytest.skip("USD Python bindings (pxr) are not installed", allow_module_level=True)

from omniverse_scene_mcp import authoring, look_at

CAR_CENTER = (5.0, 5.0, 1.0)


def _stage(up_axis="Z"):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up_axis)
    UsdGeom.Xform.Define(stage, "/World")
    car = UsdGeom.Cube.Define(stage, "/World/Car")  # size 2, so its world bbox center is its translation
    UsdGeom.XformCommonAPI(car).SetTranslate(Gf.Vec3d(*CAR_CENTER))
    return stage


def _snapshot(stage):
    return stage.GetRootLayer().ExportToString(), stage.GetSessionLayer().ExportToString()


def _unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    return [value / length for value in vector]


def _world(prim):
    return UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)


def _assert_aims_at(prim, target, tolerance=1e-4):
    """Transform local (0, 0, -1) to world space and check it points from the prim's origin at target."""
    world = _world(prim)
    forward = _unit(world.TransformDir(Gf.Vec3d(0.0, 0.0, -1.0)))
    eye = world.ExtractTranslation()
    wanted = _unit([target[index] - eye[index] for index in range(3)])
    assert all(abs(forward[index] - wanted[index]) < tolerance for index in range(3)), (forward, wanted)


def _world_up(prim):
    return _unit(_world(prim).TransformDir(Gf.Vec3d(0.0, 1.0, 0.0)))


def _assert_json(result):
    json.dumps(result, allow_nan=False)


def _refused(result, fragment):
    assert result["ok"] is False, result
    assert result["written"] is False
    assert fragment.lower() in result["error"].lower(), result["error"]


# --- look_at -------------------------------------------------------------------------------------------------


def test_look_at_points_minus_z_at_target_for_both_up_axes():
    rng = random.Random(7)
    for axis in ("Z", "Y"):
        up = look_at.UP_VECTORS[axis]
        for _ in range(300):
            eye = [rng.uniform(-50, 50) for _ in range(3)]
            target = [rng.uniform(-50, 50) for _ in range(3)]
            result = look_at.look_at_rotation(eye, target, axis)
            wanted = _unit([target[i] - eye[i] for i in range(3)])
            assert not result["degenerate"]
            # Verify by transforming local -Z with each form of the result: matrix, Euler and quaternion.
            for rows in (
                result["matrix"],
                look_at.euler_to_matrix(result["rotate_xyz"], "XYZ"),
                look_at.quaternion_to_matrix(result["quaternion"]),
            ):
                forward = look_at.transform_direction(rows, (0.0, 0.0, -1.0))
                assert all(abs(forward[i] - wanted[i]) < 1e-6 for i in range(3)), (forward, wanted)
            right = result["right"]
            assert abs(sum(right[i] * up[i] for i in range(3))) < 1e-6  # no roll: right stays level
            assert sum(result["up"][i] * up[i] for i in range(3)) > 0.0  # local +Y leans toward the up axis
            assert result["quaternion"][0] >= 0.0
            _assert_json(result)


def test_look_at_straight_up_or_down_uses_a_stable_fallback_up():
    cases = {
        ("Z", -1.0): (0.0, 1.0, 0.0),  # Z-up, looking down: image top faces +Y
        ("Z", 1.0): (0.0, -1.0, 0.0),
        ("Y", -1.0): (0.0, 0.0, -1.0),  # Y-up, looking down: image top faces -Z (continuous with tilting down)
        ("Y", 1.0): (0.0, 0.0, 1.0),
    }
    for (axis, sign), expected_up in cases.items():
        axis_vector = look_at.UP_VECTORS[axis]
        eye = (1.0, 2.0, 3.0)
        target = tuple(eye[i] + sign * 10.0 * axis_vector[i] for i in range(3))
        result = look_at.look_at_rotation(eye, target, axis)
        assert result["fallback_up"] == list(expected_up)
        assert result["up"] == list(expected_up)
        assert result["forward"] == [sign * value for value in axis_vector]
        assert result["warnings"] and "straight above or below" in result["warnings"][0]
        # A target a hair off the axis falls in the same branch, so the roll does not flip.
        nudged = list(target)
        nudged[0] += 1e-9
        assert look_at.look_at_rotation(eye, nudged, axis)["up"] == list(expected_up)


def test_look_at_eye_equal_to_target_is_degenerate_identity():
    result = look_at.look_at_rotation((1.0, 2.0, 3.0), (1.0, 2.0, 3.0), "Y")
    assert result["degenerate"] is True
    assert result["rotate_xyz"] == [0.0, 0.0, 0.0]
    assert result["quaternion"] == [1.0, 0.0, 0.0, 0.0]
    assert result["warnings"]


def test_look_at_rejects_bad_input():
    for bad in (
        lambda: look_at.look_at_rotation((0, 0, 0), (1, 0, 0), "X"),
        lambda: look_at.look_at_rotation((0, 0, float("nan")), (1, 0, 0), "Z"),
        lambda: look_at.look_at_rotation((0, 0), (1, 0, 0), "Z"),
        lambda: look_at.look_at_rotation((0, 0, 0), (1, 0, 0), (0, 0, 0)),
        lambda: look_at.euler_to_matrix((1, 2, 3), "XXY"),
    ):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_euler_round_trips_for_every_order_including_gimbal_lock():
    rng = random.Random(3)
    for order in look_at.ROTATION_ORDERS:
        for trial in range(200):
            angles = [rng.uniform(-180, 180) for _ in range(3)]
            if trial % 8 == 0:  # middle axis at +-90 degrees
                angles["XYZ".index(order[1])] = rng.choice((90.0, -90.0))
            rows = look_at.euler_to_matrix(angles, order)
            back = look_at.euler_to_matrix(look_at.matrix_to_euler(rows, order), order)
            assert max(abs(rows[r][c] - back[r][c]) for r in range(3) for c in range(3)) < 1e-9
            quat_rows = look_at.quaternion_to_matrix(look_at.matrix_to_quaternion(rows))
            assert max(abs(rows[r][c] - quat_rows[r][c]) for r in range(3) for c in range(3)) < 1e-9


def test_look_at_matrices_match_usd_xform_ops():
    stage = Usd.Stage.CreateInMemory()
    rng = random.Random(11)
    for index, order in enumerate(look_at.ROTATION_ORDERS):
        angles = [rng.uniform(-180, 180) for _ in range(3)]
        xformable = UsdGeom.Xformable(stage.DefinePrim(f"/Euler{index}", "Xform"))
        getattr(xformable, f"AddRotate{order}Op")(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(*angles))
        usd = xformable.GetLocalTransformation()
        ours = look_at.euler_to_matrix(angles, order)
        assert max(abs(usd[r][c] - ours[r][c]) for r in range(3) for c in range(3)) < 1e-9

    result = look_at.look_at_rotation((3.0, -4.0, 2.0), (-1.0, 6.0, 0.5), "Z")
    xformable = UsdGeom.Xformable(stage.DefinePrim("/Orient", "Xform"))
    w, x, y, z = result["quaternion"]
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    forward = _unit(xformable.GetLocalTransformation().TransformDir(Gf.Vec3d(0.0, 0.0, -1.0)))
    assert all(abs(forward[i] - result["forward"][i]) < 1e-9 for i in range(3))


# --- create_light ----------------------------------------------------------------------------------------------


def test_every_light_type_dry_run_then_authored_on_both_up_axes():
    cases = [
        ("rect", (2.0, 1.0), {"inputs:width": 2.0, "inputs:height": 1.0}),
        ("sphere", 0.5, {"inputs:radius": 0.5}),
        ("disk", 0.25, {"inputs:radius": 0.25}),
        ("cylinder", (4.0, 0.125), {"inputs:length": 4.0, "inputs:radius": 0.125}),
        ("distant", 0.5, {"inputs:angle": 0.5}),
        ("dome", 9.0, {}),
    ]
    for axis in ("Z", "Y"):
        stage = _stage(axis)
        for kind, size, size_attrs in cases:
            path = f"/World/{kind.title()}Light"
            kwargs = dict(intensity=1500, exposure=1.5, color=(1.0, 0.5, 0.25), size=size, aim_at="/World/Car")
            if kind != "distant":
                kwargs["translation"] = (-3.0, -4.0, 6.0)
            before = _snapshot(stage)
            plan = authoring.create_light(stage, path, kind, **kwargs)
            assert _snapshot(stage) == before, kind
            assert plan["ok"] and plan["dry_run"] and not plan["written"], plan
            would = plan["would_create"]
            assert would["type"] == authoring.LIGHT_TYPES[kind]
            assert would["up_axis"] == axis
            expected = {"inputs:intensity": 1500.0, "inputs:exposure": 1.5, "inputs:color": [1.0, 0.5, 0.25], **size_attrs}
            assert would["attributes"] == expected, would["attributes"]
            assert would["xform_op_order"] == ["xformOp:translate", "xformOp:rotateXYZ"]
            _assert_json(plan)

            result = authoring.create_light(stage, path, kind, dry_run=False, **kwargs)
            assert result["ok"] and result["written"] and not result["dry_run"], result
            prim = stage.GetPrimAtPath(path)
            assert prim.GetTypeName() == authoring.LIGHT_TYPES[kind]
            for name, value in expected.items():
                authored = prim.GetAttribute(name).Get()
                if isinstance(value, list):
                    assert all(abs(authored[i] - value[i]) < 1e-6 for i in range(3))
                else:
                    assert abs(authored - value) < 1e-6, (name, authored)
            assert list(UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get()) == ["xformOp:translate", "xformOp:rotateXYZ"]
            assert UsdGeom.XformCommonAPI(prim)  # the new op stack stays XformCommonAPI-compatible
            assert result["created"]["attributes"].keys() == expected.keys()
            if kind == "dome":
                assert would["aim"] is None
                assert any("size was ignored" in w for w in plan["warnings"])
                assert any("aim_at was ignored" in w for w in plan["warnings"])
                assert tuple(prim.GetAttribute("xformOp:rotateXYZ").Get()) == (0.0, 0.0, 0.0)
            else:
                _assert_aims_at(prim, CAR_CENTER)
                assert would["aim"]["target_source"] == "bbox_center"
                assert would["aim"]["target_point"] == list(CAR_CENTER)
                assert result["created"]["aim_error_degrees"] < 1e-3
                assert sum(_world_up(prim)[i] * look_at.UP_VECTORS[axis][i] for i in range(3)) > 0.0
            _assert_json(result)
        warnings = authoring.create_light(stage, "/World/Sun2", "distant", translation=(0, 0, 9))["warnings"]
        assert any("Distant lights are infinitely far away" in w for w in warnings)


def test_rect_light_scalar_size_and_point_target():
    stage = _stage("Y")
    result = authoring.create_light(stage, "/World/Panel", "RectLight", size=3, translation=(0, 5, 5), aim_at=(0, 0, 0), dry_run=False)
    assert result["ok"], result
    prim = stage.GetPrimAtPath("/World/Panel")
    assert prim.GetAttribute("inputs:width").Get() == 3.0 and prim.GetAttribute("inputs:height").Get() == 3.0
    assert result["would_create"]["aim"]["target_source"] == "point"
    _assert_aims_at(prim, (0.0, 0.0, 0.0))


def test_light_value_validation_refuses_without_writing():
    stage = _stage()
    before = _snapshot(stage)
    cases = [
        (dict(light_type="rect", intensity=-1), "intensity"),
        (dict(light_type="rect", intensity=True), "intensity"),
        (dict(light_type="rect", intensity=float("inf")), "finite"),
        (dict(light_type="rect", exposure="bright"), "exposure"),
        (dict(light_type="rect", color=(300, 0, 0)), "color"),
        (dict(light_type="rect", color=(-0.1, 0, 0)), "negative"),
        (dict(light_type="rect", color=(1, 0)), "color"),
        (dict(light_type="rect", size=(1, 0)), "size"),
        (dict(light_type="sphere", size=(1, 2)), "radius"),
        (dict(light_type="cylinder", size=2), "length, radius"),
        (dict(light_type="distant", size=200), "180"),
        (dict(light_type="spot"), "light_type"),
        (dict(light_type="rect", translation=(1, 2)), "translation"),
        (dict(light_type="rect", aim_at=42), "target"),
    ]
    for kwargs, fragment in cases:
        light_type = kwargs.pop("light_type")
        for dry_run in (True, False):
            _refused(authoring.create_light(stage, "/World/Bad", light_type, dry_run=dry_run, **kwargs), fragment)
    assert _snapshot(stage) == before


def test_color_0_255_is_normalized_with_a_warning():
    stage = _stage()
    plan = authoring.create_light(stage, "/World/Warm", "disk", color=(255, 128, 0))
    assert plan["would_create"]["attributes"]["inputs:color"] == [1.0, round(128 / 255, 9), 0.0]
    assert any("divided by 255" in w for w in plan["warnings"])
    plain = authoring.create_light(stage, "/World/Warm", "disk", color=(1, 0.5, 0))
    assert plain["would_create"]["attributes"]["inputs:color"] == [1.0, 0.5, 0.0]
    assert not any("divided by 255" in w for w in plain["warnings"])


# --- create_camera ---------------------------------------------------------------------------------------------


def test_camera_copies_optics_and_explicit_values_win():
    for axis in ("Z", "Y"):
        stage = _stage(axis)
        source = UsdGeom.Camera.Define(stage, "/World/Hero")
        source.CreateFocalLengthAttr(24.0)
        source.CreateHorizontalApertureAttr(36.0)
        source.CreateClippingRangeAttr(Gf.Vec2f(0.1, 500.0))
        source.CreateFocusDistanceAttr(7.5)
        source.CreateProjectionAttr("orthographic")
        before = _snapshot(stage)
        kwargs = dict(focal_length=50, translation=(10.0, -10.0, 4.0), aim_at="/World/Car", copy_optics_from="/World/Hero")
        plan = authoring.create_camera(stage, "/World/Shot", **kwargs)
        assert _snapshot(stage) == before
        assert plan["ok"], plan
        would = plan["would_create"]
        assert would["copied_optics_from"] == "/World/Hero"
        assert set(would["copied_attributes"]) == {"focalLength", "horizontalAperture", "clippingRange", "focusDistance", "projection"}
        assert would["attributes"]["focalLength"] == 50.0
        assert would["attributes"]["horizontalAperture"] == 36.0
        assert would["attributes"]["projection"] == "orthographic"
        assert any("override the copied focalLength" in w for w in plan["warnings"])
        _assert_json(plan)

        result = authoring.create_camera(stage, "/World/Shot", dry_run=False, **kwargs)
        assert result["ok"] and result["written"], result
        camera = UsdGeom.Camera(stage.GetPrimAtPath("/World/Shot"))
        assert camera.GetFocalLengthAttr().Get() == 50.0
        assert camera.GetHorizontalApertureAttr().Get() == 36.0
        assert tuple(camera.GetClippingRangeAttr().Get()) == (Gf.Vec2f(0.1, 500.0)[0], 500.0)
        assert abs(camera.GetFocusDistanceAttr().Get() - 7.5) < 1e-6
        assert camera.GetProjectionAttr().Get() == "orthographic"
        assert not camera.GetVerticalApertureAttr().HasAuthoredValue()  # unauthored on the source, so not copied
        _assert_aims_at(camera.GetPrim(), CAR_CENTER)
        assert sum(_world_up(camera.GetPrim())[i] * look_at.UP_VECTORS[axis][i] for i in range(3)) > 0.0
        _assert_json(result)


def test_camera_copy_of_animated_lens_uses_earliest_sample():
    stage = _stage()
    source = UsdGeom.Camera.Define(stage, "/World/Zoom")
    attr = source.CreateFocalLengthAttr()
    attr.Set(18.0, 10)
    attr.Set(85.0, 20)
    plan = authoring.create_camera(stage, "/World/Copy", copy_optics_from="/World/Zoom", aim_at=(0, 0, 0), translation=(0, -5, 1))
    assert plan["would_create"]["attributes"]["focalLength"] == 18.0
    assert any("earliest time" in w for w in plan["warnings"])


def test_camera_refusals_and_z_up_hint():
    stage = _stage()
    UsdGeom.Scope.Define(stage, "/World/Group")
    before = _snapshot(stage)
    _refused(authoring.create_camera(stage, "/World/C", copy_optics_from="/World/Nope"), "does not exist")
    _refused(authoring.create_camera(stage, "/World/C", copy_optics_from="/World/Car"), "must be a camera")
    _refused(authoring.create_camera(stage, "/World/C", focal_length=0), "focal_length")
    _refused(authoring.create_camera(stage, "/World/C", horizontal_aperture=-2, dry_run=False), "horizontal_aperture")
    _refused(authoring.create_camera(stage, "/World/C", clipping_range=(10, 1)), "greater than near")
    _refused(authoring.create_camera(stage, "/World/C", clipping_range=(0, 10)), "clipping_range")
    _refused(authoring.create_camera(stage, "/World/C", up_axis="X"), "up_axis")
    assert _snapshot(stage) == before
    plan = authoring.create_camera(stage, "/World/Group/C")
    assert plan["ok"] and any("looks straight down" in w for w in plan["warnings"])
    y_plan = authoring.create_camera(_stage("Y"), "/World/C")
    assert not any("looks straight down" in w for w in y_plan["warnings"])


# --- create refusals and layers ----------------------------------------------------------------------------------


def test_create_refuses_existing_paths_missing_parents_and_instances():
    stage = _stage()
    proto = UsdGeom.Xform.Define(stage, "/Proto")
    UsdGeom.Cube.Define(stage, "/Proto/Box")
    instance = stage.DefinePrim("/World/Inst")
    instance.GetReferences().AddInternalReference(proto.GetPath())
    instance.SetInstanceable(True)
    stage.DefinePrim("/World/Off").SetActive(False)
    before = _snapshot(stage)
    for dry_run in (True, False):
        _refused(authoring.create_light(stage, "/World/Car", "rect", dry_run=dry_run), "already exists")
        _refused(authoring.create_light(stage, "/World/Missing/Key", "rect", dry_run=dry_run), "parent prim does not exist")
        _refused(authoring.create_camera(stage, "World/Cam", dry_run=dry_run), "absolute prim path")
        _refused(authoring.create_camera(stage, "/World/bad name", dry_run=dry_run), "not a valid usd path")
        _refused(authoring.create_camera(stage, "/World.attr", dry_run=dry_run), "absolute prim path")
        _refused(authoring.create_camera(stage, "/World/Off/Cam", dry_run=dry_run), "inactive")
        _refused(authoring.create_camera(stage, "/World/Inst/Cam", dry_run=dry_run), "instance")
        _refused(authoring.create_light(None, "/World/Key", "rect", dry_run=dry_run), "no usd stage")
        _refused(authoring.create_light(stage, "/World/Key", "rect", aim_at="/World/Ghost", dry_run=dry_run), "target prim does not exist")
    assert _snapshot(stage) == before


def test_dry_run_leaves_a_saved_file_layer_untouched():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "scene.usda"
        stage = Usd.Stage.CreateNew(str(path))
        UsdGeom.SetStageUpAxis(stage, "Y")
        UsdGeom.Xform.Define(stage, "/World")
        UsdGeom.Cube.Define(stage, "/World/Car")
        UsdGeom.Camera.Define(stage, "/World/Cam").AddTranslateOp().Set(Gf.Vec3d(0.0, 2.0, 10.0))
        stage.GetRootLayer().Save()
        file_bytes = path.read_bytes()
        before = _snapshot(stage)
        results = [
            authoring.create_light(stage, "/World/Key", "rect", intensity=10, aim_at="/World/Car", translation=(1, 2, 3)),
            authoring.create_camera(stage, "/World/Cam2", translation=(4, 3, 2), aim_at="/World/Car", copy_optics_from="/World/Cam"),
            authoring.aim(stage, "/World/Cam", "/World/Car"),
            authoring.aim(stage, "/World/Cam", "/World/Car", dry_run=None),
        ]
        for result in results:
            assert result["ok"] and result["dry_run"] and not result["written"], result
            assert result["edit_target"]["root_layer"] and not result["edit_target"]["anonymous"]
            assert not any("anonymous" in w or "session layer" in w for w in result["warnings"])
            _assert_json(result)
        assert _snapshot(stage) == before
        assert not stage.GetRootLayer().dirty
        assert path.read_bytes() == file_bytes

        changed = authoring.aim(stage, "/World/Cam", "/World/Car", dry_run="false")
        assert changed["ok"] and changed["written"], changed
        assert stage.GetRootLayer().dirty and path.read_bytes() == file_bytes  # authored in memory, not saved
        _assert_aims_at(stage.GetPrimAtPath("/World/Cam"), (0.0, 0.0, 0.0))


def test_edit_target_warnings_and_read_only_refusal():
    stage = _stage()
    anonymous = authoring.create_light(stage, "/World/Key", "rect")
    assert any("anonymous in-memory layer" in w for w in anonymous["warnings"])
    assert anonymous["edit_target_layer"] == stage.GetRootLayer().identifier

    root_before = stage.GetRootLayer().ExportToString()
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    session = authoring.create_light(stage, "/World/Key", "sphere", aim_at="/World/Car", translation=(0, 0, 5), dry_run=False)
    assert session["ok"] and session["edit_target"]["session_layer"], session
    assert any("session layer" in w and "not saved" in w for w in session["warnings"])
    assert stage.GetRootLayer().ExportToString() == root_before
    assert stage.GetSessionLayer().GetPrimAtPath("/World/Key")
    assert any("ShapingAPI" in w for w in session["warnings"])

    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    stage.GetRootLayer().SetPermissionToEdit(False)
    try:
        _refused(authoring.create_camera(stage, "/World/Cam", dry_run=False), "read-only")
        _refused(authoring.aim(stage, "/World/Key", "/World/Car", dry_run=False), "read-only")
    finally:
        stage.GetRootLayer().SetPermissionToEdit(True)


# --- aim -------------------------------------------------------------------------------------------------------


def test_aim_edits_each_supported_stack_in_place():
    for axis in ("Z", "Y"):
        stage = _stage(axis)
        xform_common = UsdGeom.Camera.Define(stage, "/World/CommonCam")
        xform_common.AddTranslateOp().Set(Gf.Vec3d(-6.0, -6.0, 3.0))
        xform_common.AddRotateXYZOp().Set(Gf.Vec3f(10.0, 20.0, 30.0))
        xform_common.AddScaleOp().Set(Gf.Vec3f(2.0, 3.0, 0.5))

        translate_only = UsdGeom.Camera.Define(stage, "/World/BareCam")
        translate_only.AddTranslateOp().Set(Gf.Vec3d(8.0, -2.0, 6.0))
        translate_only.AddScaleOp().Set(Gf.Vec3f(1.0, 1.0, 1.0))

        zyx = UsdGeom.Xform.Define(stage, "/World/ZyxLight")
        zyx.AddTranslateOp().Set(Gf.Vec3d(1.0, 9.0, 4.0))
        zyx.AddRotateZYXOp().Set(Gf.Vec3f(5.0, 6.0, 7.0))

        isaac_d = UsdGeom.Xform.Define(stage, "/World/IsaacD")
        isaac_d.AddTranslateOp().Set(Gf.Vec3d(-3.0, 7.0, 2.0))
        isaac_d.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(1.0, Gf.Vec3d(0.0, 0.0, 0.0)))
        isaac_d.AddScaleOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(1.0, 2.0, 3.0))

        isaac_f = UsdGeom.Xform.Define(stage, "/World/IsaacF")
        isaac_f.AddTranslateOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(4.0, 4.0, 9.0))
        isaac_f.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0)))

        matrix = UsdGeom.Xform.Define(stage, "/World/Matrix")
        matrix.AddTransformOp().Set(Gf.Matrix4d().SetScale(2.0) * Gf.Matrix4d().SetTranslate(Gf.Vec3d(9.0, 1.0, -2.0)))

        expectations = {
            "/World/CommonCam": ("xform_common", "xformOp:rotateXYZ"),
            "/World/BareCam": ("xform_common", "xformOp:rotateXYZ"),
            "/World/ZyxLight": ("xform_common", "xformOp:rotateZYX"),
            "/World/IsaacD": ("translate_orient_scale", "xformOp:orient"),
            "/World/IsaacF": ("translate_orient_scale", "xformOp:orient"),
            "/World/Matrix": ("transform_matrix", "xformOp:transform"),
        }
        for path, (kind, op_name) in expectations.items():
            prim = stage.GetPrimAtPath(path)
            translation_before = _world(prim).ExtractTranslation()
            before = _snapshot(stage)
            plan = authoring.aim(stage, path, "/World/Car")
            assert _snapshot(stage) == before, path
            assert plan["ok"], plan
            would = plan["would_change"]
            assert would["xform_stack"] == kind and would["changes"][0]["attribute"] == op_name, would
            assert would["up_axis"] == axis
            _assert_json(plan)

            result = authoring.aim(stage, path, "/World/Car", dry_run=False)
            assert result["ok"] and result["written"], result
            assert result["changed"]["aim_error_degrees"] < 1e-3
            assert list(UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get()) == would["xform_op_order"]
            _assert_aims_at(prim, CAR_CENTER)
            after = _world(prim).ExtractTranslation()
            assert (after - translation_before).GetLength() < 1e-6, path  # aim only rotates
            _assert_json(result)

        assert list(translate_only.GetXformOpOrderAttr().Get()) == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
        assert isinstance(stage.GetPrimAtPath("/World/IsaacD").GetAttribute("xformOp:orient").Get(), Gf.Quatd)
        assert isinstance(stage.GetPrimAtPath("/World/IsaacF").GetAttribute("xformOp:orient").Get(), Gf.Quatf)
        assert tuple(stage.GetPrimAtPath("/World/IsaacD").GetAttribute("xformOp:scale").Get()) == (1.0, 2.0, 3.0)
        kept = Gf.Transform(stage.GetPrimAtPath("/World/Matrix").GetAttribute("xformOp:transform").Get())
        assert all(abs(value - 2.0) < 1e-9 for value in kept.GetScale())
        common_up = _world_up(stage.GetPrimAtPath("/World/BareCam"))
        assert sum(common_up[i] * look_at.UP_VECTORS[axis][i] for i in range(3)) > 0.0


def test_aim_through_rotated_scaled_parent_and_reset_xform_stack():
    stage = _stage()
    rig = UsdGeom.Xform.Define(stage, "/World/Rig")
    rig.AddTranslateOp().Set(Gf.Vec3d(2.0, -3.0, 1.0))
    rig.AddRotateXYZOp().Set(Gf.Vec3f(15.0, -30.0, 70.0))
    rig.AddScaleOp().Set(Gf.Vec3f(2.0, 1.0, 0.5))
    camera = UsdGeom.Camera.Define(stage, "/World/Rig/Cam")
    camera.AddTranslateOp().Set(Gf.Vec3d(1.0, 2.0, 3.0))
    result = authoring.aim(stage, "/World/Rig/Cam", "/World/Car", dry_run=False)
    assert result["ok"], result
    _assert_aims_at(camera.GetPrim(), CAR_CENTER)

    created = authoring.create_camera(stage, "/World/Rig/Cam2", translation=(-1.0, 4.0, 2.0), aim_at=(0.0, 0.0, 0.0), dry_run=False)
    assert created["ok"], created
    _assert_aims_at(stage.GetPrimAtPath("/World/Rig/Cam2"), (0.0, 0.0, 0.0))

    loose = UsdGeom.Camera.Define(stage, "/World/Rig/Loose")
    loose.SetResetXformStack(True)
    loose.AddTranslateOp().Set(Gf.Vec3d(0.0, -9.0, 2.0))
    assert authoring.aim(stage, "/World/Rig/Loose", "/World/Car", dry_run=False)["ok"]
    _assert_aims_at(loose.GetPrim(), CAR_CENTER)


def test_aim_straight_down_and_up_axis_override():
    stage = _stage("Z")
    camera = UsdGeom.Camera.Define(stage, "/World/Top")
    camera.AddTranslateOp().Set(Gf.Vec3d(5.0, 5.0, 20.0))
    result = authoring.aim(stage, "/World/Top", "/World/Car", dry_run=False)
    assert result["ok"], result
    assert result["would_change"]["aim"]["fallback_up"] is not None
    assert any("straight above or below" in w for w in result["warnings"])
    _assert_aims_at(camera.GetPrim(), CAR_CENTER)
    assert all(abs(a - b) < 1e-5 for a, b in zip(_world_up(camera.GetPrim()), (0.0, 1.0, 0.0)))

    side = UsdGeom.Camera.Define(stage, "/World/Side")
    side.AddTranslateOp().Set(Gf.Vec3d(5.0, -5.0, 1.0))
    override = authoring.aim(stage, "/World/Side", "/World/Car", up_axis="y", dry_run=False)
    assert override["ok"] and override["would_change"]["up_axis"] == "Y"
    assert any("overrides the stage up axis Z" in w for w in override["warnings"])
    _assert_aims_at(side.GetPrim(), CAR_CENTER)


def test_aim_target_without_bounds_uses_world_translation():
    stage = _stage("Y")
    marker = UsdGeom.Xform.Define(stage, "/World/Marker")
    marker.AddTranslateOp().Set(Gf.Vec3d(-4.0, 1.0, -8.0))
    camera = UsdGeom.Camera.Define(stage, "/World/Cam")
    camera.AddTranslateOp().Set(Gf.Vec3d(3.0, 2.0, 1.0))
    result = authoring.aim(stage, "/World/Cam", "/World/Marker", dry_run=False)
    assert result["ok"], result
    assert result["would_change"]["aim"]["target_source"] == "world_translation"
    assert result["would_change"]["aim"]["target_point"] == [-4.0, 1.0, -8.0]
    _assert_aims_at(camera.GetPrim(), (-4.0, 1.0, -8.0))


def test_aim_refusals_write_nothing():
    stage = _stage()
    proto = UsdGeom.Xform.Define(stage, "/Proto")
    UsdGeom.Cube.Define(stage, "/Proto/Box")
    instance = stage.DefinePrim("/World/Inst")
    instance.GetReferences().AddInternalReference(proto.GetPath())
    instance.SetInstanceable(True)

    animated = UsdGeom.Camera.Define(stage, "/World/Animated")
    animated.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 0.0), 1)
    single_axis = UsdGeom.Xform.Define(stage, "/World/SingleAxis")
    single_axis.AddRotateYOp().Set(30.0)
    pivoted = UsdGeom.Xform.Define(stage, "/World/Pivoted")
    UsdGeom.XformCommonAPI(pivoted).SetPivot(Gf.Vec3f(1.0, 0.0, 0.0))
    mirrored = UsdGeom.Xform.Define(stage, "/World/Mirrored")
    mirrored.AddTranslateOp().Set(Gf.Vec3d(0.0, -5.0, 2.0))
    mirrored.AddScaleOp().Set(Gf.Vec3f(1.0, 1.0, -1.0))
    child = UsdGeom.Xform.Define(stage, "/World/Mirrored/Child")
    UsdLux.DomeLight.Define(stage, "/World/Dome")
    UsdGeom.Scope.Define(stage, "/World/Scope")
    point = UsdGeom.Camera.Define(stage, "/World/AtPoint")
    point.AddTranslateOp().Set(Gf.Vec3d(1.0, 2.0, 3.0))

    before = _snapshot(stage)
    cases = [
        ("/World/Nope", "/World/Car", "does not exist"),
        ("/World/AtPoint", "/World/Ghost", "target prim does not exist"),
        ("/World/AtPoint", "/World/AtPoint", "itself"),
        ("/World/Mirrored", str(child.GetPath()), "descendant"),
        ("/World/Animated", "/World/Car", "animated"),
        ("/World/SingleAxis", "/World/Car", "unsupported xformoporder"),
        ("/World/Pivoted", "/World/Car", "pivot"),
        ("/World/Mirrored", "/World/Car", "negative scale"),
        ("/World/Dome", "/World/Car", "dome light"),
        ("/World/Scope", "/World/Car", "not transformable"),
        ("/World/Inst/Box", "/World/Car", "instance"),
        ("/World/AtPoint", (1.0, 2.0, 3.0), "no direction"),
        ("/World/AtPoint", (1.0, float("nan"), 3.0), "finite"),
        ("/World/AtPoint", "Car", "absolute prim path"),
    ]
    for path, target, fragment in cases:
        for dry_run in (True, False):
            result = authoring.aim(stage, path, target, dry_run=dry_run)
            _refused(result, fragment)
            _assert_json(result)
    assert _snapshot(stage) == before


def test_aim_warns_when_a_stronger_layer_hides_the_edit():
    stage = _stage()
    camera = UsdGeom.Camera.Define(stage, "/World/Cam")
    camera.AddTranslateOp().Set(Gf.Vec3d(0.0, -8.0, 2.0))
    rotate = camera.AddRotateXYZOp()
    rotate.Set(Gf.Vec3f(0.0, 0.0, 0.0))
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        rotate.Set(Gf.Vec3f(0.0, 0.0, 45.0))
    plan = authoring.aim(stage, "/World/Cam", "/World/Car")
    assert plan["ok"]
    assert any("stronger opinion" in w and "session" in w for w in plan["warnings"]), plan["warnings"]
    result = authoring.aim(stage, "/World/Cam", "/World/Car", dry_run=False)
    assert result["ok"] and result["written"]
    assert result["changed"]["aim_error_degrees"] > 1.0
    assert any("composed result does not point" in w for w in result["warnings"])
    assert stage.GetSessionLayer().GetAttributeAtPath("/World/Cam.xformOp:rotateXYZ").default == Gf.Vec3f(0.0, 0.0, 45.0)


def test_failed_create_rolls_back_the_new_prim_spec():
    stage = _stage()
    before = _snapshot(stage)
    original = authoring._author_new_xform

    def broken(*_args):
        raise RuntimeError("simulated failure")

    # Second stage: /World lives in a sublayer, so Define would also add an 'over World' to the root layer.
    sublayer = Sdf.Layer.CreateAnonymous("base.usda")
    layered = Usd.Stage.CreateInMemory()
    layered.GetRootLayer().subLayerPaths.append(sublayer.identifier)
    with Usd.EditContext(layered, sublayer):
        UsdGeom.Xform.Define(layered, "/World")
    layered_before = _snapshot(layered)

    authoring._author_new_xform = broken
    try:
        result = authoring.create_light(stage, "/World/Key", "rect", intensity=10, dry_run=False)
        layered_result = authoring.create_camera(layered, "/World/Cam", focal_length=20, dry_run=False)
    finally:
        authoring._author_new_xform = original
    for outcome in (result, layered_result):
        assert outcome["ok"] is False and outcome["written"] is False, outcome
        assert "removed again" in outcome["error"] and "simulated failure" in outcome["error"]
    assert not stage.GetPrimAtPath("/World/Key") and not layered.GetPrimAtPath("/World/Cam")
    assert _snapshot(stage) == before
    assert _snapshot(layered) == layered_before
    assert not layered.GetRootLayer().GetPrimAtPath("/World")


def test_modules_import_only_pxr_stdlib_and_pure_package_modules():
    """Both modules run inside Kit on the live stage, so they must not import omni or the router stack."""
    allowed = {
        authoring: ({"__future__", "math", "numbers", "typing", "pxr"}, {"look_at", "usd_insight"}),
        look_at: ({"__future__", "math", "typing"}, set()),
    }
    for module, (roots_allowed, relative_allowed) in allowed.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        roots, relative = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level:
                relative.update([node.module] if node.module else [alias.name for alias in node.names])
            elif isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
        assert roots <= roots_allowed, (module.__name__, roots)
        assert relative <= relative_allowed, (module.__name__, relative)


def test_results_stay_small_and_json_safe():
    stage = _stage()
    source = UsdGeom.Camera.Define(stage, "/World/Planes")
    source.CreateClippingPlanesAttr([Gf.Vec4f(0.0, 0.0, 1.0, float(index)) for index in range(500)])
    plan = authoring.create_camera(stage, "/World/Copy", copy_optics_from="/World/Planes", aim_at="/World/Car", translation=(0, -5, 1))
    assert plan["ok"], plan
    assert plan["would_create"]["attributes"]["clippingPlanes"]["truncated"] is True
    assert len(json.dumps(plan, allow_nan=False)) < 20_000
    created = authoring.create_camera(stage, "/World/Copy", copy_optics_from="/World/Planes", dry_run=False)
    assert len(stage.GetPrimAtPath("/World/Copy").GetAttribute("clippingPlanes").Get()) == 500
    assert len(json.dumps(created, allow_nan=False)) < 20_000
    huge = authoring.create_light(stage, "/World/" + "A" * 2000, "rect")
    _refused(huge, "longer than")
    assert len(json.dumps(huge)) < 5_000


# --- regressions from the live-authoring review -------------------------------------------------------------


def test_create_refuses_a_parent_inside_an_unloaded_payload():
    """While a payload is unloaded its children are not composed, so a new def could retype one of them."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        asset = Usd.Stage.CreateNew(str(folder / "asset.usda"))
        asset.SetDefaultPrim(UsdGeom.Xform.Define(asset, "/Asset").GetPrim())
        UsdLux.SphereLight.Define(asset, "/Asset/Key").CreateIntensityAttr(10.0)
        asset.GetRootLayer().Save()
        shot = Usd.Stage.CreateNew(str(folder / "shot.usda"))
        UsdGeom.Xform.Define(shot, "/World")
        shot.DefinePrim("/World/Asset").GetPayloads().AddPayload("./asset.usda")
        shot.GetRootLayer().Save()
        stage = Usd.Stage.Open(str(folder / "shot.usda"), Usd.Stage.LoadNone)
        assert stage.GetPrimAtPath("/World/Asset").IsLoaded() is False
        before = _snapshot(stage)
        for dry_run in (True, False):
            _refused(authoring.create_light(stage, "/World/Asset/Key", "rect", dry_run=dry_run), "unloaded payload")
            _refused(authoring.create_camera(stage, "/World/Asset/Key", dry_run=dry_run), "unloaded payload")
        assert _snapshot(stage) == before
        assert authoring.create_light(stage, "/World/NewKey", "rect", dry_run=False)["ok"]  # other parents still work
        stage.Load("/World/Asset")
        assert stage.GetPrimAtPath("/World/Asset/Key").GetTypeName() == "SphereLight"
        del stage, shot, asset


def test_only_an_explicit_false_dry_run_authors():
    stage = _stage()
    UsdGeom.Camera.Define(stage, "/World/Cam").AddTranslateOp().Set(Gf.Vec3d(0.0, -8.0, 2.0))
    before = _snapshot(stage)
    for value in (None, 0, 0.0, [], {}, "maybe"):
        assert authoring.aim(stage, "/World/Cam", "/World/Car", dry_run=value)["dry_run"] is True, value
        assert authoring.create_light(stage, "/World/Key", "rect", dry_run=value)["dry_run"] is True, value
        assert authoring.create_camera(stage, "/World/Cam2", dry_run=value)["dry_run"] is True, value
    assert _snapshot(stage) == before
    assert authoring.aim(stage, "/World/Cam", "/World/Car", dry_run=False)["written"] is True


def test_aim_keeps_the_horizon_level_under_a_non_uniformly_scaled_parent():
    """Up and horizon are plane normals: the camera's local +X has to stay perpendicular to world up."""
    for axis, rotate, scale, eye, target in (
        ("Z", (20.0, 0.0, 0.0), (1.0, 1.0, 1.5), (0.0, 0.0, 2.0), (5.0, 0.0, 0.0)),
        ("Z", (30.0, 0.0, 0.0), (1.0, 1.0, 2.0), (0.0, 0.0, 2.0), (5.0, 0.0, 0.0)),
        ("Y", (0.0, 0.0, 30.0), (2.0, 1.0, 1.0), (0.0, 2.0, 0.0), (5.0, 0.0, -3.0)),
    ):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, axis)
        parent = UsdGeom.Xform.Define(stage, "/World")
        parent.AddRotateXYZOp().Set(Gf.Vec3f(*rotate))
        parent.AddScaleOp().Set(Gf.Vec3f(*scale))
        created = authoring.create_camera(stage, "/World/Cam", translation=eye, aim_at=target, dry_run=False)
        assert created["ok"], created
        UsdGeom.Camera.Define(stage, "/World/Cam2").AddTranslateOp().Set(Gf.Vec3d(*eye))
        aimed = authoring.aim(stage, "/World/Cam2", target, dry_run=False)
        assert aimed["ok"], aimed
        up = look_at.UP_VECTORS[axis]
        for path in ("/World/Cam", "/World/Cam2"):
            prim = stage.GetPrimAtPath(path)
            _assert_aims_at(prim, target)
            right = _unit(_world(prim).TransformDir(Gf.Vec3d(1.0, 0.0, 0.0)))
            tilt = math.degrees(math.asin(min(1.0, abs(sum(right[i] * up[i] for i in range(3))))))
            assert tilt < 1e-3, (axis, rotate, scale, path, tilt)
            assert sum(_world_up(prim)[i] * up[i] for i in range(3)) > 0.0, (axis, path)


def test_half_precision_rotation_reports_the_aim_it_writes():
    """A half rotate or orient op rounds the written value: report that error instead of blaming another layer."""
    target = (7.3, -2.1, 4.4)
    for kind in ("rotate", "orient"):
        stage = _stage("Z")
        camera = UsdGeom.Camera.Define(stage, "/World/Cam")
        camera.AddTranslateOp().Set(Gf.Vec3d(1.0, 2.0, 3.0))
        if kind == "rotate":
            camera.AddRotateXYZOp(UsdGeom.XformOp.PrecisionHalf).Set(Gf.Vec3h(0.0, 0.0, 0.0))
        else:
            camera.AddOrientOp(UsdGeom.XformOp.PrecisionHalf).Set(Gf.Quath(1.0, Gf.Vec3h(0.0, 0.0, 0.0)))
        plan = authoring.aim(stage, "/World/Cam", target)
        assert plan["ok"], plan
        result = authoring.aim(stage, "/World/Cam", target, dry_run=False)
        assert result["ok"] and result["written"], result
        planned = result["would_change"]["aim"]["error_degrees"]
        assert planned == plan["would_change"]["aim"]["error_degrees"]
        assert abs(result["changed"]["aim_error_degrees"] - planned) < 1e-6, (kind, planned, result["changed"])
        assert not any("stronger opinion or another layer" in w for w in result["warnings"]), result["warnings"]
        if planned > authoring.AIM_TOLERANCE_DEGREES:
            assert any("half precision" in w for w in result["warnings"]), (kind, result["warnings"])
    rotate_plan = authoring.aim(_half_rotate_stage(), "/World/Cam", target)
    assert rotate_plan["would_change"]["aim"]["error_degrees"] > authoring.AIM_TOLERANCE_DEGREES
    assert any("half precision" in w for w in rotate_plan["warnings"])


def _half_rotate_stage():
    stage = _stage("Z")
    camera = UsdGeom.Camera.Define(stage, "/World/Cam")
    camera.AddTranslateOp().Set(Gf.Vec3d(1.0, 2.0, 3.0))
    camera.AddRotateXYZOp(UsdGeom.XformOp.PrecisionHalf).Set(Gf.Vec3h(0.0, 0.0, 0.0))
    return stage


def test_fractional_color_above_one_is_refused_not_read_as_0_255():
    stage = _stage()
    before = _snapshot(stage)
    for color in ((1.2, 1.0, 1.0), (1.5, 0.8, 0.6)):
        for dry_run in (True, False):
            _refused(authoring.create_light(stage, "/World/Warm", "disk", color=color, dry_run=dry_run), "intensity or exposure")
    assert _snapshot(stage) == before
    whole = authoring.create_light(stage, "/World/Warm", "disk", color=(255.0, 128.0, 0.0))
    assert whole["ok"] and any("divided by 255" in w for w in whole["warnings"])


def test_camera_copy_of_spline_lens_copies_its_earliest_value():
    """A spline has no time samples, so Get() alone would copy the schema fallback (50) instead of the lens."""
    try:
        from pxr import Ts

        spline = Ts.Spline("float")
        spline.SetKnot(Ts.Knot(typeName="float", time=1.0, value=20.0))
        spline.SetKnot(Ts.Knot(typeName="float", time=10.0, value=80.0))
    except Exception:  # pragma: no cover - a USD build without Ts splines
        return
    stage = _stage()
    attr = UsdGeom.Camera.Define(stage, "/World/Zoom").CreateFocalLengthAttr()
    if not hasattr(attr, "SetSpline") or not attr.SetSpline(spline):  # pragma: no cover - older USD
        return
    assert attr.GetNumTimeSamples() == 0 and attr.ValueMightBeTimeVarying()
    plan = authoring.create_camera(stage, "/World/Copy", copy_optics_from="/World/Zoom")
    assert plan["ok"], plan
    assert plan["would_create"]["attributes"]["focalLength"] == 20.0
    assert any("is animated" in w and "earliest time" in w for w in plan["warnings"])
