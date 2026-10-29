"""Action Graph (OmniGraph) helper: explain the graphs saved in a USD stage, and plan or apply small beginner recipes.

Two halves:

1. inspect_graphs(stage) is READ-ONLY and needs only pxr. OmniGraph saves every graph in USD: a prim of type
   OmniGraph (evaluator:type, pipelineStage, graph:variable:* attributes) holding OmniGraphNode prims (node:type,
   node:typeVersion, inputs:*, outputs:*). Wires are USD attribute connections from an input to the output (or a
   constant's inputs:value) it reads; prim pickers ("target" inputs such as inputs:prim) are relationships. The result
   describes each graph and node in plain words, tells the story of what happens ("When key J is pressed -> ..."),
   and lists problems: dangling wires, unknown node types, missing required inputs, event names or payload names that
   differ only by capital letters ('Colors' vs 'colors'), variant names that do not exist on the target prim, ...

2. plan_recipe(name, ...) builds a dry-run plan (nodes, wires, values, a plain explanation and a beginner-commented
   og.Controller.edit script); apply_recipe(stage, name, dry_run=False, ...) runs that edit INSIDE Kit only.
   omni.graph.core is imported lazily on each call, never at module import, so this module imports anywhere.

Node type ids, versions, attribute names and the og.Controller.edit API were checked against the INSTALLED sources
(read-only) of Isaac Sim 6.1.0-rc.26 under C:/Users/krngr/Omniverse/isaacsim-6.1.0 (OmniGraph extensions built for
Kit 110.0, omni.graph 1.142.5); the USD Composer build in kit-app-template/_build (Kit 110.1.2) ships the same
omni.graph.nodes 2.12.0 / action_nodes 2.11.0 / nodes_core 2.11.0 / ui_nodes 2.11.1 extensions. See NODE_TYPES_SOURCE.
"""

from __future__ import annotations

import difflib
import importlib
import keyword
import math
import re
import time
from typing import Any

from . import usd_insight
from .usd_insight import TimeBudget, resolve_budget

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "GLOSSARY",
    "NODE_TYPES",
    "NODE_TYPES_SOURCE",
    "RECIPES",
    "apply_recipe",
    "describe_node_type",
    "inspect_graphs",
    "list_recipes",
    "plan_recipe",
]

MAX_NODES = 2000
MAX_STORY_STEPS = 40
MAX_PROBLEMS = 200
_NOT_KIT = "Kit not available"
GRAPH_PRIM_TYPES = {"OmniGraph", "ComputeGraph"}
NODE_PRIM_TYPES = {"OmniGraphNode", "ComputeNode"}

# ------------------------------------------------------------------------------------------------ node type table

_ISAAC = "C:/Users/krngr/Omniverse/isaacsim-6.1.0"
_EXT = {
    "action_nodes": "extscache/omni.graph.action_nodes-2.11.0+110.0.0.wx64.r.cp312.u7f4/ogn/nodes.json",
    "action_nodes_core": "extscache/omni.graph.action_nodes_core-2.11.0+110.0.0.wx64.r.cp312.u7f4/ogn/nodes.json",
    "nodes": "extscache/omni.graph.nodes-2.12.0+110.0.0.wx64.r.cp312.u7f4/ogn/nodes.json",
    "nodes_core": "extscache/omni.graph.nodes_core-2.11.0+110.0.0.wx64.r.cp312.u7f4/ogn/nodes.json",
    "ui_nodes": "extscache/omni.graph.ui_nodes-2.11.1+110.0.0.wx64.r.cp312.u7f4/ogn/nodes.json",
    "scriptnode": "extscache/omni.graph.scriptnode-2.10.2+110.0.0/ogn/nodes.json",
    "isaac_core_nodes": "exts/isaacsim.core.nodes/ogn/nodes.json",
    "isaac_wheeled": "exts/isaacsim.robot.wheeled_robots.nodes/ogn/nodes.json",
    "isaac_conveyor": "exts/isaacsim.asset.gen.conveyor/ogn/nodes.json",
}
_EXT_NAMES = {
    "action_nodes": "omni.graph.action_nodes",
    "action_nodes_core": "omni.graph.action_nodes_core",
    "nodes": "omni.graph.nodes",
    "nodes_core": "omni.graph.nodes_core",
    "ui_nodes": "omni.graph.ui_nodes",
    "scriptnode": "omni.graph.scriptnode",
    "isaac_core_nodes": "isaacsim.core.nodes",
    "isaac_wheeled": "isaacsim.robot.wheeled_robots.nodes",
    "isaac_conveyor": "isaacsim.asset.gen.conveyor",
}
NODE_TYPES_SOURCE = {
    "kit": "110.0 (extension builds tagged +110.0.0; omni.graph 1.142.5); Composer build kit-app-template/_build is 110.1.2",
    "isaac_sim": "6.1.0-rc.26 (isaacsim-6.1.0/VERSION)",
    "root": _ISAAC,
    "files": {_EXT_NAMES[key]: f"{_ISAAC}/{path}" for key, path in _EXT.items()},
    "controller_api": f"{_ISAAC}/extscache/omni.graph-1.142.5+00c488ae.wx64.r.cp312/omni/graph/core/_impl/controller.py",
    "controller_keys": f"{_ISAAC}/extscache/omni.graph.tools-1.81.0+00c488ae/omni/graph/tools/_impl/node_generator/keys.py",
    "note": "Every id and version below appears in those nodes.json files (ids without a dot there are prefixed with "
    "their extension name, as OmniGraph does) and in the extensions' *Template.usda test files.",
}

# OnKeyboardInput inputs:keyIn allowedTokens (OgnOnKeyboardInput.ogn, omni.graph.action_nodes 2.11.0).
KEYBOARD_KEYS = (
    [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    + ["Apostrophe", "Backslash", "Backspace", "CapsLock", "Comma", "Del", "Down", "End", "Enter", "Equal", "Escape"]
    + [f"F{index}" for index in range(1, 13)]
    + ["GraveAccent", "Home", "Insert"]
    + [f"Key{index}" for index in range(10)]
    + ["Left", "LeftAlt", "LeftBracket", "LeftControl", "LeftShift", "LeftSuper", "Menu", "Minus", "NumLock"]
    + [f"Numpad{index}" for index in range(10)]
    + ["NumpadAdd", "NumpadDel", "NumpadDivide", "NumpadEnter", "NumpadEqual", "NumpadMultiply", "NumpadSubtract"]
    + ["PageDown", "PageUp", "Pause", "Period", "PrintScreen", "Right", "RightAlt", "RightBracket", "RightControl"]
    + ["RightShift", "RightSuper", "ScrollLock", "Semicolon", "Slash", "Space", "Tab", "Up"]
)
# OnStageEvent inputs:eventName allowedTokens (OgnOnStageEvent.ogn).
STAGE_EVENTS = (
    "Saved", "Selection Changed", "Hierarchy Changed", "OmniGraph Start Play", "OmniGraph Stop Play",
    "Simulation Start Play", "Simulation Stop Play", "Animation Start Play", "Animation Stop Play",
)

NODE_TYPES: dict[str, dict[str, Any]] = {}


def _t(type_id: str, ui: str, ext: str, version: int, role: str, plain: str, **extra: Any) -> None:
    entry = {"ui": ui, "extension": _EXT_NAMES[ext], "version": version, "role": role, "plain": plain}
    entry.update(extra)
    NODE_TYPES[type_id] = entry


_EXEC_IN = ("execIn",)
# ---- events: they start a chain of execution ("When ... ->")
_t("omni.graph.action.OnKeyboardInput", "On Keyboard Input", "action_nodes", 4, "event",
   "Starts the graph when a chosen keyboard key is pressed or released.",
   exec_outputs=("pressed", "released"), only_playback_default=True)
_t("omni.graph.action.OnTick", "On Tick", "action_nodes_core", 2, "event",
   "Starts the graph on every app update (every frame), optionally only while Play is running.",
   exec_outputs=("tick",), only_playback_default=True,
   outputs=("absoluteSimTime", "deltaSeconds", "frame", "isPlaying", "time", "timeSinceStart"))
_t("omni.graph.action.OnPlaybackTick", "On Playback Tick", "action_nodes_core", 2, "event",
   "Starts the graph on every frame while the timeline is playing.",
   exec_outputs=("tick",), outputs=("deltaSeconds", "frame", "time"))
_t("omni.graph.action.OnLoaded", "On Loaded", "action_nodes_core", 2, "event",
   "Starts the graph once, on its first update after the graph is created or the stage is loaded.")
_t("omni.graph.action.OnClosing", "On Closing", "action_nodes_core", 2, "event",
   "Starts the graph when the USD stage is about to close.")
_t("omni.graph.action.OnCustomEvent", "On Custom Event", "action_nodes", 3, "event",
   "Starts the graph when a Send Custom Event node (or code) sends a custom event with this exact name.",
   only_playback_default=True, listens="custom", outputs=("path", "bundle"))
_t("omni.graph.action.OnMessageBusEvent", "On MessageBus Event", "action_nodes_core", 2, "event",
   "Starts the graph when an event with this exact name arrives on the app message bus (from a graph, Python or a "
   "browser client); payload keys are copied into matching extra outputs.",
   only_playback_default=True, listens="message_bus", dynamic="outputs")
_t("omni.graph.action.OnStageEvent", "On Stage Event", "action_nodes", 4, "event",
   "Starts the graph when a stage event such as 'Animation Start Play' or 'Selection Changed' happens.",
   only_playback_default=True, allowed={"eventName": STAGE_EVENTS})
_t("omni.graph.action.OnImpulseEvent", "On Impulse Event", "action_nodes_core", 3, "event",
   "Starts the graph once each time its 'Enable Impulse' state is switched on (for example by a button in the UI).")
_t("omni.graph.action.OnVariableChange", "On Variable Change", "action_nodes", 2, "event",
   "Starts the graph when a graph variable's value changes.")
_t("omni.graph.action.OnObjectChange", "On USD Object Change", "action_nodes", 5, "event",
   "Starts the graph when a watched property on a prim changes.", exec_outputs=("changed",))
_t("omni.graph.action.OnMouseInput", "On Mouse Input", "action_nodes", 2, "event",
   "Starts the graph when a mouse button or the mouse wheel is used.", exec_outputs=("pressed", "released", "valueChanged"))
_t("omni.graph.action.OnGamepadInput", "On Gamepad Input", "action_nodes", 2, "event",
   "Starts the graph when a gamepad button is pressed or released.", exec_outputs=("pressed", "released"))
_t("omni.graph.ui_nodes.OnViewportClicked", "On Viewport Clicked (BETA)", "ui_nodes", 1, "event",
   "Starts the graph when the viewport is clicked.", exec_outputs=("clicked",))
_t("omni.graph.ui_nodes.OnPicked", "On Picked (BETA)", "ui_nodes", 3, "event",
   "Starts the graph when a prim is picked (clicked) in the viewport.", exec_outputs=("picked", "missed"))
_t("omni.graph.ui_nodes.OnWidgetClicked", "On Widget Clicked (BETA)", "ui_nodes", 1, "event",
   "Starts the graph when a UI button made by the graph is clicked.", exec_outputs=("clicked",))
_t("isaacsim.core.nodes.OnPhysicsStep", "On Physics Step", "isaac_core_nodes", 1, "event",
   "Isaac Sim: starts the graph on every physics simulation step (only while the simulation runs).",
   exec_outputs=("step",))
# ---- actions: they do something when their execution input fires
_t("omni.graph.action.SendCustomEvent", "Send Custom Event", "action_nodes", 2, "action",
   "Sends a named custom event that On Custom Event nodes with the same name react to.", sends="custom")
_t("omni.graph.action.SendMessageBusEvent", "Send MessageBus Event", "action_nodes_core", 1, "action",
   "Pushes a named event to the app message bus; its extra inputs become the event's payload (key = input name).",
   sends="message_bus", dynamic="inputs")
_t("omni.graph.nodes.SetVariantSelection", "Set Variant Selection", "nodes", 3, "action",
   "Switches a prim's variant set (such as Leather) to the named variant (such as Black).", targets=("prim",))
_t("omni.graph.nodes.ClearVariantSelection", "Clear Variant Selection", "nodes", 3, "action",
   "Removes the variant selection a layer holds for a prim's variant set.", targets=("prim",))
# Inputs from isaacsim.asset.gen.conveyor/ogn/docs/OgnIsaacConveyor.rst (Isaac Sim 6.1): conveyorPrim, velocity,
# direction, onStep, enabled, curved, delta. Needs the isaacsim.asset.gen.conveyor extension enabled.
_t("isaacsim.asset.gen.conveyor.IsaacConveyor", "Conveyor Belt", "isaac_conveyor", 1, "action",
   "Moves whatever rests on a kinematic rigid-body belt (conveyorPrim) at a velocity (m/s) along a direction, each "
   "time onStep fires.", targets=("conveyorPrim",), exec_inputs=("onStep",), exec_outputs=())
_t("omni.graph.nodes.WritePrimAttribute", "Write Prim Attribute", "nodes", 3, "action",
   "Writes a value into a named attribute of a prim (for example its rotation).", targets=("prim",))
_t("omni.graph.nodes.WritePrimLocalTransform", "Write Prim Local Transform", "nodes", 1, "action",
   "Writes a whole local transform matrix to a prim.", targets=("prim",))
_t("omni.graph.action.SetPrimActive", "Set Prim Active", "action_nodes", 2, "action",
   "Switches a prim on (active) or off (inactive).", targets=("primTarget",))
_t("omni.graph.ui_nodes.PrintText", "Print Text", "ui_nodes", 1, "action",
   "Prints a text to the log, or on screen in the viewport when 'To Screen' is on.")
_t("omni.graph.action.LogMessage", "Log Message", "action_nodes_core", 1, "action",
   "Writes a text message to the Kit log.")
_t("omni.graph.nodes.WriteSetting", "Write Setting", "nodes", 1, "action",
   "Changes a Kit application setting (such as grid visibility) by its setting path.")
_t("omni.graph.ui_nodes.SetActiveViewportCamera", "Set Active Camera", "ui_nodes", 1, "action",
   "Makes the viewport look through the camera at the given path.")
_t("omni.graph.ui_nodes.SetViewportMode", "Set Viewport Mode (BETA)", "ui_nodes", 1, "action",
   "Switches a viewport between Default mode and Scripted mode (where the graph handles mouse input).",
   exec_outputs=("defaultMode", "scriptedMode"))
_t("omni.graph.ui_nodes.SetViewportFullscreen", "Set Viewport Fullscreen", "ui_nodes", 1, "action",
   "Turns fullscreen viewport on or off and hides or shows the other panels.", exec_inputs=("exec",),
   exec_outputs=("exec",))
_t("omni.graph.ui_nodes.LockViewportRender", "Lock Viewport Render", "ui_nodes", 1, "action",
   "Freezes the viewport image while the scene changes, then fades to the new image when unlocked.",
   exec_inputs=("lock", "unlock"), exec_outputs=("locked", "fadeStarted", "fadeComplete"))
_t("omni.graph.nodes.TranslateToLocation", "Translate To Location", "nodes", 2, "action",
   "Moves a prim smoothly to a target position at a given speed.", targets=("prim",), exec_outputs=("finished",))
_t("omni.graph.nodes.RotateToOrientation", "Rotate To Orientation", "nodes", 2, "action",
   "Turns a prim smoothly to a target rotation at a given speed.", targets=("prim",), exec_outputs=("finished",))
_t("omni.graph.scriptnode.ScriptNode", "Script Node", "scriptnode", 2, "action",
   "Runs a piece of Python code inside the graph; its extra inputs and outputs are chosen by the author.",
   dynamic="both")
_t("isaacsim.core.nodes.IsaacArticulationController", "Articulation Controller", "isaac_core_nodes", 1, "action",
   "Isaac Sim: sends joint position, velocity or effort commands to a robot (articulation).",
   targets=("targetPrim",))
_t("isaacsim.robot.wheeled_robots.DifferentialController", "Differential Controller", "isaac_wheeled", 1, "action",
   "Isaac Sim: turns a wanted forward speed and turning speed into left and right wheel speeds.")
# ---- flow control: they decide where execution goes next
_t("omni.graph.action.Branch", "Branch", "action_nodes_core", 2, "flow",
   "Sends execution to 'True' or 'False' depending on its condition input (an if/else).",
   exec_outputs=("execTrue", "execFalse"))
_t("omni.graph.action.Sequence", "Sequence", "action_nodes_core", 2, "flow",
   "Runs its 'A' output and then its 'B' output, one after the other.", exec_outputs=("a", "b"))
_t("omni.graph.action.Once", "Once", "action_nodes_core", 2, "flow",
   "Lets execution through only the first time ('Once'); later times go to 'After' until it is reset.",
   exec_outputs=("once", "after"))
_t("omni.graph.action.Delay", "Delay", "action_nodes", 2, "flow",
   "Waits a number of seconds before letting execution continue.", exec_outputs=("finished",))
_t("omni.graph.action.Gate", "Gate", "action_nodes_core", 2, "flow",
   "Lets execution through only while the gate is open; other inputs open, close or toggle it.",
   exec_inputs=("enter", "toggle"), exec_outputs=("exit",))
_t("omni.graph.action.Counter", "Counter", "action_nodes_core", 2, "flow",
   "Counts how many times it has run since it was last reset.")
_t("omni.graph.action.FlipFlop", "Flip Flop", "action_nodes_core", 2, "flow",
   "Alternates: odd runs go to 'A', even runs go to 'B'.", exec_outputs=("a", "b"))
_t("omni.graph.action.SwitchToken", "Switch On Token", "action_nodes_core", 2, "flow",
   "Picks one of several outputs by comparing a token (text) with each branch's name.")
# ---- variables
_t("omni.graph.core.ReadVariable", "Read Variable", "nodes", 2, "variable",
   "Reads the current value of one of the graph's variables.")
_t("omni.graph.core.WriteVariable", "Write Variable", "nodes", 2, "action",
   "Stores a new value in one of the graph's variables.")
# ---- data: they compute or hold values; they run when something downstream needs them
_t("omni.graph.nodes.GetVariantSelection", "Get Variant Selection", "nodes", 2, "data",
   "Reads which variant a prim's variant set currently has selected.", targets=("prim",))
_t("omni.graph.nodes.GetVariantNames", "Get Variant Names", "nodes", 2, "data",
   "Lists the variant names inside a prim's variant set.", targets=("prim",))
_t("omni.graph.nodes.ReadPrimAttribute", "Read Prim Attribute", "nodes", 3, "data",
   "Reads the value of a named attribute of a prim.", targets=("prim",), outputs=("value",))
_t("omni.graph.nodes.ReadSetting", "Read Setting", "nodes", 1, "data",
   "Reads a Kit application setting by its setting path.", outputs=("value",))
_t("omni.graph.nodes.ReadTime", "Read Time", "nodes", 1, "data",
   "Gives the current time values of the app and the timeline.")
_t("omni.graph.nodes.ReadStageSelection", "Read Stage Selection", "nodes", 1, "data",
   "Gives the paths of the prims currently selected in the stage.", outputs=("selectedPrims",))
_t("omni.graph.nodes.ConstantToken", "Constant Token", "nodes_core", 1, "constant",
   "Holds one fixed token (short text) that other nodes read from its value.")
_t("omni.graph.nodes.ConstantString", "Constant String", "nodes_core", 1, "constant",
   "Holds one fixed text string that other nodes read from its value.")
_t("omni.graph.nodes.ConstantDouble", "Constant Double", "nodes_core", 1, "constant",
   "Holds one fixed decimal number (double precision).")
_t("omni.graph.nodes.ConstantFloat", "Constant Float", "nodes_core", 1, "constant",
   "Holds one fixed decimal number (single precision).")
_t("omni.graph.nodes.ConstantInt", "Constant Int", "nodes_core", 1, "constant",
   "Holds one fixed whole number.")
_t("omni.graph.nodes.ConstantBool", "Constant Bool", "nodes_core", 1, "constant",
   "Holds one fixed true/false value.")
_t("omni.graph.nodes.ConstantPath", "Constant Path", "nodes", 1, "constant",
   "Holds one fixed path text, such as a prim path.")
_t("isaacsim.core.nodes.IsaacReadWorldPose", "Isaac Read World Pose", "isaac_core_nodes", 2, "data",
   "Reads a prim's position, rotation and scale in WORLD space (not relative to its parent).", targets=("prim",))
_t("omni.graph.nodes.Distance3D", "Distance3D", "nodes_core", 1, "data",
   "Computes the straight-line distance between two 3D points.")
_t("omni.graph.nodes.Compare", "Compare", "nodes_core", 1, "data",
   "Compares two values (equal, less than, ...) and gives true or false.", outputs=("result",))
_t("omni.graph.nodes.ToString", "To String", "nodes_core", 1, "data",
   "Turns any value into text.", outputs=("converted",))
_t("omni.graph.nodes.ToToken", "To Token", "nodes_core", 1, "data",
   "Turns any value into a token (short text).", outputs=("converted",))
_t("omni.graph.nodes.BooleanNot", "Boolean Not", "nodes_core", 1, "data",
   "Flips true to false and false to true.", outputs=("valueOut",))
_t("omni.graph.nodes.SelectIf", "Select If", "nodes_core", 1, "data",
   "Gives its 'If True' value or its 'If False' value depending on a condition.", outputs=("result",))
_t("omni.graph.nodes.Add", "Add", "nodes_core", 2, "data", "Adds two or more numbers or vectors.", outputs=("sum",))
_t("omni.graph.nodes.Multiply", "Multiply", "nodes_core", 2, "data",
   "Multiplies two or more numbers or vectors.", outputs=("product",))
_t("omni.graph.nodes.MakeVector3", "Make 3-Vector", "nodes_core", 1, "data",
   "Combines three numbers x, y and z into one 3-vector.", outputs=("tuple",))
_t("isaacsim.core.nodes.IsaacArticulationState", "Articulation State", "isaac_core_nodes", 1, "data",
   "Isaac Sim: reads a robot's joint positions, velocities and efforts.", targets=("targetPrim",))
_t("isaacsim.core.nodes.IsaacReadSimulationTime", "Isaac Read Simulation Time", "isaac_core_nodes", 1, "data",
   "Isaac Sim: gives the current simulation time in seconds.")

for _info in NODE_TYPES.values():
    _info.setdefault("exec_inputs", _EXEC_IN if _info["role"] in ("action", "flow") else ())
    _info.setdefault("exec_outputs", ("execOut",) if _info["role"] in ("event", "action") else ())
    _info.setdefault("source", NODE_TYPES_SOURCE["files"][_info["extension"]])
del _info

# Graph-level words (OmniGraph prim attributes evaluator:type and pipelineStage).
EVALUATORS = {
    "execution": "an Action Graph: nothing runs until an event node (key press, tick, message) fires, then execution "
    "flows along the white execution wires from node to node",
    "push": "a Push Graph: every node is evaluated on every update",
    "dirty_push": "a lazy Push Graph: nodes are evaluated only when one of their inputs changed",
}
PIPELINE_STAGES = {
    "pipelineStageSimulation": "simulation (the normal stage, evaluated every app update)",
    "pipelineStagePreRender": "pre-render (evaluated just before rendering)",
    "pipelineStagePostRender": "post-render (evaluated after rendering)",
    "pipelineStageOnDemand": "on demand (evaluated only when code asks for it)",
}

GLOSSARY = {
    "OmniGraph": "OmniGraph is Omniverse's visual programming system; each graph is saved in USD as an OmniGraph prim with node prims inside.",
    "Action Graph": "An Action Graph (evaluator 'execution') does nothing until an event node fires; then execution flows from node to node.",
    "node": "A node is one box in the graph; its node:type says what it does (for example omni.graph.nodes.SetVariantSelection).",
    "event node": "An event node (On Keyboard Input, On Tick, On MessageBus Event, ...) starts a chain of execution.",
    "execution wire": "An execution wire says what runs next: when one node finishes, the node its exec output is wired to runs.",
    "data wire": "A data wire hands a value (a token, number, path ...) from one node's output to another node's input.",
    "input": "An input (inputs:name) is a value a node uses; it is typed in directly or comes through a data wire.",
    "target input": "A target input (such as inputs:prim) picks prims in the stage; USD saves it as a relationship.",
    "constant node": "A constant node just holds one value (such as 'Pink') for other nodes to read.",
    "message bus": "The app message bus carries named events between graphs, Python code and browser clients; listeners must use the exact same name.",
    "payload": "A payload is the data an event carries, such as colors='Black'; On MessageBus Event copies each key into an output with the same name.",
    "custom event": "A custom event is sent by Send Custom Event and heard by On Custom Event nodes with the exact same name.",
    "dynamic attribute": "A dynamic attribute is an extra input or output a user adds to a node (such as outputs:colors on On MessageBus Event).",
    "graph variable": "A graph variable is a named value stored on the graph itself; Read Variable and Write Variable use it.",
    "Only Simulate On Play": "'Only Simulate On Play' (inputs:onlyPlayback) makes an event node fire only while the timeline is playing.",
    "variant set": "A variant set is a switch with named choices (Leather = Black, Pink, White); variant names are case-sensitive.",
    "evaluator": "The evaluator decides how a graph runs: 'execution' for Action Graphs, 'push' for graphs that run every update.",
    "pipeline stage": "The pipeline stage says when in each app update the graph is evaluated (usually 'simulation').",
}


def describe_node_type(type_id: str) -> dict[str, Any]:
    """The table entry for a node type id (plain sentence, version, extension, source), or ok False when unknown."""
    info = NODE_TYPES.get(str(type_id))
    if info is None:
        guess = difflib.get_close_matches(str(type_id), list(NODE_TYPES), n=3, cutoff=0.6)
        return {"ok": False, "type": str(type_id), "known": False, "did_you_mean": guess}
    return {"ok": True, "type": str(type_id), "known": True, **_public_info(info)}


def _public_info(info: dict[str, Any]) -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in info.items()
            if key not in ("allowed",)}


# ------------------------------------------------------------------------------------------------ small helpers


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise ImportError("USD Python bindings (pxr) are required for the OmniGraph helper.") from _PXR_IMPORT_ERROR


def _json(value: Any) -> Any:
    return usd_insight.to_jsonable(value)


def _attr_value(attr: Any) -> Any:
    try:
        return attr.Get()
    except Exception:  # noqa: BLE001 - an unreadable value is reported as missing
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _quote(value: Any) -> str:
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _short(path: str) -> str:
    """'/World/Graphs/VariantController/set_variant' -> 'VariantController/set_variant'."""
    parts = [part for part in str(path).split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(path)


def _close(name: str, candidates: Any) -> list[str]:
    """Candidates equal to name ignoring case first, then close spellings."""
    pool = [str(item) for item in candidates]
    same = [item for item in pool if item.lower() == str(name).lower() and item != name]
    return same or difflib.get_close_matches(str(name), pool, n=3, cutoff=0.75)


# ------------------------------------------------------------------------------------------------ reading graphs


def _is_graph(prim: Any) -> bool:
    return prim.GetTypeName() in GRAPH_PRIM_TYPES or (
        prim.GetTypeName() not in NODE_PRIM_TYPES and prim.HasAttribute("evaluator:type")
    )


def _is_node(prim: Any) -> bool:
    return prim.GetTypeName() in NODE_PRIM_TYPES or prim.HasAttribute("node:type")


class _Node:
    """One OmniGraphNode prim, read once."""

    def __init__(self, prim: Any, graph_path: str | None) -> None:
        self.prim = prim
        self.path = str(prim.GetPath())
        self.name = prim.GetName()
        self.graph_path = graph_path
        type_attr = prim.GetAttribute("node:type")
        self.type = _text(_attr_value(type_attr)) if type_attr else None
        version = prim.GetAttribute("node:typeVersion")
        self.type_version = _attr_value(version) if version else None
        self.info = NODE_TYPES.get(self.type or "")
        self.values: dict[str, Any] = {}  # local name ('keyIn') -> raw authored value
        self.attr_types: dict[str, str] = {}
        self.authored: set[str] = set()  # full names: 'inputs:keyIn', 'outputs:colors'
        self.exec_flags: set[str] = set()  # full names with customData isExecution
        self.targets: dict[str, list[str]] = {}
        self.in_wires: list[dict[str, Any]] = []  # {input, from, from_node, from_attr, kind}
        self.out_wires: list[dict[str, Any]] = []
        for prop in prim.GetAuthoredProperties():
            name = prop.GetName()
            if not (name.startswith("inputs:") or name.startswith("outputs:") or name.startswith("state:")):
                continue
            self.authored.add(name)
            if isinstance(prop, Usd.Relationship):
                prim_targets, wire_sources = [], []
                for target in prop.GetTargets():
                    (wire_sources if target.IsPropertyPath() else prim_targets).append(target)
                if name.startswith("inputs:"):
                    self.targets[name[7:]] = [str(target) for target in prim_targets]
                for source in wire_sources:
                    self._add_wire(name, source)
                continue
            self.attr_types[name] = str(prop.GetTypeName())
            try:
                if dict(prop.GetCustomData()).get("isExecution"):
                    self.exec_flags.add(name)
            except Exception:  # noqa: BLE001
                pass
            if name.startswith("inputs:"):
                value = _attr_value(prop)
                if value is not None:
                    self.values[name[7:]] = value
                for source in prop.GetConnections():
                    self._add_wire(name, source)

    def _add_wire(self, input_name: str, source: Any) -> None:
        self.in_wires.append({
            "input": input_name,
            "from": str(source),
            "from_node": str(source.GetPrimPath()),
            "from_attr": source.name,
            "kind": "data",
        })

    # -- convenience
    @property
    def ui(self) -> str:
        if self.info:
            return self.info["ui"]
        return (self.type or "Unknown node").rsplit(".", 1)[-1]

    @property
    def role(self) -> str:
        if self.info:
            return self.info["role"]
        tail = (self.type or "").rsplit(".", 1)[-1]
        return "event" if re.match(r"On[A-Z]", tail) else "unknown"

    def value(self, local: str) -> Any:
        return self.values.get(local)

    def wires_into(self, local: str) -> list[dict[str, Any]]:
        return [wire for wire in self.in_wires if wire["input"] == f"inputs:{local}"]

    def exec_input_names(self) -> set[str]:
        names = {f"inputs:{name}" for name in (self.info or {}).get("exec_inputs", ())}
        for name, type_name in self.attr_types.items():
            if name.startswith("inputs:") and type_name == "uint" and name[7:].lower().startswith("exec"):
                names.add(name)
        return names

    def exec_output_names(self) -> list[str]:
        names = [f"outputs:{name}" for name in (self.info or {}).get("exec_outputs", ())]
        for name in sorted(self.exec_flags):
            if name.startswith("outputs:") and name not in names:
                names.append(name)
        return names

    def dynamic_names(self, port: str) -> list[str]:
        """Authored extra inputs/outputs of message-bus style nodes (payload keys), without the prefix."""
        fixed = {"execIn", "eventName", "onlyPlayback", "execOut"}
        return [name.split(":", 1)[1] for name in sorted(self.authored)
                if name.startswith(port + ":") and name.split(":", 1)[1] not in fixed]


class _Inspection:
    def __init__(self, stage: Any, root: Any, budget: Any, max_nodes: int) -> None:
        self.stage = stage
        self.root = root
        self.budget = budget
        self.max_nodes = max_nodes
        self.graph_prims: list[Any] = []
        self.graph_nodes: dict[str, list[_Node]] = {}
        self.nodes: dict[str, _Node] = {}
        self.orphans: list[_Node] = []
        self.terms: dict[str, None] = {}
        self.time_stopped = False
        self.truncated = False
        self.visited = 0
        self.problems: dict[str, list[dict[str, Any]]] = {}

    def _out_of_time(self) -> bool:
        if self.budget is not None and self.budget.tick():
            self.time_stopped = True
            return True
        return False

    # -- walk
    def walk(self) -> None:
        iterator = iter(Usd.PrimRange(self.root, Usd.PrimDefaultPredicate))
        if self.root.IsPseudoRoot():
            next(iterator, None)
        graph_stack: list[str] = []
        for prim in iterator:
            if self._out_of_time():
                return
            self.visited += 1
            path = str(prim.GetPath())
            while graph_stack and not path.startswith(graph_stack[-1] + "/"):
                graph_stack.pop()
            if _is_graph(prim):
                self.graph_prims.append(prim)
                self.graph_nodes[path] = []
                graph_stack.append(path)
                continue
            if _is_node(prim):
                if len(self.nodes) >= self.max_nodes:
                    self.truncated = True
                    return
                node = _Node(prim, graph_stack[-1] if graph_stack else None)
                self.nodes[node.path] = node
                if node.graph_path:
                    self.graph_nodes[node.graph_path].append(node)
                else:
                    self.orphans.append(node)

    def link(self) -> None:
        for node in self.nodes.values():
            for wire in node.in_wires:
                source = self.nodes.get(wire["from_node"])
                if source is not None and (
                    wire["from"] in {f"{source.path}.{name}" for name in source.exec_output_names()}
                    or f"{wire['from_attr']}" in source.exec_flags
                ):
                    wire["kind"] = "execution"
                elif wire["input"] in node.exec_input_names():
                    wire["kind"] = "execution"
                if source is not None:
                    source.out_wires.append({
                        "output": wire["from_attr"], "to": f"{node.path}.{wire['input']}", "to_node": node.path,
                        "kind": wire["kind"],
                    })

    # -- problems
    def problem(self, node: _Node | None, severity: str, code: str, message: str, fix: str | None = None,
                graph: str | None = None) -> None:
        key = graph or (node.graph_path if node else None) or "(stage)"
        bucket = self.problems.setdefault(key, [])
        if len(bucket) >= MAX_PROBLEMS:
            return
        item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
        if node is not None:
            item["node"] = node.path
        if fix:
            item["fix"] = fix
        bucket.append(item)

    def check(self) -> None:
        for node in self.nodes.values():
            if self._out_of_time():
                return
            self._check_type(node)
            self._check_wires(node)
            self._check_required(node)
            self._check_trigger(node)
            self._check_playback(node)
        self._check_events()
        for node in self.nodes.values():
            if node.type == "omni.graph.nodes.SetVariantSelection":
                self._check_variant(node)
            elif node.type in ("omni.graph.core.ReadVariable", "omni.graph.core.WriteVariable"):
                self._check_variable(node)
        for node in self.orphans:
            self.problem(node, "warning", "node_outside_graph",
                         f"{node.name} is a node prim that is not inside any OmniGraph prim, so it never runs.",
                         "Move it inside a graph (or delete it).")

    def _check_type(self, node: _Node) -> None:
        if not node.type:
            self.problem(node, "error", "missing_node_type",
                         f"{node.name} has no node:type, so OmniGraph cannot tell what it should do.",
                         "Delete the node and create it again from the node library.")
        elif node.info is None:
            guess = difflib.get_close_matches(node.type, list(NODE_TYPES), n=1, cutoff=0.8)
            fix = f"Did you mean {guess[0]}?" if guess else (
                "This helper's table does not know it; it may be fine if its extension is enabled in Kit "
                "(check Window > Extensions).")
            self.problem(node, "warning" if guess else "info", "unknown_node_type",
                         f"{node.name} uses node type {node.type}, which is not in this helper's table of known types.",
                         fix)
        elif node.type == "omni.graph.action.OnKeyboardInput":
            key = _text(node.value("keyIn"))
            if key and key not in KEYBOARD_KEYS:
                close = _close(key, KEYBOARD_KEYS)
                self.problem(node, "error", "bad_key",
                             f"{node.name} listens for key {key!r}, which is not one of the key names it accepts.",
                             f"Use {close[0]!r}." if close else "Pick the key from the node's Key In list.")
        elif node.type == "omni.graph.action.OnStageEvent":
            event = _text(node.value("eventName"))
            if event and event not in STAGE_EVENTS:
                close = _close(event, STAGE_EVENTS)
                self.problem(node, "error", "bad_stage_event",
                             f"{node.name} waits for stage event {event!r}, which On Stage Event does not know.",
                             f"Use {close[0]!r}." if close else f"Use one of: {', '.join(STAGE_EVENTS)}.")

    def _check_wires(self, node: _Node) -> None:
        for wire in node.in_wires:
            source_prim = self.stage.GetPrimAtPath(wire["from_node"])
            local = wire["input"].split(":", 1)[1]
            if not source_prim or not source_prim.IsValid():
                self.problem(node, "error", "dangling_connection",
                             f"{node.name}.{local} is wired to {wire['from']}, but there is no prim at "
                             f"{wire['from_node']} (deleted or renamed).",
                             "Delete this wire, or wire it to an existing node.")
                continue
            source = self.nodes.get(wire["from_node"])
            if source is None:
                if not _is_node(source_prim):
                    continue  # wired to a plain USD attribute: legal (for example a prim attribute)
                source = _Node(source_prim, None)
            attr_name = wire["from_attr"]
            if attr_name in source.authored or source_prim.HasProperty(attr_name):
                continue
            port, _, short = attr_name.partition(":")
            known = (source.info or {})
            known_names = set(known.get("exec_outputs", ())) | set(known.get("outputs", ()))
            if port == "inputs" and known.get("role") == "constant" and short == "value":
                continue
            dynamic = known.get("dynamic") in ("outputs", "both") and port == "outputs"
            if not dynamic and short in known_names:
                continue
            if source.info is None and not dynamic:
                continue  # unknown node type: cannot tell which outputs it has
            close = _close(short, [name.split(":", 1)[1] for name in source.authored if name.startswith(port + ":")])
            if dynamic:
                message = (f"{node.name}.{local} reads {source.name}.{attr_name}, but {source.name} has no "
                           f"{attr_name} attribute. Event payload outputs must be added to the event node by hand "
                           "with exactly the payload key's name.")
            else:
                message = f"{node.name}.{local} is wired to {source.name}.{attr_name}, which does not exist."
            fix = f"Did you mean {port}:{close[0]}? Names are case-sensitive." if close else (
                f"Add {attr_name} to {source.name} (+ button in the node's outputs), or rewire.")
            self.problem(node, "error", "dangling_attribute", message, fix)

    def _satisfied(self, node: _Node, local: str) -> bool:
        if node.wires_into(local) or node.targets.get(local):
            return True
        value = node.value(local)
        return value is not None and str(value) != ""

    def _check_required(self, node: _Node) -> None:
        kind = node.type or ""
        needs: list[tuple[str, str]] = []
        if kind in ("omni.graph.nodes.SetVariantSelection", "omni.graph.nodes.ClearVariantSelection",
                    "omni.graph.nodes.GetVariantSelection", "omni.graph.nodes.GetVariantNames"):
            needs = [("prim", "which prim's variants to change (the Prim input)"),
                     ("variantSetName", "the variant set name (such as Leather)")]
            if kind == "omni.graph.nodes.SetVariantSelection":
                needs.append(("variantName", "the variant to select (such as Black)"))
        elif kind in ("omni.graph.nodes.WritePrimAttribute", "omni.graph.nodes.ReadPrimAttribute"):
            needs = [("name", "the attribute name to write or read")]
            uses_path = bool(node.value("usePath"))
            if uses_path and not self._satisfied(node, "primPath"):
                self.problem(node, "error", "missing_input",
                             f"{node.name} has 'Use Path' on but no Prim Path, so it does not know which prim to use.",
                             "Type the prim path into Prim Path, or turn off Use Path and pick the prim.")
            elif not uses_path and not self._satisfied(node, "prim"):
                self.problem(node, "error", "missing_input",
                             f"{node.name} has no Prim chosen, so it does not know which prim to use.",
                             "Pick the prim in the node's Prim input (or turn on Use Path and type a path).")
        elif kind in ("omni.graph.action.SendCustomEvent", "omni.graph.action.OnCustomEvent",
                      "omni.graph.action.SendMessageBusEvent", "omni.graph.action.OnMessageBusEvent"):
            needs = [("eventName", "the event name (it must match on both sides, letter for letter)")]
        elif kind == "omni.graph.action.SetPrimActive":
            if not (self._satisfied(node, "prim") or self._satisfied(node, "primTarget")):
                self.problem(node, "error", "missing_input",
                             f"{node.name} has no prim chosen, so it cannot switch anything on or off.",
                             "Pick the prim in Prim Target (or type a path in Prim).")
        elif kind == "isaacsim.core.nodes.IsaacArticulationController":
            if not (self._satisfied(node, "targetPrim") or self._satisfied(node, "robotPath")):
                self.problem(node, "error", "missing_input",
                             f"{node.name} has neither a Target Prim nor a Robot Path, so it controls no robot.",
                             "Pick the robot's articulation root prim in Target Prim.")
        elif kind == "omni.graph.ui_nodes.SetActiveViewportCamera":
            needs = [("primPath", "the camera prim path")]
        elif kind == "omni.graph.nodes.WriteSetting":
            needs = [("settingPath", "the setting path")]
        elif kind in ("omni.graph.core.ReadVariable", "omni.graph.core.WriteVariable"):
            needs = [("variableName", "the graph variable's name")]
        elif kind == "omni.graph.ui_nodes.PrintText" and not self._satisfied(node, "text"):
            self.problem(node, "warning", "missing_input", f"{node.name} has no Text, so it prints an empty line.",
                         "Type a message into Text (or wire a To String node into it).")
        for local, what in needs:
            if not self._satisfied(node, local):
                self.problem(node, "error", "missing_input", f"{node.name} is missing {what}: inputs:{local} is empty.",
                             f"Fill in inputs:{local} in the node's Property panel, or wire a value into it.")
        for local in (node.info or {}).get("targets", ()):
            for target in node.targets.get(local, []):
                prim = self.stage.GetPrimAtPath(target)
                if not prim or not prim.IsValid():
                    self.problem(node, "error", "missing_target",
                                 f"{node.name}.{local} points at {target}, but no prim exists there.",
                                 "Pick an existing prim (the path may have been renamed or deleted).")

    def _check_trigger(self, node: _Node) -> None:
        if node.role not in ("action", "flow"):
            return
        exec_inputs = node.exec_input_names()
        if not exec_inputs:
            return
        if not any(wire["input"] in exec_inputs for wire in node.in_wires):
            self.problem(node, "warning", "no_trigger",
                         f"Nothing is wired into {node.name}'s execution input, so it never runs.",
                         "Wire an event node's exec output (such as On Keyboard Input 'Pressed') into it.")

    def _check_playback(self, node: _Node) -> None:
        default = (node.info or {}).get("only_playback_default")
        if node.role != "event" or ("inputs:onlyPlayback" not in node.authored and not default):
            return
        value = node.value("onlyPlayback")
        if value is None:
            value = (node.info or {}).get("only_playback_default", False)
        if not value:
            return
        ticks = node.type in ("omni.graph.action.OnTick",)
        self.problem(node, "info" if ticks else "warning", "only_playback",
                     f"{node.name} has 'Only Simulate On Play' on, so it fires only while Play is pressed.",
                     "Turn off Only Simulate On Play (inputs:onlyPlayback) to react without pressing Play.")
        self.terms["Only Simulate On Play"] = None

    def _event_nodes(self, attribute: str, channel: str) -> list[_Node]:
        return [node for node in self.nodes.values() if (node.info or {}).get(attribute) == channel]

    def _check_events(self) -> None:
        for channel, label in (("message_bus", "message bus event"), ("custom", "custom event")):
            senders = self._event_nodes("sends", channel)
            listeners = self._event_nodes("listens", channel)
            for listener in listeners:
                name = _text(listener.value("eventName"))
                if not name:
                    continue
                exact = [sender for sender in senders if _text(sender.value("eventName")) == name]
                near = [sender for sender in senders if (_text(sender.value("eventName")) or "").lower() == name.lower()
                        and _text(sender.value("eventName")) != name]
                for sender in near:
                    self.problem(listener, "error", "event_name_mismatch",
                                 f"{listener.name} listens for {name!r} but {sender.name} sends "
                                 f"{_text(sender.value('eventName'))!r}; event names are case-sensitive, so it never fires.",
                                 "Use exactly the same spelling and capital letters on both nodes.")
                    self.terms["message bus" if channel == "message_bus" else "custom event"] = None
                if not exact and not near:
                    if channel == "message_bus":
                        self.problem(listener, "info", "no_sender",
                                     f"No graph in this stage sends {label} {name!r}; {listener.name} waits for it from "
                                     "outside (Python code or a browser client) - which must use exactly this name.")
                    else:
                        self.problem(listener, "warning", "no_sender",
                                     f"No Send Custom Event node in this stage sends {name!r}, so {listener.name} only "
                                     "fires if code sends it.")
                if channel == "message_bus":
                    for sender in exact:
                        self._check_payload(sender, listener, name)
            for sender in senders:
                name = _text(sender.value("eventName"))
                if not name:
                    continue
                if not any((_text(listener.value("eventName")) or "").lower() == name.lower() for listener in listeners):
                    other = "custom" if channel == "message_bus" else "message_bus"
                    crossed = [node for node in self._event_nodes("listens", other)
                               if _text(node.value("eventName")) == name]
                    if crossed:
                        self.problem(sender, "error", "event_channel_mismatch",
                                     f"{sender.name} sends {label} {name!r}, but {crossed[0].name} listens on the other "
                                     "channel; custom events and message bus events never reach each other.",
                                     "Use On MessageBus Event with Send MessageBus Event (or On/Send Custom Event) as a pair.")
                    else:
                        self.problem(sender, "info", "no_listener",
                                     f"No node in this stage listens for {label} {name!r} (code or a browser client may).")

    def _check_payload(self, sender: _Node, listener: _Node, name: str) -> None:
        sent = sender.dynamic_names("inputs")
        received = listener.dynamic_names("outputs")
        for key in received:
            if key in sent:
                continue
            close = [item for item in sent if item.lower() == key.lower()]
            if close:
                self.problem(listener, "error", "payload_mismatch",
                             f"{sender.name} sends payload key {close[0]!r} with {name!r}, but {listener.name} has an "
                             f"output named {key!r}; payload names are case-sensitive, so the value never arrives.",
                             f"Rename one of them so both are exactly {close[0]!r} (or both {key!r}).")
                self.terms["payload"] = None
        for key in sent:
            if key not in received and not any(item.lower() == key.lower() for item in received):
                self.problem(listener, "info", "payload_unused",
                             f"{sender.name} sends payload key {key!r}, but {listener.name} has no outputs:{key}, so "
                             "that value is not used by this listener.",
                             f"Add an output named exactly {key!r} to {listener.name} if you need the value.")

    # -- value tracing (used by variant checks and the story)
    def candidate_values(self, node: _Node, local: str) -> tuple[list[Any], str]:
        """Possible values of an input: literal, constants, graph variable defaults, or senders' payload values."""
        wires = node.wires_into(local)
        if not wires:
            value = node.value(local)
            return ([value] if value is not None and str(value) != "" else []), "typed in"
        values: list[Any] = []
        origin = "wired"
        for wire in wires:
            source = self.nodes.get(wire["from_node"])
            if source is None:
                continue
            if (source.info or {}).get("role") == "constant":
                if source.value("value") is not None:
                    values.append(source.value("value"))
                origin = "constant"
            elif source.type == "omni.graph.core.ReadVariable":
                var = _text(source.value("variableName"))
                graph = self.stage.GetPrimAtPath(source.graph_path or "")
                attr = graph.GetAttribute(f"graph:variable:{var}") if graph and var else None
                if attr and _attr_value(attr) is not None:
                    values.append(_attr_value(attr))
                origin = "variable"
            elif source.type == "omni.graph.action.OnMessageBusEvent" and wire["from_attr"].startswith("outputs:"):
                key = wire["from_attr"].split(":", 1)[1]
                event = _text(source.value("eventName"))
                origin = "payload"
                for sender in self._event_nodes("sends", "message_bus"):
                    if _text(sender.value("eventName")) == event:
                        sent, _ = self.candidate_values(sender, key)
                        values.extend(sent)
        return values, origin

    def _check_variant(self, node: _Node) -> None:
        targets = node.targets.get("prim", [])
        if not targets:
            return  # missing_input already reported (or the prim comes through a wire)
        prim = self.stage.GetPrimAtPath(targets[0])
        if not prim or not prim.IsValid():
            return  # missing_target already reported
        self.terms["variant set"] = None
        sets = prim.GetVariantSets()
        names = list(sets.GetNames())
        set_values, _ = self.candidate_values(node, "variantSetName")
        if not names:
            self.problem(node, "error", "no_variant_sets",
                         f"{node.name} targets {targets[0]}, but that prim has no variant sets at all.",
                         "Point the Prim input at the prim that holds the variant set (for the bag: its Looks prim).")
            return
        for set_name in [str(value) for value in set_values]:
            if set_name not in names:
                close = _close(set_name, names)
                self.problem(node, "error", "variant_set_missing",
                             f"{node.name} uses variant set {set_name!r}, but {targets[0]} only has {names}.",
                             f"Use {close[0]!r} (names are case-sensitive)." if close else f"Use one of {names}.")
                continue
            variants = list(sets.GetVariantSet(set_name).GetVariantNames())
            values, origin = self.candidate_values(node, "variantName")
            for value in [str(item) for item in values]:
                if value not in variants:
                    close = _close(value, variants)
                    where = {"payload": "the event payload sent in this stage", "constant": "a constant node",
                             "variable": "a graph variable", "typed in": "the typed-in value"}.get(origin, "a wire")
                    self.problem(node, "error", "variant_missing",
                                 f"{node.name} would select {set_name}={value!r} (from {where}), but {targets[0]}'s "
                                 f"{set_name} variants are {variants}.",
                                 f"Use {close[0]!r}: variant names are case-sensitive." if close
                                 else f"Use one of {variants}, or add a {value!r} variant first.")
            if origin == "payload" and not values:
                self.problem(node, "info", "variant_from_outside",
                             f"{node.name} takes the variant name from an event payload that no node in this stage "
                             f"sends; whoever sends it must use one of {variants} exactly.")

    def _check_variable(self, node: _Node) -> None:
        var = _text(node.value("variableName"))
        if not var:
            return
        graph_path = (node.targets.get("graph") or [node.graph_path or ""])[0]
        graph = self.stage.GetPrimAtPath(graph_path)
        if not graph or not graph.IsValid():
            return
        names = [prop.GetName()[len("graph:variable:"):] for prop in graph.GetAuthoredProperties()
                 if prop.GetName().startswith("graph:variable:")]
        self.terms["graph variable"] = None
        if var not in names:
            close = _close(var, names)
            self.problem(node, "error", "unknown_variable",
                         f"{node.name} uses graph variable {var!r}, but graph {graph.GetName()} has {names or 'no variables'}.",
                         f"Use {close[0]!r} (names are case-sensitive)." if close else "Create the variable in the graph's Variables panel.")

    # -- plain words
    def describe(self, node: _Node, local: str) -> str:
        """Plain words for where an input's value comes from."""
        wires = node.wires_into(local)
        if wires:
            words = []
            for wire in wires:
                source = self.nodes.get(wire["from_node"])
                if source is None:
                    words.append(f"{wire['from']} (missing)" if not self.stage.GetPrimAtPath(wire["from_node"])
                                 else wire["from"])
                    continue
                role = (source.info or {}).get("role")
                if role == "constant":
                    words.append(_quote(_json(source.value("value"))) if source.value("value") is not None else "(empty constant)")
                elif source.type == "omni.graph.core.ReadVariable":
                    words.append(f"variable '{_text(source.value('variableName'))}'")
                elif source.role == "event" and wire["from_attr"].startswith("outputs:"):
                    key = wire["from_attr"].split(":", 1)[1]
                    event = _text(source.value("eventName"))
                    words.append(f"the '{key}' value carried by the '{event}' event" if event else f"{source.ui}'s {key}")
                else:
                    words.append(f"{source.ui} ({source.name}).{wire['from_attr'].split(':', 1)[-1]}")
            return " or ".join(words)
        targets = node.targets.get(local)
        if targets:
            return ", ".join(targets)
        value = node.value(local)
        if value is None or str(value) == "":
            return "(not set)"
        return _quote(_json(value))

    def phrase(self, node: _Node) -> str:
        kind = node.type or ""
        d = lambda local: self.describe(node, local)  # noqa: E731
        if kind == "omni.graph.nodes.SetVariantSelection":
            return f"Set Variant Selection sets {d('variantSetName').strip(chr(39))}={d('variantName')} on {d('prim')}"
        if kind == "omni.graph.nodes.ClearVariantSelection":
            return f"Clear Variant Selection clears {d('variantSetName')} on {d('prim')}"
        if kind == "omni.graph.action.SendMessageBusEvent":
            payload = ", ".join(f"{key}={d(key)}" for key in node.dynamic_names("inputs"))
            return f"Send MessageBus Event sends {d('eventName')}" + (f" with {payload}" if payload else "")
        if kind == "omni.graph.action.SendCustomEvent":
            return f"Send Custom Event sends {d('eventName')}"
        if kind == "omni.graph.nodes.WritePrimAttribute":
            where = d("primPath") if node.value("usePath") else d("prim")
            return f"Write Prim Attribute writes {d('name')} = {d('value')} on {where}"
        if kind == "omni.graph.core.WriteVariable":
            return f"Write Variable sets variable {d('variableName')} to {d('value')}"
        if kind == "omni.graph.action.Branch":
            return f"Branch checks {d('condition')}"
        if kind == "omni.graph.ui_nodes.PrintText":
            return f"Print Text prints {d('text')}" + (" on screen" if node.value("toScreen") else "")
        if kind == "omni.graph.action.LogMessage":
            return f"Log Message logs {d('message')}"
        if kind == "omni.graph.nodes.WriteSetting":
            return f"Write Setting sets {d('settingPath')} to {d('value')}"
        if kind == "omni.graph.action.SetPrimActive":
            return f"Set Prim Active sets {d('primTarget') if node.targets.get('primTarget') else d('prim')} active={d('active')}"
        if kind == "omni.graph.ui_nodes.SetActiveViewportCamera":
            return f"Set Active Camera looks through {d('primPath')}"
        if kind == "omni.graph.ui_nodes.SetViewportMode":
            mode = {0: "Default", 1: "Scripted"}.get(node.value("mode"), d("mode"))
            return f"Set Viewport Mode switches the viewport to {mode} mode"
        if kind == "omni.graph.ui_nodes.SetViewportFullscreen":
            return f"Set Viewport Fullscreen sets mode {d('mode')}"
        if kind == "omni.graph.action.Delay":
            return f"Delay waits {d('duration')} s"
        if kind == "isaacsim.core.nodes.IsaacArticulationController":
            return f"Articulation Controller commands the robot at {d('targetPrim') if node.targets.get('targetPrim') else d('robotPath')}"
        if kind in ("omni.graph.nodes.TranslateToLocation", "omni.graph.nodes.RotateToOrientation"):
            return f"{node.ui} moves {d('prim')} toward {d('target')}"
        return f"{node.ui} ({node.name})"

    def trigger_phrase(self, node: _Node, output: str) -> str:
        kind = node.type or ""
        short = output.split(":", 1)[-1]
        if kind == "omni.graph.action.OnKeyboardInput":
            mods = [label for label, local in (("Ctrl", "ctrlIn"), ("Alt", "altIn"), ("Shift", "shiftIn"))
                    if node.value(local)]
            key = "+".join(mods + [str(node.value("keyIn") or "A")])
            return f"key {key} is {'released' if short == 'released' else 'pressed'}"
        event = _text(node.value("eventName"))
        if kind == "omni.graph.action.OnMessageBusEvent":
            return f"message bus event '{event}' arrives"
        if kind == "omni.graph.action.OnCustomEvent":
            return f"custom event '{event}' is sent"
        if kind == "omni.graph.action.OnStageEvent":
            return f"stage event '{event or 'Animation Start Play'}' happens"
        if kind == "omni.graph.action.OnTick":
            return "every app update (tick)" + (" while playing" if node.value("onlyPlayback") is not False else "")
        if kind == "omni.graph.action.OnPlaybackTick":
            return "every frame while the timeline plays"
        if kind == "omni.graph.action.OnLoaded":
            return "the graph loads"
        if kind == "omni.graph.action.OnClosing":
            return "the stage is about to close"
        if kind == "omni.graph.action.OnVariableChange":
            return f"variable '{_text(node.value('variableName'))}' changes"
        if kind == "isaacsim.core.nodes.OnPhysicsStep":
            return "every physics step"
        return f"{node.ui} fires ({short})"

    def chain(self, node: _Node, seen: set[str], steps: list[int]) -> str:
        if node.path in seen:
            return f"(back to {node.name})"
        if steps[0] >= MAX_STORY_STEPS:
            return "..."
        steps[0] += 1
        seen = seen | {node.path}
        text = self.phrase(node)
        if (node.info or {}).get("sends"):
            channel = node.info["sends"]
            event = _text(node.value("eventName"))
            heard = [other.name if other.graph_path == node.graph_path else _short(other.path)
                     for other in self._event_nodes("listens", channel) if _text(other.value("eventName")) == event]
            text += f" (heard by {', '.join(heard)})" if heard else " (no listener in this stage)"
        branches = self.next_steps(node)
        if not branches:
            return text
        if len(branches) == 1 and len(branches[0][1]) == 1:
            return f"{text} -> {self.chain(branches[0][1][0], seen, steps)}"
        parts = []
        for output, targets in branches:
            label = output.split(":", 1)[-1]
            for target in targets:
                parts.append(f"[{label}] {self.chain(target, seen, steps)}")
        return f"{text} -> " + "; ".join(parts)

    def next_steps(self, node: _Node) -> list[tuple[str, list[_Node]]]:
        result = []
        outputs = node.exec_output_names()
        extra = sorted({wire["output"] for wire in node.out_wires if wire["kind"] == "execution"} - set(outputs))
        for output in outputs + extra:
            targets = [self.nodes[wire["to_node"]] for wire in node.out_wires
                       if wire["kind"] == "execution" and wire["output"] == output and wire["to_node"] in self.nodes]
            if targets:
                result.append((output, targets))
        return result

    def story(self, graph_path: str) -> list[str]:
        lines = []
        for node in self.graph_nodes.get(graph_path, []):
            if node.role != "event":
                continue
            for output, targets in self.next_steps(node) or [("", [])]:
                if not output:
                    lines.append(f"When {self.trigger_phrase(node, 'outputs:execOut')} -> nothing is wired to it yet.")
                    continue
                for target in targets:
                    lines.append(f"When {self.trigger_phrase(node, output)} -> {self.chain(target, {node.path}, [0])}")
        return lines

    # -- result
    def node_record(self, node: _Node) -> dict[str, Any]:
        info = node.info or {}
        record: dict[str, Any] = {
            "path": node.path,
            "name": node.name,
            "type": node.type,
            "type_version": _json(node.type_version),
            "ui_name": node.ui,
            "role": node.role,
            "known": node.info is not None,
            "plain": info.get("plain") or "A node type this helper does not know; see its documentation in Kit.",
            "inputs": {key: _json(value) for key, value in sorted(node.values.items())},
            "targets": {key: value for key, value in sorted(node.targets.items())},
            "connections": {
                "in": [{key: wire[key] for key in ("input", "from", "from_node", "kind")} for wire in node.in_wires],
                "out": [dict(wire) for wire in node.out_wires],
            },
        }
        if info:
            record["installed_version"] = info["version"]
            record["extension"] = info["extension"]
        if info.get("dynamic") in ("inputs", "both"):
            record["payload_inputs"] = node.dynamic_names("inputs")
        if info.get("dynamic") in ("outputs", "both"):
            record["payload_outputs"] = node.dynamic_names("outputs")
        return record

    def graph_record(self, prim: Any) -> dict[str, Any]:
        path = str(prim.GetPath())
        values = {}
        for name in ("evaluator:type", "pipelineStage", "evaluationMode", "fileFormatVersion", "fabricCacheBacking"):
            attr = prim.GetAttribute(name)
            values[name] = _json(_attr_value(attr)) if attr else None
        variables = {}
        for prop in prim.GetAuthoredProperties():
            if prop.GetName().startswith("graph:variable:") and isinstance(prop, Usd.Attribute):
                variables[prop.GetName()[len("graph:variable:"):]] = _json(_attr_value(prop))
        evaluator = values["evaluator:type"] or "push"
        stage_name = values["pipelineStage"] or "pipelineStageSimulation"
        nodes = self.graph_nodes.get(path, [])
        if evaluator == "execution":
            self.terms["Action Graph"] = None
        counts: dict[str, int] = {}
        for node in nodes:
            counts[node.role] = counts.get(node.role, 0) + 1
        plain = (f"{prim.GetName()} is {EVALUATORS.get(evaluator, f'a graph with evaluator {evaluator!r}')}. "
                 f"It has {len(nodes)} node(s)")
        if counts.get("event"):
            plain += f", {counts['event']} of them event node(s) that start the action"
        plain += "."
        return {
            "path": path,
            "name": prim.GetName(),
            "evaluator": evaluator,
            "evaluator_plain": EVALUATORS.get(evaluator, "an unusual evaluator"),
            "pipeline_stage": stage_name.replace("pipelineStage", "")[:1].lower() + stage_name.replace("pipelineStage", "")[1:],
            "pipeline_stage_plain": PIPELINE_STAGES.get(stage_name, stage_name),
            "evaluation_mode": values["evaluationMode"],
            "file_format_version": values["fileFormatVersion"],
            "variables": variables,
            "plain": plain,
            "node_count": len(nodes),
            "nodes": [self.node_record(node) for node in nodes],
            "flow": self.story(path),
            "problems": self.problems.get(path, []),
        }


def _resolve(deadline: Any) -> Any:
    if isinstance(deadline, TimeBudget) or deadline is None:
        return resolve_budget(deadline)
    return resolve_budget(deadline, now=time.monotonic())


def inspect_graphs(stage: Any, root_path: str | None = None, *, deadline: Any = None,
                   max_nodes: int = MAX_NODES) -> dict[str, Any]:
    """Read-only, plain-language report of every OmniGraph (Action Graph) saved in the stage.

    root_path limits the search to one prim and everything below it (a graph prim, or a folder such as
    /World/Graphs). deadline is a usd_insight.TimeBudget or a time.monotonic() value to stop at; an ambient
    ``with TimeBudget(ms):`` also applies. Nothing is edited, saved or reloaded.

    Returns {ok, graphs: [{path, evaluator, pipeline_stage, variables, plain, nodes: [{path, name, type, plain,
    inputs, targets, connections: {in, out}}], flow: ["When key J is pressed -> ..."], problems: [{severity, code,
    node, message, fix}]}], problems (all, stage-wide), counts, glossary, truncated, truncated_by_time, node_types_source}.
    """
    _require_pxr()
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a whole number, 1 or more")
    budget = _resolve(deadline)
    root = stage.GetPseudoRoot()
    if root_path:
        text = str(root_path)
        if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsoluteRootOrPrimPath():
            return {"ok": False, "error": f"root_path is not a valid prim path: {text!r}", "root_path": text}
        root = stage.GetPrimAtPath(text)
        if not root or not root.IsValid():
            return {"ok": False, "error": f"No prim at root_path {text}.", "root_path": text}
        if not _is_graph(root):
            parent = root.GetParent()
            while parent and parent.IsValid() and not parent.IsPseudoRoot():
                if _is_graph(parent):
                    root = parent  # a node path: report its whole graph
                    break
                parent = parent.GetParent()
    run = _Inspection(stage, root, budget, max_nodes)
    run.walk()
    if not run.time_stopped:
        run.link()
        run.check()
    run.terms.update({"OmniGraph": None, "node": None, "event node": None, "execution wire": None, "data wire": None})
    graphs = [] if run.time_stopped and not run.graph_prims else [run.graph_record(prim) for prim in run.graph_prims]
    if any(node.targets for node in run.nodes.values()):
        run.terms["target input"] = None
    if any(node.type == "omni.graph.action.OnMessageBusEvent" for node in run.nodes.values()):
        run.terms["message bus"] = None
        run.terms["payload"] = None
    if any((node.info or {}).get("role") == "constant" for node in run.nodes.values()):
        run.terms["constant node"] = None
    all_problems = [dict(item, graph=key) for key, items in run.problems.items() for item in items]
    severity_counts = {level: sum(1 for item in all_problems if item["severity"] == level)
                       for level in ("error", "warning", "info")}
    if graphs:
        summary = f"Found {len(graphs)} graph(s) with {len(run.nodes)} node(s) in total"
        summary += f"; {severity_counts['error']} error(s) and {severity_counts['warning']} warning(s)." if (
            severity_counts["error"] or severity_counts["warning"]) else "; no errors or warnings."
    else:
        summary = "No OmniGraph (Action Graph) prims were found" + (f" under {root_path}." if root_path else " in this stage.")
    result: dict[str, Any] = {
        "ok": True,
        "root_path": str(root_path) if root_path else None,
        "summary": summary,
        "graphs": graphs,
        "problems": all_problems,
        "counts": {"graphs": len(graphs), "nodes": len(run.nodes), "prims_visited": run.visited,
                   "unknown_node_types": sum(1 for node in run.nodes.values() if node.info is None), **severity_counts},
        "glossary": [{"term": term, "meaning": GLOSSARY[term]} for term in run.terms if term in GLOSSARY],
        "truncated": run.truncated,
        "truncated_by_time": run.time_stopped,
        "node_types_source": {"kit": NODE_TYPES_SOURCE["kit"], "isaac_sim": NODE_TYPES_SOURCE["isaac_sim"]},
    }
    if run.truncated:
        result["note"] = f"Stopped after max_nodes={max_nodes} nodes; raise max_nodes to see the rest."
    if budget is not None:
        note = None
        if run.time_stopped:
            note = (f"Stopped at the time budget after {run.visited} prim(s); graphs, wires and problems are partial "
                    "(problem checks were skipped).")
        result["time_budget"] = budget.report(run.visited, run.time_stopped, note)
    return result


# ------------------------------------------------------------------------------------------------ recipes

RECIPES: dict[str, dict[str, Any]] = {
    "key_sets_variant": {
        "summary": "Pressing a key selects one variant (for example J sets Leather = Black).",
        "params": {"key": "keyboard key name such as 'J'", "prim": "prim path that holds the variant set",
                   "variant_set": "variant set name such as 'Leather'", "variant": "variant name such as 'Black'",
                   "graph_path": "optional new graph path"},
        "required": ("key", "prim", "variant_set", "variant"),
    },
    "event_sets_variant": {
        "summary": "The configurator pattern: a message bus event (such as setColor with payload colors='Black') "
        "selects the variant named in its payload; optionally a test key sends that event.",
        "params": {"event_name": "message bus event name such as 'setColor'",
                   "payload": "payload attribute name such as 'colors' (default 'colors')",
                   "prim": "prim path that holds the variant set", "variant_set": "variant set name such as 'Leather'",
                   "test_key": "optional key that sends the event (such as 'J')",
                   "test_variant": "variant the test key sends (such as 'Black')", "graph_path": "optional new graph path"},
        "required": ("event_name", "prim", "variant_set"),
    },
    "on_tick_rotate": {
        "summary": "A prim spins around one axis at a steady speed, driven by On Tick.",
        "params": {"prim": "prim path with an xformOp:rotateXYZ", "degrees_per_second": "spin speed (number)",
                   "axis": "'X', 'Y' or 'Z' (default: the stage up axis)",
                   "only_on_play": "spin only while Play runs (default True)", "graph_path": "optional new graph path"},
        "required": ("prim", "degrees_per_second"),
    },
    "print_on_play": {
        "summary": "Pressing Play prints a message (to the screen and the log).",
        "params": {"text": "message to print (default 'Hello from Action Graph!')", "to_screen": "default True",
                   "graph_path": "optional new graph path"},
        "required": (),
    },
    "keyboard_velocity": {
        "summary": "Class session 6: W/S/A/D push a rigid body by writing its physics:velocity; the four directions "
        "are graph variables you can change while it plays.",
        "params": {"prim": "a dynamic rigid body (not kinematic), e.g. /World/Cube",
                   "speed": "metres per second (default 5)",
                   "stop_on_release": "letting go of a key stops the body (default True)",
                   "graph_path": "optional new graph path"},
        "required": ("prim",),
    },
    "print_attribute": {
        "summary": "Class session 6: every tick, read one attribute of a prim, turn it into text (To String) and "
        "print it on the screen, e.g. a cube's physics:velocity.",
        "params": {"prim": "prim to read, e.g. /World/Cube", "attribute": "attribute name, e.g. physics:velocity",
                   "graph_path": "optional new graph path"},
        "required": ("prim", "attribute"),
    },
    "differential_drive": {
        "summary": "Class session 9: drive a two-wheel robot (Jetbot by default) with W/S (forward/back) and A/D "
        "(turn). Keys set speed/turn variables; a Differential Controller turns them into wheel speeds.",
        "params": {"robot": "the robot's articulation root, e.g. /World/Jetbot",
                   "wheel_joints": "[left, right] joint names (default Jetbot's left_wheel_joint, right_wheel_joint)",
                   "wheel_radius": "metres (Jetbot 0.03)", "wheel_distance": "metres between wheels (Jetbot 0.1125)",
                   "speed": "forward speed m/s (default 0.3)", "turn": "turn rate rad/s (default 1.5)",
                   "graph_path": "optional new graph path"},
        "required": ("robot",),
    },
    "drive_sequence": {
        "summary": "Class session 9: a fixed route, e.g. move -> stop -> turn -> move. Each step sets speed and "
        "turn variables, then a Delay waits its seconds; a Differential Controller drives the wheels.",
        "params": {"robot": "the robot's articulation root, e.g. /World/Jetbot",
                   "steps": "list of {linear (m/s), angular (rad/s), seconds}; default move-stop-turn-move",
                   "wheel_joints": "[left, right] joint names", "wheel_radius": "metres",
                   "wheel_distance": "metres", "graph_path": "optional new graph path"},
        "required": ("robot",),
    },
    "obstacle_stop": {
        "summary": "Class session 10: sense -> decide -> act. Every frame, measure the distance from the robot to an "
        "obstacle; if it is closer than stop_distance, stop the wheels, otherwise drive forward.",
        "params": {"robot": "the robot's articulation root, e.g. /World/Jetbot",
                   "sensor_prim": "a moving part of the robot to measure from, e.g. /World/Jetbot/chassis",
                   "obstacle": "the obstacle prim, e.g. /World/Obstacle",
                   "stop_distance": "metres (default 0.5)", "speed": "forward speed m/s (default 0.3)",
                   "wheel_joints": "[left, right] joint names", "wheel_radius": "metres",
                   "graph_path": "optional new graph path"},
        "required": ("robot", "sensor_prim", "obstacle"),
    },
    "color_sort": {
        "summary": "Class session 13: sort packages by colour. When a package enters the detection zone, the graph "
        "SEES its colour, DECIDES its bin (e.g. red -> Bin A) and ACTS by pushing it toward that bin. A Script Node "
        "does it for every package in the group.",
        "params": {"packages": "group holding the packages (default /World/Packages, as scene_spawn_packages makes)",
                   "bins": "{colour: bin prim path}, e.g. {'red': '/World/BinA', 'blue': '/World/BinB'}",
                   "zone_min": "detection zone corner [x, y, z] (world)", "zone_max": "the opposite corner",
                   "speed": "push speed toward the bin, m/s (default 1.5)",
                   "hop": "small upward speed so it clears the belt edge, m/s (default 1.0)",
                   "palette": "optional learned colours: a colour_model.json path (from train_colour_model) or "
                              "{name: [r, g, b]}; the route names must match its class names",
                   "camera": "optional Camera prim watching the zone: then colours are read from its PICTURE "
                             "(640x480, e.g. made with sensor_create); without it, from each package's colour data",
                   "graph_path": "optional new graph path"},
        "required": ("bins", "zone_min", "zone_max"),
    },
    "conveyor": {
        "summary": "Class session 6: a conveyor belt. A kinematic rigid-body belt moves what lies on it; the speed "
        "is a graph variable (Velocity) you can change while it plays. Same graph as Isaac's own Create Conveyor.",
        "params": {"belt": "the belt prim: a rigid body with a collider, kinematic, e.g. /World/Belt",
                   "velocity": "metres per second (default 2; negative runs backwards)",
                   "direction": "belt-local direction [x, y, z] (default [1, 0, 0])",
                   "graph_path": "optional new graph path"},
        "required": ("belt",),
    },
}
_DEFAULT_GRAPH = {
    "key_sets_variant": "/World/Graphs/KeySetsVariant",
    "event_sets_variant": "/World/Graphs/EventSetsVariant",
    "on_tick_rotate": "/World/Graphs/OnTickRotate",
    "print_on_play": "/World/Graphs/PrintOnPlay",
    "keyboard_velocity": "/World/Graphs/KeyboardVelocity",
    "print_attribute": "/World/Graphs/PrintAttribute",
    "conveyor": "/World/Graphs/Conveyor",
    "differential_drive": "/World/Graphs/DifferentialDrive",
    "drive_sequence": "/World/Graphs/DriveSequence",
    "obstacle_stop": "/World/Graphs/ObstacleStop",
    "color_sort": "/World/Graphs/ColorSort",
}


def list_recipes() -> dict[str, Any]:
    """The recipe names with a one-line summary and their parameters."""
    return {"ok": True, "recipes": [{"name": name, "summary": spec["summary"], "params": spec["params"],
                                     "required": list(spec["required"])} for name, spec in RECIPES.items()]}


class _Plan:
    def __init__(self, recipe: str, graph_path: str) -> None:
        self.recipe = recipe
        self.graph_path = graph_path
        self.nodes: list[dict[str, Any]] = []
        self.attributes: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = []
        self.values: list[dict[str, Any]] = []
        self.variables: list[dict[str, Any]] = []
        self.explanation: list[str] = []
        self.checks: list[str] = []
        self.warnings: list[str] = []

    def node(self, name: str, type_id: str, why: str) -> None:
        info = NODE_TYPES[type_id]
        self.nodes.append({"name": name, "type": type_id, "ui_name": info["ui"], "path": f"{self.graph_path}/{name}",
                           "plain": info["plain"], "why": why, "source": info["source"]})

    def attribute(self, attribute: str, type_name: str, why: str) -> None:
        self.attributes.append({"attribute": attribute, "type": type_name, "why": why})

    def variable(self, name: str, type_name: str, default: Any, why: str) -> None:
        self.variables.append({"name": name, "type": type_name, "default": default, "why": why})

    def wire(self, source: str, target: str, kind: str, why: str) -> None:
        self.connections.append({"from": source, "to": target, "kind": kind, "why": why})

    def value(self, attribute: str, value: Any, why: str, *, type_name: str | None = None, target: bool = False,
              array: bool = False) -> None:
        item = {"attribute": attribute, "value": value, "why": why}
        if array:
            item["array"] = True
        if type_name:
            item["type"] = type_name
        if target:
            item["target"] = True
        self.values.append(item)


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "dry_run": True, "error": message, **extra}


def _graph_path_problem(stage: Any, graph_path: str, explicit: bool) -> tuple[str | None, str]:
    """(error, final path). A default path that is taken gets a numbered suffix; an explicit one is refused."""
    text = str(graph_path)
    if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsoluteRootOrPrimPath() or text == "/":
        return f"graph_path is not a valid absolute prim path: {text!r}", text
    if stage is None:
        return None, text
    candidate, index = text, 1
    while stage.GetPrimAtPath(candidate):
        if explicit:
            return (f"A prim already exists at {candidate}; this helper never edits an existing graph. "
                    "Choose a new graph_path."), candidate
        candidate = f"{text}_{index:02d}"
        index += 1
    parent = Sdf.Path(candidate).GetParentPath()
    while parent != Sdf.Path.absoluteRootPath:
        prim = stage.GetPrimAtPath(parent)
        if prim and (_is_graph(prim) or _is_node(prim)):
            return f"graph_path {candidate} would be inside the graph {parent}; put it next to graphs, not inside one.", candidate
        parent = parent.GetParentPath()
    return None, candidate


def _check_prim(stage: Any, prim_path: Any, plan: _Plan, what: str) -> tuple[str | None, Any]:
    if not prim_path:
        return f"{what} is required (a prim path such as /World/MyPrim).", None
    text = str(prim_path)
    if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsoluteRootOrPrimPath():
        return f"{what} is not a valid absolute prim path: {text!r}", None
    if stage is None:
        plan.warnings.append(f"No stage was given, so {text} was not checked; apply_recipe checks it.")
        return None, None
    prim = stage.GetPrimAtPath(text)
    if not prim or not prim.IsValid():
        return f"No prim at {text} in this stage.", None
    plan.checks.append(f"{text} exists ({prim.GetTypeName() or 'typeless'} prim).")
    return None, prim


def _check_variant(prim: Any, variant_set: str, variant: str | None, plan: _Plan) -> tuple[str | None, list[str]]:
    if prim is None:
        return None, []
    sets = prim.GetVariantSets()
    names = list(sets.GetNames())
    if variant_set not in names:
        close = _close(variant_set, names)
        hint = f" Did you mean {close[0]!r}? Names are case-sensitive." if close else ""
        return f"{prim.GetPath()} has no variant set {variant_set!r}; it has {names or 'none'}.{hint}", []
    variants = list(sets.GetVariantSet(variant_set).GetVariantNames())
    if variant is not None and variant not in variants:
        close = _close(variant, variants)
        hint = f" Did you mean {close[0]!r}? Names are case-sensitive." if close else ""
        return f"Variant set {variant_set} on {prim.GetPath()} has no variant {variant!r}; it has {variants}.{hint}", variants
    plan.checks.append(f"Variant set {variant_set} exists with variants {variants}.")
    return None, variants


def _check_key(key: Any) -> str | None:
    if key not in KEYBOARD_KEYS:
        close = _close(str(key), KEYBOARD_KEYS)
        return f"key {key!r} is not a key name On Keyboard Input accepts." + (f" Use {close[0]!r}." if close else "")
    return None


def _plan_key_sets_variant(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    key, variant_set, variant = params.get("key"), params.get("variant_set"), params.get("variant")
    error = _check_key(key)
    if error:
        return error
    if not variant_set or not variant:
        return "variant_set and variant are required."
    error, prim = _check_prim(stage, params.get("prim"), plan, "prim")
    if error:
        return error
    error, _ = _check_variant(prim, str(variant_set), str(variant), plan)
    if error:
        return error
    prim_path = str(params["prim"])
    plan.node("OnKeyPress", "omni.graph.action.OnKeyboardInput", f"listens for key {key}")
    plan.node("SetVariant", "omni.graph.nodes.SetVariantSelection", f"selects {variant_set}={variant}")
    plan.wire("OnKeyPress.outputs:pressed", "SetVariant.inputs:execIn", "execution",
              "execution wire: when the key goes down, Set Variant Selection runs")
    plan.value("OnKeyPress.inputs:keyIn", str(key), "the key to listen for")
    plan.value("OnKeyPress.inputs:onlyPlayback", False, "react without pressing Play ('Only Simulate On Play' off)")
    plan.value("SetVariant.inputs:prim", [prim_path], "the prim that owns the variant set (a target input)", target=True)
    plan.value("SetVariant.inputs:variantSetName", str(variant_set), "which variant set to switch")
    plan.value("SetVariant.inputs:variantName", str(variant), "which variant to select")
    plan.value("SetVariant.inputs:setVariant", True, "change the variant selection itself (the node's default is off)")
    plan.explanation += [
        f"When key {key} is pressed -> Set Variant Selection sets {variant_set}={variant} on {prim_path}.",
        "The graph is an Action Graph (evaluator 'execution'): it does nothing until the key event fires.",
        "Click in the viewport first so it has keyboard focus, then press the key.",
    ]
    return None


def _plan_event_sets_variant(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    event, payload = params.get("event_name"), params.get("payload") or "colors"
    variant_set = params.get("variant_set")
    test_key, test_variant = params.get("test_key"), params.get("test_variant")
    if not event or not str(event).strip():
        return "event_name is required (for the course: 'setColor')."
    if not isinstance(payload, str) or not payload.isidentifier() or keyword.iskeyword(payload):
        return f"payload must be a simple attribute name such as 'colors', not {payload!r}."
    if not variant_set:
        return "variant_set is required (for the bag: 'Leather')."
    if (test_key is None) != (test_variant is None):
        return "test_key and test_variant go together: give both (for example J and Black) or neither."
    if test_key is not None:
        error = _check_key(test_key)
        if error:
            return error
    error, prim = _check_prim(stage, params.get("prim"), plan, "prim")
    if error:
        return error
    error, variants = _check_variant(prim, str(variant_set), None if test_variant is None else str(test_variant), plan)
    if error:
        return error
    if stage is not None:
        existing = inspect_graphs(stage)
        for graph in existing.get("graphs", []):
            for node in graph["nodes"]:
                name = node["inputs"].get("eventName")
                if node["type"] == "omni.graph.action.OnMessageBusEvent" and isinstance(name, str):
                    if name.lower() == str(event).lower() and name != event:
                        plan.warnings.append(f"{node['path']} already listens for {name!r}; {event!r} differs only in "
                                             "capital letters, so the two will not hear each other.")
                    elif name == event:
                        plan.warnings.append(f"{node['path']} already listens for {event!r} too; both graphs will react.")
    prim_path = str(params["prim"])
    plan.node("OnEvent", "omni.graph.action.OnMessageBusEvent", f"waits for message bus event {event!r}")
    plan.node("SetVariant", "omni.graph.nodes.SetVariantSelection", f"selects the {variant_set} variant named in the payload")
    plan.attribute(f"OnEvent.outputs:{payload}", "token",
                   f"extra output that receives the payload key {payload!r} (must match the sender exactly)")
    plan.wire("OnEvent.outputs:execOut", "SetVariant.inputs:execIn", "execution", "run Set Variant Selection when the event arrives")
    plan.wire(f"OnEvent.outputs:{payload}", "SetVariant.inputs:variantName", "data",
              f"data wire: the payload's {payload} value becomes the variant name")
    plan.value("OnEvent.inputs:eventName", str(event), "the event name, letter for letter")
    plan.value("OnEvent.inputs:onlyPlayback", False, "react without pressing Play")
    plan.value("SetVariant.inputs:prim", [prim_path], "the prim that owns the variant set (a target input)", target=True)
    plan.value("SetVariant.inputs:variantSetName", str(variant_set), "which variant set to switch")
    plan.value("SetVariant.inputs:setVariant", True, "change the variant selection itself (the node's default is off)")
    choices = ", ".join(variants) if variants else "a variant name"
    plan.explanation += [
        f"When message bus event '{event}' arrives -> Set Variant Selection sets {variant_set}=<the '{payload}' value> on {prim_path}.",
        f"The sender (a browser client, Python, or Send MessageBus Event) must use event name '{event}' and payload key "
        f"'{payload}' exactly, with a value such as {choices}.",
        "Python test inside Kit: import carb.events, omni.kit.app; omni.kit.app.get_app().get_message_bus_event_stream()"
        f".push(carb.events.type_from_string('{event}'), payload={{'{payload}': '{variants[0] if variants else 'Black'}'}})",
    ]
    if test_key is not None:
        plan.node("TestKey", "omni.graph.action.OnKeyboardInput", f"key {test_key} sends a test event")
        plan.node("SendEvent", "omni.graph.action.SendMessageBusEvent", f"sends {event!r} with {payload}={test_variant!r}")
        plan.attribute(f"SendEvent.inputs:{payload}", "token", f"extra input: becomes the payload key {payload!r}")
        plan.wire("TestKey.outputs:pressed", "SendEvent.inputs:execIn", "execution", "send the event when the key goes down")
        plan.value("TestKey.inputs:keyIn", str(test_key), "the test key")
        plan.value("TestKey.inputs:onlyPlayback", False, "react without pressing Play")
        plan.value("SendEvent.inputs:eventName", str(event), "same event name as the listener")
        plan.value(f"SendEvent.inputs:{payload}", str(test_variant), "the variant name to send")
        plan.explanation.append(
            f"When key {test_key} is pressed -> Send MessageBus Event sends '{event}' with {payload}='{test_variant}' "
            "-> the listener above selects it.")
    return None


def _plan_on_tick_rotate(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    speed = params.get("degrees_per_second")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(float(speed)):
        return "degrees_per_second must be a number, such as 45."
    error, prim = _check_prim(stage, params.get("prim"), plan, "prim")
    if error:
        return error
    axis = params.get("axis")
    if axis is None:
        axis = str(UsdGeom.GetStageUpAxis(stage)) if stage is not None else "Y"
    axis = str(axis).upper()
    if axis not in ("X", "Y", "Z"):
        return "axis must be 'X', 'Y' or 'Z'."
    start = [0.0, 0.0, 0.0]
    if prim is not None:
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            return f"{prim.GetPath()} is not transformable (not an Xform or a shape), so it cannot rotate."
        ops = [op.GetOpName() for op in xformable.GetOrderedXformOps()]
        if "xformOp:rotateXYZ" not in ops:
            return (f"{prim.GetPath()} has no xformOp:rotateXYZ (its ops are {ops or 'none'}). Give it a Rotate XYZ "
                    "transform first (Property panel > Transform), then plan again.")
        current = prim.GetAttribute("xformOp:rotateXYZ").Get()
        if current is not None:
            start = [float(item) for item in current]
        plan.checks.append(f"xformOp:rotateXYZ found; current rotation {start} is kept on the other axes.")
    index = "XYZ".index(axis)
    only_on_play = bool(params.get("only_on_play", True))
    prim_path = str(params["prim"])
    plan.node("OnTick", "omni.graph.action.OnTick", "fires every app update")
    plan.node("Speed", "omni.graph.nodes.ConstantDouble", f"holds the speed {float(speed):g} degrees per second")
    plan.node("AngleFromTime", "omni.graph.nodes.Multiply", "angle = time x speed")
    plan.node("PlusStart", "omni.graph.nodes.Add", "adds the starting angle so the prim does not jump")
    plan.node("MakeRotation", "omni.graph.nodes.MakeVector3", f"builds (x, y, z) with the new angle on {axis}")
    plan.node("WriteRotation", "omni.graph.nodes.WritePrimAttribute", "writes xformOp:rotateXYZ on the prim")
    plan.wire("OnTick.outputs:tick", "WriteRotation.inputs:execIn", "execution", "write the rotation on every tick")
    plan.wire("OnTick.outputs:absoluteSimTime", "AngleFromTime.inputs:a", "data", "seconds of ticking so far")
    plan.wire("Speed.inputs:value", "AngleFromTime.inputs:b", "data", "degrees per second")
    plan.wire("AngleFromTime.outputs:product", "PlusStart.inputs:a", "data", "degrees turned so far")
    plan.wire("PlusStart.outputs:sum", f"MakeRotation.inputs:{'xyz'[index]}", "data", f"the {axis} angle")
    plan.wire("MakeRotation.outputs:tuple", "WriteRotation.inputs:value", "data", "the full rotation")
    plan.value("OnTick.inputs:onlyPlayback", only_on_play, "spin only while Play runs" if only_on_play else "spin all the time")
    plan.value("Speed.inputs:value", float(speed), "degrees per second")
    plan.value("PlusStart.inputs:b", start[index], "the starting angle on that axis", type_name="double")
    for other in range(3):
        if other != index:
            plan.value(f"MakeRotation.inputs:{'xyz'[other]}", start[other], "keep this axis as it is", type_name="double")
    plan.value("WriteRotation.inputs:prim", [prim_path], "the prim to rotate (a target input)", target=True)
    plan.value("WriteRotation.inputs:name", "xformOp:rotateXYZ", "the rotation attribute")
    plan.value("WriteRotation.inputs:usdWriteBack", True, "write into USD so the viewport and the Property panel show it")
    plan.explanation += [
        f"When the app ticks{' while playing' if only_on_play else ''} -> {prim_path} gets rotateXYZ with "
        f"{axis} = start + time x {float(speed):g} degrees per second.",
        "Data nodes (Multiply, Add, Make 3-Vector) have no execution wires: they compute when Write Prim Attribute needs the value.",
        "Because usdWriteBack is on, the rotation is written into the current edit layer every tick; delete the graph "
        "(or set the rotation back) to undo the spin.",
    ]
    return None


def _plan_print_on_play(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    text = params.get("text", "Hello from Action Graph!")
    if not isinstance(text, str) or not text:
        return "text must be a non-empty string."
    to_screen = bool(params.get("to_screen", True))
    plan.node("OnPlay", "omni.graph.action.OnStageEvent", "fires when timeline playback starts")
    plan.node("Print", "omni.graph.ui_nodes.PrintText", "prints the message")
    plan.wire("OnPlay.outputs:execOut", "Print.inputs:execIn", "execution", "print when Play starts")
    plan.value("OnPlay.inputs:eventName", "Animation Start Play", "the stage event sent when Play is pressed")
    plan.value("OnPlay.inputs:onlyPlayback", False, "listen even before playback has started")
    plan.value("Print.inputs:text", text, "the message")
    plan.value("Print.inputs:toScreen", to_screen, "show it in the viewport too" if to_screen else "log only")
    plan.value("Print.inputs:logLevel", "Info", "an Info log line (not a warning)")
    plan.explanation += [
        f"When stage event 'Animation Start Play' happens (you press Play) -> Print Text prints {text!r}"
        + (" on screen and in the log." if to_screen else " in the log."),
    ]
    return None


def _rigid_problems(stage: Any, prim: Any, plan: _Plan, *, want_kinematic: bool) -> None:
    """Warnings (not refusals) about the physics a graph relies on; the learner may add them after the graph."""
    if prim is None:
        return
    from pxr import UsdPhysics

    path = prim.GetPath()
    if not prim.HasAPI(UsdPhysics.RigidBodyAPI):
        plan.warnings.append(f"{path} is not a rigid body yet: add one (scene_make_rigid_body) or nothing will move.")
    if not prim.HasAPI(UsdPhysics.CollisionAPI):
        plan.warnings.append(f"{path} has no collider: add one (scene_make_collider) so things can rest on or hit it.")
    kinematic = prim.GetAttribute("physics:kinematicEnabled")
    is_kinematic = bool(kinematic and kinematic.Get())
    if want_kinematic and not is_kinematic:
        plan.warnings.append(f"Make {path} kinematic (scene_make_rigid_body with kinematic=True): a belt must stay "
                             "put while it carries things, as in class.")
    if not want_kinematic and is_kinematic:
        plan.warnings.append(f"{path} is kinematic, so physics ignores the velocity this graph writes; turn kinematic off.")
    scene = any(item.GetTypeName() == "PhysicsScene" for item in stage.Traverse()) if stage is not None else True
    if not scene:
        plan.warnings.append("The stage has no PhysicsScene (scene_add_physics_scene): nothing simulates without one.")


def _plan_keyboard_velocity(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    speed = params.get("speed", 5)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(float(speed)) or speed <= 0:
        return "speed must be a positive number of metres per second, such as 5."
    error, prim = _check_prim(stage, params.get("prim"), plan, "prim")
    if error:
        return error
    _rigid_problems(stage, prim, plan, want_kinematic=False)
    prim_path, speed = str(params["prim"]), float(speed)
    stop = bool(params.get("stop_on_release", True))
    directions = (("W", "forward", [speed, 0.0, 0.0]), ("S", "back", [-speed, 0.0, 0.0]),
                  ("A", "left", [0.0, speed, 0.0]), ("D", "right", [0.0, -speed, 0.0]))
    for key, name, vector in directions:
        plan.variable(name, "float[3]", vector, f"velocity while {key} is held (m/s); change it live")
        plan.node(f"Key{key}", "omni.graph.action.OnKeyboardInput", f"fires when {key} is pressed or released")
        plan.node(f"Read_{name}", "omni.graph.core.ReadVariable", f"reads the {name} variable")
        plan.node(f"Write_{name}", "omni.graph.nodes.WritePrimAttribute", f"writes {name} into physics:velocity")
        plan.wire(f"Key{key}.outputs:pressed", f"Write_{name}.inputs:execIn", "execution", f"when {key} goes down")
        plan.wire(f"Read_{name}.outputs:value", f"Write_{name}.inputs:value", "data", f"the {name} velocity")
        plan.value(f"Key{key}.inputs:keyIn", key, "the key")
        plan.value(f"Key{key}.inputs:onlyPlayback", True, "only while Play runs (physics is off otherwise)")
        plan.value(f"Read_{name}.inputs:variableName", name, "which graph variable to read")
        plan.value(f"Write_{name}.inputs:prim", [prim_path], "the body to push (a target input)", target=True)
        plan.value(f"Write_{name}.inputs:name", "physics:velocity", "the rigid body's linear velocity attribute")
        plan.value(f"Write_{name}.inputs:usdWriteBack", True, "write into USD so physics picks it up")
    if stop:
        plan.variable("stop", "float[3]", [0.0, 0.0, 0.0], "velocity when no key is held")
        plan.node("Read_stop", "omni.graph.core.ReadVariable", "reads the stop variable")
        plan.node("Write_stop", "omni.graph.nodes.WritePrimAttribute", "writes (0, 0, 0) into physics:velocity")
        plan.wire("Read_stop.outputs:value", "Write_stop.inputs:value", "data", "zero velocity")
        for key, _name, _vector in directions:
            plan.wire(f"Key{key}.outputs:released", "Write_stop.inputs:execIn", "execution", f"when {key} comes up")
        plan.value("Read_stop.inputs:variableName", "stop", "which graph variable to read")
        plan.value("Write_stop.inputs:prim", [prim_path], "the body to stop", target=True)
        plan.value("Write_stop.inputs:name", "physics:velocity", "the rigid body's linear velocity attribute")
        plan.value("Write_stop.inputs:usdWriteBack", True, "write into USD so physics picks it up")
    plan.explanation += [
        f"When W/S/A/D is pressed (while playing) -> Write Prim Attribute sets {prim_path}.physics:velocity to the "
        "forward/back/left/right variable" + (" ; releasing the key writes the stop variable (0, 0, 0)." if stop else "."),
        "The four directions are graph VARIABLES: while it plays, change one in the graph editor's Variables panel and "
        "the next key press uses the new value, just as in class.",
        "Velocities are along the WORLD axes: after the body turns, W still pushes along world X. Moving along the "
        "body's own axes needs its rotation matrix (the class's 'local axes' point).",
    ]
    return None


def _plan_print_attribute(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    error, prim = _check_prim(stage, params.get("prim"), plan, "prim")
    if error:
        return error
    attribute = params.get("attribute")
    if not isinstance(attribute, str) or not attribute.strip():
        return "attribute must be an attribute name such as physics:velocity or xformOp:translate."
    if prim is not None and not prim.GetAttribute(attribute):
        names = sorted(a.GetName() for a in prim.GetAttributes())[:12]
        return f"{prim.GetPath()} has no attribute {attribute!r}. Some it has: {names}."
    prim_path = str(params["prim"])
    plan.node("OnTick", "omni.graph.action.OnTick", "fires every app update")
    plan.node("Read", "omni.graph.nodes.ReadPrimAttribute", f"reads {attribute}")
    plan.node("AsText", "omni.graph.nodes.ToString", "turns the value (numbers, a vector...) into text")
    plan.node("Print", "omni.graph.ui_nodes.PrintText", "shows the text on the screen")
    plan.wire("OnTick.outputs:tick", "Print.inputs:execIn", "execution", "print on every tick")
    plan.wire("Read.outputs:value", "AsText.inputs:value", "data", "the attribute's value")
    plan.wire("AsText.outputs:converted", "Print.inputs:text", "data", "the value as text")
    plan.value("OnTick.inputs:onlyPlayback", True, "only while Play runs")
    plan.value("Read.inputs:prim", [prim_path], "the prim to read (a target input)", target=True)
    plan.value("Read.inputs:name", attribute, "which attribute")
    plan.value("Print.inputs:toScreen", True, "show it in the viewport")
    plan.value("Print.inputs:logLevel", "Info", "an Info log line")
    plan.explanation += [
        f"Every tick while playing -> read {prim_path}.{attribute} -> To String -> Print Text on the screen.",
        "Print Text only takes text (a string), so a vector such as a velocity must go through To String first: "
        "that is the class's 'convert before printing' point.",
    ]
    return None


def _plan_conveyor(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    velocity = params.get("velocity", 2)
    if isinstance(velocity, bool) or not isinstance(velocity, (int, float)) or not math.isfinite(float(velocity)):
        return "velocity must be a number of metres per second, such as 2 or -2."
    direction = params.get("direction", [1.0, 0.0, 0.0])
    if (not isinstance(direction, (list, tuple)) or len(direction) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in direction)):
        return "direction must be three numbers such as [1, 0, 0]."
    error, prim = _check_prim(stage, params.get("belt"), plan, "belt")
    if error:
        return error
    _rigid_problems(stage, prim, plan, want_kinematic=True)
    plan.warnings.append("The Conveyor Belt node lives in the isaacsim.asset.gen.conveyor extension: if Isaac says "
                         "the node type is unknown, enable it in Window > Extensions.")
    belt = str(params["belt"])
    plan.variable("Velocity", "float", float(velocity), "belt speed in m/s; change it live (0 stops the belt)")
    plan.node("OnTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs")
    plan.node("ConveyorNode", "isaacsim.asset.gen.conveyor.IsaacConveyor", f"moves things lying on {belt}")
    plan.node("ReadSpeed", "omni.graph.core.ReadVariable", "reads the Velocity variable")
    plan.wire("OnTick.outputs:tick", "ConveyorNode.inputs:onStep", "execution", "update the belt every frame")
    plan.wire("OnTick.outputs:deltaSeconds", "ConveyorNode.inputs:delta", "data", "time since the last frame")
    plan.wire("ReadSpeed.outputs:value", "ConveyorNode.inputs:velocity", "data", "the belt speed")
    plan.value("ReadSpeed.inputs:variableName", "Velocity", "which graph variable to read")
    plan.value("ConveyorNode.inputs:conveyorPrim", [belt], "the belt (a target input)", target=True)
    if [float(v) for v in direction] != [1.0, 0.0, 0.0]:
        # No type_name: direction already has a fixed float[3] type. Seen live in Isaac 6.1: passing the type for a
        # fixed-type pin made og refuse the value; Isaac's own Create Conveyor leaves the default (1, 0, 0) alone.
        plan.value("ConveyorNode.inputs:direction", [float(v) for v in direction], "belt-local direction")
    plan.explanation += [
        f"While playing -> each frame the Conveyor Belt node gives {belt} a surface velocity of Velocity m/s along "
        f"{list(direction)}: the belt stays still (kinematic) but anything resting on it slides along.",
        "For a loop, make several belts, rotate each (e.g. -90 degrees about Z) and run this recipe once per belt.",
        "Homework idea (class): to stop a box for 5 s, set Velocity to 0, wait (a Delay node), then set it back.",
    ]
    return None


_JETBOT = {"wheel_joints": ["left_wheel_joint", "right_wheel_joint"], "wheel_radius": 0.03, "wheel_distance": 0.1125}


def _number(params: dict[str, Any], key: str, default: float, *, positive: bool = True) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be a number.")
    if positive and value <= 0:
        raise ValueError(f"{key} must be bigger than 0.")
    return float(value)


def _wheels(stage: Any, plan: _Plan, params: dict[str, Any]) -> tuple[str | None, str, list[str], float, float]:
    """(error, robot path, wheel joint names, wheel radius, wheel distance), with the robot checked on the stage."""
    error, prim = _check_prim(stage, params.get("robot"), plan, "robot")
    if error:
        return error, "", [], 0.0, 0.0
    joints = params.get("wheel_joints", _JETBOT["wheel_joints"])
    if not isinstance(joints, list) or len(joints) != 2 or not all(isinstance(j, str) and j for j in joints):
        return "wheel_joints must be [left joint name, right joint name].", "", [], 0.0, 0.0
    try:
        radius = _number(params, "wheel_radius", _JETBOT["wheel_radius"])
        distance = _number(params, "wheel_distance", _JETBOT["wheel_distance"])
    except ValueError as exc:
        return str(exc), "", [], 0.0, 0.0
    if prim is not None:
        from pxr import UsdPhysics
        root = any(p.HasAPI(UsdPhysics.ArticulationRootAPI) for p in Usd.PrimRange(prim))
        if not root:
            plan.warnings.append(f"No articulation root under {prim.GetPath()}: the Articulation Controller needs one "
                                 "(add the robot with robot_add_ready, or scene_add_articulation_root).")
        names = {p.GetName() for p in Usd.PrimRange(prim) if p.IsA(UsdPhysics.Joint)}
        missing = [j for j in joints if names and j not in names]
        if missing:
            return (f"{prim.GetPath()} has no joints named {missing}; its joints include {sorted(names)[:8]}. "
                    "Pass wheel_joints."), "", [], 0.0, 0.0
    return None, str(params["robot"]), list(joints), radius, distance


def _driver(plan: _Plan, robot: str, joints: list[str], radius: float, distance: float) -> None:
    """Every frame: read the linear/angular variables -> Differential Controller -> Articulation Controller."""
    plan.variable("linear", "double", 0.0, "forward speed in m/s (0 = stand still)")
    plan.variable("angular", "double", 0.0, "turn rate in rad/s (positive = left)")
    plan.node("OnTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs")
    plan.node("ReadLinear", "omni.graph.core.ReadVariable", "reads the forward speed")
    plan.node("ReadAngular", "omni.graph.core.ReadVariable", "reads the turn rate")
    plan.node("Differential", "isaacsim.robot.wheeled_robots.DifferentialController",
              "turns speed + turn rate into a speed for each wheel")
    plan.node("Wheels", "isaacsim.core.nodes.IsaacArticulationController", "sends the wheel speeds to PhysX")
    plan.wire("OnTick.outputs:tick", "Differential.inputs:execIn", "execution", "work out wheel speeds every frame")
    plan.wire("OnTick.outputs:deltaSeconds", "Differential.inputs:dt", "data", "time since the last frame")
    plan.wire("OnTick.outputs:tick", "Wheels.inputs:execIn", "execution", "send wheel speeds every frame")
    plan.wire("ReadLinear.outputs:value", "Differential.inputs:linearVelocity", "data", "desired forward speed")
    plan.wire("ReadAngular.outputs:value", "Differential.inputs:angularVelocity", "data", "desired turn rate")
    plan.wire("Differential.outputs:velocityCommand", "Wheels.inputs:velocityCommand", "data",
              "one speed per wheel (rad/s)")
    plan.value("ReadLinear.inputs:variableName", "linear", "which graph variable to read")
    plan.value("ReadAngular.inputs:variableName", "angular", "which graph variable to read")
    plan.value("Differential.inputs:wheelRadius", radius, "wheel radius in metres")
    plan.value("Differential.inputs:wheelDistance", distance, "distance between the two wheels in metres")
    plan.value("Wheels.inputs:targetPrim", [robot], "the robot (its articulation root)", target=True)
    plan.value("Wheels.inputs:jointNames", list(joints), "which joints the speeds go to, in order", array=True)


def _plan_differential_drive(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    error, robot, joints, radius, distance = _wheels(stage, plan, params)
    if error:
        return error
    try:
        speed = _number(params, "speed", 0.3)
        turn = _number(params, "turn", 1.5)
    except ValueError as exc:
        return str(exc)
    _driver(plan, robot, joints, radius, distance)
    for key, variable, value in (("W", "linear", speed), ("S", "linear", -speed), ("A", "angular", turn),
                                 ("D", "angular", -turn)):
        plan.node(f"Key{key}", "omni.graph.action.OnKeyboardInput", f"fires when {key} is pressed or released")
        plan.node(f"Set{key}", "omni.graph.core.WriteVariable", f"sets {variable} = {value:g}")
        plan.wire(f"Key{key}.outputs:pressed", f"Set{key}.inputs:execIn", "execution", f"when {key} goes down")
        plan.value(f"Key{key}.inputs:keyIn", key, "the key")
        plan.value(f"Set{key}.inputs:variableName", variable, "which variable to change")
        plan.value(f"Set{key}.inputs:value", value, "the new value", type_name="double")
    for variable, keys in (("linear", "WS"), ("angular", "AD")):
        name = f"Zero{variable.capitalize()}"
        plan.node(name, "omni.graph.core.WriteVariable", f"sets {variable} back to 0")
        for key in keys:
            plan.wire(f"Key{key}.outputs:released", f"{name}.inputs:execIn", "execution", f"when {key} comes up")
        plan.value(f"{name}.inputs:variableName", variable, "which variable to reset")
        plan.value(f"{name}.inputs:value", 0.0, "stop", type_name="double")
    plan.explanation += [
        f"While playing: W/S set linear to +/-{speed:g} m/s, A/D set angular to +/-{turn:g} rad/s; letting go sets "
        "them back to 0.",
        "Every frame the Differential Controller turns (linear, angular) into one speed per wheel: equal speeds go "
        "straight, different speeds turn. The Articulation Controller sends them to the wheel joints.",
        "Click inside the viewport after pressing Play, or the keys go to another panel.",
    ]
    return None


_DEFAULT_ROUTE = [{"linear": 0.3, "angular": 0.0, "seconds": 2.0}, {"linear": 0.0, "angular": 0.0, "seconds": 1.0},
                  {"linear": 0.0, "angular": 1.5, "seconds": 1.05}, {"linear": 0.3, "angular": 0.0, "seconds": 2.0}]


def _plan_drive_sequence(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    error, robot, joints, radius, distance = _wheels(stage, plan, params)
    if error:
        return error
    steps = params.get("steps", _DEFAULT_ROUTE)
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        return "steps must be a list of 1-12 {linear, angular, seconds} steps."
    clean = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return f"step {index} must be {{linear, angular, seconds}}."
        try:
            clean.append((_number(step, "linear", 0.0, positive=False), _number(step, "angular", 0.0, positive=False),
                          _number(step, "seconds", 1.0)))
        except ValueError as exc:
            return f"step {index}: {exc}"
    _driver(plan, robot, joints, radius, distance)
    plan.node("Start", "omni.graph.action.Once", "lets the first tick through only once")
    plan.wire("OnTick.outputs:tick", "Start.inputs:execIn", "execution", "start the route on the first frame")
    previous = "Start.outputs:once"
    for index, (linear, angular, seconds) in enumerate(clean, start=1):
        plan.node(f"Lin{index}", "omni.graph.core.WriteVariable", f"step {index}: speed {linear:g} m/s")
        plan.node(f"Ang{index}", "omni.graph.core.WriteVariable", f"step {index}: turn {angular:g} rad/s")
        plan.node(f"Wait{index}", "omni.graph.action.Delay", f"step {index}: hold for {seconds:g} s")
        plan.wire(previous, f"Lin{index}.inputs:execIn", "execution", f"begin step {index}")
        plan.wire(f"Lin{index}.outputs:execOut", f"Ang{index}.inputs:execIn", "execution", "then set the turn")
        plan.wire(f"Ang{index}.outputs:execOut", f"Wait{index}.inputs:execIn", "execution", "then wait")
        for node, variable, value in ((f"Lin{index}", "linear", linear), (f"Ang{index}", "angular", angular)):
            plan.value(f"{node}.inputs:variableName", variable, "which variable to set")
            plan.value(f"{node}.inputs:value", value, "its value for this step", type_name="double")
        plan.value(f"Wait{index}.inputs:duration", seconds, "seconds to hold this step")
        previous = f"Wait{index}.outputs:finished"
    for node, variable in (("StopLin", "linear"), ("StopAng", "angular")):
        plan.node(node, "omni.graph.core.WriteVariable", f"route done: {variable} = 0")
        plan.value(f"{node}.inputs:variableName", variable, "which variable to reset")
        plan.value(f"{node}.inputs:value", 0.0, "stop", type_name="double")
    plan.wire(previous, "StopLin.inputs:execIn", "execution", "after the last step")
    plan.wire("StopLin.outputs:execOut", "StopAng.inputs:execIn", "execution", "then stop turning too")
    route = " -> ".join(f"({lin:g} m/s, {ang:g} rad/s for {sec:g} s)" for lin, ang, sec in clean)
    plan.explanation += [
        f"When Play starts, Once lets the first frame through, and the route runs step by step: {route} -> stop.",
        "Each step writes the two variables, then a Delay holds them; the Delay's Finished output starts the next "
        "step. Meanwhile the driver part turns the variables into wheel speeds every frame.",
        "A 90 degree turn at 1.5 rad/s takes about 1.05 s (pi/2 / 1.5).",
    ]
    return None


def _plan_obstacle_stop(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    error, robot, joints, radius, distance = _wheels(stage, plan, params)
    if error:
        return error
    error, _sensor = _check_prim(stage, params.get("sensor_prim"), plan, "sensor_prim")
    if error:
        return error
    error, _obstacle = _check_prim(stage, params.get("obstacle"), plan, "obstacle")
    if error:
        return error
    try:
        stop_distance = _number(params, "stop_distance", 0.5)
        speed = _number(params, "speed", 0.3)
    except ValueError as exc:
        return str(exc)
    sensor, obstacle = str(params["sensor_prim"]), str(params["obstacle"])
    # Seen live in Isaac 6.1: the Articulation Controller empties a velocityCommand TYPED into it when Stop is
    # pressed (custom_reset), so a fixed command drove only on the first Play. The decision therefore writes the
    # 'linear' variable, and the shared driver feeds the controller through a wire every frame.
    _driver(plan, robot, joints, radius, distance)
    plan.node("RobotPose", "isaacsim.core.nodes.IsaacReadWorldPose", "where the robot is (world space)")
    plan.node("ObstaclePose", "isaacsim.core.nodes.IsaacReadWorldPose", "where the obstacle is (world space)")
    plan.node("Gap", "omni.graph.nodes.Distance3D", "how far apart they are (SENSE)")
    plan.node("TooClose", "omni.graph.nodes.Compare", f"is the gap smaller than {stop_distance:g} m? (DECIDE)")
    plan.node("Decide", "omni.graph.action.Branch", "chooses stop or go")
    plan.node("Go", "omni.graph.core.WriteVariable", f"sets linear = {speed:g} m/s (ACT)")
    plan.node("Halt", "omni.graph.core.WriteVariable", "sets linear = 0 (ACT)")
    plan.wire("OnTick.outputs:tick", "Decide.inputs:execIn", "execution", "decide every frame")
    plan.wire("RobotPose.outputs:translation", "Gap.inputs:a", "data", "robot position")
    plan.wire("ObstaclePose.outputs:translation", "Gap.inputs:b", "data", "obstacle position")
    plan.wire("Gap.outputs:distance", "TooClose.inputs:a", "data", "the gap")
    plan.wire("TooClose.outputs:result", "Decide.inputs:condition", "data", "true = too close")
    plan.wire("Decide.outputs:execTrue", "Halt.inputs:execIn", "execution", "too close: stop")
    plan.wire("Decide.outputs:execFalse", "Go.inputs:execIn", "execution", "path clear: drive")
    plan.value("RobotPose.inputs:prim", [sensor], "the part of the robot to measure from", target=True)
    plan.value("ObstaclePose.inputs:prim", [obstacle], "the obstacle", target=True)
    plan.value("TooClose.inputs:b", stop_distance, "the stopping distance in metres", type_name="double")
    plan.value("TooClose.inputs:operation", "<", "smaller than")
    for node, value in (("Go", speed), ("Halt", 0.0)):
        plan.value(f"{node}.inputs:variableName", "linear", "the forward-speed variable the driver reads")
        plan.value(f"{node}.inputs:value", value, "speed in m/s", type_name="double")
    plan.explanation += [
        f"Every frame: SENSE the gap between {sensor} and {obstacle} -> DECIDE whether it is under {stop_distance:g} m "
        f"-> ACT: set the speed variable to 0 (stop) or {speed:g} m/s (go). The driver part turns the speed into "
        "wheel speeds every frame.",
        "This measures the distance from known positions, like a perfect range sensor. A real LiDAR or camera measures "
        "the same gap from its own data (class session 10); the decide and act parts stay the same.",
        "World positions matter: a robot part's own translate is relative to the robot, so Isaac Read World Pose is used.",
        "Commands go to the Articulation Controller through a wire: it clears commands typed into it when Stop is "
        "pressed, so a typed command would only work on the first Play.",
    ]
    return None


_COLOR_SORT_SCRIPT = 'import json\nimport math\nimport omni.timeline\nimport omni.usd\nfrom pxr import Gf, Usd, UsdGeom, UsdPhysics\n\nHEADING = {}        # package path -> bin path, for packages already sorted during this Play\nARRIVED = set()     # packages that reached their bin\nLAST_TIME = [0.0]   # to notice Stop -> Play (the clock goes back to 0)\nARRIVE_RADIUS = 0.25\nWIDTH, HEIGHT = 640, 480          # camera image size (sensor_create makes 640x480, 4:3 cameras)\nSENSOR = {}                       # camera path -> CameraSensor, made on first use\nPALETTE = {"red": (0.85, 0.15, 0.12), "green": (0.15, 0.7, 0.25), "blue": (0.15, 0.35, 0.9),\n           "yellow": (0.95, 0.8, 0.1), "orange": (0.95, 0.5, 0.1), "purple": (0.55, 0.25, 0.8)}\nACTIVE_PALETTE = [PALETTE]        # the palette input (e.g. colours learned by train_colour_model) replaces it\n\n\ndef setup(db):\n    db.outputs.sorted = 0\n\n\ndef where(prim):\n    # world position, whatever the package\'s parent is\n    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()\n\n\ndef nearest_color(r, g, b):\n    # Compare the colour\'s DIRECTION (hue), not its brightness, so light and shadow do not change the answer.\n    length = math.sqrt(r * r + g * g + b * b)\n    if length < 1e-6:\n        return None\n    best, score = None, -1.0\n    for name, (pr, pg, pb) in ACTIVE_PALETTE[0].items():\n        plen = math.sqrt(pr * pr + pg * pg + pb * pb)\n        similarity = (r * pr + g * pg + b * pb) / (length * plen)\n        if similarity > score:\n            best, score = name, similarity\n    return best\n\n\ndef to_linear(value):\n    # Camera pixels are sRGB (brightened for screens); the palette is linear like displayColor. Seen live: without\n    # this, rendered blue looked almost as much like purple as blue, and blue packages were never sorted.\n    c = value / 255.0\n    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4\n\n\ndef colourful(r, g, b):\n    # Grey floor and white highlights carry no colour: ignore them instead of guessing.\n    top = max(r, g, b)\n    return top > 0.02 and (top - min(r, g, b)) / top > 0.4\n\n\ndef camera_image(stage, camera_path):\n    if camera_path not in SENSOR:\n        from isaacsim.sensors.experimental.rtx import CameraSensor, RtxCamera\n        SENSOR[camera_path] = CameraSensor(RtxCamera(camera_path), resolution=(HEIGHT, WIDTH), annotators=["rgb"])\n    rgb, _info = SENSOR[camera_path].get_data("rgb")\n    return None if rgb is None else rgb.numpy()      # (height, width, 3) values 0-255; None while warming up\n\n\ndef camera_sees(stage, camera_path, prim, image):\n    # PERCEPTION with a real camera: project the package into the picture and look at those pixels.\n    camera = UsdGeom.Camera(stage.GetPrimAtPath(camera_path)).GetCamera(Usd.TimeCode.Default())\n    frustum = camera.frustum\n    in_camera = frustum.ComputeViewMatrix().Transform(Gf.Vec3d(where(prim)))\n    if in_camera[2] >= 0:\n        return None                                    # behind the camera\n    ndc = frustum.ComputeProjectionMatrix().Transform(in_camera)\n    u, v = int((ndc[0] + 1) / 2 * WIDTH), int((1 - ndc[1]) / 2 * HEIGHT)\n    if not (2 <= u < WIDTH - 2 and 2 <= v < HEIGHT - 2):\n        return None                                    # outside the picture\n    total, used = [0.0, 0.0, 0.0], 0\n    for y in range(v - 2, v + 3):                      # look at a 5 x 5 patch of pixels\n        for x in range(u - 2, u + 3):\n            pixel = [to_linear(float(image[y][x][channel])) for channel in range(3)]\n            if colourful(*pixel):                      # skip floor / highlight pixels at the package\'s edge\n                used += 1\n                for channel in range(3):\n                    total[channel] += pixel[channel]\n    if used < 5:\n        return None                                    # not enough coloured pixels to tell\n    return nearest_color(*(value / used for value in total))\n\n\ndef see_color(stage, db, prim, image):\n    if db.inputs.camera:\n        return camera_sees(stage, db.inputs.camera, prim, image) if image is not None else None\n    # Without a camera: the package\'s colour data, like a perfect camera + classifier\n    token = prim.GetAttribute("package:color")\n    if token and token.Get():\n        return str(token.Get())\n    colors = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get() if prim.IsA(UsdGeom.Gprim) else None\n    if colors:\n        return nearest_color(*colors[0])\n    return None\n\n\ndef steer(prim, target, speed, up):\n    # ACT: push the package toward its bin; stop it once it is there\n    here = where(prim)\n    flat = Gf.Vec3d(target[0] - here[0], target[1] - here[1], 0.0)\n    velocity = UsdPhysics.RigidBodyAPI(prim).CreateVelocityAttr()\n    if flat.GetLength() <= ARRIVE_RADIUS:\n        velocity.Set(Gf.Vec3f(0.0, 0.0, -0.2))\n        return True\n    push = flat.GetNormalized() * speed\n    velocity.Set(Gf.Vec3f(push[0], push[1], up))\n    return False\n\n\ndef compute(db):\n    now = omni.timeline.get_timeline_interface().get_current_time()\n    if now < LAST_TIME[0]:          # the simulation restarted: forget the last run\n        HEADING.clear()\n        ARRIVED.clear()\n        db.outputs.sorted = 0\n    LAST_TIME[0] = now\n    stage = omni.usd.get_context().get_stage()\n    group = stage.GetPrimAtPath(db.inputs.packages)\n    if not group:\n        return True\n    routes = json.loads(db.inputs.routes or "{}")\n    learned = getattr(db.inputs, "palette", "")  # graphs built before the palette pin have none\n    ACTIVE_PALETTE[0] = json.loads(learned) if learned else PALETTE\n    low, high = db.inputs.zone_min, db.inputs.zone_max\n    image = camera_image(stage, db.inputs.camera) if db.inputs.camera else None\n    for prim in group.GetChildren():\n        path = str(prim.GetPath())\n        if path in ARRIVED or not prim.HasAPI(UsdPhysics.RigidBodyAPI):\n            continue\n        if path in HEADING:          # already sorted: keep steering it to its bin\n            bin_prim = stage.GetPrimAtPath(HEADING[path])\n            if bin_prim and steer(prim, where(bin_prim), db.inputs.speed, -0.2):\n                ARRIVED.add(path)\n            continue\n        here = where(prim)\n        if not all(low[i] <= here[i] <= high[i] for i in range(3)):\n            continue                 # not in the detection zone yet\n        color = see_color(stage, db, prim, image)   # SENSE\n        bin_path = routes.get(color)                # DECIDE\n        if not bin_path or not stage.GetPrimAtPath(bin_path):\n            continue\n        HEADING[path] = bin_path\n        db.outputs.last_seen = f"{prim.GetName()}: {color} -> {bin_path}"\n        steer(prim, where(stage.GetPrimAtPath(bin_path)), db.inputs.speed, db.inputs.hop)  # ACT (first push hops)\n        db.outputs.sorted = db.outputs.sorted + 1\n    return True\n\n\ndef cleanup(db):\n    HEADING.clear()\n    ARRIVED.clear()\n    SENSOR.clear()\n'


def _plan_color_sort(stage: Any, plan: _Plan, params: dict[str, Any]) -> str | None:
    packages = params.get("packages", "/World/Packages")
    error, group = _check_prim(stage, packages, plan, "packages")
    if error:
        return error
    bins = params.get("bins")
    if not isinstance(bins, dict) or not bins or not all(isinstance(k, str) and isinstance(v, str) for k, v in bins.items()):
        return "bins must map colour names to bin prim paths, e.g. {'red': '/World/BinA', 'blue': '/World/BinB'}."
    for colour, path in bins.items():
        error, _bin = _check_prim(stage, path, plan, f"bin for {colour}")
        if error:
            return error
    corners = []
    for key in ("zone_min", "zone_max"):
        value = params.get(key)
        if (not isinstance(value, (list, tuple)) or len(value) != 3
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            return f"{key} must be three numbers [x, y, z] (world)."
        corners.append([float(v) for v in value])
    if any(lo >= hi for lo, hi in zip(*corners)):
        return "zone_max must be bigger than zone_min on every axis."
    try:
        speed = _number(params, "speed", 1.5)
        hop = _number(params, "hop", 1.0, positive=False)
    except ValueError as exc:
        return str(exc)
    if group is not None:
        colours = {str(child.GetAttribute("package:color").Get()) for child in group.GetChildren()
                   if child.GetAttribute("package:color") and child.GetAttribute("package:color").Get()}
        unsorted = sorted(colours - set(bins))
        if unsorted:
            plan.warnings.append(f"Packages come in {sorted(colours)}, but no bin is given for {unsorted}: they ride on.")
    palette = params.get("palette")
    if palette is not None:
        from .colour_model import load_palette

        try:
            palette = load_palette(palette)
        except (OSError, ValueError) as exc:
            return f"palette: {exc}"
        missing = sorted(set(bins) - set(palette))
        if missing:
            return f"The palette has no colour for {missing}; its classes are {sorted(palette)}."
    camera = params.get("camera") or ""
    if camera:
        error, camera_prim = _check_prim(stage, camera, plan, "camera")
        if error:
            return error
        if camera_prim is not None and camera_prim.GetTypeName() != "Camera":
            return f"{camera} is a {camera_prim.GetTypeName()}, not a Camera."
        plan.warnings.append("Camera mode reads the picture: aim the camera at the detection zone, keep the scene lit "
                             "(a dome light), and give it a few frames to warm up after Play.")
    import json as _json
    plan.node("OnTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs")
    plan.node("Sorter", "omni.graph.scriptnode.ScriptNode", "checks each package: in the zone? which colour? push")
    for pin, kind, why in (("inputs:packages", "token", "the package group"),
                           ("inputs:routes", "string", "colour -> bin path, as JSON text"),
                           ("inputs:zone_min", "double[3]", "detection zone corner"),
                           ("inputs:zone_max", "double[3]", "detection zone opposite corner"),
                           ("inputs:speed", "double", "push speed"), ("inputs:hop", "double", "small upward speed"),
                           ("inputs:camera", "token", "camera to read colours from (empty: colour data)"),
                           ("inputs:palette", "string", "learned colours as JSON (empty: the built-in palette)"),
                           ("outputs:sorted", "int", "how many packages were sorted this Play"),
                           ("outputs:last_seen", "string", "the last decision, e.g. 'Package_003: blue -> /World/BinA'")):
        plan.attribute(f"Sorter.{pin}", kind, why)
    plan.wire("OnTick.outputs:tick", "Sorter.inputs:execIn", "execution", "check the packages every frame")
    plan.value("Sorter.inputs:script", _COLOR_SORT_SCRIPT, "the sorting code (setup/compute/cleanup)")
    plan.value("Sorter.inputs:packages", str(packages), "which group to watch")
    plan.value("Sorter.inputs:routes", _json.dumps(bins), "which bin each colour goes to")
    plan.value("Sorter.inputs:zone_min", corners[0], "detection zone corner")
    plan.value("Sorter.inputs:zone_max", corners[1], "detection zone opposite corner")
    plan.value("Sorter.inputs:speed", speed, "push speed in m/s")
    plan.value("Sorter.inputs:hop", hop, "upward speed in m/s")
    plan.value("Sorter.inputs:camera", str(camera), "camera mode" if camera else "colour-data mode")
    plan.value("Sorter.inputs:palette", _json.dumps(palette) if palette else "",
               "learned colours" if palette else "built-in palette")
    routes = ", ".join(f"{colour} -> {path}" for colour, path in bins.items())
    plan.explanation += [
        f"Every frame while playing, for each package in {packages} that is inside the zone {corners[0]} .. "
        f"{corners[1]}: SENSE its colour -> DECIDE its bin ({routes}) -> ACT: push it toward that bin at "
        f"{speed:g} m/s (a {hop:g} m/s hop first) and keep steering it every frame until it is within 0.25 m of the "
        "bin, then stop it. Each package is sorted once per Play.",
        (f"Perception: the camera {camera} takes a picture each frame; each package's 3D position is projected into "
         "it, and the coloured pixels of a 5x5 patch (converted from sRGB to linear) are classified by hue."
         if camera else
         "Perception here reads the package's colour data, like a perfect camera + classifier. Pass camera=<a Camera "
         "prim> to read the colours from a real camera picture instead (class session 13)."),
        "Open the Sorter node's Script field to read or change the code: compute() runs every frame.",
    ]
    return None


_PLANNERS = {
    "key_sets_variant": _plan_key_sets_variant,
    "event_sets_variant": _plan_event_sets_variant,
    "on_tick_rotate": _plan_on_tick_rotate,
    "print_on_play": _plan_print_on_play,
    "keyboard_velocity": _plan_keyboard_velocity,
    "print_attribute": _plan_print_attribute,
    "conveyor": _plan_conveyor,
    "differential_drive": _plan_differential_drive,
    "drive_sequence": _plan_drive_sequence,
    "obstacle_stop": _plan_obstacle_stop,
    "color_sort": _plan_color_sort,
}


def plan_recipe(name: str, *, stage: Any = None, graph_path: str | None = None, **params: Any) -> dict[str, Any]:
    """A dry-run plan for one recipe; nothing is created. With stage, prim paths and variants are checked via pxr.

    Returns {ok, dry_run: True, recipe, graph_path, nodes, attributes, connections, values, explanation, checks,
    warnings, python} where python is a beginner-commented og.Controller.edit script for Kit's Script Editor.
    """
    _require_pxr()
    if name not in RECIPES:
        close = difflib.get_close_matches(str(name), list(RECIPES), n=1)
        return _fail(f"Unknown recipe {name!r}." + (f" Did you mean {close[0]!r}?" if close else ""),
                     recipes=list(RECIPES))
    allowed = set(RECIPES[name]["params"]) - {"graph_path"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        return _fail(f"Unknown parameter(s) for {name}: {unknown}. It takes {sorted(allowed)}.", recipe=name)
    missing = [key for key in RECIPES[name]["required"] if params.get(key) in (None, "")]
    if missing:
        return _fail(f"{name} needs {missing}.", recipe=name, params=RECIPES[name]["params"])
    error, final_path = _graph_path_problem(stage, graph_path or _DEFAULT_GRAPH[name], explicit=graph_path is not None)
    if error:
        return _fail(error, recipe=name)
    plan = _Plan(name, final_path)
    error = _PLANNERS[name](stage, plan, params)
    if error:
        return _fail(error, recipe=name, checks=plan.checks)
    if stage is None:
        plan.warnings.append("Planned without a stage: nothing was validated against your scene.")
    if final_path != (graph_path or _DEFAULT_GRAPH[name]):
        plan.warnings.append(f"{graph_path or _DEFAULT_GRAPH[name]} was taken, so the new graph goes to {final_path}.")
    result = {
        "ok": True,
        "dry_run": True,
        "recipe": name,
        "summary": RECIPES[name]["summary"],
        "graph_path": final_path,
        "graph": {"graph_path": final_path, "evaluator_name": "execution"},
        "nodes": plan.nodes,
        "attributes": plan.attributes,
        "variables": plan.variables,
        "connections": plan.connections,
        "values": plan.values,
        "explanation": plan.explanation,
        "checks": plan.checks,
        "warnings": plan.warnings,
        "validated": stage is not None,
        "python": _python_script(plan),
        "how_to_run": "Inside Kit: apply_recipe(stage, name, dry_run=False, ...) - or paste 'python' into "
        "Window > Script Editor. One Ctrl+Z undoes the whole edit.",
    }
    return result


def _python_script(plan: _Plan) -> str:
    """A beginner-commented og.Controller.edit script equivalent to the plan."""
    lines = [
        f"# Recipe {plan.recipe!r}: builds a new Action Graph at {plan.graph_path}",
        "# Run it in Kit: Window > Script Editor, paste, press Run. Ctrl+Z undoes the whole edit at once.",
        "# API: og.Controller.edit(graph, edits) from omni.graph.core (omni.graph 1.142.5, Kit 110),",
        "#      see omni/graph/core/_impl/controller.py in the omni.graph extension.",
        "import omni.graph.core as og",
    ]
    if any(item.get("target") for item in plan.values):
        lines.append("import usdrt  # prim pickers ('target' inputs) take a list of usdrt.Sdf.Path values")
    lines += [
        "",
        "keys = og.Controller.Keys  # names of the edit sections: CREATE_NODES, CONNECT, SET_VALUES, ...",
        "",
        "(graph, nodes, _, _) = og.Controller.edit(",
        "    # Which graph: a dict means 'create this graph'. 'execution' makes it an Action Graph.",
        f"    {{\"graph_path\": {plan.graph_path!r}, \"evaluator_name\": \"execution\"}},",
        "    {",
        "        # Step 1. The nodes: (short name inside the graph, node type id)",
        "        keys.CREATE_NODES: [",
    ]
    for node in plan.nodes:
        lines.append(f"            ({node['name']!r}, {node['type']!r}),  # {node['why']}")
    lines.append("        ],")
    if plan.variables:
        lines.append("        # Graph variables: (name, type, starting value); edit them live in the graph's Variables panel")
        lines.append("        keys.CREATE_VARIABLES: [")
        for item in plan.variables:
            default = tuple(item["default"]) if isinstance(item["default"], list) else item["default"]
            lines.append(f"            ({item['name']!r}, {item['type']!r}, {default!r}),  # {item['why']}")
        lines.append("        ],")
    if plan.attributes:
        lines.append("        # Step 2. Extra (dynamic) attributes: the name decides input or output")
        lines.append("        keys.CREATE_ATTRIBUTES: [")
        for item in plan.attributes:
            lines.append(f"            ({item['attribute']!r}, {item['type']!r}),  # {item['why']}")
        lines.append("        ],")
    step = 3 if plan.attributes else 2
    lines.append(f"        # Step {step}. The wires: (from an output, to an input)")
    lines.append("        keys.CONNECT: [")
    for item in plan.connections:
        lines.append(f"            ({item['from']!r}, {item['to']!r}),  # {item['why']}")
    lines.append("        ],")
    lines.append(f"        # Step {step + 1}. Values typed into inputs (a third item names the type for 'any'-type inputs)")
    lines.append("        keys.SET_VALUES: [")
    for item in plan.values:
        if item.get("target"):
            value = "[" + ", ".join(f"usdrt.Sdf.Path({path!r})" for path in item["value"]) + "]"
        else:
            value = repr(tuple(item["value"]) if isinstance(item["value"], list) and not item.get("array")
                         else item["value"])
        typed = f", {item['type']!r}" if item.get("type") else ""
        lines.append(f"            ({item['attribute']!r}, {value}{typed}),  # {item['why']}")
    lines += [
        "        ],",
        "    },",
        ")",
        "print('Created', [node.get_prim_path() for node in nodes])",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ applying (Kit only)


def _kit_modules() -> dict[str, Any]:
    """{'og': omni.graph.core or None, 'live_stage': the Kit USD context's stage or None, 'usdrt': module or None}.

    Imported lazily on every call; tests replace this function with a fake.
    """
    found: dict[str, Any] = {"og": None, "live_stage": None, "usdrt": None}
    try:
        found["og"] = importlib.import_module("omni.graph.core")
    except Exception:  # noqa: BLE001 - ImportError outside Kit, or an extension that fails while loading
        return found
    try:
        usd = importlib.import_module("omni.usd")
        found["live_stage"] = usd.get_context().get_stage()
    except Exception:  # noqa: BLE001
        found["live_stage"] = None
    try:
        found["usdrt"] = importlib.import_module("usdrt")
    except Exception:  # noqa: BLE001
        found["usdrt"] = None
    return found


def _same_stage(stage: Any, live: Any) -> bool:
    if stage is live:
        return True
    try:
        return stage.GetRootLayer().identifier == live.GetRootLayer().identifier
    except Exception:  # noqa: BLE001
        return False


def _build_edit(og: Any, usdrt: Any, plan: dict[str, Any]) -> dict[str, Any]:
    keys = og.Controller.Keys
    edit: dict[Any, Any] = {keys.CREATE_NODES: [(node["name"], node["type"]) for node in plan["nodes"]]}
    if plan.get("variables"):
        # OmniGraph rejects a list default for a tuple type (seen live); a tuple works.
        edit[keys.CREATE_VARIABLES] = [(item["name"], item["type"], tuple(item["default"])
                                        if isinstance(item["default"], list) else item["default"])
                                       for item in plan["variables"]]
    if plan["attributes"]:
        edit[keys.CREATE_ATTRIBUTES] = [(item["attribute"], item["type"]) for item in plan["attributes"]]
    edit[keys.CONNECT] = [(item["from"], item["to"]) for item in plan["connections"]]
    values = []
    for item in plan["values"]:
        value = item["value"]
        if item.get("target"):
            make = getattr(getattr(usdrt, "Sdf", None), "Path", None) if usdrt is not None else None
            value = [make(path) if callable(make) else path for path in value]
        elif isinstance(value, list) and not item.get("array"):
            # Seen live in Isaac 6.1: og refuses a list for a tuple input such as the conveyor's float[3] direction.
            # Arrays (token[], double[]) keep their list.
            value = tuple(value)
        values.append((item["attribute"], value, item["type"]) if item.get("type") else (item["attribute"], value))
    edit[keys.SET_VALUES] = values
    return edit


def apply_recipe(stage: Any, name: str, *, dry_run: bool = True, graph_path: str | None = None,
                 **params: Any) -> dict[str, Any]:
    """Plan a recipe against stage and, only with dry_run=False and only inside Kit, build it via og.Controller.edit.

    Must run on Kit's main thread. Refuses when stage is not Kit's open stage, and never edits an existing graph:
    every recipe creates a new graph prim (undoable as one step). Returns the plan (dry run) or
    {ok, dry_run: False, graph_path, created, problems, ...}.
    """
    plan = plan_recipe(name, stage=stage, graph_path=graph_path, **params)
    if not plan.get("ok") or dry_run:
        return plan
    kit = _kit_modules()
    og = kit.get("og")
    if og is None:
        return {"ok": False, "dry_run": False, "message": _NOT_KIT,
                "detail": "omni.graph.core is not importable here; run this inside USD Composer or Isaac Sim, or "
                "paste the plan's python into Kit's Script Editor.", "plan": plan}
    live = kit.get("live_stage")
    if live is None or not _same_stage(stage, live):
        return {"ok": False, "dry_run": False, "error": "The given stage is not the stage open in Kit; OmniGraph only "
                "edits Kit's open stage, so nothing was changed.", "plan": plan}
    edit = _build_edit(og, kit.get("usdrt"), plan)
    try:
        result = og.Controller.edit(dict(plan["graph"]), edit)
    except Exception as exc:  # noqa: BLE001 - og.OmniGraphError and friends
        # Seen live in Isaac 6.1: a failed value left a half-built graph. The path was new (plan_recipe never reuses
        # one), so removing it touches nothing of the user's.
        removed = False
        try:
            if stage.GetPrimAtPath(plan["graph_path"]):
                removed = bool(stage.RemovePrim(plan["graph_path"]))
        except Exception:  # noqa: BLE001
            removed = False
        return {"ok": False, "dry_run": False, "error": f"og.Controller.edit failed: {type(exc).__name__}: {exc}",
                "note": ("The partly built graph was removed; nothing is left behind." if removed else
                         "OmniGraph may have left a partly built graph; press Ctrl+Z once to remove it."),
                "plan": plan}
    created = []
    try:
        for node in result[1]:
            get_path = getattr(node, "get_prim_path", None)
            created.append(str(get_path()) if callable(get_path) else str(node))
    except Exception:  # noqa: BLE001
        created = []
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "recipe": name,
        "graph_path": plan["graph_path"],
        "created": created,
        "explanation": plan["explanation"],
        "warnings": plan["warnings"],
        "message": f"Built {name} at {plan['graph_path']}. Ctrl+Z undoes it in one step; nothing was saved.",
    }
    graph_prim = stage.GetPrimAtPath(plan["graph_path"])
    if graph_prim and graph_prim.IsValid():
        check = inspect_graphs(stage, plan["graph_path"])
        report["flow"] = [line for graph in check.get("graphs", []) for line in graph["flow"]]
        report["problems"] = check.get("problems", [])
    else:
        report["note"] = "The graph prim is not visible on the stage yet; run inspect_graphs after the next update."
    return report
