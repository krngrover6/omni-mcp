"""Tools for class sessions 7-12: random package spawner, Script Nodes, wheeled-robot recipes, scene_diagnose and
the debug challenges. Pure pxr on temporary stages; the live graph path uses the fake og from test_graph_builder."""
import json
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import debug_trainer as dt
from omniverse_scene_mcp import graph_builder as gb
from omniverse_scene_mcp import omnigraph_helper as ogh
from omniverse_scene_mcp import spawner
from test_graph_builder import _Controller, _live, _stage_with_graph


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def test_spawner_previews_then_creates_non_overlapping_coloured_physics_packages():
    stage = _stage()
    before = stage.GetRootLayer().ExportToString()
    preview = spawner.spawn_packages(stage, count=12, seed=7, colors=["red", "blue"])
    assert preview["ok"] and preview["dry_run"] and stage.GetRootLayer().ExportToString() == before, preview
    assert "random.uniform" in preview["python"]
    made = spawner.spawn_packages(stage, count=12, seed=7, colors=["red", "blue"], dry_run=False)
    assert made["ok"] and made["created"]["count"] == 12, made
    packages = stage.GetPrimAtPath("/World/Packages").GetChildren()
    assert len(packages) == 12 and all(p.HasAPI(UsdPhysics.RigidBodyAPI) and p.HasAPI(UsdPhysics.CollisionAPI)
                                       for p in packages)
    assert {p.GetAttribute("package:color").Get() for p in packages} <= {"red", "blue"}
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default"])
    boxes = [cache.ComputeWorldBound(p).ComputeAlignedRange() for p in packages]
    assert all(Gf.Range3d.GetIntersection(a, b).IsEmpty() for i, a in enumerate(boxes) for b in boxes[i + 1:])
    assert dt.diagnose(stage, "physics")["count"] >= 1  # no physics scene yet: diagnose says so
    again = spawner.spawn_packages(stage, count=12, seed=7, colors=["red", "blue"])
    assert not again["ok"] and "already exists" in again["error"]
    same = spawner.spawn_packages(_stage(), count=12, seed=7, colors=["red", "blue"])
    assert same["would_create"]["packages"][:3] == preview["would_create"]["packages"][:3]  # a seed repeats a batch
    for bad in ({"count": 0}, {"count": 500}, {"region_min": [0, 0, 0], "region_max": [0, 1, 1]},
                {"colors": ["pink"]}, {"size_range": [0.3, 0.1]}, {"count": 200, "region_max": [0, 0, 1.0]}):
        assert not spawner.spawn_packages(_stage(), **bad)["ok"], bad


def test_script_node_templates_preview_and_send_pins_code_and_trigger():
    stage = _stage_with_graph()
    preview = gb.add_script_node(stage, "/World/ActionGraph", "MoveBox", "move_on_trigger",
                                 values={"inputs:prim_path": "/World/Cube"}, trigger_key="T")
    assert preview["ok"] and preview["dry_run"] and "def compute(db)" in preview["would"]["code"], preview
    for spec in gb.SCRIPT_TEMPLATES.values():
        compile(spec["code"], "<template>", "exec")
    done = _live(stage, lambda: gb.add_script_node(stage, "/World/ActionGraph", "MoveBox", "move_on_trigger",
                                                   values={"inputs:prim_path": "/World/Cube"}, trigger_key="T",
                                                   dry_run=False))
    assert done["ok"], done
    graph, edits = _Controller.calls[0]
    assert edits["create_nodes"] == [("MoveBox", "omni.graph.scriptnode.ScriptNode"),
                                     ("KeyT", "omni.graph.action.OnKeyboardInput")]
    assert ("MoveBox.inputs:offset", "double[3]") in edits["create_attributes"]
    assert ("MoveBox.inputs:offset", (0.0, 0.0, 0.5)) in edits["set_values"]  # a tuple, as og wants
    assert ("KeyT.outputs:pressed", "MoveBox.inputs:execIn") in edits["connect"]
    pin = _live(stage, lambda: gb.add_attribute(stage, "/World/ActionGraph", "OnTick.inputs:speed", "double",
                                                dry_run=False))
    assert pin["ok"] and _Controller.calls[0][1] == {"create_attributes": [("/World/ActionGraph/OnTick.inputs:speed",
                                                                            "double")]}
    for bad in (gb.add_script_node(stage, "/World/ActionGraph", "X", "nope"),
                gb.add_script_node(stage, "/World/ActionGraph", "X", "counter", values={"inputs:nope": 1}),
                gb.add_script_node(stage, "/World/ActionGraph", "X", "counter", trigger_key="Banana"),
                gb.add_attribute(stage, "/World/ActionGraph", "OnTick.inputs:speed", "vector")):
        assert not bad["ok"], bad


def _jetbot(stage, path="/World/Jetbot"):
    robot = UsdGeom.Xform.Define(stage, path).GetPrim()
    UsdPhysics.ArticulationRootAPI.Apply(robot)
    chassis = UsdGeom.Cube.Define(stage, f"{path}/chassis").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(chassis)
    for side in ("left", "right"):
        wheel = UsdGeom.Cylinder.Define(stage, f"{path}/{side}_wheel").GetPrim()
        UsdPhysics.RigidBodyAPI.Apply(wheel)
        joint = UsdPhysics.RevoluteJoint.Define(stage, f"{path}/chassis/{side}_wheel_joint")
        joint.CreateBody0Rel().SetTargets([chassis.GetPath()])
        joint.CreateBody1Rel().SetTargets([wheel.GetPath()])
    return robot


def test_wheeled_robot_recipes_plan_controllers_variables_and_sense_decide_act():
    stage = _stage()
    _jetbot(stage)
    UsdGeom.Cube.Define(stage, "/World/Obstacle")
    drive = ogh.plan_recipe("differential_drive", stage=stage, robot="/World/Jetbot")
    assert drive["ok"], drive
    assert [v["name"] for v in drive["variables"]] == ["linear", "angular"]
    joints = next(v for v in drive["values"] if v["attribute"] == "Wheels.inputs:jointNames")
    assert joints["value"] == ["left_wheel_joint", "right_wheel_joint"] and joints.get("array")
    edit = ogh._build_edit(type("Og", (), {"Controller": _Controller}), None, drive)
    assert ("Wheels.inputs:jointNames", ["left_wheel_joint", "right_wheel_joint"]) in edit["set_values"]  # stays a list
    route = ogh.plan_recipe("drive_sequence", stage=stage, robot="/World/Jetbot")
    assert route["ok"] and sum(1 for n in route["nodes"] if n["type"] == "omni.graph.action.Delay") == 4, route
    assert any(c["from"] == "Wait4.outputs:finished" and c["to"] == "StopLin.inputs:execIn" for c in route["connections"])
    stop = ogh.plan_recipe("obstacle_stop", stage=stage, robot="/World/Jetbot", sensor_prim="/World/Jetbot/chassis",
                           obstacle="/World/Obstacle", stop_distance=0.4)
    assert stop["ok"], stop
    assert any(c["from"] == "Decide.outputs:execTrue" and c["to"] == "Halt.inputs:execIn" for c in stop["connections"])
    # Seen live: the Articulation Controller wipes commands TYPED into it on Stop, so the decision writes the speed
    # variable and the controller is fed through a wire (Differential -> Wheels) every frame.
    assert any(c["from"] == "Differential.outputs:velocityCommand" and c["to"] == "Wheels.inputs:velocityCommand"
               for c in stop["connections"])
    assert not any(v["attribute"].endswith("inputs:velocityCommand") for v in stop["values"])
    go = next(v for v in stop["values"] if v["attribute"] == "Go.inputs:value")
    assert go["value"] == 0.3 and go["type"] == "double"
    wrong = ogh.plan_recipe("differential_drive", stage=stage, robot="/World/Jetbot", wheel_joints=["a", "b"])
    assert not wrong["ok"] and "no joints named" in wrong["error"]
    assert not ogh.plan_recipe("drive_sequence", stage=stage, robot="/World/Jetbot", steps=[{"seconds": -1}])["ok"]


def test_diagnose_names_symptom_cause_and_fix():
    stage = _stage()
    floor = UsdGeom.Cube.Define(stage, "/World/Floor").GetPrim()
    box = UsdGeom.Cube.Define(stage, "/World/Box").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(box)
    joint = UsdPhysics.RevoluteJoint.Define(stage, "/World/Box/Hinge")
    joint.CreateBody1Rel().SetTargets(["/World/Bxo"])
    codes = {f["code"] for f in dt.diagnose(stage)["findings"]}
    assert {"no_physics_scene", "no_collider", "wrong_prim_path"} <= codes, codes
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    UsdPhysics.CollisionAPI.Apply(box)
    joint.GetBody1Rel().SetTargets(["/World/Box"])
    UsdPhysics.CollisionAPI.Apply(floor)
    clean = dt.diagnose(stage)
    assert not {f["code"] for f in clean["findings"]} & {"no_physics_scene", "no_collider", "wrong_prim_path"}, clean
    assert not dt.diagnose(stage, "nonsense")["ok"]
    json.dumps(clean)


def test_every_debug_challenge_is_caught_by_diagnose_and_graded():
    with tempfile.TemporaryDirectory() as folder:
        expected = {"box_wont_fall": "no_physics_scene", "falls_through_floor": None, "box_passes_through": "no_collider",
                    "frozen_box": "kinematic", "exploding_boxes": "overlap", "door_falls_off": "wrong_prim_path",
                    "missing_box": "invisible"}
        assert set(expected) == set(dt.CHALLENGES)
        for name, code in expected.items():
            preview = dt.make_challenge(name, folder)
            assert preview["ok"] and not Path(preview["file"]).exists()
            made = dt.make_challenge(name, folder, dry_run=False)
            assert made["written"] and Path(made["file"]).is_file()
            assert not dt.make_challenge(name, folder, dry_run=False)["ok"]  # never overwrites
            stage = Usd.Stage.Open(made["file"])
            codes = {f["code"] for f in dt.diagnose(stage)["findings"]}
            if code:
                assert code in codes, (name, codes)
        grade = dt.check_answer("falls_through_floor", "the box has no collider")
        assert not grade["correct"] and "WHICH prim" in grade["feedback"]
        assert dt.check_answer("falls_through_floor", "The floor has no collider so it is not solid")["correct"]
        assert dt.check_answer("door_falls_off", "body0 has a typo: /World/Dor")["correct"]
        assert len(dt.check_answer("frozen_box", hint=True)["hints"]) == 2
        assert "kinematic" in dt.check_answer("frozen_box", reveal=True)["cause"]


def test_diagnose_sees_colliders_inside_instanced_parts():
    # Seen live: Franka links keep their collision shapes under instanced prims; diagnose called them collider-less.
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    proto = UsdGeom.Xform.Define(stage, "/Prototypes/Part").GetPrim()
    shape = UsdGeom.Cube.Define(stage, "/Prototypes/Part/Shape").GetPrim()
    UsdPhysics.CollisionAPI.Apply(shape)
    link = UsdGeom.Xform.Define(stage, "/World/Arm/Link").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(link)
    geometry = stage.DefinePrim("/World/Arm/Link/Geometry")
    geometry.GetReferences().AddInternalReference(proto.GetPath())
    geometry.SetInstanceable(True)
    codes = [f["code"] for f in dt.diagnose(stage, "physics")["findings"] if f["prim"] == "/World/Arm/Link"]
    assert "no_collider" not in codes, codes


def _run_sorter(stage, code, inputs, times):
    """Execute the colour-sort Script Node code against a USD stage, with fake omni.usd / omni.timeline."""
    import sys
    import types
    clock = {"now": 0.0}
    usd = types.ModuleType("omni.usd")
    usd.get_context = lambda: types.SimpleNamespace(get_stage=lambda: stage)
    timeline = types.ModuleType("omni.timeline")
    timeline.get_timeline_interface = lambda: types.SimpleNamespace(get_current_time=lambda: clock["now"])
    omni = types.ModuleType("omni")  # a fresh fake package: "import omni.timeline" then reads omni.timeline
    omni.usd, omni.timeline = usd, timeline
    saved = {name: sys.modules.get(name) for name in ("omni", "omni.usd", "omni.timeline")}
    sys.modules.update({"omni": omni, "omni.usd": usd, "omni.timeline": timeline})
    try:
        scope = {}
        exec(compile(code, "<sorter>", "exec"), scope)
        db = types.SimpleNamespace(inputs=types.SimpleNamespace(**inputs), outputs=types.SimpleNamespace(sorted=0))
        scope["setup"](db)
        counts = []
        for now in times:
            clock["now"] = now
            assert scope["compute"](db) is True
            counts.append(db.outputs.sorted)
        return counts
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_color_sort_recipe_routes_each_package_to_its_bin_once_per_play():
    stage = _stage()
    made = spawner.spawn_packages(stage, count=6, seed=4, colors=["red", "blue"], region_min=[-0.5, -0.5, 0.2],
                                  region_max=[0.5, 0.5, 0.6], dry_run=False)
    assert made["ok"], made
    for name, y in (("BinA", 2.0), ("BinB", -2.0)):
        UsdGeom.XformCommonAPI(UsdGeom.Cube.Define(stage, f"/World/{name}")).SetTranslate(Gf.Vec3d(0, y, 0))
    bins = {"red": "/World/BinA", "blue": "/World/BinB"}
    plan = ogh.plan_recipe("color_sort", stage=stage, bins=bins, zone_min=[-1, -1, 0], zone_max=[1, 1, 1])
    assert plan["ok"], plan
    assert ("Sorter.inputs:zone_min", "double[3]") in [(a["attribute"], a["type"]) for a in plan["attributes"]]
    edit = ogh._build_edit(type("Og", (), {"Controller": _Controller}), None, plan)
    assert ("Sorter.inputs:zone_min", (-1.0, -1.0, 0.0)) in edit["set_values"]  # tuple for a double[3] pin
    code = next(v["value"] for v in plan["values"] if v["attribute"] == "Sorter.inputs:script")
    inputs = {"packages": "/World/Packages", "routes": json.dumps(bins), "zone_min": (-1, -1, 0), "zone_max": (1, 1, 1),
              "speed": 1.5, "hop": 1.0}
    counts = _run_sorter(stage, code, inputs, [0.0, 0.1, 0.2])
    assert counts == [6, 6, 6], counts  # all six sorted on the first frame, never twice
    for prim in stage.GetPrimAtPath("/World/Packages").GetChildren():
        velocity = UsdPhysics.RigidBodyAPI(prim).GetVelocityAttr().Get()
        colour = prim.GetAttribute("package:color").Get()
        assert (velocity[1] > 0) == (colour == "red") and velocity[2] == 1.0, (colour, velocity)
    assert _run_sorter(stage, code, inputs, [5.0, 0.0])[-1] == 6  # Stop -> Play: the clock went back, so it re-sorts
    missing = ogh.plan_recipe("color_sort", stage=stage, bins={"red": "/World/BinA"}, zone_min=[-1, -1, 0],
                              zone_max=[1, 1, 1])
    assert missing["ok"] and any("blue" in w for w in missing["warnings"]), missing["warnings"]
    assert not ogh.plan_recipe("color_sort", stage=stage, bins={"red": "/World/Nope"}, zone_min=[-1, -1, 0],
                               zone_max=[1, 1, 1])["ok"]
    assert not ogh.plan_recipe("color_sort", stage=stage, bins=bins, zone_min=[1, 1, 1], zone_max=[0, 0, 0])["ok"]
