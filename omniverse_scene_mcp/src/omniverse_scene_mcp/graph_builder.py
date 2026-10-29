"""Build an Action Graph one step at a time, the way a class does it: make a graph, add a node, connect two pins,
set a value, add a graph variable, delete a node. Plus a keyword search over every node the installed Isaac Sim offers.

Why step by step: recipes build a whole graph at once, which is quick but teaches little. Here each call is one
small, previewed change (dry_run=True by default) with the matching og.Controller.edit Python, so the learner sees
both the canvas and the code. Edits run inside Kit on the open stage (omni.graph.core); Ctrl+Z undoes each one.

Node search reads the per-extension ogn/nodes.json indexes of the local Isaac Sim install (about 40 small files, read
once), so it works offline and without Kit.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import omnigraph_helper as ogh
from .exam.progress import workspace_root

ISAAC_FOLDER = "isaacsim-6.1.0"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PIN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.((?:inputs|outputs|state):[A-Za-z0-9_:]+)$")
VARIABLE_TYPES = ("bool", "int", "float", "double", "token", "string", "float[3]", "double[3]", "int[]", "float[]",
                  "double[]", "token[]", "path", "matrixd[4]")


def og_value(value: Any) -> Any:
    """A JSON list as OmniGraph wants a tuple value. Seen live in Isaac 6.1: a float[3] variable default given as a
    list failed with "type is not compatible with <class 'list'>" and left a half-made variable behind."""
    return tuple(og_value(item) for item in value) if isinstance(value, list) else value


# --------------------------------------------------------------------------------------------------- node search
@lru_cache(maxsize=1)
def _node_index() -> tuple[dict[str, Any], ...]:
    root = workspace_root() / ISAAC_FOLDER
    rows: list[dict[str, Any]] = []
    for pattern in ("exts/*/ogn/nodes.json", "extscache/*/ogn/nodes.json"):
        for path in sorted(root.glob(pattern)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            extension = str(data.get("extension") or path.parent.parent.name.split("-")[0])
            for key, info in (data.get("nodes") or {}).items():
                node_id = key if "." in key else f"{extension}.{key}"
                description = info.get("description", "")
                if isinstance(description, list):
                    description = " ".join(description)
                rows.append({"id": node_id, "name": info.get("uiName") or key.split(".")[-1],
                             "description": str(description).strip(), "extension": extension})
    return tuple(rows)


def node_search(keyword: str, limit: int = 15) -> dict[str, Any]:
    """Find Action Graph nodes by keyword ('keyboard', 'conveyor', 'print', 'velocity') in the installed Isaac Sim."""
    if not isinstance(keyword, str) or not keyword.strip():
        return {"ok": False, "error": "Give a keyword such as 'keyboard', 'conveyor' or 'print'."}
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        return {"ok": False, "error": "limit must be 1 to 50."}
    index = _node_index()
    if not index:
        return {"ok": False, "error": f"No node indexes found under {ISAAC_FOLDER}/exts or extscache."}
    words = keyword.lower().split()
    scored = []
    for row in index:
        name, text = row["name"].lower(), f"{row['id']} {row['name']} {row['description']}".lower()
        if all(word in text for word in words):
            score = sum(3 if word in name else 2 if word in row["id"].lower() else 1 for word in words)
            scored.append((-score, row["id"], row))
    scored.sort()
    hits = [{**row, "explained": row["id"] in ogh.NODE_TYPES} for _, _, row in scored[:limit]]
    return {"ok": True, "keyword": keyword, "total_nodes": len(index), "matches": len(scored), "nodes": hits,
            "next": "graph_node_type(type_id) explains a node in plain words (when known); graph_add_node adds one."}


def _known_type(node_type: str) -> bool:
    return node_type in ogh.NODE_TYPES or any(row["id"] == node_type for row in _node_index())


# --------------------------------------------------------------------------------------------------- Kit plumbing
def _kit(stage: Any) -> tuple[Any, dict[str, Any] | None]:
    kit = ogh._kit_modules()
    og = kit.get("og")
    if og is None:
        return None, {"ok": False, "dry_run": False, "message": ogh._NOT_KIT,
                      "detail": "Graph edits run inside Isaac Sim or USD Composer (omni.graph.core)."}
    live = kit.get("live_stage")
    if live is None or not ogh._same_stage(stage, live):
        return None, {"ok": False, "dry_run": False, "error": "This stage is not the one open in Kit; nothing was "
                                                              "changed."}
    return og, None


def _graph_prim(stage: Any, graph_path: str) -> Any:
    prim = stage.GetPrimAtPath(graph_path) if stage is not None else None
    return prim if prim and prim.IsValid() and prim.GetTypeName() == "OmniGraph" else None


def _node_prim(stage: Any, graph_path: str, name: str) -> Any:
    prim = stage.GetPrimAtPath(f"{graph_path}/{name}")
    return prim if prim and prim.IsValid() and prim.GetTypeName() in ogh.NODE_PRIM_TYPES else None


def _describe_node(og: Any, path: str) -> dict[str, Any]:
    """The node's pins as og sees them: inputs and outputs with their types (what you can connect)."""
    try:
        node = og.Controller.node(path)
        pins: dict[str, list[str]] = {"inputs": [], "outputs": []}
        for attribute in node.get_attributes():
            name = attribute.get_name()
            kind = "inputs" if name.startswith("inputs:") else "outputs" if name.startswith("outputs:") else None
            if kind:
                type_name = attribute.get_resolved_type().get_ogn_type_name()
                pins[kind].append(f"{name} ({type_name})")
        return pins
    except Exception as exc:  # noqa: BLE001 - a description is a bonus, never a failure
        return {"note": f"Pins could not be listed: {type(exc).__name__}"}


def _full(graph_path: str, pin: str) -> str:
    """NodeName.inputs:x as /Graph/NodeName.inputs:x. Seen live: og.Controller.edit on an EXISTING graph cannot look
    up short names ("node=None, graph=None"); they only work in the same edit that creates the node."""
    return pin if pin.startswith("/") else f"{graph_path}/{pin}"


def _kit_value(value: Any) -> Any:
    """Prim paths for a prim-picker (target) pin become usdrt.Sdf.Path objects, as og expects; lists become tuples."""
    if isinstance(value, list) and value and all(isinstance(v, str) and v.startswith("/") for v in value):
        usdrt = ogh._kit_modules().get("usdrt")
        make = getattr(getattr(usdrt, "Sdf", None), "Path", None) if usdrt is not None else None
        if callable(make):
            return [make(v) for v in value]
        return value
    return og_value(value)


def _run(og: Any, stage: Any, graph_path: str, edits: dict[str, Any], message: str,
         describe: str | None = None) -> dict[str, Any]:
    keys = og.Controller.Keys
    real = {getattr(keys, key.upper()): value for key, value in edits.items()}
    try:
        og.Controller.edit(graph_path, real)
    except Exception as exc:  # noqa: BLE001 - og.OmniGraphError and friends
        return {"ok": False, "dry_run": False, "error": f"og.Controller.edit failed: {type(exc).__name__}: {exc}"}
    result = {"ok": True, "dry_run": False, "graph_path": graph_path, "message": message + " Ctrl+Z undoes it; "
              "nothing was saved."}
    if describe:
        result["pins"] = _describe_node(og, describe)
    check = ogh.inspect_graphs(stage, graph_path)
    result["flow"] = [line for graph in check.get("graphs", []) for line in graph.get("flow", [])]
    result["problems"] = check.get("problems", [])
    return result


def _python(graph_path: str, key: str, item: str) -> str:
    return ("import omni.graph.core as og\nkeys = og.Controller.Keys\n"
            f"og.Controller.edit({graph_path!r}, {{keys.{key.upper()}: [{item}]}})\n")


def _preview(plan: dict[str, Any], python: str, warnings: list[str]) -> dict[str, Any]:
    return {"ok": True, "dry_run": True, "written": False, "would": plan, "python": python, "warnings": warnings,
            "next_step": "Dry run: nothing changed. Repeat with dry_run=False (or through scene_safe_edit)."}


def _check_graph(stage: Any, graph_path: str) -> str | None:
    if not isinstance(graph_path, str) or not graph_path.startswith("/"):
        return "graph_path must be an absolute prim path such as /World/ActionGraph."
    if _graph_prim(stage, graph_path) is None:
        return f"There is no Action Graph at {graph_path}. Make one first with graph_create."
    return None


# --------------------------------------------------------------------------------------------------- the steps
def create_graph(stage: Any, graph_path: str = "/World/ActionGraph", *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or with dry_run=False make) an empty Action Graph: the canvas the nodes go on."""
    if not isinstance(graph_path, str) or not graph_path.startswith("/") or "//" in graph_path:
        return {"ok": False, "error": "graph_path must be an absolute prim path such as /World/ActionGraph."}
    existing = stage.GetPrimAtPath(graph_path)
    if existing and existing.IsValid():
        return {"ok": False, "error": f"{graph_path} already exists; pick another path or add nodes to it."}
    plan = {"graph_path": graph_path, "evaluator": "execution",
            "explained": "An execution graph runs only when an event node (On Tick, On Keyboard Input...) fires."}
    python = ("import omni.graph.core as og\n"
              f"og.Controller.edit({{'graph_path': {graph_path!r}, 'evaluator_name': 'execution'}}, {{}})\n")
    if dry_run is not False:
        return _preview(plan, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    try:
        og.Controller.edit({"graph_path": graph_path, "evaluator_name": "execution"}, {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "error": f"og.Controller.edit failed: {type(exc).__name__}: {exc}"}
    return {"ok": True, "dry_run": False, "graph_path": graph_path, "python": python,
            "message": f"Made an empty Action Graph at {graph_path}. Open it: right-click it in the Stage panel > "
                       "Open Graph. Ctrl+Z undoes it."}


def add_node(stage: Any, graph_path: str, name: str, node_type: str, *, values: dict[str, Any] | None = None,
             dry_run: bool = True) -> dict[str, Any]:
    """Preview (or add) one node of node_type named `name`, optionally setting input values (pin name -> value)."""
    problem = _check_graph(stage, graph_path)
    if problem:
        return {"ok": False, "error": problem}
    if not isinstance(name, str) or not _NAME.match(name):
        return {"ok": False, "error": "name must be a simple word such as OnTick or WriteVelocity (letters, digits, _)."}
    if _node_prim(stage, graph_path, name) is not None:
        return {"ok": False, "error": f"{graph_path} already has a node named {name}."}
    if not isinstance(node_type, str) or not _known_type(node_type):
        hits = node_search(str(node_type).split(".")[-1], limit=5).get("nodes", []) if isinstance(node_type, str) else []
        return {"ok": False, "error": f"Unknown node type {node_type!r}.",
                "did_you_mean": [row["id"] for row in hits], "hint": "graph_node_search('keyword') finds node ids."}
    values = dict(values or {})
    bad = [pin for pin in values if not str(pin).startswith("inputs:")]
    if bad:
        return {"ok": False, "error": f"values keys must be input pins such as 'inputs:velocity', not {bad}."}
    known = ogh.NODE_TYPES.get(node_type) or {}
    plan = {"graph_path": graph_path, "node": f"{graph_path}/{name}", "type": node_type,
            "what_it_does": known.get("plain", ""), "values": values}
    python = _python(graph_path, "create_nodes", f"({name!r}, {node_type!r})")
    if values:
        python += _python(graph_path, "set_values", ", ".join(f"({name + '.' + pin!r}, {v!r})" for pin, v in values.items()))
    if dry_run is not False:
        return _preview(plan, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    edits: dict[str, Any] = {"create_nodes": [(name, node_type)]}
    if values:
        edits["set_values"] = [(f"{name}.{pin}", _kit_value(value)) for pin, value in values.items()]
    result = _run(og, stage, graph_path, edits, f"Added {name} ({node_type}).", describe=f"{graph_path}/{name}")
    return {**result, "python": python}


def _check_pin(stage: Any, graph_path: str, pin: str) -> str | None:
    match = _PIN.match(pin) if isinstance(pin, str) else None
    if not match:
        return f"{pin!r} must look like NodeName.outputs:tick or NodeName.inputs:execIn."
    if _node_prim(stage, graph_path, match.group(1)) is None:
        return f"{graph_path} has no node named {match.group(1)}."
    return None


def connect(stage: Any, graph_path: str, source: str, target: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or draw) one wire from an output pin to an input pin, e.g. OnTick.outputs:tick -> Write.inputs:execIn."""
    problem = _check_graph(stage, graph_path) or _check_pin(stage, graph_path, source) or _check_pin(stage, graph_path, target)
    if problem:
        return {"ok": False, "error": problem}
    if ".inputs:" in source or ".outputs:" in target:
        return {"ok": False, "error": "Wires go FROM an output pin TO an input pin (source outputs:..., target inputs:...)."}
    plan = {"graph_path": graph_path, "from": source, "to": target,
            "explained": "Execution pins (white) decide WHEN a node runs; data pins carry values. Types must match."}
    python = _python(graph_path, "connect", f"({_full(graph_path, source)!r}, {_full(graph_path, target)!r})")
    if dry_run is not False:
        return _preview(plan, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    wire = (_full(graph_path, source), _full(graph_path, target))
    return {**_run(og, stage, graph_path, {"connect": [wire]}, f"Connected {source} -> {target}."),
            "python": python}


def disconnect(stage: Any, graph_path: str, source: str, target: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or remove) one wire between two pins."""
    problem = _check_graph(stage, graph_path) or _check_pin(stage, graph_path, source) or _check_pin(stage, graph_path, target)
    if problem:
        return {"ok": False, "error": problem}
    python = _python(graph_path, "disconnect", f"({_full(graph_path, source)!r}, {_full(graph_path, target)!r})")
    if dry_run is not False:
        return _preview({"graph_path": graph_path, "remove": f"{source} -> {target}"}, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    wire = (_full(graph_path, source), _full(graph_path, target))
    return {**_run(og, stage, graph_path, {"disconnect": [wire]}, f"Removed {source} -> {target}."),
            "python": python}


def set_value(stage: Any, graph_path: str, pin: str, value: Any, *, value_type: str | None = None,
              dry_run: bool = True) -> dict[str, Any]:
    """Preview (or set) one input pin's value. value_type ('float[3]', 'double', 'bool'...) resolves a flexible pin
    such as Write Prim Attribute's inputs:value; without it, a flexible pin stays 'unresolved' and the node errors."""
    problem = _check_graph(stage, graph_path) or _check_pin(stage, graph_path, pin)
    if problem:
        return {"ok": False, "error": problem}
    if ".inputs:" not in pin:
        return {"ok": False, "error": "Only input pins take values (NodeName.inputs:...)."}
    item = (pin, value, value_type) if value_type else (pin, value)
    python = _python(graph_path, "set_values", repr((_full(graph_path, pin), *item[1:])))
    warnings = [] if value_type or not isinstance(value, list) else [
        "A list on a flexible pin may need value_type (e.g. 'float[3]' for a velocity) to avoid an 'unresolved type'."]
    if dry_run is not False:
        return _preview({"graph_path": graph_path, "pin": pin, "value": value, "value_type": value_type}, python, warnings)
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    live_item = (_full(graph_path, pin), _kit_value(value), *item[2:])
    values = [live_item]
    note = f"Set {pin} = {value!r}."
    node_name, pin_name = pin.split(".", 1)
    node = _node_prim(stage, graph_path, node_name)
    kind = node.GetAttribute("node:type").Get() if node is not None and node.GetAttribute("node:type") else None
    if kind == "omni.graph.scriptnode.ScriptNode" and pin_name in ("inputs:script", "inputs:scriptPath", "inputs:usePath"):
        # Seen live in Isaac 6.1: a Script Node compiles its code once and keeps it until its
        # state:omni_initialized flag is cleared (the node's Reset button), so new code was silently ignored.
        values.append((_full(graph_path, f"{node_name}.state:omni_initialized"), False))
        note += " The Script Node was reset, so the new code compiles on the next frame."
    return {**_run(og, stage, graph_path, {"set_values": values}, note), "python": python}


def add_variable(stage: Any, graph_path: str, name: str, value_type: str, *, default: Any = None,
                 dry_run: bool = True) -> dict[str, Any]:
    """Preview (or add) a graph variable: a named value every node in the graph can read (Read Variable) or change
    (Write Variable), and that you can edit while the simulation runs, like the class's 'forward' = (5, 0, 0)."""
    problem = _check_graph(stage, graph_path)
    if problem:
        return {"ok": False, "error": problem}
    if not isinstance(name, str) or not _NAME.match(name):
        return {"ok": False, "error": "name must be a simple word such as forward or speed."}
    if value_type not in VARIABLE_TYPES:
        return {"ok": False, "error": f"value_type must be one of {', '.join(VARIABLE_TYPES)}."}
    item = (name, value_type, og_value(default)) if default is not None else (name, value_type)
    python = _python(graph_path, "create_variables", repr(item))
    plan = {"graph_path": graph_path, "variable": name, "type": value_type, "default": default,
            "how_to_use": f"Add a node of type omni.graph.core.ReadVariable with inputs:variableName = {name!r}."}
    if dry_run is not False:
        return _preview(plan, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    return {**_run(og, stage, graph_path, {"create_variables": [item]}, f"Added variable {name} ({value_type})."),
            "python": python}


def delete_node(stage: Any, graph_path: str, name: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or delete) one node and all its wires."""
    problem = _check_graph(stage, graph_path)
    if problem:
        return {"ok": False, "error": problem}
    if not isinstance(name, str) or _node_prim(stage, graph_path, name) is None:
        return {"ok": False, "error": f"{graph_path} has no node named {name!r}."}
    python = _python(graph_path, "delete_nodes", repr(f"{graph_path}/{name}"))
    if dry_run is not False:
        return _preview({"graph_path": graph_path, "delete": f"{graph_path}/{name}"}, python,
                        ["Its wires go too."])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    return {**_run(og, stage, graph_path, {"delete_nodes": [f"{graph_path}/{name}"]}, f"Deleted {name}."),
            "python": python}


# --------------------------------------------------------------------------------------------------- script nodes
SCRIPT_TEMPLATES: dict[str, dict[str, Any]] = {
    "move_on_trigger": {
        "about": "Each time it is triggered, moves a prim by an offset (class session 8's practical test).",
        "pins": {"inputs:prim_path": "token", "inputs:offset": "double[3]"},
        "defaults": {"inputs:offset": [0.0, 0.0, 0.5]},
        "code": (
            "import omni.usd\n"
            "from pxr import Gf, UsdGeom\n"
            "\n\n"
            "def setup(db):\n"
            "    # Runs once, when the graph starts (like Arduino's setup).\n"
            "    pass\n"
            "\n\n"
            "def compute(db):\n"
            "    # Runs every time an execution wire triggers this node.\n"
            "    stage = omni.usd.get_context().get_stage()\n"
            "    prim = stage.GetPrimAtPath(db.inputs.prim_path)\n"
            "    if not prim or not prim.IsValid():\n"
            "        db.log_warning(f'No prim at {db.inputs.prim_path!r}: check the prim_path input.')\n"
            "        return False\n"
            "    xform = UsdGeom.XformCommonAPI(prim)\n"
            "    translation = xform.GetXformVectors(0)[0]          # where it is now\n"
            "    dx, dy, dz = db.inputs.offset                      # how far to move it\n"
            "    xform.SetTranslate(Gf.Vec3d(translation[0] + dx, translation[1] + dy, translation[2] + dz))\n"
            "    return True                                        # True = it worked; execOut fires next\n"
            "\n\n"
            "def cleanup(db):\n"
            "    # Runs once, when the node is deleted or the graph stops.\n"
            "    pass\n"
        ),
    },
    "counter": {
        "about": "Counts how many times it was triggered; the count is an output other nodes can read.",
        "pins": {"outputs:count": "int"},
        "defaults": {},
        "code": (
            "def setup(db):\n"
            "    db.outputs.count = 0\n"
            "\n\n"
            "def compute(db):\n"
            "    db.outputs.count = db.outputs.count + 1   # outputs keep their value between triggers\n"
            "    return True\n"
            "\n\n"
            "def cleanup(db):\n"
            "    pass\n"
        ),
    },
    "spawn_package": {
        "about": "Each time it is triggered, drops one random-coloured package with physics at a spawn point "
                 "(a package generator, class session 8).",
        "pins": {"inputs:parent": "token", "inputs:spawn_at": "double[3]"},
        "defaults": {"inputs:parent": "/World/Spawned", "inputs:spawn_at": [0.0, 0.0, 1.0]},
        "code": (
            "import random\n"
            "import omni.usd\n"
            "from pxr import Gf, UsdGeom, UsdPhysics\n"
            "\n\n"
            "def setup(db):\n"
            "    pass\n"
            "\n\n"
            "def compute(db):\n"
            "    stage = omni.usd.get_context().get_stage()\n"
            "    parent = db.inputs.parent\n"
            "    if not stage.GetPrimAtPath(parent):\n"
            "        UsdGeom.Xform.Define(stage, parent)\n"
            "    index = len(stage.GetPrimAtPath(parent).GetChildren()) + 1\n"
            "    cube = UsdGeom.Cube.Define(stage, f'{parent}/Package_{index:03d}')\n"
            "    cube.CreateSizeAttr(0.2)\n"
            "    x, y, z = db.inputs.spawn_at\n"
            "    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(x + random.uniform(-0.05, 0.05), y, z))\n"
            "    color = random.choice([(0.85, 0.15, 0.12), (0.15, 0.7, 0.25), (0.15, 0.35, 0.9)])\n"
            "    cube.CreateDisplayColorAttr([Gf.Vec3f(*color)])\n"
            "    UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())\n"
            "    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())\n"
            "    return True\n"
            "\n\n"
            "def cleanup(db):\n"
            "    pass\n"
        ),
    },
}
ATTRIBUTE_TYPES = VARIABLE_TYPES + ("double[3][]", "float[3][]", "int[3]", "uint")


def add_attribute(stage: Any, graph_path: str, pin: str, value_type: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or add) a custom pin on a node that allows them, such as a Script Node: Node.inputs:speed or
    Node.outputs:count, with a type such as 'double', 'token' or 'double[3]'."""
    problem = _check_graph(stage, graph_path)
    if problem:
        return {"ok": False, "error": problem}
    match = _PIN.match(pin) if isinstance(pin, str) else None
    if not match or match.group(2).startswith("state:"):
        return {"ok": False, "error": "pin must look like NodeName.inputs:speed or NodeName.outputs:count."}
    if _node_prim(stage, graph_path, match.group(1)) is None:
        return {"ok": False, "error": f"{graph_path} has no node named {match.group(1)}."}
    if value_type not in ATTRIBUTE_TYPES:
        return {"ok": False, "error": f"value_type must be one of {', '.join(ATTRIBUTE_TYPES)}."}
    python = _python(graph_path, "create_attributes", repr((_full(graph_path, pin), value_type)))
    if dry_run is not False:
        return _preview({"graph_path": graph_path, "pin": pin, "type": value_type}, python, [])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    return {**_run(og, stage, graph_path, {"create_attributes": [(_full(graph_path, pin), value_type)]},
                   f"Added pin {pin} ({value_type}).", describe=f"{graph_path}/{match.group(1)}"), "python": python}


def add_script_node(stage: Any, graph_path: str, name: str, template: str = "move_on_trigger", *,
                    values: dict[str, Any] | None = None, trigger_key: str | None = None,
                    dry_run: bool = True) -> dict[str, Any]:
    """Preview (or add) a Script Node (Python inside the graph) from a template: its custom pins, its
    setup/compute/cleanup code and starting values. trigger_key (e.g. 'T') also adds an On Keyboard Input that fires
    it. Templates: move_on_trigger, counter, spawn_package. Edit the code afterwards in the node's Script field."""
    problem = _check_graph(stage, graph_path)
    if problem:
        return {"ok": False, "error": problem}
    if template not in SCRIPT_TEMPLATES:
        return {"ok": False, "error": f"Unknown template {template!r}. Templates: {', '.join(SCRIPT_TEMPLATES)}."}
    if not isinstance(name, str) or not _NAME.match(name):
        return {"ok": False, "error": "name must be a simple word such as MoveBox."}
    if _node_prim(stage, graph_path, name) is not None:
        return {"ok": False, "error": f"{graph_path} already has a node named {name}."}
    spec = SCRIPT_TEMPLATES[template]
    merged = {**spec["defaults"], **(values or {})}
    unknown = [pin for pin in merged if pin not in spec["pins"]]
    if unknown:
        return {"ok": False, "error": f"{template} has no pins {unknown}; it has {sorted(spec['pins'])}."}
    if trigger_key is not None and trigger_key not in ogh.KEYBOARD_KEYS:
        return {"ok": False, "error": "trigger_key must be a key name such as 'T' or 'Up'."}
    trigger = f"Key{trigger_key}" if trigger_key else None
    if trigger and _node_prim(stage, graph_path, trigger) is not None:
        return {"ok": False, "error": f"{graph_path} already has a node named {trigger}."}
    edits: dict[str, Any] = {
        "create_nodes": [(name, "omni.graph.scriptnode.ScriptNode")]
                        + ([(trigger, "omni.graph.action.OnKeyboardInput")] if trigger else []),
        "create_attributes": [(f"{name}.{pin}", kind) for pin, kind in spec["pins"].items()],
        "set_values": [(f"{name}.inputs:script", spec["code"])]
                      + [(f"{name}.{pin}", og_value(value)) for pin, value in merged.items()]
                      + ([(f"{trigger}.inputs:keyIn", trigger_key)] if trigger else []),
    }
    if trigger:
        edits["connect"] = [(f"{trigger}.outputs:pressed", f"{name}.inputs:execIn")]
    plan = {"graph_path": graph_path, "node": f"{graph_path}/{name}", "template": template, "about": spec["about"],
            "pins": spec["pins"], "values": merged, "trigger": trigger, "code": spec["code"],
            "how_it_runs": "setup() once at start, compute() each time execIn fires (return True), cleanup() at the "
                           "end. Read inputs as db.inputs.<name>, write outputs as db.outputs.<name>."}
    python = ("import omni.graph.core as og\nkeys = og.Controller.Keys\n"
              f"og.Controller.edit({graph_path!r}, {{\n"
              + "".join(f"    keys.{key.upper()}: {value!r},\n" for key, value in edits.items()) + "})\n")
    if dry_run is not False:
        return _preview(plan, python, ["Press Play, click the viewport, then press the trigger key."] if trigger else
                        ["Wire an event (e.g. On Keyboard Input 'Pressed') into its execIn, or it never runs."])
    og, refusal = _kit(stage)
    if refusal:
        return refusal
    result = _run(og, stage, graph_path, edits, f"Added Script Node {name} ({template}).",
                  describe=f"{graph_path}/{name}")
    return {**result, "python": python, "code": spec["code"]}
