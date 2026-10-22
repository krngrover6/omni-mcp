"""omnigraph_helper: reading Action Graphs saved in USD, spotting problems, and planning/applying recipes.

Graphs are authored as small in-memory USDA stages that copy how Kit serializes them (OmniGraph / OmniGraphNode prims,
node:type tokens, connections from inputs to outputs, relationships for target inputs). Nothing is saved; Kit is never
needed: apply_recipe is exercised with a fake omni.graph.core that only records the edit.
"""

import json
import sys
import time
from pathlib import Path

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import omnigraph_helper as ogh
from omniverse_scene_mcp.omnigraph_helper import (
    NODE_TYPES,
    NODE_TYPES_SOURCE,
    RECIPES,
    apply_recipe,
    describe_node_type,
    inspect_graphs,
    list_recipes,
    plan_recipe,
)
from omniverse_scene_mcp.usd_insight import TimeBudget

BAG_STEP4 = Path(r"C:\Users\krngr\Omniverse\bag_configurator_course\assets\Step4_Web_Savepoint.usd")
_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules

HEADER = """#usda 1.0
(
    defaultPrim = "World"
    upAxis = "Y"
)

def Xform "World"
{
    def Xform "Bag"
    {
        float3 xformOp:rotateXYZ = (0, 30, 0)
        double3 xformOp:translate = (0, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]

        def Scope "Looks" (
            variants = {
                string Leather = "White"
            }
            prepend variantSets = "Leather"
        )
        {
            variantSet "Leather" = {
                "Black" {
                }
                "Pink" {
                }
                "White" {
                }
            }
        }
    }

    def Cube "Plain"
    {
    }

    def Scope "Graphs"
    {
%s
    }
}
"""


def graph(name, body, variables=""):
    return f"""
        def OmniGraph "{name}"
        {{
            token evaluationMode = "Automatic"
            token evaluator:type = "execution"
            token fabricCacheBacking = "Shared"
            int2 fileFormatVersion = (1, 9)
            token pipelineStage = "pipelineStageSimulation"
{variables}
{body}
        }}
"""


def node(name, type_id, lines="", version=1):
    return f"""
            def OmniGraphNode "{name}"
            {{
                token node:type = "{type_id}"
                int node:typeVersion = {version}
{lines}
            }}
"""


def stage_from(*graphs):
    layer = Sdf.Layer.CreateAnonymous(".usda")
    assert layer.ImportFromString(HEADER % "".join(graphs))
    return Usd.Stage.Open(layer)


G = "/World/Graphs"
EXEC_OUT = '                custom uint outputs:execOut (customData = {bool isExecution = 1})'


def key_node(name, key="J", only_playback="false"):
    lines = f"""                custom token inputs:keyIn = "{key}"
                custom bool inputs:onlyPlayback = {only_playback}
                custom uint outputs:pressed (customData = {{bool isExecution = 1}})
                custom uint outputs:released (customData = {{bool isExecution = 1}})"""
    return node(name, "omni.graph.action.OnKeyboardInput", lines, 4)


def configurator(graph_name="VariantController", sender_event="setColor", listener_event="setColor",
                 sender_key="colors", listener_key="colors", value="Black", variant_set="Leather",
                 wire_from_key=None, prim="/World/Bag/Looks"):
    wire_key = wire_from_key or listener_key
    path = f"{G}/{graph_name}"
    body = (
        key_node("on_keyboard_input")
        + node("constant_token", "omni.graph.nodes.ConstantToken", f'                custom token inputs:value = "{value}"')
        + node("send_messagebus_event", "omni.graph.action.SendMessageBusEvent", f"""
                custom token inputs:{sender_key}
                prepend token inputs:{sender_key}.connect = <{path}/constant_token.inputs:value>
                custom token inputs:eventName = "{sender_event}"
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{path}/on_keyboard_input.outputs:pressed>
{EXEC_OUT}""")
        + node("on_messagebus_event", "omni.graph.action.OnMessageBusEvent", f"""
                custom token inputs:eventName = "{listener_event}"
                custom bool inputs:onlyPlayback = false
                custom token outputs:{listener_key}
{EXEC_OUT}""", 2)
        + node("set_variant_selection", "omni.graph.nodes.SetVariantSelection", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{path}/on_messagebus_event.outputs:execOut>
                custom rel inputs:prim = <{prim}>
                custom bool inputs:setVariant = true
                custom token inputs:variantName
                prepend token inputs:variantName.connect = <{path}/on_messagebus_event.outputs:{wire_key}>
                custom token inputs:variantSetName = "{variant_set}"
{EXEC_OUT}""", 3)
    )
    return graph(graph_name, body)


def codes(result, severity=None):
    return [item["code"] for item in result["problems"] if severity is None or item["severity"] == severity]


def problem(result, code):
    return next(item for item in result["problems"] if item["code"] == code)


# ------------------------------------------------------------------------------------------------ inspect_graphs


def test_configurator_graph_story_is_plain_and_clean():
    stage = stage_from(configurator())
    result = inspect_graphs(stage)
    json.dumps(result)
    assert result["ok"] and result["counts"]["graphs"] == 1 and result["counts"]["nodes"] == 5
    graph_record = result["graphs"][0]
    assert graph_record["evaluator"] == "execution" and graph_record["pipeline_stage"] == "simulation"
    assert "Action Graph" in graph_record["plain"]
    flow = graph_record["flow"]
    assert "When key J is pressed -> Send MessageBus Event sends 'setColor' with colors='Black' (heard by on_messagebus_event)" in flow
    assert ("When message bus event 'setColor' arrives -> Set Variant Selection sets Leather=the 'colors' value "
            "carried by the 'setColor' event on /World/Bag/Looks") in flow
    assert codes(result, "error") == [] and codes(result, "warning") == []
    nodes = {item["name"]: item for item in graph_record["nodes"]}
    setter = nodes["set_variant_selection"]
    assert setter["known"] and setter["type"] == "omni.graph.nodes.SetVariantSelection" and setter["installed_version"] == 3
    assert setter["targets"] == {"prim": ["/World/Bag/Looks"]}
    assert setter["inputs"]["variantSetName"] == "Leather" and setter["inputs"]["setVariant"] is True
    kinds = {wire["input"]: wire["kind"] for wire in setter["connections"]["in"]}
    assert kinds == {"inputs:execIn": "execution", "inputs:variantName": "data"}
    assert nodes["on_messagebus_event"]["payload_outputs"] == ["colors"]
    assert nodes["send_messagebus_event"]["payload_inputs"] == ["colors"]
    out = nodes["on_keyboard_input"]["connections"]["out"]
    assert out == [{"output": "outputs:pressed", "to": f"{G}/VariantController/send_messagebus_event.inputs:execIn",
                    "to_node": f"{G}/VariantController/send_messagebus_event", "kind": "execution"}]
    assert all(item["plain"] for item in graph_record["nodes"])
    terms = {item["term"] for item in result["glossary"]}
    assert {"Action Graph", "message bus", "payload", "target input", "constant node"} <= terms


def test_event_name_that_differs_only_in_capitals_is_an_error():
    stage = stage_from(configurator(sender_event="setColor", listener_event="SetColor"))
    result = inspect_graphs(stage)
    item = problem(result, "event_name_mismatch")
    assert item["severity"] == "error" and "'SetColor'" in item["message"] and "'setColor'" in item["message"]
    assert item["node"].endswith("on_messagebus_event")
    assert "no_sender" not in codes(result)


def test_payload_key_that_differs_only_in_capitals_is_an_error():
    stage = stage_from(configurator(sender_key="Colors", listener_key="colors"))
    result = inspect_graphs(stage)
    item = problem(result, "payload_mismatch")
    assert item["severity"] == "error" and "'Colors'" in item["message"] and "'colors'" in item["message"]


def test_wire_to_a_missing_payload_output_suggests_the_right_spelling():
    stage = stage_from(configurator(wire_from_key="Colors"))  # wired to outputs:Colors, node has outputs:colors
    item = problem(inspect_graphs(stage), "dangling_attribute")
    assert item["severity"] == "error" and item["node"].endswith("set_variant_selection")
    assert "outputs:colors" in item["fix"]


def test_dangling_connection_to_a_deleted_node():
    body = node("print", "omni.graph.ui_nodes.PrintText", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Gone/on_tick.outputs:tick>
                custom string inputs:text = "hi"
{EXEC_OUT}""")
    result = inspect_graphs(stage_from(graph("Broken", body)))
    item = problem(result, "dangling_connection")
    assert item["severity"] == "error" and f"{G}/Gone/on_tick" in item["message"]


def test_unknown_and_missing_node_types():
    body = node("typo", "omni.graph.nodes.SetVariantSelectionn") + f"""
            def OmniGraphNode "no_type"
            {{
                custom token inputs:value = "x"
            }}
"""
    result = inspect_graphs(stage_from(graph("Odd", body)))
    unknown = problem(result, "unknown_node_type")
    assert unknown["severity"] == "warning" and "omni.graph.nodes.SetVariantSelection?" in unknown["fix"]
    assert problem(result, "missing_node_type")["severity"] == "error"
    assert result["counts"]["unknown_node_types"] == 2
    exotic = inspect_graphs(stage_from(graph("Exotic", node("x", "my.company.FancyNode"))))
    assert problem(exotic, "unknown_node_type")["severity"] == "info"


def test_missing_required_inputs_and_missing_target_prims():
    body = (
        key_node("key")
        + node("no_prim", "omni.graph.nodes.SetVariantSelection", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Inputs/key.outputs:pressed>
                custom token inputs:variantSetName = "Leather"
                custom token inputs:variantName = "Black"
{EXEC_OUT}""", 3)
        + node("bad_target", "omni.graph.nodes.SetVariantSelection", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Inputs/key.outputs:pressed>
                custom rel inputs:prim = </World/Nowhere>
                custom token inputs:variantSetName = "Leather"
                custom token inputs:variantName = "Black"
{EXEC_OUT}""", 3)
        + node("writer", "omni.graph.nodes.WritePrimAttribute", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Inputs/key.outputs:pressed>
                custom rel inputs:prim = </World/Bag>
{EXEC_OUT}""", 3)
    )
    result = inspect_graphs(stage_from(graph("Inputs", body)))
    missing = [item for item in result["problems"] if item["code"] == "missing_input"]
    assert any(item["node"].endswith("no_prim") and "inputs:prim" in item["message"] for item in missing)
    assert any(item["node"].endswith("writer") and "inputs:name" in item["message"] for item in missing)
    target = problem(result, "missing_target")
    assert target["node"].endswith("bad_target") and "/World/Nowhere" in target["message"]


def test_variant_names_are_checked_against_the_target_prim():
    wrong_variant = inspect_graphs(stage_from(configurator(value="black")))
    item = problem(wrong_variant, "variant_missing")
    assert "'black'" in item["message"] and "'Black'" in item["fix"] and "payload" in item["message"]
    wrong_set = inspect_graphs(stage_from(configurator(variant_set="leather")))
    assert "'Leather'" in problem(wrong_set, "variant_set_missing")["fix"]
    no_sets = inspect_graphs(stage_from(configurator(prim="/World/Plain")))
    assert problem(no_sets, "no_variant_sets")["severity"] == "error"
    literal = node("set", "omni.graph.nodes.SetVariantSelection", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Lit/key.outputs:pressed>
                custom rel inputs:prim = </World/Bag/Looks>
                custom bool inputs:setVariant = false
                custom token inputs:variantName = "Purple"
                custom token inputs:variantSetName = "Leather"
{EXEC_OUT}""", 3)
    typed = inspect_graphs(stage_from(graph("Lit", key_node("key") + literal)))
    assert "typed-in" in problem(typed, "variant_missing")["message"]


def test_listener_without_a_sender_is_explained():
    body = node("listen", "omni.graph.action.OnMessageBusEvent", f"""
                custom token inputs:eventName = "setColor"
                custom bool inputs:onlyPlayback = false
                custom token outputs:colors
{EXEC_OUT}""", 2) + node("set", "omni.graph.nodes.SetVariantSelection", f"""
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Outside/listen.outputs:execOut>
                custom rel inputs:prim = </World/Bag/Looks>
                custom bool inputs:setVariant = true
                custom token inputs:variantName
                prepend token inputs:variantName.connect = <{G}/Outside/listen.outputs:colors>
                custom token inputs:variantSetName = "Leather"
{EXEC_OUT}""", 3)
    result = inspect_graphs(stage_from(graph("Outside", body)))
    assert problem(result, "no_sender")["severity"] == "info"
    assert "Black" in problem(result, "variant_from_outside")["message"]
    assert codes(result, "error") == []


def test_only_playback_no_trigger_bad_key_and_unknown_variable():
    body = (
        node("key", "omni.graph.action.OnKeyboardInput", '                custom token inputs:keyIn = "j"', 4)
        + node("lonely_print", "omni.graph.ui_nodes.PrintText", '                custom string inputs:text = "hi"\n'
               '                custom uint inputs:execIn')
        + node("read", "omni.graph.core.ReadVariable", '                custom token inputs:variableName = "Backdrop"', 2)
    )
    variables = '            custom token graph:variable:backdrop = "Plinths"'
    result = inspect_graphs(stage_from(graph("Misc", body, variables)))
    assert problem(result, "only_playback")["severity"] == "warning"  # onlyPlayback defaults to true
    assert problem(result, "no_trigger")["node"].endswith("lonely_print")
    assert "'J'" in problem(result, "bad_key")["fix"]
    assert "'backdrop'" in problem(result, "unknown_variable")["fix"]
    assert result["graphs"][0]["variables"] == {"backdrop": "Plinths"}


def test_custom_event_and_message_bus_are_different_channels():
    body = key_node("key") + node("send", "omni.graph.action.SendCustomEvent", f"""
                custom token inputs:eventName = "setColor"
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{G}/Mixed/key.outputs:pressed>
{EXEC_OUT}""", 2) + node("listen", "omni.graph.action.OnMessageBusEvent", f"""
                custom token inputs:eventName = "setColor"
                custom bool inputs:onlyPlayback = false
{EXEC_OUT}""", 2)
    result = inspect_graphs(stage_from(graph("Mixed", body)))
    assert problem(result, "event_channel_mismatch")["severity"] == "error"


def test_story_stops_at_loops_and_shows_branches():
    path = f"{G}/Loop"
    body = key_node("key") + node("branch", "omni.graph.action.Branch", f"""
                custom bool inputs:condition = true
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = [<{path}/key.outputs:pressed>, <{path}/delay.outputs:finished>]
                custom uint outputs:execTrue (customData = {{bool isExecution = 1}})
                custom uint outputs:execFalse (customData = {{bool isExecution = 1}})""", 2) + node(
        "delay", "omni.graph.action.Delay", f"""
                custom double inputs:duration = 1
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{path}/branch.outputs:execTrue>
                custom uint outputs:finished (customData = {{bool isExecution = 1}})""", 2) + node(
        "print", "omni.graph.ui_nodes.PrintText", f"""
                custom string inputs:text = "no"
                custom uint inputs:execIn
                prepend uint inputs:execIn.connect = <{path}/branch.outputs:execFalse>
{EXEC_OUT}""")
    flow = inspect_graphs(stage_from(graph("Loop", body)))["graphs"][0]["flow"]
    assert flow == ["When key J is pressed -> Branch checks true -> [execTrue] Delay waits 1 s -> (back to branch); "
                    "[execFalse] Print Text prints 'no'"]


def test_root_path_and_empty_stages():
    stage = stage_from(configurator(), graph("Other", key_node("k")))
    one = inspect_graphs(stage, f"{G}/VariantController/set_variant_selection")  # a node path -> its whole graph
    assert [item["path"] for item in one["graphs"]] == [f"{G}/VariantController"]
    assert inspect_graphs(stage, G)["counts"]["graphs"] == 2
    assert inspect_graphs(stage, "/World/Missing")["ok"] is False
    assert inspect_graphs(stage, "not a path")["ok"] is False
    empty = inspect_graphs(Usd.Stage.CreateInMemory())
    assert empty["ok"] and empty["graphs"] == [] and "No OmniGraph" in empty["summary"]
    try:
        inspect_graphs(stage, max_nodes=0)
    except ValueError:
        pass
    else:
        raise AssertionError("max_nodes=0 must be refused")
    capped = inspect_graphs(stage, max_nodes=2)
    assert capped["truncated"] and capped["counts"]["nodes"] == 2


def test_deadline_stops_cooperatively():
    stage = stage_from(configurator())
    spent = inspect_graphs(stage, deadline=TimeBudget(0))
    assert spent["truncated_by_time"] and spent["time_budget"]["truncated_by_time"]
    json.dumps(spent)
    past = inspect_graphs(stage, deadline=time.monotonic() - 1)
    assert past["truncated_by_time"]
    with TimeBudget(60_000):
        relaxed = inspect_graphs(stage)
    assert not relaxed["truncated_by_time"] and relaxed["time_budget"]["truncated_by_time"] is False
    assert "time_budget" not in inspect_graphs(stage)


def test_inspection_never_edits_the_stage():
    stage = stage_from(configurator(value="black"))
    before = stage.GetRootLayer().ExportToString()
    inspect_graphs(stage)
    plan_recipe("key_sets_variant", stage=stage, key="K", prim="/World/Bag/Looks", variant_set="Leather", variant="Pink")
    assert stage.GetRootLayer().ExportToString() == before


# ------------------------------------------------------------------------------------------------ node table


def test_node_table_is_large_and_well_formed():
    assert len(NODE_TYPES) >= 40
    for type_id, info in NODE_TYPES.items():
        assert "." in type_id and info["plain"].endswith(".") and info["ui"]
        assert info["role"] in ("event", "action", "flow", "data", "constant", "variable")
        assert isinstance(info["version"], int) and info["source"].endswith("nodes.json")
    assert describe_node_type("omni.graph.action.OnMessageBusEvent")["version"] == 2
    miss = describe_node_type("omni.graph.action.OnMessageBusEvnt")
    assert miss["ok"] is False and miss["did_you_mean"][0] == "omni.graph.action.OnMessageBusEvent"
    json.dumps(describe_node_type("omni.graph.action.OnKeyboardInput"))


def test_node_table_matches_the_installed_extensions():
    files = NODE_TYPES_SOURCE["files"]
    if not all(Path(path).is_file() for path in files.values()):
        return  # local-only: needs the Isaac Sim 6.1.0 install
    installed = {}
    for path in files.values():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in data.get("nodes", {}).items():
            installed[key if "." in key else f"{data['extension']}.{key}"] = value.get("version")
    for type_id, info in NODE_TYPES.items():
        assert type_id in installed, type_id
        assert installed[type_id] == info["version"], (type_id, installed[type_id], info["version"])


def test_real_bag_graph_story():
    if _IN_KIT or not BAG_STEP4.is_file():
        return  # local-only, and never opened on Kit's main thread
    stage = Usd.Stage.Open(str(BAG_STEP4), Usd.Stage.LoadNone)
    result = inspect_graphs(stage, G)
    json.dumps(result)
    flow = [line for item in result["graphs"] for line in item["flow"]]
    assert "When key J is pressed -> Send MessageBus Event sends 'setColor' with colors='Pink' (heard by on_messagebus_event)" in flow
    assert any(line.startswith("When message bus event 'setColor' arrives -> Set Variant Selection sets Leather=") for line in flow)
    assert codes(result, "error") == []
    assert not stage.GetRootLayer().dirty


# ------------------------------------------------------------------------------------------------ recipes


def recipe_stage():
    return stage_from(configurator())


RECIPE_ARGS = {
    "key_sets_variant": {"key": "J", "prim": "/World/Bag/Looks", "variant_set": "Leather", "variant": "Black"},
    "event_sets_variant": {"event_name": "setColor", "payload": "colors", "prim": "/World/Bag/Looks",
                           "variant_set": "Leather", "test_key": "K", "test_variant": "Pink"},
    "on_tick_rotate": {"prim": "/World/Bag", "degrees_per_second": 45},
    "print_on_play": {"text": "Play pressed!"},
}


def test_every_recipe_plans_and_its_python_compiles():
    stage = recipe_stage()
    assert {item["name"] for item in list_recipes()["recipes"]} == set(RECIPES)
    for name, args in RECIPE_ARGS.items():
        plan = plan_recipe(name, stage=stage, **args)
        json.dumps(plan)
        assert plan["ok"] and plan["dry_run"] and plan["validated"], (name, plan)
        assert plan["graph"] == {"graph_path": plan["graph_path"], "evaluator_name": "execution"}
        assert all(node["type"] in NODE_TYPES for node in plan["nodes"])
        names = {node["name"] for node in plan["nodes"]}
        for wire in plan["connections"]:
            assert wire["from"].split(".")[0] in names and wire["to"].split(".")[0] in names
        for item in plan["values"]:
            assert item["attribute"].split(".")[0] in names
        compile(plan["python"], f"<{name}>", "exec")
        assert "og.Controller.edit(" in plan["python"] and "keys.CREATE_NODES" in plan["python"]
        assert plan["explanation"] and plan["explanation"][0].startswith("When ")
    event = plan_recipe("event_sets_variant", stage=stage, **RECIPE_ARGS["event_sets_variant"])
    assert {"attribute": "OnEvent.outputs:colors", "type": "token",
            "why": event["attributes"][0]["why"]} == event["attributes"][0]
    assert ("OnEvent.outputs:colors", "SetVariant.inputs:variantName") in [(w["from"], w["to"]) for w in event["connections"]]
    assert any("already listens for 'setColor'" in item for item in event["warnings"])
    assert "usdrt.Sdf.Path('/World/Bag/Looks')" in event["python"]
    rotate = plan_recipe("on_tick_rotate", stage=stage, prim="/World/Bag", degrees_per_second=45)
    typed = {item["attribute"]: (item["value"], item.get("type")) for item in rotate["values"]}
    assert typed["PlusStart.inputs:b"] == (30.0, "double")  # Y-up stage: spins around Y, keeps the start angle
    assert ("PlusStart.outputs:sum", "MakeRotation.inputs:y") in [(w["from"], w["to"]) for w in rotate["connections"]]


def test_recipe_validation_errors():
    stage = recipe_stage()
    assert "Did you mean 'key_sets_variant'" in plan_recipe("key_set_variant")["error"]
    assert "Unknown parameter" in plan_recipe("print_on_play", txt="x")["error"]
    assert "needs" in plan_recipe("key_sets_variant", stage=stage, key="J")["error"]
    bad_key = plan_recipe("key_sets_variant", stage=stage, **dict(RECIPE_ARGS["key_sets_variant"], key="j"))
    assert "Use 'J'" in bad_key["error"]
    bad_variant = plan_recipe("key_sets_variant", stage=stage, **dict(RECIPE_ARGS["key_sets_variant"], variant="black"))
    assert "Did you mean 'Black'" in bad_variant["error"]
    bad_set = plan_recipe("event_sets_variant", stage=stage, event_name="setColor", prim="/World/Bag/Looks",
                          variant_set="leather")
    assert "Did you mean 'Leather'" in bad_set["error"]
    missing_prim = plan_recipe("key_sets_variant", stage=stage, **dict(RECIPE_ARGS["key_sets_variant"], prim="/World/Nope"))
    assert "No prim at /World/Nope" in missing_prim["error"]
    assert "payload must be" in plan_recipe("event_sets_variant", stage=stage, event_name="e", prim="/World/Bag/Looks",
                                            variant_set="Leather", payload="my colors")["error"]
    assert "go together" in plan_recipe("event_sets_variant", stage=stage, event_name="e", prim="/World/Bag/Looks",
                                        variant_set="Leather", test_key="J")["error"]
    assert "rotateXYZ" in plan_recipe("on_tick_rotate", stage=stage, prim="/World/Plain", degrees_per_second=1)["error"]
    assert "number" in plan_recipe("on_tick_rotate", stage=stage, prim="/World/Bag", degrees_per_second="fast")["error"]
    taken = plan_recipe("print_on_play", stage=stage, graph_path=f"{G}/VariantController")
    assert taken["ok"] is False and "never edits an existing graph" in taken["error"]
    inside = plan_recipe("print_on_play", stage=stage, graph_path=f"{G}/VariantController/sub")
    assert inside["ok"] is False and "inside the graph" in inside["error"]
    unvalidated = plan_recipe("key_sets_variant", **RECIPE_ARGS["key_sets_variant"])
    assert unvalidated["ok"] and not unvalidated["validated"] and unvalidated["warnings"]


def test_default_graph_path_gets_a_free_name():
    stage = stage_from(configurator(), graph("PrintOnPlay", key_node("k")))
    plan = plan_recipe("print_on_play", stage=stage)
    assert plan["graph_path"] == f"{G}/PrintOnPlay_01"
    assert any("was taken" in item for item in plan["warnings"])


class _FakeNode:
    def __init__(self, path):
        self._path = path

    def get_prim_path(self):
        return self._path


class _FakeController:
    calls = []

    class Keys:  # same values as omni.graph.tools GraphSetupKeys (node_generator/keys.py)
        CONNECT = "connect"
        CREATE_ATTRIBUTES = "create_attributes"
        CREATE_NODES = "create_nodes"
        SET_VALUES = "set_values"

    @classmethod
    def edit(cls, graph_id, edits):
        cls.calls.append((graph_id, edits))
        nodes = [_FakeNode(f"{graph_id['graph_path']}/{name}") for name, _ in edits["create_nodes"]]
        return ("graph", nodes, [], {})


class _FakeOg:
    Controller = _FakeController


class _FakeSdfPath(str):
    pass


class _FakeUsdrt:
    class Sdf:
        Path = _FakeSdfPath


def _with_kit(fake, func):
    original = ogh._kit_modules
    ogh._kit_modules = lambda: fake
    try:
        return func()
    finally:
        ogh._kit_modules = original


def test_apply_recipe_dry_run_does_not_touch_kit():
    stage = recipe_stage()

    def boom():
        raise AssertionError("dry run must not import Kit")

    original = ogh._kit_modules
    ogh._kit_modules = boom
    try:
        plan = apply_recipe(stage, "key_sets_variant", **RECIPE_ARGS["key_sets_variant"])
    finally:
        ogh._kit_modules = original
    assert plan["ok"] and plan["dry_run"] is True


def test_apply_recipe_runs_controller_edit_with_a_fake_og():
    stage = recipe_stage()
    _FakeController.calls = []
    fake = {"og": _FakeOg, "live_stage": stage, "usdrt": _FakeUsdrt}
    result = _with_kit(fake, lambda: apply_recipe(stage, "event_sets_variant", dry_run=False,
                                                  **RECIPE_ARGS["event_sets_variant"]))
    json.dumps(result)
    assert result["ok"] and result["dry_run"] is False
    assert result["created"] == [f"{G}/EventSetsVariant/{name}" for name in ("OnEvent", "SetVariant", "TestKey", "SendEvent")]
    assert "note" in result  # the fake created no prims, so no follow-up inspection
    (graph_id, edits), = _FakeController.calls
    assert graph_id == {"graph_path": f"{G}/EventSetsVariant", "evaluator_name": "execution"}
    assert set(edits) == {"create_nodes", "create_attributes", "connect", "set_values"}
    assert ("OnEvent", "omni.graph.action.OnMessageBusEvent") in edits["create_nodes"]
    assert ("OnEvent.outputs:colors", "token") in edits["create_attributes"]
    assert ("TestKey.outputs:pressed", "SendEvent.inputs:execIn") in edits["connect"]
    values = {item[0]: item[1:] for item in edits["set_values"]}
    prim_value = values["SetVariant.inputs:prim"][0]
    assert prim_value == ["/World/Bag/Looks"] and isinstance(prim_value[0], _FakeSdfPath)
    assert values["SendEvent.inputs:colors"] == ("Pink",)
    _FakeController.calls = []
    rotate = _with_kit(fake, lambda: apply_recipe(stage, "on_tick_rotate", dry_run=False, prim="/World/Bag",
                                                  degrees_per_second=10))
    assert rotate["ok"]
    typed = {item[0]: item for item in _FakeController.calls[0][1]["set_values"]}
    assert typed["MakeRotation.inputs:x"] == ("MakeRotation.inputs:x", 0.0, "double")
    assert "create_attributes" not in _FakeController.calls[0][1]


def test_apply_recipe_refuses_outside_kit_or_on_another_stage():
    stage = recipe_stage()
    args = RECIPE_ARGS["print_on_play"]
    no_kit = _with_kit({"og": None, "live_stage": None, "usdrt": None},
                       lambda: apply_recipe(stage, "print_on_play", dry_run=False, **args))
    assert no_kit["ok"] is False and no_kit["message"] == "Kit not available" and no_kit["plan"]["ok"]
    _FakeController.calls = []
    other = _with_kit({"og": _FakeOg, "live_stage": Usd.Stage.CreateInMemory(), "usdrt": None},
                      lambda: apply_recipe(stage, "print_on_play", dry_run=False, **args))
    assert other["ok"] is False and "not the stage open in Kit" in other["error"] and _FakeController.calls == []

    class Failing:
        class Controller(_FakeController):
            @classmethod
            def edit(cls, graph_id, edits):
                raise RuntimeError("bad node type")

    failed = _with_kit({"og": Failing, "live_stage": stage, "usdrt": None},
                       lambda: apply_recipe(stage, "print_on_play", dry_run=False, **args))
    assert failed["ok"] is False and "bad node type" in failed["error"] and "Ctrl+Z" in failed["note"]
    invalid = apply_recipe(stage, "print_on_play", dry_run=False, text="")
    assert invalid["ok"] is False and invalid["dry_run"] is True


def test_module_never_imports_omni_graph():
    if _IN_KIT:
        return
    assert "omni.graph.core" not in sys.modules
    assert ogh._kit_modules()["og"] is None  # really not importable outside Kit
