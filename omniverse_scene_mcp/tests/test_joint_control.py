"""joint_control: drive targets, gains and poses on synthetic robots; live_* against FAKE Isaac Sim modules.

Fixture-free test_* functions. Stages are in memory; pose files go to a fresh temporary folder. The live tests put
fake omni / isaacsim modules into sys.modules for one test and put the previous entries back; inside a real Kit
process they return at once. The real-Franka smoke test only opens the asset read-only and never saves.
"""

import contextlib
import json
import math
import sys
import tempfile
import types
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import joint_control as jc

ASSET_PACK = Path(__file__).resolve().parents[2] / "isaacsim-assets-6.1.0"
FRANKA = ASSET_PACK / "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules


def _json(result):
    json.dumps(result, allow_nan=False)
    return result


def _body(stage, path, z=0.0):
    xform = UsdGeom.Xform.Define(stage, path)
    xform.AddTranslateOp().Set(Gf.Vec3d(0, 0, z))
    UsdPhysics.RigidBodyAPI.Apply(xform.GetPrim())
    cube = UsdGeom.Cube.Define(stage, path + "/geom")
    cube.CreateSizeAttr(0.2)
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())


def _robot(meters_per_unit=1.0, *, elbow_drive=True, mimic=True):
    """base -(shoulder hinge, drive)-> link1 -(elbow hinge)-> link2 -(slide, drive)-> link3 -(follower, mimic)-> link4."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    arm = UsdGeom.Xform.Define(stage, "/World/Arm")
    UsdPhysics.ArticulationRootAPI.Apply(arm.GetPrim())
    for index, name in enumerate(("base", "link1", "link2", "link3", "link4")):
        _body(stage, f"/World/Arm/{name}", z=0.2 * index)

    root = UsdPhysics.FixedJoint.Define(stage, "/World/Arm/root_joint")
    root.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/base")])

    def hinge(name, parent, child, drive):
        joint = UsdPhysics.RevoluteJoint.Define(stage, f"/World/Arm/{parent}/{name}")
        joint.CreateBody0Rel().SetTargets([Sdf.Path(f"/World/Arm/{parent}")])
        joint.CreateBody1Rel().SetTargets([Sdf.Path(f"/World/Arm/{child}")])
        joint.CreateAxisAttr("Z")
        joint.CreateLowerLimitAttr(-90.0)
        joint.CreateUpperLimitAttr(90.0)
        if drive:
            api = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular")
            api.CreateStiffnessAttr(100.0)
            api.CreateDampingAttr(10.0)
            api.CreateTargetPositionAttr(15.0)

    hinge("shoulder", "base", "link1", True)
    hinge("elbow", "link1", "link2", elbow_drive)

    slide = UsdPhysics.PrismaticJoint.Define(stage, "/World/Arm/link2/slide")
    slide.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link2")])
    slide.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link3")])
    slide.CreateAxisAttr("X")
    slide.CreateLowerLimitAttr(0.0)
    slide.CreateUpperLimitAttr(0.5 / meters_per_unit)
    linear = UsdPhysics.DriveAPI.Apply(slide.GetPrim(), "linear")
    linear.CreateStiffnessAttr(1000.0)
    linear.CreateDampingAttr(50.0)
    linear.CreateTargetPositionAttr(0.1 / meters_per_unit)

    follower = UsdPhysics.PrismaticJoint.Define(stage, "/World/Arm/link3/follower")
    follower.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link3")])
    follower.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link4")])
    follower.CreateAxisAttr("X")
    follower.CreateLowerLimitAttr(0.0)
    follower.CreateUpperLimitAttr(0.5 / meters_per_unit)
    if mimic:
        prim = follower.GetPrim()
        prim.CreateRelationship("physxMimicJoint:rotX:referenceJoint").SetTargets([Sdf.Path("/World/Arm/link2/slide")])
        prim.CreateAttribute("physxMimicJoint:rotX:gearing", Sdf.ValueTypeNames.Float).Set(-1.0)
    return stage


def _target(stage, path, instance="angular", field="targetPosition"):
    return stage.GetPrimAtPath(path).GetAttribute(f"drive:{instance}:physics:{field}").Get()


# ------------------------------------------------------------------------------------------------ joint_table


def test_joint_table_lists_hinges_and_sliders_in_tree_order_with_units():
    stage = _robot(meters_per_unit=0.01)
    result = _json(jc.joint_table(stage, "/World/Arm"))
    assert result["ok"] and result["read_only"], result
    assert [row["name"] for row in result["joints"]] == ["shoulder", "elbow", "slide", "follower"]
    shoulder, elbow, slide, follower = result["joints"]
    assert shoulder["unit"] == "degrees" and shoulder["lower"] == -90.0 and shoulder["upper"] == 90.0
    assert shoulder["drive"]["target_position"] == 15.0 and shoulder["drive"]["stiffness"] == 100.0
    assert shoulder["drive"]["gains_si"]["stiffness_unit"] == "N*m/rad"
    assert not elbow["has_drive"] and elbow["drive"] is None
    assert slide["unit"] == "metres" and math.isclose(slide["upper"], 0.5) and math.isclose(slide["drive"]["target_position"], 0.1)
    assert follower["mimics"]["reference_joint"] == "/World/Arm/link2/slide"
    assert result["dof_count"] == 4 and result["driven_count"] == 2
    assert {row["kind"] for row in result["other_joints"]} == {"fixed"}
    assert any(problem["code"] == "no_drive" for problem in result["problems"])


def test_joint_table_from_inside_the_robot_and_refusals():
    stage = _robot()
    inside = jc.joint_table(stage, "/World/Arm/link1")
    assert inside["ok"] and inside["robot_root"] == "/World/Arm"
    assert "error" in jc.joint_table(stage, "/World/Nope")
    assert "error" in jc.joint_table(stage, "World/Arm")
    assert "error" in jc.joint_table(None, "/World/Arm")
    empty = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(empty, "/World")
    assert "No robot" in jc.joint_table(empty, "/World")["error"]


# ------------------------------------------------------------------------------------------------ drive targets


def test_set_drive_targets_dry_run_plans_and_leaves_the_layer_byte_identical():
    stage = _robot(meters_per_unit=0.01)
    before = stage.GetRootLayer().ExportToString()
    result = _json(jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 45, "slide": 0.3}))
    assert result["ok"] and result["dry_run"] and not result["written"], result
    plan = {step["joint"]: step for step in result["plan"]}
    assert plan["shoulder"]["authored_value"] == 45.0 and plan["shoulder"]["previous"] == 15.0
    assert plan["slide"]["authored_value"] == 30.0  # 0.3 m in centimetre stage units
    assert stage.GetRootLayer().ExportToString() == before
    assert "dry_run=False" in result["next_step"]


def test_set_drive_targets_authors_degrees_and_stage_units():
    stage = _robot(meters_per_unit=0.01)
    result = _json(jc.set_drive_targets(stage, "/World/Arm", {"shoulder": -30.5, "/World/Arm/link2/slide": 0.25}, dry_run=False))
    assert result["ok"] and result["written"], result
    assert math.isclose(_target(stage, "/World/Arm/base/shoulder"), -30.5)
    assert math.isclose(_target(stage, "/World/Arm/link2/slide", "linear"), 25.0, rel_tol=1e-6)
    readback = {row["joint"]: row for row in result["readback"]}
    assert math.isclose(readback["slide"]["targetPosition"], 25.0, rel_tol=1e-6)
    again = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": -30.5}, dry_run=False)
    assert again["ok"] and not again["written"] and "already" in again["note"]


def test_set_drive_targets_clamps_to_limits_with_warnings():
    stage = _robot()
    result = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 120, "slide": -1.0}, dry_run=False)
    assert result["ok"], result
    plan = {step["joint"]: step for step in result["plan"]}
    assert plan["shoulder"]["clamped"] and plan["shoulder"]["target"] == 90.0
    assert plan["slide"]["clamped"] and plan["slide"]["target"] == 0.0
    assert math.isclose(_target(stage, "/World/Arm/base/shoulder"), 90.0)
    assert sum("clamped" in warning for warning in result["warnings"]) == 2


def test_set_drive_targets_refusals_write_nothing():
    stage = _robot()
    before = stage.GetRootLayer().ExportToString()
    cases = [
        ({"wrist": 10}, "Unknown joint"),
        ({"elbow": 10}, "no 'angular' drive"),
        ({"root_joint": 0}, "fixed joint"),
        ({"shoulder": "ten"}, "must be a number"),
        ({"shoulder": float("nan")}, "finite"),
        ({}, "non-empty"),
        ({"shoulder": 10, "/World/Arm/base/shoulder": 20}, "twice"),
    ]
    for targets, expected in cases:
        result = _json(jc.set_drive_targets(stage, "/World/Arm", targets, dry_run=False))
        assert not result["ok"] and not result["written"], (targets, result)
        assert expected in result["error"], (targets, result["error"])
    assert stage.GetRootLayer().ExportToString() == before


def test_set_drive_targets_refuses_animated_targets_and_read_only_layers():
    stage = _robot()
    _target_attr = stage.GetPrimAtPath("/World/Arm/base/shoulder").GetAttribute("drive:angular:physics:targetPosition")
    _target_attr.Set(5.0, 1.0)
    result = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 10}, dry_run=False)
    assert not result["ok"] and "animated" in result["error"]

    stage = _robot()
    stage.GetRootLayer().SetPermissionToEdit(False)
    try:
        result = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 10})
        assert not result["ok"] and "read-only" in result["error"]
    finally:
        stage.GetRootLayer().SetPermissionToEdit(True)


def test_set_drive_targets_refuses_a_stronger_layer_opinion():
    weak = Sdf.Layer.CreateAnonymous("weak.usda")
    stage = _robot()
    stage.GetRootLayer().subLayerPaths.append(weak.identifier)
    stage.SetEditTarget(Usd.EditTarget(weak))
    result = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 10}, dry_run=False)
    assert not result["ok"] and "stronger layer" in result["error"], result


def test_set_drive_targets_skips_mimic_joints_with_a_warning():
    stage = _robot()
    result = jc.set_drive_targets(stage, "/World/Arm", {"slide": 0.2, "follower": 0.2}, dry_run=False)
    assert result["ok"], result
    assert [step["joint"] for step in result["plan"]] == ["slide"]
    assert any("follower has no motor" in warning and "slide" in warning for warning in result["warnings"])


def test_set_drive_targets_rolls_back_when_a_write_fails():
    stage = _robot()
    before = stage.GetRootLayer().ExportToString()
    original = jc._author_value
    calls = []

    def flaky(prim, instance, field, value):
        calls.append(str(prim.GetPath()))
        if len(calls) == 2:
            raise RuntimeError("disk on fire")
        original(prim, instance, field, value)

    jc._author_value = flaky
    try:
        result = jc.set_drive_targets(stage, "/World/Arm", {"shoulder": 40, "slide": 0.3}, dry_run=False)
    finally:
        jc._author_value = original
    assert not result["ok"] and not result["written"], result
    assert "put back" in result["error"] and len(calls) == 2
    assert stage.GetRootLayer().ExportToString() == before


def test_set_drive_targets_on_a_referenced_robot_writes_overs_in_the_root_layer():
    asset = _robot()
    asset_layer = Sdf.Layer.CreateAnonymous("robot.usda")
    asset_layer.TransferContent(asset.GetRootLayer())
    asset_layer.defaultPrim = "World"
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Scene")
    stage.DefinePrim("/Scene/Robot").GetReferences().AddReference(asset_layer.identifier)
    asset_before = asset_layer.ExportToString()
    result = jc.set_drive_targets(stage, "/Scene/Robot/Arm", {"shoulder": 25}, dry_run=False)
    assert result["ok"] and result["written"], result
    assert math.isclose(_target(stage, "/Scene/Robot/Arm/base/shoulder"), 25.0)
    assert asset_layer.ExportToString() == asset_before


# ------------------------------------------------------------------------------------------------ gains


def test_set_drive_gains_dry_run_write_and_si_units():
    stage = _robot()
    before = stage.GetRootLayer().ExportToString()
    dry = _json(jc.set_drive_gains(stage, "/World/Arm", "shoulder", stiffness=200, damping=20))
    assert dry["ok"] and dry["dry_run"] and stage.GetRootLayer().ExportToString() == before
    step = dry["plan"][0]
    assert step["new"] == {"stiffness": 200.0, "damping": 20.0}
    assert math.isclose(step["gains_si"]["stiffness"], 200 * 180 / math.pi, rel_tol=1e-6)
    assert "stiffness" in dry["guide"] and "typical" in dry["guide"]

    done = jc.set_drive_gains(stage, "/World/Arm", None, damping=30, dry_run=False)
    assert done["ok"] and done["written"], done
    assert [step["joint"] for step in done["plan"]] == ["shoulder", "slide"]
    assert _target(stage, "/World/Arm/base/shoulder", field="damping") == 30.0
    assert _target(stage, "/World/Arm/base/shoulder", field="stiffness") == 100.0  # unchanged
    assert _target(stage, "/World/Arm/link2/slide", "linear", "damping") == 30.0


def test_set_drive_gains_warnings_and_refusals():
    stage = _robot()
    limp = jc.set_drive_gains(stage, "/World/Arm", ["shoulder"], stiffness=0, damping=0)
    assert limp["ok"] and any("limp" in warning for warning in limp["warnings"])
    bouncy = jc.set_drive_gains(stage, "/World/Arm", "shoulder", stiffness=500, damping=0)
    assert any("overshoot" in warning for warning in bouncy["warnings"])
    assert "no 'angular' drive" in jc.set_drive_gains(stage, "/World/Arm", "elbow", stiffness=1)["error"]
    assert "0 or more" in jc.set_drive_gains(stage, "/World/Arm", "shoulder", stiffness=-1)["error"]
    assert "Pass stiffness" in jc.set_drive_gains(stage, "/World/Arm", "shoulder")["error"]
    assert "joints must" in jc.set_drive_gains(stage, "/World/Arm", [], stiffness=1)["error"]


# ------------------------------------------------------------------------------------------------ poses


def test_builtin_franka_poses_come_from_isaac_radians():
    home = jc.pose_targets("home")
    assert home["ok"] and home["source"] == "built-in" and "robot_descriptor.yaml" in home["reference"]
    assert math.isclose(home["targets"]["panda_joint4"], round(math.degrees(-2.87), 3))
    assert home["targets"]["panda_finger_joint1"] == 0.04
    ready = jc.pose_targets("READY")
    assert ready["ok"] and math.isclose(ready["targets"]["panda_joint6"], round(math.degrees(3.037), 3))
    assert len(ready["targets"]) == 9
    assert "No built-in pose" in jc.pose_targets("dance")["error"]


def test_save_list_and_apply_poses_roundtrip_in_a_temp_project():
    stage = _robot()
    with tempfile.TemporaryDirectory() as project:
        saved = _json(jc.save_pose(stage, "/World/Arm", "reach", {"shoulder": 60, "slide": 0.4}, project_dir=project))
        assert saved["ok"] and saved["written"], saved
        path = Path(project) / "robots" / "poses.json"
        assert Path(saved["file"]) == path and path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["poses"]["reach"]["targets"] == {"shoulder": 60.0, "slide": 0.4}

        current = jc.save_pose(stage, "/World/Arm", "start", project_dir=project)
        assert current["ok"] and current["targets"] == {"shoulder": 15.0, "slide": 0.1}

        listed = _json(jc.list_poses(project))
        assert [pose["name"] for pose in listed["poses"]] == ["home", "ready", "reach", "start"]
        assert jc.pose_targets("reach", project_dir=project)["targets"] == {"shoulder": 60.0, "slide": 0.4}

        duplicate = jc.save_pose(stage, "/World/Arm", "reach", {"shoulder": 1}, project_dir=project)
        assert not duplicate["ok"] and "overwrite=True" in duplicate["error"]
        replaced = jc.save_pose(stage, "/World/Arm", "reach", {"shoulder": 100}, project_dir=project, overwrite=True)
        assert replaced["ok"] and any("clamped when the pose is applied" in warning for warning in replaced["warnings"])

        applied = jc.apply_pose(stage, "/World/Arm", "reach", project_dir=project, dry_run=False)
        assert applied["ok"] and applied["action"] == "apply_pose" and applied["pose"]["source"] == "project"
        assert math.isclose(_target(stage, "/World/Arm/base/shoulder"), 90.0)  # 100 clamped on apply
        assert sorted(item.name for item in Path(project).rglob("*")) == ["poses.json", "robots"]


def test_pose_refusals_never_touch_the_disk():
    stage = _robot()
    with tempfile.TemporaryDirectory() as project:
        assert "explicitly" in jc.save_pose(stage, "/World/Arm", "a", {"shoulder": 1}, project_dir=None)["error"]
        assert "absolute" in jc.save_pose(stage, "/World/Arm", "a", {"shoulder": 1}, project_dir="relative")["error"]
        missing = str(Path(project) / "nope")
        assert "does not exist" in jc.save_pose(stage, "/World/Arm", "a", {"shoulder": 1}, project_dir=missing)["error"]
        assert "built-in" in jc.save_pose(stage, "/World/Arm", "Home", {"shoulder": 1}, project_dir=project)["error"]
        assert "name must" in jc.save_pose(stage, "/World/Arm", "../evil", {"shoulder": 1}, project_dir=project)["error"]
        assert "Unknown joint" in jc.save_pose(stage, "/World/Arm", "a", {"wrist": 1}, project_dir=project)["error"]
        assert list(Path(project).iterdir()) == []

        (Path(project) / "robots").mkdir()
        bad = Path(project) / "robots" / "poses.json"
        bad.write_text("{not json", encoding="utf-8")
        result = jc.save_pose(stage, "/World/Arm", "a", {"shoulder": 1}, project_dir=project)
        assert not result["ok"] and "not changed" in result["error"]
        assert bad.read_text(encoding="utf-8") == "{not json"
        assert not jc.list_poses(project)["ok"]


def test_apply_builtin_pose_to_a_non_franka_robot_is_refused():
    stage = _robot()
    result = jc.apply_pose(stage, "/World/Arm", "home")
    assert not result["ok"] and "Unknown joint 'panda_joint1'" in result["error"]


# ------------------------------------------------------------------------------------------------ live (fake Kit)

_LIVE_NAMES = (
    "omni",
    "omni.timeline",
    "omni.usd",
    "isaacsim",
    "isaacsim.core",
    "isaacsim.core.experimental",
    "isaacsim.core.experimental.prims",
    "isaacsim.core.prims",
    "isaacsim.core.utils",
    "isaacsim.core.utils.types",
    "numpy",
)
_MISSING = object()


@contextlib.contextmanager
def _fake_isaac(**modules):
    """Install fake modules (keyword: dotted name with '_' for '.'); every other listed name fails to import."""
    saved = {name: sys.modules.get(name, _MISSING) for name in _LIVE_NAMES}
    try:
        for name in _LIVE_NAMES:
            sys.modules[name] = None
        for key, module in modules.items():
            sys.modules[key.replace("_", ".")] = module
        yield
    finally:
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class FakeTimeline:
    def __init__(self, state):
        self.state = state

    def is_playing(self):
        return self.state == "playing"

    def is_stopped(self):
        return self.state == "stopped"


def _timeline_module(state):
    module = types.ModuleType("omni.timeline")
    module.get_timeline_interface = lambda: FakeTimeline(state)
    return module


class _DofType:
    def __init__(self, name):
        self.name = name


class FakeArray:
    def __init__(self, rows):
        self.rows = rows

    def numpy(self):
        return self

    def tolist(self):
        return [list(row) for row in self.rows]


class FakeArticulation:
    """isaacsim.core.experimental.prims.Articulation stand-in: 1 hinge + 1 slider, records every call."""

    calls = []
    valid = True

    def __init__(self, paths):
        FakeArticulation.calls.append(("init", paths))
        self.dof_names = ["shoulder", "slide"]
        self.dof_types = [_DofType("Rotation"), _DofType("Translation")]

    def is_physics_tensor_entity_valid(self):
        return FakeArticulation.valid

    def get_dof_positions(self):
        return FakeArray([[math.pi / 2, 0.2]])

    def get_dof_velocities(self):
        return FakeArray([[math.pi, 0.0]])

    def get_dof_efforts(self):
        return FakeArray([[1.5, -2.0]])

    def get_dof_limits(self):
        return FakeArray([[-math.pi, 0.0]]), FakeArray([[math.pi, 0.5]])

    def set_dof_position_targets(self, positions, *, dof_indices=None):
        FakeArticulation.calls.append(("set_dof_position_targets", positions, dof_indices))


def _experimental_module():
    FakeArticulation.calls = []
    FakeArticulation.valid = True
    module = types.ModuleType("isaacsim.core.experimental.prims")
    module.Articulation = FakeArticulation
    return module


def test_live_functions_without_kit_say_so():
    if _IN_KIT:
        return
    with _fake_isaac():
        state = _json(jc.live_joint_state("/World/Arm"))
        assert not state["ok"] and "Kit not available" in state["reason"]
        move = jc.live_move("/World/Arm", {"shoulder": 10})
        assert not move["ok"] and "Kit not available" in move["reason"] and not move["applied"]


def test_live_functions_refuse_a_stopped_timeline():
    if _IN_KIT:
        return
    with _fake_isaac(omni_timeline=_timeline_module("stopped"), isaacsim_core_experimental_prims=_experimental_module()):
        state = jc.live_joint_state("/World/Arm")
        assert not state["ok"] and state["timeline"] == "stopped" and "Press Play" in state["reason"]
        move = jc.live_move("/World/Arm", {"shoulder": 10}, dry_run=False)
        assert not move["ok"] and not move["applied"]
        assert FakeArticulation.calls == []  # never even opened the articulation


def test_live_joint_state_reads_and_converts_units():
    if _IN_KIT:
        return
    with _fake_isaac(omni_timeline=_timeline_module("playing"), isaacsim_core_experimental_prims=_experimental_module()):
        state = _json(jc.live_joint_state("/World/Arm"))
    assert state["ok"] and state["api"].startswith("isaacsim.core.experimental"), state
    shoulder, slide = state["joints"]
    assert shoulder["position"] == 90.0 and shoulder["velocity"] == 180.0 and shoulder["unit"] == "degrees"
    assert shoulder["lower"] == -180.0 and shoulder["effort"] == 1.5 and shoulder["effort_unit"] == "N*m"
    assert slide["position"] == 0.2 and slide["upper"] == 0.5 and slide["unit"] == "metres"


def test_live_move_dry_run_then_apply_with_clamping():
    if _IN_KIT:
        return
    with _fake_isaac(omni_timeline=_timeline_module("paused"), isaacsim_core_experimental_prims=_experimental_module()):
        dry = _json(jc.live_move("/World/Arm", {"shoulder": 45, "slide": 0.9}))
        assert dry["ok"] and dry["dry_run"] and not dry["applied"], dry
        assert not any(call[0] == "set_dof_position_targets" for call in FakeArticulation.calls)
        plan = {step["joint"]: step for step in dry["plan"]}
        assert plan["shoulder"]["current"] == 90.0 and math.isclose(plan["shoulder"]["physx_value"], math.pi / 4)
        assert plan["slide"]["clamped"] and plan["slide"]["target"] == 0.5
        assert any("clamped" in warning for warning in dry["warnings"])

        done = jc.live_move("/World/Arm", {"slide": 0.3, "/World/Arm/base/shoulder": -30}, dry_run=False)
    assert done["ok"] and done["applied"] and "paused" in done["note"], done
    name, positions, indices = FakeArticulation.calls[-1]
    assert name == "set_dof_position_targets" and indices == [1, 0]
    assert math.isclose(positions[0][0], 0.3) and math.isclose(positions[0][1], math.radians(-30))


def test_live_move_refusals():
    if _IN_KIT:
        return
    with _fake_isaac(omni_timeline=_timeline_module("playing"), isaacsim_core_experimental_prims=_experimental_module()):
        unknown = jc.live_move("/World/Arm", {"wrist": 1}, dry_run=False)
        assert not unknown["ok"] and "Unknown joint" in unknown["reason"]
        bad = jc.live_move("/World/Arm", {"shoulder": "x"})
        assert not bad["ok"] and "number" in bad["reason"]
        assert "robot_root" in jc.live_move("World", {"shoulder": 1})["reason"]
        FakeArticulation.valid = False
        invalid = jc.live_joint_state("/World/Arm")
        assert not invalid["ok"] and "not ready in PhysX" in invalid["reason"]
        assert not any(call[0] == "set_dof_position_targets" for call in FakeArticulation.calls)


class FakeSingleArticulation:
    calls = []

    def __init__(self, prim_path, name, reset_xform_properties=True):
        FakeSingleArticulation.calls.append(("init", prim_path, reset_xform_properties))
        self.dof_names = ["shoulder", "slide"]
        self.dof_properties = {"type": [1, 2], "lower": [-math.pi, 0.0], "upper": [math.pi, 0.5]}
        self.handles_initialized = False

    def initialize(self):
        FakeSingleArticulation.calls.append(("initialize",))
        self.handles_initialized = True

    def get_joint_positions(self):
        return [0.0, 0.1]

    def get_joint_velocities(self):
        return [0.0, 0.0]

    def get_measured_joint_efforts(self):
        return [0.0, 0.0]

    def apply_action(self, action):
        FakeSingleArticulation.calls.append(("apply_action", action.joint_positions, action.joint_indices))


def test_live_move_falls_back_to_the_deprecated_single_articulation():
    if _IN_KIT:
        return
    FakeSingleArticulation.calls = []
    prims = types.ModuleType("isaacsim.core.prims")
    prims.SingleArticulation = FakeSingleArticulation
    utils_types = types.ModuleType("isaacsim.core.utils.types")

    class ArticulationAction:
        def __init__(self, joint_positions=None, joint_indices=None):
            self.joint_positions = joint_positions
            self.joint_indices = joint_indices

    utils_types.ArticulationAction = ArticulationAction
    with _fake_isaac(omni_timeline=_timeline_module("playing"), isaacsim_core_prims=prims, isaacsim_core_utils_types=utils_types):
        state = jc.live_joint_state("/World/Arm")
        assert state["ok"] and "SingleArticulation" in state["api"] and state["joints"][1]["position"] == 0.1
        done = jc.live_move("/World/Arm", {"shoulder": 90}, dry_run=False)
    assert done["ok"] and done["applied"], done
    assert ("init", "/World/Arm", False) in FakeSingleArticulation.calls  # never resets the robot's xform ops
    name, positions, indices = FakeSingleArticulation.calls[-1]
    assert name == "apply_action" and list(indices) == [0] and math.isclose(list(positions)[0], math.pi / 2)


# ------------------------------------------------------------------------------------------------ real asset


def test_real_franka_joint_table_read_only():
    if _IN_KIT or not FRANKA.is_file():
        return
    layer = Sdf.Layer.FindOrOpen(str(FRANKA))
    before = layer.ExportToString()
    stage = Usd.Stage.Open(layer, Usd.Stage.LoadAll)
    result = _json(jc.joint_table(stage, stage.GetDefaultPrim().GetPath().pathString))
    assert result["ok"], result
    names = [row["name"] for row in result["joints"]]
    assert names[:7] == [f"panda_joint{index}" for index in range(1, 8)], names
    assert set(names[7:]) == {"panda_finger_joint1", "panda_finger_joint2"}
    rows = {row["name"]: row for row in result["joints"]}
    assert rows["panda_joint4"]["unit"] == "degrees" and rows["panda_joint4"]["upper"] < 0
    assert rows["panda_finger_joint2"]["mimics"] is not None
    ready = jc.pose_targets("ready")["targets"]
    plan = jc.set_drive_targets(stage, "/" + stage.GetDefaultPrim().GetName(), ready)  # dry run only
    assert plan["ok"] and not plan["written"], plan
    assert not any(step["clamped"] for step in plan["plan"])
    home = jc.set_drive_targets(stage, "/" + stage.GetDefaultPrim().GetName(), jc.pose_targets("home")["targets"])
    assert home["ok"] and not any(step["clamped"] for step in home["plan"])
    assert layer.ExportToString() == before and not layer.dirty
