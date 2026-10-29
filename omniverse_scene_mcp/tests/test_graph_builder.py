"""Step-by-step Action Graph tools and the class session 6 recipes: previews change nothing, refusals are plain,
and the live path sends exactly the og.Controller.edit sections a learner would type."""
import json

from omniverse_scene_mcp import graph_builder as gb
from omniverse_scene_mcp import omnigraph_helper as ogh


def _require_pxr() -> None:
    """Skip (not fail) a test that needs the USD Python bindings when pxr is missing or does not import."""
    try:
        import pxr  # noqa: F401
    except ImportError:
        import unittest

        raise unittest.SkipTest("USD Python bindings (pxr) are not installed")


def _stage_with_graph():
    from pxr import Sdf, Usd, UsdGeom, UsdPhysics
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/World")
    cube = UsdGeom.Cube.Define(stage, "/World/Cube").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(cube)
    UsdPhysics.CollisionAPI.Apply(cube)
    cube.CreateAttribute("physics:velocity", Sdf.ValueTypeNames.Vector3f).Set((0, 0, 0))
    belt = UsdGeom.Cube.Define(stage, "/World/Belt").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(belt)
    stage.DefinePrim("/World/ActionGraph", "OmniGraph")
    node = stage.DefinePrim("/World/ActionGraph/OnTick", "OmniGraphNode")
    node.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set("omni.graph.action.OnTick")
    return stage


class _Controller:
    calls = []

    class Keys:
        CONNECT = "connect"
        CREATE_NODES = "create_nodes"
        CREATE_VARIABLES = "create_variables"
        DELETE_NODES = "delete_nodes"
        DISCONNECT = "disconnect"
        SET_VALUES = "set_values"

    @classmethod
    def edit(cls, graph, edits):
        cls.calls.append((graph, edits))
        return ("graph", [], [], {})

    @staticmethod
    def node(path):
        raise RuntimeError("not needed in this fake")


class _Og:
    Controller = _Controller


def _live(stage, func):
    original = ogh._kit_modules
    ogh._kit_modules = lambda: {"og": _Og, "live_stage": stage, "usdrt": None}
    _Controller.calls = []
    try:
        return func()
    finally:
        ogh._kit_modules = original


def test_node_search_finds_class_nodes_in_the_installed_isaac_sim():
    result = gb.node_search("conveyor")
    if not result["ok"]:  # no local Isaac Sim install (e.g. CI): the refusal must say why
        assert "No node indexes" in result["error"]
        return
    assert any(row["id"] == "isaacsim.asset.gen.conveyor.IsaacConveyor" for row in result["nodes"]), result
    keyboard = gb.node_search("keyboard")["nodes"]
    assert any(row["id"] == "omni.graph.action.OnKeyboardInput" for row in keyboard)
    assert not gb.node_search("")["ok"] and not gb.node_search("x", limit=0)["ok"]
    json.dumps(result)


def test_previews_change_nothing_and_refusals_are_plain():
    _require_pxr()
    stage = _stage_with_graph()
    before = stage.GetRootLayer().ExportToString()
    previews = [
        gb.create_graph(stage, "/World/Graph2"),
        gb.add_node(stage, "/World/ActionGraph", "KeyW", "omni.graph.action.OnKeyboardInput",
                    values={"inputs:keyIn": "W"}),
        gb.connect(stage, "/World/ActionGraph", "OnTick.outputs:tick", "OnTick.inputs:onlyPlayback"),
        gb.set_value(stage, "/World/ActionGraph", "OnTick.inputs:onlyPlayback", True),
        gb.add_variable(stage, "/World/ActionGraph", "forward", "float[3]", default=[5, 0, 0]),
        gb.delete_node(stage, "/World/ActionGraph", "OnTick"),
    ]
    for preview in previews:
        assert preview["ok"] and preview["dry_run"] and "og.Controller.edit" in preview["python"], preview
    assert stage.GetRootLayer().ExportToString() == before
    refusals = [
        (gb.create_graph(stage, "/World/ActionGraph"), "already exists"),
        (gb.add_node(stage, "/World/Nope", "A", "omni.graph.action.OnTick"), "no Action Graph"),
        (gb.add_node(stage, "/World/ActionGraph", "OnTick", "omni.graph.action.OnTick"), "already has a node"),
        (gb.add_node(stage, "/World/ActionGraph", "X", "omni.graph.action.OnTickk"), "Unknown node type"),
        (gb.connect(stage, "/World/ActionGraph", "OnTick.inputs:a", "OnTick.outputs:b"), "FROM an output"),
        (gb.connect(stage, "/World/ActionGraph", "Ghost.outputs:a", "OnTick.inputs:b"), "no node named Ghost"),
        (gb.set_value(stage, "/World/ActionGraph", "OnTick.outputs:tick", 1), "Only input pins"),
        (gb.add_variable(stage, "/World/ActionGraph", "speed", "vector"), "value_type must be"),
        (gb.delete_node(stage, "/World/ActionGraph", "Ghost"), "no node named"),
    ]
    for result, text in refusals:
        assert not result["ok"] and text in result["error"], (text, result)


def test_live_steps_send_the_matching_controller_sections():
    _require_pxr()
    stage = _stage_with_graph()
    graph = "/World/ActionGraph"
    steps = [
        (lambda: gb.add_node(stage, graph, "KeyW", "omni.graph.action.OnKeyboardInput",
                             values={"inputs:keyIn": "W"}, dry_run=False),
         {"create_nodes": [("KeyW", "omni.graph.action.OnKeyboardInput")], "set_values": [("KeyW.inputs:keyIn", "W")]}),
        (lambda: gb.add_variable(stage, graph, "forward", "float[3]", default=[5, 0, 0], dry_run=False),
         {"create_variables": [("forward", "float[3]", [5, 0, 0])]}),
        (lambda: gb.set_value(stage, graph, "OnTick.inputs:onlyPlayback", True, dry_run=False),
         {"set_values": [("OnTick.inputs:onlyPlayback", True)]}),
        (lambda: gb.delete_node(stage, graph, "OnTick", dry_run=False), {"delete_nodes": ["OnTick"]}),
    ]
    for step, expected in steps:
        result = _live(stage, step)
        assert result["ok"] and not result["dry_run"], result
        assert _Controller.calls == [(graph, expected)], _Controller.calls
    made = _live(stage, lambda: gb.create_graph(stage, "/World/New", dry_run=False))
    assert made["ok"] and _Controller.calls[0][0] == {"graph_path": "/World/New", "evaluator_name": "execution"}


def test_session6_recipes_plan_variables_and_warn_about_missing_physics():
    _require_pxr()
    stage = _stage_with_graph()
    keys = ogh.plan_recipe("keyboard_velocity", stage=stage, prim="/World/Cube")
    assert keys["ok"], keys
    assert [v["name"] for v in keys["variables"]] == ["forward", "back", "left", "right", "stop"]
    assert any(c["from"] == "KeyW.outputs:pressed" and c["to"] == "Write_forward.inputs:execIn" for c in keys["connections"])
    assert "CREATE_VARIABLES" in keys["python"]
    printing = ogh.plan_recipe("print_attribute", stage=stage, prim="/World/Cube", attribute="physics:velocity")
    assert printing["ok"] and any(c["to"] == "Print.inputs:text" for c in printing["connections"]), printing
    assert not ogh.plan_recipe("print_attribute", stage=stage, prim="/World/Cube", attribute="nope")["ok"]
    belt = ogh.plan_recipe("conveyor", stage=stage, belt="/World/Belt", velocity=-2)
    assert belt["ok"] and belt["variables"][0] == {"name": "Velocity", "type": "float", "default": -2.0,
                                                  "why": belt["variables"][0]["why"]}
    warnings = " ".join(belt["warnings"])
    assert "kinematic" in warnings and "collider" in warnings and "isaacsim.asset.gen.conveyor" in warnings
    edit = ogh._build_edit(_Og, None, belt)
    assert edit["create_variables"] == [("Velocity", "float", -2.0)]
    assert ("OnTick.outputs:tick", "ConveyorNode.inputs:onStep") in edit["connect"]
