"""robot_inspector.inspect_robot on small synthetic robots (healthy and broken), instanced robots, and the real Franka."""

import json
import tempfile
import time
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp.robot_inspector import GLOSSARY, inspect_robot
from omniverse_scene_mcp.usd_insight import TimeBudget

# The workspace next to omniverse_scene_mcp/ (the asset pack is local-only; the smoke test below returns when absent).
ASSET_PACK = Path(__file__).resolve().parents[2] / "isaacsim-assets-6.1.0"
# Inside Kit (the bridge reload self-test) the real-asset smoke test returns at once: it opens a big file.
_IN_KIT = "carb" in __import__("sys").modules or "omni.kit.app" in __import__("sys").modules
FRANKA_CANDIDATES = (
    "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd",
    "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaEmika/panda_instanceable.usd",
)


def _json(result):
    json.dumps(result, allow_nan=False)
    return result


def _body(stage, path, *, mass=None, collider=True, z=0.0, size=0.2):
    xform = UsdGeom.Xform.Define(stage, path)
    xform.AddTranslateOp().Set(Gf.Vec3d(0, 0, z))
    UsdPhysics.RigidBodyAPI.Apply(xform.GetPrim())
    if mass is not None:
        UsdPhysics.MassAPI.Apply(xform.GetPrim()).CreateMassAttr(mass)
    cube = UsdGeom.Cube.Define(stage, path + "/geom")
    cube.CreateSizeAttr(size)
    cube.AddTranslateOp().Set(Gf.Vec3d(0, 0, size / 2))
    if collider:
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return xform.GetPrim()


def _arm(
    *,
    root=True,
    meters_per_unit=1.0,
    lift=0.0,
    shoulder_limits=True,
    drive=(100.0, 10.0),
    link1_collider=True,
    link1_mass=2.0,
    floor=True,
):
    """A 2-link arm on a fixed base: base -(hinge)-> link1 -(slider)-> link2 -(fixed)-> tool."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    if floor:
        ground = UsdGeom.Cube.Define(stage, "/World/Floor")
        ground.CreateSizeAttr(1.0)
        ground.AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.5))
        ground.AddScaleOp().Set(Gf.Vec3f(20, 20, 1))
        UsdPhysics.CollisionAPI.Apply(ground.GetPrim())
    arm = UsdGeom.Xform.Define(stage, "/World/Arm")
    arm.AddTranslateOp().Set(Gf.Vec3d(0, 0, lift))
    if root:
        UsdPhysics.ArticulationRootAPI.Apply(arm.GetPrim())
    _body(stage, "/World/Arm/base", mass=5.0)
    _body(stage, "/World/Arm/link1", mass=link1_mass, collider=link1_collider, z=0.2)
    _body(stage, "/World/Arm/link2", mass=1.0, z=0.4)
    _body(stage, "/World/Arm/tool", z=0.6)
    UsdGeom.Scope.Define(stage, "/World/Arm/joints")

    root_joint = UsdPhysics.FixedJoint.Define(stage, "/World/Arm/joints/root_joint")
    root_joint.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/base")])

    shoulder = UsdPhysics.RevoluteJoint.Define(stage, "/World/Arm/joints/shoulder")
    shoulder.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/base")])
    shoulder.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link1")])
    shoulder.CreateAxisAttr("Z")
    if shoulder_limits:
        shoulder.CreateLowerLimitAttr(-90.0)
        shoulder.CreateUpperLimitAttr(90.0)
    if drive is not None:
        angular = UsdPhysics.DriveAPI.Apply(shoulder.GetPrim(), "angular")
        angular.CreateStiffnessAttr(drive[0])
        angular.CreateDampingAttr(drive[1])
        angular.CreateMaxForceAttr(50.0)
        angular.CreateTargetPositionAttr(30.0)

    slide = UsdPhysics.PrismaticJoint.Define(stage, "/World/Arm/joints/slide")
    slide.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link1")])
    slide.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link2")])
    slide.CreateAxisAttr("X")
    slide.CreateLowerLimitAttr(0.0)
    slide.CreateUpperLimitAttr(0.5 / meters_per_unit)
    linear = UsdPhysics.DriveAPI.Apply(slide.GetPrim(), "linear")
    linear.CreateStiffnessAttr(1000.0)
    linear.CreateDampingAttr(50.0)
    linear.CreateTargetPositionAttr(0.25 / meters_per_unit)

    wrist = UsdPhysics.FixedJoint.Define(stage, "/World/Arm/joints/wrist")
    wrist.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link2")])
    wrist.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/tool")])
    return stage


def _only_robot(result):
    assert result["ok"], result
    assert result["robot_count"] == 1, [robot["root"] for robot in result["robots"]]
    return result["robots"][0]


def _codes(robot, severity=None):
    return [item["code"] for item in robot["problems"] if severity is None or item["severity"] == severity]


def _joint(robot, name):
    return next(joint for joint in robot["joints"] if joint["name"] == name)


def _link(robot, name):
    return next(link for link in robot["links"] if link["name"] == name)


# --- healthy arm --------------------------------------------------------------------------------------------


def test_healthy_arm_is_described_in_plain_words():
    result = _json(inspect_robot(_arm()))
    robot = _only_robot(result)
    assert robot["root"] == "/World/Arm" and robot["name"] == "Arm"
    assert robot["articulation_root"] == "/World/Arm"
    assert robot["base"]["fixed_to_world"] is True
    assert robot["base"]["link"] == "/World/Arm/base"
    assert robot["base"]["via_joint"] == "/World/Arm/joints/root_joint"
    assert [link["name"] for link in robot["links"]] == ["base", "link1", "link2", "tool"]
    assert _link(robot, "base")["mass"] == 5.0
    assert _link(robot, "tool")["mass"] == "computed by PhysX"
    assert all(link["has_collider"] and link["rigid_body"] for link in robot["links"])

    shoulder = _joint(robot, "shoulder")
    assert shoulder["type"] == "hinge" and shoulder["kind"] == "revolute"
    assert shoulder["body0"] == "/World/Arm/base" and shoulder["body1"] == "/World/Arm/link1"
    assert shoulder["axis"] == "Z"
    assert shoulder["limits"] == {"lower": -90.0, "upper": 90.0, "unit": "degrees"}
    drive = shoulder["drive"]
    assert drive["type"] == "angular" and drive["stiffness"] == 100.0 and drive["damping"] == 10.0
    assert drive["max_force"] == 50.0 and drive["target_position"] == 30.0 and drive["target_velocity"] == 0.0
    assert "position control" in drive["control"]

    slide = _joint(robot, "slide")
    assert slide["type"] == "slider" and slide["limits"] == {"lower": 0.0, "upper": 0.5, "unit": "metres"}
    assert slide["drive"]["type"] == "linear" and slide["drive"]["max_force"] is None
    assert _joint(robot, "wrist")["type"] == "fixed" and _joint(robot, "wrist")["drive"] is None
    assert _joint(robot, "root_joint")["connects_to_world"] is True

    assert robot["dof_count"] == 2
    assert robot["joint_counts"] == {"hinge": 1, "slider": 1, "fixed": 1}
    assert robot["tree"] == "world => base -> link1 -> link2 -> tool"
    assert [(edge["parent"], edge["child"]) for edge in robot["tree_edges"]][0] == ("/World/Arm/base", "/World/Arm/link1")
    assert robot["problems"] == [], robot["problems"]
    summary = robot["plain_summary"]
    assert "1 hinge joint, 1 sliding joint and 1 fixed joint" in summary
    assert "2 degrees of freedom" in summary and "bolted" in summary and "No setup problems" in summary
    assert 3 <= summary.count(". ") + 1 <= 7
    for term in ("articulation", "link", "joint", "drive", "DOF", "collider", "mass"):
        assert term in result["glossary"] and term in GLOSSARY
    assert result["truncated"] is False and result["read_only"] is True


def test_centimetre_stage_reports_slider_limits_and_targets_in_metres():
    robot = _only_robot(inspect_robot(_arm(meters_per_unit=0.01)))
    slide = _joint(robot, "slide")
    assert slide["limits"]["upper"] == 0.5 and slide["limits"]["unit"] == "metres"
    assert slide["drive"]["target_position"] == 0.25
    assert _joint(robot, "shoulder")["limits"]["upper"] == 90.0  # hinge limits stay in degrees


def test_finds_every_robot_when_root_path_is_none_and_one_when_given():
    stage = _arm()
    _body(stage, "/World/Cart/chassis", mass=10.0)
    _body(stage, "/World/Cart/wheel", mass=1.0)
    UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath("/World/Cart"))
    wheel = UsdPhysics.RevoluteJoint.Define(stage, "/World/Cart/wheel_joint")
    wheel.CreateBody0Rel().SetTargets([Sdf.Path("/World/Cart/chassis")])
    wheel.CreateBody1Rel().SetTargets([Sdf.Path("/World/Cart/wheel")])
    drive = UsdPhysics.DriveAPI.Apply(wheel.GetPrim(), "angular")
    drive.CreateDampingAttr(5.0)
    result = _json(inspect_robot(stage))
    assert sorted(robot["root"] for robot in result["robots"]) == ["/World/Arm", "/World/Cart"]
    cart = next(robot for robot in result["robots"] if robot["root"] == "/World/Cart")
    assert cart["base"]["fixed_to_world"] is False and "free" in cart["base"]["plain"]
    assert _joint(cart, "wheel_joint")["drive"]["control"].startswith("velocity control")
    assert "no_limits" in _codes(cart, "warning")
    only = _only_robot(inspect_robot(stage, "/World/Cart"))
    assert only["root"] == "/World/Cart"


def test_root_path_inside_a_robot_inspects_the_whole_robot():
    result = inspect_robot(_arm(), "/World/Arm/link1/geom")
    robot = _only_robot(result)
    assert robot["root"] == "/World/Arm" and len(robot["links"]) == 4
    assert any("inside the robot /World/Arm" in note for note in result["notes"])


def test_bad_arguments_are_reported_not_raised():
    stage = _arm()
    assert inspect_robot(None)["ok"] is False
    assert "valid" in inspect_robot(stage, "not a path")["error"]
    assert "No prim" in inspect_robot(stage, "/World/Nope")["error"]
    assert inspect_robot(stage, max_prims=0)["ok"] is False
    assert inspect_robot(stage, max_prims=True)["ok"] is False
    assert inspect_robot(stage, 5)["ok"] is False
    empty = _json(inspect_robot(stage, "/World/Floor"))
    assert empty["ok"] and empty["robots"] == [] and any("No robots" in note for note in empty["notes"])


# --- broken variants ----------------------------------------------------------------------------------------


def test_jointed_parts_without_articulation_root_are_reported():
    robot = _only_robot(_json(inspect_robot(_arm(root=False))))
    assert robot["articulation_root"] is None
    assert robot["root"] == "/World/Arm"
    assert "no_articulation_root" in _codes(robot, "warning")
    assert "no articulation root" in robot["plain_summary"]
    problem = next(item for item in robot["problems"] if item["code"] == "no_articulation_root")
    assert problem["why_it_matters"] and problem["how_to_fix"] and problem["path"] == "/World/Arm"


def test_second_articulation_root_in_the_same_tree_is_an_error():
    stage = _arm()
    UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath("/World/Arm/link1"))
    robot = _only_robot(inspect_robot(stage))
    assert robot["articulation_roots"] == ["/World/Arm", "/World/Arm/link1"]
    problem = next(item for item in robot["problems"] if item["code"] == "multiple_articulation_roots")
    assert problem["severity"] == "error" and problem["path"] == "/World/Arm/link1"
    assert robot["problems"][0]["severity"] == "error"  # errors are listed first


def test_missing_body_and_same_body_joints_are_errors():
    stage = _arm()
    stage.GetPrimAtPath("/World/Arm/joints/slide").GetRelationship("physics:body1").SetTargets(
        [Sdf.Path("/World/Arm/ghost")]
    )
    loop = UsdPhysics.RevoluteJoint.Define(stage, "/World/Arm/joints/self")
    loop.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link1")])
    loop.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link1")])
    robot = _only_robot(_json(inspect_robot(stage)))
    errors = _codes(robot, "error")
    assert "missing_body" in errors and "same_body" in errors
    missing = next(item for item in robot["problems"] if item["code"] == "missing_body")
    assert missing["path"] == "/World/Arm/joints/slide" and "/World/Arm/ghost" in missing["problem"]
    # link2 lost its only joint to the arm, so it is no longer reached from the base.
    assert "disconnected_link" in _codes(robot)


def test_joint_with_no_bodies_is_reported_even_outside_robots():
    stage = _arm()
    UsdPhysics.RevoluteJoint.Define(stage, "/World/LooseJoint")
    result = _json(inspect_robot(stage))
    loose = next(robot for robot in result["robots"] if robot["root"] == "/World")
    assert "joint_connects_nothing" in _codes(loose, "error")


def test_revolute_without_limits_is_a_warning():
    robot = _only_robot(inspect_robot(_arm(shoulder_limits=False)))
    assert _joint(robot, "shoulder")["limits"] is None
    assert "no_limits" in _codes(robot, "warning")
    assert "with no limits" in _joint(robot, "shoulder")["plain"]


def test_inverted_limits_lock_the_joint():
    stage = _arm()
    joint = UsdPhysics.RevoluteJoint(stage.GetPrimAtPath("/World/Arm/joints/shoulder"))
    joint.GetLowerLimitAttr().Set(45.0)
    joint.GetUpperLimitAttr().Set(-45.0)
    robot = _only_robot(inspect_robot(stage))
    assert _joint(robot, "shoulder")["limits"]["locked"] is True
    assert "inverted_limits" in _codes(robot, "warning")


def test_drive_with_zero_stiffness_and_damping_is_a_warning():
    robot = _only_robot(inspect_robot(_arm(drive=(0.0, 0.0))))
    assert _joint(robot, "shoulder")["drive"]["control"].startswith("no strength")
    problem = next(item for item in robot["problems"] if item["code"] == "drive_without_strength")
    assert problem["severity"] == "warning" and problem["path"] == "/World/Arm/joints/shoulder"


def test_hinge_without_drive_is_noted():
    robot = _only_robot(inspect_robot(_arm(drive=None)))
    assert _joint(robot, "shoulder")["drive"] is None and _joint(robot, "shoulder")["drives"] == []
    assert "no_drive" in _codes(robot, "info")


def test_link_without_collider_is_a_warning():
    robot = _only_robot(inspect_robot(_arm(link1_collider=False)))
    assert _link(robot, "link1")["has_collider"] is False
    problem = next(item for item in robot["problems"] if item["code"] == "link_without_collider")
    assert problem["severity"] == "warning" and problem["path"] == "/World/Arm/link1"


def test_frame_link_on_fixed_joints_without_collider_is_only_info():
    stage = _arm()
    stage.GetPrimAtPath("/World/Arm/tool/geom").RemoveAPI(UsdPhysics.CollisionAPI)
    robot = _only_robot(inspect_robot(stage))
    problem = next(item for item in robot["problems"] if item["code"] == "link_without_collider")
    assert problem["severity"] == "info" and "reference frame" in problem["why_it_matters"]


def test_zero_and_negative_mass():
    zero = _only_robot(inspect_robot(_arm(link1_mass=0.0)))
    assert "zero_mass" in _codes(zero, "warning")
    assert _link(zero, "link1")["mass"] == "computed by PhysX"
    negative = _only_robot(inspect_robot(_arm(link1_mass=-1.0)))
    assert "negative_mass" in _codes(negative, "error")


def test_kilograms_per_unit_converts_link_mass():
    stage = _arm()
    UsdPhysics.SetStageKilogramsPerUnit(stage, 0.001)  # grams
    robot = _only_robot(inspect_robot(stage))
    assert _link(robot, "base")["mass"] == 0.005


def test_rigid_body_nested_without_joint_is_a_warning():
    stage = _arm()
    _body(stage, "/World/Arm/link2/screw", mass=0.1)
    robot = _only_robot(inspect_robot(stage))
    nested = next(item for item in robot["problems"] if item["code"] == "nested_rigid_body")
    assert nested["severity"] == "warning" and nested["path"] == "/World/Arm/link2/screw"
    fixed = UsdPhysics.FixedJoint.Define(stage, "/World/Arm/joints/screw_joint")
    fixed.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/link2")])
    fixed.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link2/screw")])
    robot = _only_robot(inspect_robot(stage))
    assert "nested_rigid_body" not in _codes(robot)
    assert "disconnected_link" not in _codes(robot)


def test_joint_bodies_that_are_not_rigid_bodies():
    stage = _arm()
    UsdGeom.Xform.Define(stage, "/World/Marker")
    pin = UsdPhysics.FixedJoint.Define(stage, "/World/Arm/joints/pin")
    pin.CreateBody0Rel().SetTargets([Sdf.Path("/World/Marker")])
    pin.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/tool")])
    shoulder = stage.GetPrimAtPath("/World/Arm/joints/shoulder")
    shoulder.GetRelationship("physics:body1").SetTargets([Sdf.Path("/World/Arm/link1/geom")])
    robot = _only_robot(_json(inspect_robot(stage)))
    not_rigid = [item for item in robot["problems"] if item["code"] == "body_not_rigid"]
    by_path = {item["path"]: item["severity"] for item in not_rigid}
    assert by_path["/World/Arm/joints/pin"] == "warning"  # a plain Xform: PhysX treats it as the world
    assert by_path["/World/Arm/joints/shoulder"] == "info"  # a mesh inside link1: PhysX uses link1
    assert robot["dof_count"] == 2 and robot["tree"].startswith("world => base -> link1")


def test_closed_loop_is_a_warning():
    stage = _arm()
    extra = UsdPhysics.RevoluteJoint.Define(stage, "/World/Arm/joints/loop")
    extra.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/base")])
    extra.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/link2")])
    extra.CreateLowerLimitAttr(-10.0)
    extra.CreateUpperLimitAttr(10.0)
    robot = _only_robot(inspect_robot(stage))
    assert "closed_loop" in _codes(robot, "warning")
    extra.GetExcludeFromArticulationAttr().Set(True)
    robot = _only_robot(inspect_robot(stage))
    assert "closed_loop" not in _codes(robot) and robot["dof_count"] == 2


def test_floating_robot_is_reported_as_info_only():
    robot = _only_robot(inspect_robot(_arm(lift=0.5)))
    problem = next(item for item in robot["problems"] if item["code"] == "floating_above_ground")
    assert problem["severity"] == "info" and "0.5 m above the floor Floor" in problem["problem"]
    assert "hang in the air" in problem["problem"]  # the base is fixed to the world
    assert "floating_above_ground" not in _codes(_only_robot(inspect_robot(_arm())))
    assert "floating_above_ground" not in _codes(_only_robot(inspect_robot(_arm(lift=0.5, floor=False))))
    sunk = _only_robot(inspect_robot(_arm(lift=-0.3)))
    assert "below_ground" in _codes(sunk, "info")
    # With root_path the floor outside the robot is still found.
    scoped = _only_robot(inspect_robot(_arm(lift=0.5), "/World/Arm"))
    assert "floating_above_ground" in _codes(scoped)


def test_single_body_fixed_to_world_is_not_a_robot():
    stage = Usd.Stage.CreateInMemory()
    _body(stage, "/World/Table", mass=20.0)
    anchor = UsdPhysics.FixedJoint.Define(stage, "/World/anchor")
    anchor.CreateBody1Rel().SetTargets([Sdf.Path("/World/Table")])
    result = _json(inspect_robot(stage))
    assert result["robots"] == []
    assert any("only attaches /World/Table to the world" in note for note in result["notes"])


def test_spherical_and_d6_joint_dof():
    stage = _arm()
    _body(stage, "/World/Arm/ball", mass=0.5, z=0.8)
    _body(stage, "/World/Arm/free", mass=0.5, z=1.0)
    ball = UsdPhysics.SphericalJoint.Define(stage, "/World/Arm/joints/ball_joint")
    ball.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/tool")])
    ball.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/ball")])
    ball.CreateConeAngle0LimitAttr(30.0)
    ball.CreateConeAngle1LimitAttr(20.0)
    d6 = UsdPhysics.Joint.Define(stage, "/World/Arm/joints/d6")
    d6.CreateBody0Rel().SetTargets([Sdf.Path("/World/Arm/ball")])
    d6.CreateBody1Rel().SetTargets([Sdf.Path("/World/Arm/free")])
    for axis in ("transX", "transY", "transZ", "rotY"):
        limit = UsdPhysics.LimitAPI.Apply(d6.GetPrim(), axis)
        limit.CreateLowAttr(1.0)
        limit.CreateHighAttr(-1.0)  # low > high: locked
    UsdPhysics.DriveAPI.Apply(d6.GetPrim(), "rotX").CreateStiffnessAttr(10.0)
    robot = _only_robot(_json(inspect_robot(stage)))
    assert _joint(robot, "ball_joint")["type"] == "ball joint" and _joint(robot, "ball_joint")["dof"] == 3
    assert _joint(robot, "ball_joint")["limits"]["cone_angle0"] == 30.0
    d6_entry = _joint(robot, "d6")
    assert d6_entry["kind"] == "d6" and d6_entry["dof"] == 2 and d6_entry["limits"]["transX"] == "locked"
    assert d6_entry["drive"]["instance"] == "rotX" and d6_entry["drive"]["type"] == "angular"
    assert robot["dof_count"] == 2 + 3 + 2


# --- instancing, read-only, limits --------------------------------------------------------------------------


def test_instanceable_links_and_instanced_robots_are_read_through_proxies():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    proto = stage.CreateClassPrim("/LinkGeo")
    cube = UsdGeom.Cube.Define(stage, "/LinkGeo/mesh")
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    assert proto

    def robot_into(path):
        UsdGeom.Xform.Define(stage, path)
        UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath(path))
        for name in ("base", "arm"):
            link = UsdGeom.Xform.Define(stage, f"{path}/{name}").GetPrim()
            UsdPhysics.RigidBodyAPI.Apply(link)
            geometry = UsdGeom.Xform.Define(stage, f"{path}/{name}/geometry").GetPrim()
            geometry.GetReferences().AddInternalReference("/LinkGeo")
            geometry.SetInstanceable(True)
        hinge = UsdPhysics.RevoluteJoint.Define(stage, f"{path}/hinge")
        hinge.CreateBody0Rel().SetTargets([Sdf.Path(f"{path}/base")])
        hinge.CreateBody1Rel().SetTargets([Sdf.Path(f"{path}/arm")])
        hinge.CreateLowerLimitAttr(-45.0)
        hinge.CreateUpperLimitAttr(45.0)
        UsdPhysics.DriveAPI.Apply(hinge.GetPrim(), "angular").CreateStiffnessAttr(10.0)

    robot_into("/RobotClass")
    stage.GetPrimAtPath("/RobotClass").SetSpecifier(Sdf.SpecifierClass)
    robot_into("/World/RobotA")
    whole = UsdGeom.Xform.Define(stage, "/World/RobotB").GetPrim()
    whole.GetReferences().AddInternalReference("/RobotClass")
    whole.SetInstanceable(True)

    result = _json(inspect_robot(stage))
    roots = sorted(robot["root"] for robot in result["robots"])
    assert roots == ["/World/RobotA", "/World/RobotB"], roots
    for robot in result["robots"]:
        assert [link["has_collider"] for link in robot["links"]] == [True, True]
        assert robot["dof_count"] == 1 and robot["tree"] == "base -> arm"
        assert "link_without_collider" not in _codes(robot)
    instanced = next(robot for robot in result["robots"] if robot["root"] == "/World/RobotB")
    assert all(link["instance_proxy"] for link in instanced["links"])
    assert _joint(instanced, "hinge")["instance_proxy"] is True
    assert _joint(instanced, "hinge")["body1"] == "/World/RobotB/arm"


def test_inspection_never_changes_the_layers_on_disk_or_in_memory():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "arm.usda"
        source = _arm(lift=0.5)  # keep the stage alive while its layer is exported
        assert source.GetRootLayer().Export(str(path))
        before = path.read_bytes()
        stage = Usd.Stage.Open(str(path))
        session = stage.GetSessionLayer().ExportToString()
        root_text = stage.GetRootLayer().ExportToString()
        result = _json(inspect_robot(stage))
        assert result["ok"] and result["robot_count"] == 1
        assert not stage.GetRootLayer().dirty
        assert stage.GetRootLayer().ExportToString() == root_text
        assert stage.GetSessionLayer().ExportToString() == session
        assert path.read_bytes() == before
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["arm.usda"]
        del stage


def test_max_prims_truncates_and_says_so():
    result = _json(inspect_robot(_arm(), max_prims=4))
    assert result["ok"] and result["truncated"] is True and result["scanned"] == 4
    assert any("max_prims=4" in note for note in result["notes"])


def test_deadline_stops_the_scan_and_says_so():
    result = _json(inspect_robot(_arm(), deadline=TimeBudget(0)))
    assert result["ok"] and result["truncated_by_time"] is True and result["truncated"] is True
    assert result["time_budget"]["truncated_by_time"] is True
    roomy = _json(inspect_robot(_arm(), deadline=time.monotonic() + 30))
    assert roomy["truncated_by_time"] is False and roomy["robot_count"] == 1
    assert inspect_robot(_arm(), deadline="soon")["ok"] is False


# --- the real Franka (local only) ----------------------------------------------------------------------------


def test_real_franka_from_the_isaac_asset_pack_read_only():
    if _IN_KIT:
        return
    candidates = [ASSET_PACK / relative for relative in FRANKA_CANDIDATES if (ASSET_PACK / relative).is_file()]
    if not candidates and ASSET_PACK.is_dir():
        candidates = sorted((ASSET_PACK / "Assets").rglob("franka*.usd*"))[:1] if (ASSET_PACK / "Assets").is_dir() else []
    if not candidates:
        print("SKIP-ish: no Franka asset in", ASSET_PACK)
        return
    path = candidates[0]
    before = path.stat().st_mtime_ns, path.stat().st_size
    try:
        stage = Usd.Stage.Open(str(path))
    except Exception as exc:  # pragma: no cover - depends on the asset pack
        print(f"Franka asset {path} does not open in this usd-core: {exc}")
        return
    if stage is None:
        print(f"Franka asset {path} did not open in this usd-core")
        return
    started = time.perf_counter()
    result = _json(inspect_robot(stage, deadline=time.monotonic() + 30))
    elapsed = time.perf_counter() - started
    assert result["ok"], result.get("error")
    assert result["robot_count"] >= 1 or result["truncated"], result["notes"]
    assert not stage.GetRootLayer().dirty
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before
    print(f"Franka {path.name}: {result['robot_count']} robot(s), {elapsed * 1000:.0f} ms")
    del stage
