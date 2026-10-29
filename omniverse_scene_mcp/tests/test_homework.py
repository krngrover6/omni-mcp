"""homework: session 3-5 checkers on tiny scenes; read-only; phrases route without changing existing routes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, homework as hw, joints as j, nl_tier8
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter
from omniverse_scene_mcp.service import SceneMCPService


def _body(stage, path, t, kind=UsdGeom.Cube, rigid=True, collider=True):
    prim = kind.Define(stage, path)
    UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*t))
    if collider:
        UsdPhysics.CollisionAPI.Apply(prim.GetPrim())
    if rigid:
        UsdPhysics.RigidBodyAPI.Apply(prim.GetPrim())
    return prim


def _status(result):
    return {row["rule"]: row["status"] for row in result["results"]}


def test_session4_bowling_from_partial_to_full():
    stage = Usd.Stage.CreateInMemory()
    ground = UsdGeom.Plane.Define(stage, "/World/GroundPlane/CollisionPlane")
    UsdPhysics.CollisionAPI.Apply(ground.GetPrim())
    UsdGeom.Xform.Define(stage, "/World/Pins")  # a group folder is not a pin
    for i in range(6):
        _body(stage, f"/World/Pins/Pin_{i}", (i * 0.3, 5, 0.5), kind=UsdGeom.Cylinder)
    ball = _body(stage, "/World/Ball", (0, 0, 0.5), kind=UsdGeom.Sphere)
    first = hw.check_homework(stage, 4)
    assert _status(first) == {"physics_scene": "partial", "floor_collider": "pass", "pins_dynamic": "pass",
                              "ball_dynamic": "pass", "ball_launched": "partial"}
    assert "Linear Velocity" in first["next_step"] or "Physics Scene" in first["next_step"]
    UsdPhysics.RigidBodyAPI(ball.GetPrim()).CreateVelocityAttr().Set(Gf.Vec3f(0, 5, 0))
    UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    done = hw.check_homework(stage, 4)
    assert done["passed"] and done["score_pct"] == 100.0 and done["read_only"]


def test_session4_pins_without_physics_fail_with_steps():
    stage = Usd.Stage.CreateInMemory()
    for i in range(4):
        UsdGeom.Cylinder.Define(stage, f"/World/Pin_{i}")
    result = hw.check_homework(stage, 4)
    status = _status(result)
    assert status["pins_dynamic"] == "fail" and status["floor_collider"] == "fail" and status["ball_dynamic"] == "fail"
    pins = next(r for r in result["results"] if r["rule"] == "pins_dynamic")
    assert pins["evidence"]["pins"] == 4 and any("Rigid Body with Colliders Preset" in s for s in pins["how_to_fix"])


def test_session5_drawers_door_and_robot():
    stage = Usd.Stage.CreateInMemory()
    for path, t in (("/Table", (0, 0, 1)), ("/Drawer1", (0, 0, 1)), ("/Drawer2", (0, 0, 0.5)), ("/Door", (3, 0, 1)),
                    ("/Robot/Base", (0, 5, 0.5))):
        _body(stage, path, t)
    for i in range(4):
        _body(stage, f"/Robot/Wheel{i}", (i, 5, 0.2), kind=UsdGeom.Cylinder)
    empty = hw.check_homework(stage, 5)
    assert _status(empty)["drawers"] == "fail" and _status(empty)["robot_wheels"] == "fail"
    for drawer in ("/Drawer1", "/Drawer2"):
        j.create_joint(stage, "prismatic", "/Table", drawer, lower=0, upper=0.4, dry_run=False)
    j.create_joint(stage, "revolute", None, "/Door", axis="Z", dry_run=False)
    middle = _status(hw.check_homework(stage, 5))
    assert middle["drawers"] == "pass" and middle["self_closing_door"] == "partial" and middle["robot_wheels"] == "fail"
    j.add_drive(stage, "/Door/RevoluteJoint", target_position=0, stiffness=500, dry_run=False)
    for i in range(4):
        j.create_joint(stage, "revolute", "/Robot/Base", f"/Robot/Wheel{i}", axis="Y", dry_run=False)
    j.add_articulation_root(stage, "/Robot/Base", dry_run=False)
    done = hw.check_homework(stage, 5)
    assert done["passed"] and done["read_only"]
    robot = next(r for r in done["results"] if r["rule"] == "robot_wheels")
    assert robot["evidence"] == {"base": "/Robot/Base", "wheel_joints": 4}


def test_session5_flags_a_joint_between_static_bodies():
    stage = Usd.Stage.CreateInMemory()
    a, b = UsdGeom.Cube.Define(stage, "/A"), UsdGeom.Cube.Define(stage, "/B")
    joint = UsdPhysics.FixedJoint.Define(stage, "/B/FixedJoint")
    joint.CreateBody0Rel().SetTargets([a.GetPath()])
    joint.CreateBody1Rel().SetTargets([b.GetPath()])
    assert _status(hw.check_homework(stage, 5))["joint_bodies"] == "fail"


def test_session3_references_and_materials():
    stage = Usd.Stage.CreateInMemory()
    asset = Sdf.Layer.CreateAnonymous("carton.usda")
    asset.ImportFromString('#usda 1.0\n(\n defaultPrim = "Box"\n)\ndef Cube "Box"\n{\n}\n')
    for i in range(10):
        stage.DefinePrim(f"/World/Cartons/Carton_{i}").GetReferences().AddReference(asset.identifier)
    shelf = UsdGeom.Cube.Define(stage, "/World/Shelf_A")
    material = UsdShade.Material.Define(stage, "/Looks/Wood")
    UsdShade.MaterialBindingAPI.Apply(shelf.GetPrim()).Bind(material)
    UsdGeom.Cube.Define(stage, "/World/Wall_Back")
    result = hw.check_homework(stage, 3)
    status = _status(result)
    assert status["cartons_referenced"] == "pass" and status["shelf_materials"] == "pass"
    assert status["wall_materials"] == "fail" and status["library_props"] == "partial"
    assert result["read_only"] and not result["passed"]


def test_bad_session_and_no_stage():
    assert not hw.check_homework(Usd.Stage.CreateInMemory(), 2)["ok"]
    assert not hw.check_homework(Usd.Stage.CreateInMemory(), True)["ok"]
    assert not hw.check_homework(None, 5)["ok"]
    assert [s["session"] for s in hw.homework_sessions()["sessions"]] == [3, 4, 5, 6, 14]
    assert not hw.check_homework(Usd.Stage.CreateInMemory(), 7)["ok"]
    assert features.FEATURES["homework_check"].mutates is False


def test_phrases_for_homework_and_joints():
    cases = {
        "check my session 5 homework": ("homework_check", {"session": 5}),
        "grade my homework for session 6": ("homework_check", {"session": 6}),
        "grade my homework for session 4": ("homework_check", {"session": 4}),
        "check my session 14 homework": ("homework_check", {"session": 14}),
        "create a revolute joint between /World/Base and /World/Wheel on the y axis": (
            "scene_create_joint", {"kind": "revolute", "body0": "/World/Base", "body1": "/World/Wheel", "axis": "Y",
                                   "dry_run": True}),
        "add a fixed joint for /World/Base to the world": (
            "scene_create_joint", {"kind": "fixed", "body1": "/World/Base", "dry_run": True}),
    }
    for phrase, (tool, arguments) in cases.items():
        parsed = nl_tier8.parse(phrase)
        assert parsed is not None and parsed["tool"] == tool and parsed["arguments"] == arguments, (phrase, parsed)
    assert nl_tier8.parse("check my homework") is None or nl_tier8.parse("check my homework")["tool"] != "homework_check"


def test_existing_routes_are_unchanged():
    spec = importlib.util.spec_from_file_location("round8_corpus", Path(__file__).with_name("test_round8_integration.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    phrases = module._corpus()
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    after = {phrase: router.route(phrase) for phrase in phrases}
    original = nl_tier8._physics_route
    nl_tier8._physics_route = lambda _clean: None
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier8._physics_route = original
    assert not [p for p in phrases if before[p] != after[p]]


def _node(stage, path, kind):
    prim = stage.DefinePrim(path, "OmniGraphNode")
    prim.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(kind)
    return prim


def _pin(prim, name, type_name=Sdf.ValueTypeNames.Token):
    return prim.CreateAttribute(name, type_name)


def _wire(source, pin, target, target_pin):
    _pin(target, target_pin).AddConnection(f"{source.GetPath()}.{pin}")


def _belt(stage, path, t, yaw):
    belt = _body(stage, path, t)
    UsdGeom.XformCommonAPI(belt).SetScale(Gf.Vec3f(1, 0.3, 0.05))
    UsdGeom.XformCommonAPI(belt).SetRotate(Gf.Vec3f(0, 0, yaw))
    belt.GetPrim().CreateAttribute("physics:kinematicEnabled", Sdf.ValueTypeNames.Bool).Set(True)
    return belt.GetPrim()


def _conveyor(stage, graph, name, belt, tick):
    node = _node(stage, f"{graph}/{name}", hw._CONVEYOR)
    node.CreateRelationship("inputs:conveyorPrim").SetTargets([belt.GetPath()])
    _wire(tick, "outputs:tick", node, "inputs:onStep")
    return node


def test_session6_conveyor_loop_from_one_belt_to_a_full_stop_and_go():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    stage.DefinePrim("/World/Graph", "OmniGraph")
    tick = _node(stage, "/World/Graph/OnTick", "omni.graph.action.OnPlaybackTick")
    first = _belt(stage, "/World/Belt1", (0, 0, 0.05), 0)
    _conveyor(stage, "/World/Graph", "Conveyor1", first, tick)
    _body(stage, "/World/Box", (0, 0, 0.3)).GetPrim()
    UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Box")).SetScale(Gf.Vec3f(0.1, 0.1, 0.1))
    one = hw.check_homework(stage, 6)
    status = _status(one)
    assert one["ok"] and status["conveyor_nodes"] == "pass" and status["belt_physics"] == "pass", status
    assert status["box"] == "pass" and status["loop"] == "fail" and status["wait_5s"] == "fail", status
    assert "Delay" in " ".join(r["how_to_fix"][0] for r in one["results"] if r["rule"] == "wait_5s")
    # a loop: two more belts turned -90 degrees each, with their own conveyor nodes
    for index, (t, yaw) in enumerate((((2, 0, 0.05), -90), ((2, -2, 0.05), -180)), start=2):
        _conveyor(stage, "/World/Graph", f"Conveyor{index}", _belt(stage, f"/World/Belt{index}", t, yaw), tick)
    # detect -> stop -> wait 5 s -> resume
    read = _node(stage, "/World/Graph/ReadBox", "omni.graph.nodes.ReadPrimAttribute")
    compare = _node(stage, "/World/Graph/AtStopper", "omni.graph.nodes.Compare")
    _wire(read, "outputs:value", compare, "inputs:a")
    stop = _node(stage, "/World/Graph/Stop", "omni.graph.core.WriteVariable")
    delay = _node(stage, "/World/Graph/Wait", "omni.graph.action.Delay")
    _pin(delay, "inputs:duration", Sdf.ValueTypeNames.Double).Set(5.0)
    _wire(stop, "outputs:execOut", delay, "inputs:execIn")
    resume = _node(stage, "/World/Graph/Resume", "omni.graph.core.WriteVariable")
    _wire(delay, "outputs:finished", resume, "inputs:execIn")
    full = hw.check_homework(stage, 6)
    assert set(_status(full).values()) == {"pass"}, _status(full)
    assert full["passed"] and full["read_only"]
    assert 6 in [s["session"] for s in hw.homework_sessions()["sessions"]]


def test_session6_stop_must_act_on_the_belt_and_kit_graphs_do_not_count():
    # Seen live: a W-key graph writing a ball's velocity passed "stop", and /Render graphs counted as the learner's.
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.DefinePrim("/Render/PostProcess/SDGPipeline", "OmniGraph")
    assert _status(hw.check_homework(stage, 6))["action_graph"] == "fail"
    stage.DefinePrim("/World/Graph", "OmniGraph")
    tick = _node(stage, "/World/Graph/OnTick", "omni.graph.action.OnPlaybackTick")
    belt = _belt(stage, "/World/Belt", (0, 0, 0.05), 0)
    conveyor = _conveyor(stage, "/World/Graph", "Conveyor", belt, tick)
    speed = _node(stage, "/World/Graph/ReadSpeed", "omni.graph.core.ReadVariable")
    _pin(speed, "inputs:variableName").Set("Velocity")
    _wire(speed, "outputs:value", conveyor, "inputs:velocity")
    _body(stage, "/World/Ball", (5, 5, 0.5))
    push = _node(stage, "/World/Graph/PushBall", "omni.graph.nodes.WritePrimAttribute")
    _pin(push, "inputs:name").Set("physics:velocity")
    push.CreateRelationship("inputs:prim").SetTargets(["/World/Ball"])
    other = _node(stage, "/World/Graph/Score", "omni.graph.core.WriteVariable")
    _pin(other, "inputs:variableName").Set("score")
    result = hw.check_homework(stage, 6)
    assert _status(result)["stop"] == "fail", result["results"]
    stop = _node(stage, "/World/Graph/StopBelt", "omni.graph.core.WriteVariable")
    _pin(stop, "inputs:variableName").Set("Velocity")
    rows = {r["rule"]: r for r in hw.check_homework(stage, 6)["results"]}
    assert rows["stop"]["status"] == "pass" and rows["stop"]["evidence"]["conveyor_speed_variables"] == ["Velocity"]


def test_session14_factory_cell_from_a_lone_arm_to_a_full_cell():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.Xform.Define(stage, "/World/Arm")
    base = _body(stage, "/World/Arm/link0", (0, 0, 0.05)).GetPrim()
    UsdPhysics.ArticulationRootAPI.Apply(base)  # Isaac puts the root on a link: the robot is its parent
    previous = base
    for i in range(1, 8):
        link = _body(stage, f"/World/Arm/link{i}", (0, 0, 0.1 * i)).GetPrim()
        joint = UsdPhysics.RevoluteJoint.Define(stage, f"/World/Arm/joints/joint{i}")
        joint.CreateBody0Rel().SetTargets([previous.GetPath()])
        joint.CreateBody1Rel().SetTargets([link.GetPath()])
        previous = link
    lone = hw.check_homework(stage, 14)
    status = _status(lone)
    assert lone["ok"] and lone["read_only"] and status["arm"] == "pass" and status["gripper"] == "partial", status
    assert status["conveyor"] == "fail" and status["sensor"] == "fail" and status["control"] == "unknown", status
    UsdPhysics.PrismaticJoint.Define(stage, "/World/Arm/joints/panda_finger_joint1")
    UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
    stage.DefinePrim("/World/Graph", "OmniGraph")
    tick = _node(stage, "/World/Graph/OnTick", "omni.graph.action.OnPlaybackTick")
    _conveyor(stage, "/World/Graph", "Conveyor", _belt(stage, "/World/Belt", (0.6, 0, 0.05), 90), tick)
    big = _body(stage, "/World/Package_big", (0.6, 0.0, 0.3))
    UsdGeom.XformCommonAPI(big).SetScale(Gf.Vec3f(0.1, 0.1, 0.1))  # cube of size 2 -> 0.2 m: too wide to grab
    far_bin = _body(stage, "/World/Bin", (2.0, 0, 0.1), rigid=False)
    half = _status(hw.check_homework(stage, 14))
    assert half["gripper"] == "pass" and half["conveyor"] == "pass" and half["physics"] == "pass", half
    assert half["package"] == "partial" and half["place_target"] == "partial" and half["control"] == "unknown", half
    UsdGeom.XformCommonAPI(big).SetScale(Gf.Vec3f(0.025, 0.025, 0.025))  # 5 cm
    UsdGeom.XformCommonAPI(far_bin).SetTranslate(Gf.Vec3d(0, 0.6, 0.1))
    UsdGeom.Camera.Define(stage, "/World/TopCam")
    wheels = _node(stage, "/World/Graph/Wheels", "isaacsim.core.nodes.IsaacArticulationController")
    wheels.CreateRelationship("inputs:targetPrim").SetTargets(["/World/Jetbot"])  # drives something else
    _node(stage, "/World/Graph/Sorter", "omni.graph.scriptnode.ScriptNode")
    assert _status(hw.check_homework(stage, 14))["control"] == "partial"  # a script might, a wheel controller doesn't
    arm_node = _node(stage, "/World/Graph/ArmControl", "isaacsim.core.nodes.IsaacArticulationController")
    arm_node.CreateRelationship("inputs:targetPrim").SetTargets(["/World/Arm"])
    full = hw.check_homework(stage, 14)
    assert full["passed"] and set(_status(full).values()) == {"pass"}, _status(full)
