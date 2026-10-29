"""Hands-on Isaac Sim labs for NCP-OUSD objectives: learn the idea in plain words, then see it in the viewport.

Each lab is data: a short concept, then steps. A step names one router tool with exact arguments, says what to look
for in Isaac Sim, and is either a read (call it directly) or an edit (run it through scene_safe_edit: preview, the
learner's yes, then apply with the confirm code). Labs work in a dedicated study stage, never in a course scene:
isaac_study/study.usda inside the workspace, each lab under its own /World/<Lab> prim.

Some objectives are about compiling plugins or writing resolvers; a viewport cannot show those, so their lab says so
and points at the Python or USDA exercise instead of pretending.
"""

from __future__ import annotations

from typing import Any

from .progress import workspace_root

STUDY_FOLDER = "isaac_study"
STUDY_STAGE = "study.usda"


def _assets() -> str:
    return (workspace_root() / "isaacsim-assets-6.1.0" / "Assets" / "Isaac" / "6.1" / "Isaac").as_posix()


def _read(tool: str, arguments: dict[str, Any], look: str, say: str = "") -> dict[str, Any]:
    return {"kind": "read", "say": say, "tool": tool, "arguments": arguments, "look": look}


def _edit(tool: str, arguments: dict[str, Any], look: str, say: str = "") -> dict[str, Any]:
    return {"kind": "edit", "say": say, "tool": tool, "arguments": arguments, "look": look}


def _ui(say: str, look: str) -> dict[str, Any]:
    """Something the learner does by hand in Isaac Sim (no tool call): a click, a drag, finding a panel."""
    return {"kind": "ui", "say": say, "tool": None, "arguments": {}, "look": look}


# Taught before any exam objective: the learner may know nothing about Isaac Sim or USD yet.
FOUNDATIONS = ("basics_isaac", "basics_usd")
# Labs for the mentorship class sessions (Isaac Sim skills that are not NCP-OUSD objectives).
CLASS_LABS = {"action_graph": 6}


def _labs() -> dict[str, dict[str, Any]]:
    props = f"{_assets()}/Props/Blocks"
    franka = f"{_assets()}/Robots/FrankaRobotics/FrankaPanda/franka.usd"
    return {
        "basics_isaac": {
            "title": "Lesson 0a: finding your way around Isaac Sim",
            "objectives": [],
            "words": {
                "viewport": "the big 3D window in the middle where you see the scene, like a camera you can move",
                "Stage panel": "the list (top right) of everything in the scene, shown as a tree of folders",
                "Property panel": "the panel (bottom right) showing the settings of whatever you selected",
                "select": "click a thing (in the viewport or the Stage panel) so the app knows you mean it",
                "Play / Stop": "the buttons on the toolbar at the left edge of the viewport that start and reset physics",
            },
            "concept": (
                "Isaac Sim is a program for building 3D worlds where robots can be tested before they exist in real "
                "life. Its screen has three areas you will use every day: the viewport in the middle (your camera "
                "into the world), the Stage panel at the top right (the list of everything in the world) and the "
                "Property panel at the bottom right (the settings of the thing you clicked). You change what you "
                "see in the viewport by moving the camera, not the objects."),
            "steps": [
                _ui("Look at the middle of the screen: that is the viewport. You should see a grey grid (the ground).",
                    "A grid and probably nothing else: a new scene is empty."),
                _ui("Move the camera: hold Alt and drag with the LEFT mouse button to orbit around; drag with the "
                    "MIDDLE button (press the wheel) to slide sideways; roll the wheel to zoom in and out.",
                    "The grid turns, slides and gets bigger or smaller. The world did not move, your camera did."),
                _ui("Find the Stage panel at the top right. Click the small arrow next to World to open it.",
                    "A tree: World and whatever is inside it (maybe a ground or a light)."),
                _ui("Find the Property panel at the bottom right. Click World in the Stage panel.",
                    "The Property panel fills with World's settings (Transform: Translate, Rotate, Scale)."),
                _read("scene_summary", {},
                      "I read the same scene from my side: stage_name should end with isaac_study/study.usda.",
                      "Check that Isaac and I are looking at the same file."),
                _read("scene_children", {"path": "/World"},
                      "The same children you saw under World in the Stage panel.",
                      "Ask the scene what is inside World."),
            ],
            "try_yourself": "Without help: orbit the camera all the way round, then select World and press F. "
                            "What does F do?",
        },
        "basics_usd": {
            "title": "Lesson 0b: what a USD scene is made of (stage, prim, path, attribute)",
            "objectives": [],
            "words": {
                "USD": "Universal Scene Description: the file format and language Isaac Sim uses to describe a 3D "
                       "world, like a recipe for the whole scene",
                "stage": "the whole scene you have open right now (everything in the Stage panel)",
                "prim": "one thing in the scene: a cube, a light, a robot part, or a folder (Xform) holding others",
                "path": "a prim's address, like a folder path: /World/Basics/MyCube",
                "attribute": "one setting of a prim with a value, e.g. a cube's size = 2",
                "Xform": "a prim that is just a container you can move; like a folder that also has a position",
                "layer / .usda file": "the file the stage is saved in; .usda is readable text you can open in Notepad",
            },
            "concept": (
                "A USD scene (the stage) is a tree of prims, like folders and files on your computer. Every prim has "
                "an address called a path, starting at / (for example /World/Basics/MyCube). Each prim has "
                "attributes: named settings with values, like size = 2 or a position. Everything you see in Isaac "
                "Sim is just prims and attribute values; when you change a value, the scene changes. When you save, "
                "all of it is written into a .usda text file you can read."),
            "steps": [
                _edit("scene_create", {"path": "/World/Basics", "type_name": "Xform"},
                      "A new folder-like prim Basics appears under World in the Stage panel.",
                      "Make a container prim (an Xform)."),
                _edit("scene_create", {"path": "/World/Basics/MyCube", "type_name": "Cube"},
                      "A grey cube appears in the viewport, and MyCube appears inside Basics in the Stage panel.",
                      "Make a cube inside it. Its path is /World/Basics/MyCube."),
                _read("scene_select", {"paths": ["/World/Basics/MyCube"]},
                      "MyCube is highlighted in the Stage panel, and the Property panel shows its settings.",
                      "Select it, so Isaac shows its attributes (selecting changes nothing in the scene)."),
                _read("scene_properties", {"prim_path": "/World/Basics/MyCube"},
                      "The cube's attributes: size is 2 (the default, nobody wrote it yet), visibility, and more.",
                      "List the cube's attributes from my side."),
                _edit("scene_set_attribute", {"path": "/World/Basics/MyCube", "name": "size", "value": 0.5},
                      "The cube shrinks to a quarter of its size. In the Property panel, Size now reads 0.5.",
                      "Change one attribute: size."),
                _edit("scene_move", {"path": "/World/Basics/MyCube", "translation": [0, 0, 1]},
                      "The cube lifts 1 m up. In the Property panel, Translate Z reads 1.",
                      "Change its position (the translate attribute)."),
                _ui("Now you: in the Property panel, find Translate and type 2 into the X box, then press Enter.",
                    "The cube jumps 2 m sideways. You just changed an attribute by hand, exactly like I did."),
                _read("scene_inspect", {"path": "/World/Basics/MyCube"},
                      "My read now shows translation x = 2: we both see the same stage."),
            ],
            "try_yourself": "Say MyCube's path out loud, then name two of its attributes and their values.",
        },
        "action_graph": {
            "title": "Class session 6: Action Graph from scratch (press W to push a cube)",
            "objectives": [],
            "class_session": 6,
            "words": {
                "Action Graph": "a picture-program: boxes joined by wires that say 'when this happens, do that', "
                                "instead of typed code",
                "node": "one box in the graph that does one job (wait for a key, write a value, add two numbers)",
                "pin": "a little dot on a node's side: inputs on the left (what it needs), outputs on the right "
                       "(what it gives)",
                "execution wire": "a white wire that says WHEN the next node runs, like a domino knocking the next",
                "data wire": "a coloured wire that carries a VALUE (a number, a vector) from one node to another",
                "event node": "a node that starts a chain when something happens: a key press, every frame (tick)",
                "graph variable": "a named value stored in the graph that any node can read, and that you can "
                                  "change while it runs, like a dial on a machine",
                "velocity": "speed with a direction, e.g. (2, 0, 0) = 2 metres per second along X",
            },
            "concept": (
                "In class the teacher made a cube move with the W key without writing code. An Action Graph is a set "
                "of nodes joined by wires. Execution (white) wires decide WHEN something happens: 'when W is "
                "pressed -> write a value'. Data wires carry WHAT the value is: 'the value comes from the variable "
                "forward'. The cube needs physics (a rigid body) so that writing its velocity makes it move, and "
                "something to land on (a floor with a collider)."),
            "steps": [
                _edit("scene_add_physics_scene", {},
                      "Nothing visible: the scene now has gravity and physics. (Skip if it says one exists.)",
                      "Turn physics on for the scene."),
                _edit("scene_create", {"path": "/World/AG", "type_name": "Xform"}, "A group AG appears.",
                      "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/AG/Floor", "type_name": "Cube"}, "A big grey cube.",
                      "Make a floor (a flattened cube)."),
                _edit("scene_move", {"path": "/World/AG/Floor", "translation": [0, -4, -0.05],
                                     "scale": [3, 3, 0.05]},
                      "It flattens into a 6 x 6 m slab just under the ground line.", "Flatten it."),
                _edit("scene_make_collider", {"prim_path": "/World/AG/Floor"},
                      "Nothing visible: things can now land on the floor.", "Give the floor a collider."),
                _edit("scene_create", {"path": "/World/AG/Cube", "type_name": "Cube"}, "A cube appears.",
                      "Make the cube we will push."),
                _edit("scene_move", {"path": "/World/AG/Cube", "translation": [0, -4, 0.5],
                                     "scale": [0.25, 0.25, 0.25]},
                      "A small 0.5 m cube floats above the floor.", "Make it small and lift it."),
                _edit("scene_make_rigid_body", {"prim_path": "/World/AG/Cube"},
                      "Nothing yet: press Play and it would fall onto the floor (then press Stop).",
                      "Give the cube physics (a rigid body with a collider)."),
                _edit("graph_create", {"graph_path": "/World/AG/Graph"},
                      "A Graph prim appears. Right-click it in the Stage panel > Open Graph to see the empty canvas.",
                      "Make an empty Action Graph."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "KeyW",
                                         "node_type": "omni.graph.action.OnKeyboardInput",
                                         "values": {"inputs:keyIn": "W"}},
                      "An 'On Keyboard Input' box appears on the canvas; its outputs include Pressed and Released.",
                      "Add the EVENT node: it fires when W is pressed."),
                _edit("graph_add_variable", {"graph_path": "/World/AG/Graph", "name": "forward",
                                             "value_type": "float[3]", "default": [2, 0, 0]},
                      "In the graph window's Variables panel, 'forward' appears with (2, 0, 0).",
                      "Add a graph VARIABLE: the push direction and speed."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "ReadForward",
                                         "node_type": "omni.graph.core.ReadVariable",
                                         "values": {"inputs:variableName": "forward"}},
                      "A 'Read Variable' box appears; its Value output carries (2, 0, 0).",
                      "Add a node that READS the variable."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "Push",
                                         "node_type": "omni.graph.nodes.WritePrimAttribute",
                                         "values": {"inputs:name": "physics:velocity", "inputs:usdWriteBack": True}},
                      "A 'Write Prim Attribute' box appears. It will write the cube's velocity.",
                      "Add the node that DOES the job: write a value into an attribute."),
                _edit("graph_set_value", {"graph_path": "/World/AG/Graph", "pin": "Push.inputs:prim",
                                          "value": ["/World/AG/Cube"]},
                      "Push's Prim input now shows /World/AG/Cube.", "Tell it WHICH prim: the cube."),
                _edit("graph_connect", {"graph_path": "/World/AG/Graph", "source": "KeyW.outputs:pressed",
                                        "target": "Push.inputs:execIn"},
                      "A white EXECUTION wire joins Pressed to Push: 'when W is pressed, run Push'.",
                      "Wire WHEN: the key press starts the write."),
                _edit("graph_connect", {"graph_path": "/World/AG/Graph", "source": "ReadForward.outputs:value",
                                        "target": "Push.inputs:value"},
                      "A coloured DATA wire carries (2, 0, 0) into Push's Value.",
                      "Wire WHAT: the value to write comes from the variable."),
                _read("graph_inspect", {"root_path": "/World/AG/Graph"},
                      "The flow reads: 'When key W is pressed -> Write Prim Attribute writes physics:velocity = "
                      "variable forward on /World/AG/Cube'.", "Ask the graph to explain itself."),
                _ui("Press Play (left toolbar). Click once inside the viewport so it has the keyboard, then press W.",
                    "The cube slides along X at 2 m/s. (Press Stop to put it back.)"),
                _ui("In the graph window's Variables panel, change forward to (0, 2, 0). Press Play, click the "
                    "viewport, press W.",
                    "Now it slides along Y: you changed the behaviour without touching a node, as in class."),
            ],
            "try_yourself": "Add an S key that pushes backwards: an On Keyboard Input (S), a new variable back = "
                            "(-2, 0, 0), a Read Variable and a Write Prim Attribute. Which two wires do you need?",
        },
        "surprising_renders": {
            "title": "Why did it vanish? Visibility, purpose and stale extents",
            "objectives": ["5.5", "6.4", "5.6"],
            "words": {'visibility': 'a setting that hides or shows a prim (and everything inside it)', 'purpose': 'who a prim is for: default (everyone), render, proxy (a fast stand-in) or guide (helper drawings)', 'extent': 'the box a shape says it fits inside; programs use it to frame and skip things'},
            "concept": (
                "Sometimes a prim is in the scene but you can't see it, or it looks wrong. The usual reasons: "
                "visibility='invisible' hides it (and everything under it); purpose='guide' or 'proxy' means 'only "
                "show this for a special view', so normal rendering skips it; the extent (the box USD thinks the "
                "shape fits in) can be stale after the shape changes, so culling and framing go wrong; a "
                "leftHanded orientation flips which side faces you; and unit/up-axis mismatches make things tiny "
                "or sideways. Think of extent as the label on a moving box: if you put a bigger thing inside and "
                "keep the old label, the movers plan with the wrong size."),
            "steps": [
                _edit("scene_create", {"path": "/World/Render", "type_name": "Xform"},
                      "A new empty group Render appears in the Stage panel.", "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/Render/Box", "type_name": "Cube"},
                      "A grey 2 m cube appears at the origin (Cube's fallback size is 2).", "Add a cube."),
                _read("scene_bounds", {"prim_path": "/World/Render/Box"},
                      "authored extent [-1,-1,-1]..[1,1,1] matches the computed one: the label fits the box."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "visibility", "value": "invisible"},
                      "The cube disappears from the viewport but is still listed in the Stage panel.",
                      "Hide it with visibility."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "visibility", "value": "inherited"},
                      "It comes back: 'inherited' means 'visible unless a parent is invisible'.", "Show it again."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "purpose", "value": "guide"},
                      "It disappears again: guides are only drawn when a viewer turns guides on.",
                      "Now try purpose."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "purpose", "value": "default"},
                      "Back to normal.", "Put purpose back."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "size", "value": 4.0},
                      "The cube doubles to 4 m.", "Change the shape's size..."),
                _read("scene_bounds", {"prim_path": "/World/Render/Box"},
                      "Compare authored and computed extent: if the authored one still says 1, it is stale. That is "
                      "objective 5.6: recompute the extent (UsdGeom.Boundable.ComputeExtentFromPlugins) after changing "
                      "points or size.", "...and check whether the extent label kept up."),
            ],
            "try_yourself": "Set visibility='invisible' on /World/Render (the parent). Does Box hide too? Why?",
        },
        "value_ignored": {
            "title": "Why is my value ignored? Layers, strength and the edit target",
            "objectives": ["1.1", "1.8", "6.2", "1.5"],
            "words": {'layer': 'one file (sheet) of settings', 'sublayer': 'another layer stacked under yours; it is weaker', 'opinion': 'a value one layer writes for an attribute', 'edit target': 'the layer your next change is written into'},
            "concept": (
                "A USD scene is a stack of transparent sheets (layers). Each sheet can write an 'opinion' for a value. "
                "When two sheets disagree, the sheet higher in the stack wins. The root layer (your study.usda) sits "
                "above its sublayers, so if both write a ball's radius, the root wins. The edit target is the sheet "
                "your next edit is written on. Many 'my change is ignored' bugs are: you wrote on a weaker sheet "
                "than the one that already has an opinion."),
            "steps": [
                _edit("scene_create", {"path": "/World/Layers", "type_name": "Xform"}, "A Layers group appears.",
                      "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/Layers/Ball", "type_name": "Sphere"},
                      "A sphere of radius 1 appears.", "Add a ball (written on the root layer)."),
                _edit("scene_set_attribute", {"path": "/World/Layers/Ball", "name": "radius", "value": 2.0},
                      "The ball grows to radius 2. This opinion lives on study.usda (the root layer).",
                      "Give it a radius on the root layer."),
                _edit("scene_add_sublayer", {"layer_path": "layers/anim.usda", "create_if_missing": True},
                      "Nothing changes in the viewport: a new, empty sheet anim.usda joined the stack, BELOW the root.",
                      "Add a weaker sheet: a sublayer."),
                _read("scene_layers", {}, "Strongest first: session layer, study.usda, then anim.usda."),
                _edit("scene_set_edit_target", {"layer": "anim.usda"},
                      "Nothing visible: from now on edits are written on anim.usda.", "Write on the weaker sheet."),
                _edit("scene_set_attribute", {"path": "/World/Layers/Ball", "name": "radius", "value": 5.0},
                      "The ball does NOT change. The preview already warned a stronger opinion hides it.",
                      "Try to make it radius 5 from the weaker sheet."),
                _read("scene_why_value", {"prim_path": "/World/Layers/Ball", "attribute": "radius"},
                      "Radius 2 wins from study.usda; radius 5 in anim.usda loses because the root is stronger."),
                _edit("scene_set_edit_target", {"layer": "study.usda"},
                      "Edits go to the root layer again.", "Always put the edit target back."),
            ],
            "try_yourself": "Predict: if you delete the radius from study.usda, what radius shows? Check with "
                            "scene_what_if(prim_path='/World/Layers/Ball', attribute='radius').",
        },
        "arcs": {
            "title": "Sublayer, reference or payload?",
            "objectives": ["1.3", "1.6"],
            "words": {'reference': 'pull one asset file into one prim, like a sticker of a ready-made model', 'payload': 'a reference you can unload to save memory', 'composition arc': 'any way USD pulls in content from elsewhere (sublayer, reference, payload, variant...)'},
            "concept": (
                "A sublayer adds a whole sheet to your stack (good for departments: layout, lighting). A reference "
                "pulls one asset into one prim, like placing a sticker of a ready-made model (good for props you "
                "reuse). A payload is a reference you can switch off: unloaded, it costs no memory (good for heavy "
                "assets). Your own local edits on that prim beat what the referenced file says: that is the L in "
                "LIVRPS beating the R."),
            "steps": [
                _edit("scene_create", {"path": "/World/Arcs", "type_name": "Xform"}, "An Arcs group appears.",
                      "Make a group."),
                _edit("scene_add_asset", {"prim_path": "/World/Arcs/RefBlock", "asset_path": f"{props}/red_block.usd",
                                          "translation": [0, 0, 0.5], "scale": 10},
                      "A red block (5 cm, scaled x10 so you can see it) appears.", "Reference a red block."),
                _edit("scene_add_payload", {"prim_path": "/World/Arcs/PayBlock", "asset_path": f"{props}/blue_block.usd",
                                            "translation": [1, 0, 0.5], "scale": 10},
                      "A blue block appears next to it, brought in as a payload.", "Payload a blue block."),
                _read("scene_composition", {"prim_path": "/World/Arcs/RefBlock"},
                      "The arcs list shows a reference (R) to red_block.usd."),
                _edit("scene_unload_payloads", {"paths": ["/World/Arcs/PayBlock"]},
                      "The blue block vanishes, but PayBlock stays in the Stage panel: its contents left memory.",
                      "Switch the payload off."),
                _edit("scene_load_payloads", {"paths": ["/World/Arcs/PayBlock"]}, "The blue block is back.",
                      "Switch it on again."),
            ],
            "try_yourself": "Which would you use for a 2 GB factory building you only need sometimes, and why?",
        },
        "instancing": {
            "title": "Instancing: many copies, one prototype",
            "objectives": ["1.2", "2.4", "2.2", "1.10"],
            "words": {'instance': 'a copy that shares its insides with other copies instead of owning them', 'prototype': 'the one shared original that instances point at', 'instance proxy': 'a read-only view of a part inside an instance'},
            "concept": (
                "If a warehouse has 1,000 identical boxes, storing 1,000 full copies wastes memory. Instancing keeps "
                "ONE shared prototype and lets each box point at it, like 1,000 shortcuts to one file. You mark a "
                "referencing prim instanceable. The catch: the parts inside an instance are shared, so you can't "
                "edit them one copy at a time; you edit the instance prim itself (its transform, a material bound "
                "on it) or make that one copy non-instanceable. Point instancing is the even lighter option for "
                "huge counts of simple copies."),
            "steps": [
                _edit("scene_create", {"path": "/World/Inst", "type_name": "Xform"}, "An Inst group appears.",
                      "Make a group."),
                _edit("scene_add_asset", {"prim_path": "/World/Inst/Box1", "asset_path": f"{props}/red_block.usd",
                                          "translation": [0, 2, 0.5], "scale": 10, "instanceable": True},
                      "A red block appears.", "Add the first instanced block."),
                _edit("scene_add_asset", {"prim_path": "/World/Inst/Box2", "asset_path": f"{props}/red_block.usd",
                                          "translation": [1, 2, 0.5], "scale": 10, "instanceable": True},
                      "A second identical block.", "Add a second one."),
                _read("scene_composition", {"prim_path": "/World/Inst/Box2"},
                      "It reports instanceable / an instance sharing a prototype with Box1."),
                _read("scene_children", {"path": "/World/Inst/Box1"},
                      "Children are instance proxies: you can look, not edit."),
                _edit("scene_move", {"path": "/World/Inst/Box2", "translation": [1, 2, 1.0]},
                      "Box2 rises: the instance prim itself can be edited.", "Edit the instance root (allowed)."),
            ],
            "try_yourself": "Try scene_set_attribute on /World/Inst/Box1/Cube (inside the instance) through "
                            "scene_safe_edit. What does the preview say, and how would you recolour only Box1?",
        },
        "variants": {
            "title": "Variant sets: one asset, switchable versions",
            "objectives": ["1.7", "1.6"],
            "words": {'variant set': 'a menu of versions on one asset', 'variant': 'one item in that menu', 'selection': 'which item is picked right now'},
            "concept": (
                "A variant set is a menu on an asset: pick one item and the asset changes, without copying files. "
                "The Franka robot has a 'Gripper' menu: Default, None, Robotiq_2F_85 and AlternateFinger. Your "
                "selection is just a small opinion on your prim; the asset file stays untouched. Variants are right "
                "when the versions are alternatives of the SAME thing (colours, LODs, grippers); they are wrong for "
                "unrelated things or when you need several at once."),
            "steps": [
                _edit("scene_create", {"path": "/World/Var", "type_name": "Xform"}, "A Var group appears.",
                      "Make a group."),
                _edit("robot_add_ready", {"name": "franka", "prim_path": "/World/Var/Franka", "position": [0, -2, 0]},
                      "A Franka arm appears.", "Add a Franka (it references franka.usd)."),
                _read("variant_list", {"root_path": "/World/Var/Franka"},
                      "Two sets: Mesh (Performance/Quality) and Gripper (Default selected)."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper", "variant": "None"},
                      "The hand and fingers disappear.", "Pick Gripper = None."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper",
                                         "variant": "Robotiq_2F_85"},
                      "A different (Robotiq) gripper appears.", "Pick the Robotiq gripper."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper",
                                         "variant": "Default"}, "The normal Franka hand is back.", "Back to Default."),
            ],
            "try_yourself": "Your selection lives in study.usda, franka.usd says 'Default'. Which wins and which "
                            "LIVRPS letter explains it?",
        },
        "primvars": {
            "title": "Primvars: colour data on a mesh",
            "objectives": ["5.1", "8.1"],
            "words": {'primvar': 'extra data painted on a shape that renderers can read, like colour', 'interpolation': 'how many values a primvar has and how they spread over the shape', 'displayColor': 'the simple built-in colour primvar'},
            "concept": (
                "A primvar is extra data painted onto geometry that renderers can read, like displayColor. Its "
                "interpolation says how many values there are and where they go: constant = one value for the whole "
                "shape; uniform = one per face; vertex = one per point (blended across faces); faceVarying = one per "
                "corner of each face (sharp seams). The array length must match, or the renderer ignores it."),
            "steps": [
                _edit("scene_create", {"path": "/World/Prim", "type_name": "Xform"}, "A Prim group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Prim/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _edit("scene_move", {"path": "/World/Prim/Box", "translation": [0, 4, 1]}, "It moves aside.",
                      "Move it where you can see it."),
                _edit("scene_set_attribute", {"path": "/World/Prim/Box", "name": "primvars:displayColor",
                                              "value": [[0.9, 0.1, 0.1]]},
                      "The cube turns red: one colour, constant interpolation.", "Paint it with displayColor."),
                _read("scene_properties", {"prim_path": "/World/Prim/Box"},
                      "primvars:displayColor is color3f[] with 1 value: that is constant interpolation."),
            ],
            "try_yourself": "A cube has 6 faces. How many colours would uniform interpolation need? Then do the "
                            "day's usd_playground_compose exercise to try vertex and uniform for real.",
        },
        "materials": {
            "title": "Materials and binding",
            "objectives": ["8.2", "8.3"],
            "words": {'material': 'a recipe for how a surface looks', 'UsdPreviewSurface': 'the simple material recipe every USD viewer understands', 'binding': 'the link that says which material a prim uses'},
            "concept": (
                "A material is a recipe for how a surface looks; UsdPreviewSurface is the simple recipe every viewer "
                "understands (colour, roughness, metallic...). Binding connects a material to a prim. A child's own "
                "binding normally beats its parent's, unless the parent binds with strength "
                "'strongerThanDescendants', which forces its material onto everything below."),
            "steps": [
                _edit("scene_create", {"path": "/World/Mat", "type_name": "Xform"}, "A Mat group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Mat/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _edit("scene_move", {"path": "/World/Mat/Box", "translation": [0, 7, 1]}, "It moves aside.",
                      "Move it where you can see it."),
                _edit("scene_create_material", {"path": "/World/Mat/Looks/Blue", "diffuse_color": [0.1, 0.3, 0.9],
                                                "roughness": 0.4}, "Nothing visible yet: a material exists but is "
                      "bound to nothing.", "Make a blue UsdPreviewSurface material."),
                _edit("scene_bind_material", {"prim_path": "/World/Mat/Box", "material_path": "/World/Mat/Looks/Blue"},
                      "The cube turns blue.", "Bind it to the cube."),
                _read("scene_why_value", {"prim_path": "/World/Mat/Box", "attribute": "material"},
                      "It explains which binding wins and from where."),
            ],
            "try_yourself": "Make a red material, bind it to /World/Mat with strength 'strongerThanDescendants'. "
                            "Predict the cube's colour first.",
        },
        "attributes": {
            "title": "Attributes: types, authored values and fallbacks",
            "objectives": ["5.4", "5.2"],
            "words": {'type': 'what kind of value an attribute holds (number, text, colour...)', 'fallback': 'the built-in value USD reports when nobody wrote one', 'authored': 'actually written by some layer', 'token': 'a value from a fixed list of choices'},
            "concept": (
                "Every attribute has a type (double, float, token, string, color3f, arrays...). If nobody authored a "
                "value, USD returns the schema's fallback: a Cube's size is 2 even though no file says so. "
                "'Authored' means some layer wrote it. Tokens are for fixed choices (like visibility), strings for "
                "free text, doubles for precise numbers, float/color3f for lighter graphics data."),
            "steps": [
                _edit("scene_create", {"path": "/World/Attr", "type_name": "Xform"}, "An Attr group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Attr/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _read("scene_properties", {"prim_path": "/World/Attr/Box"},
                      "size shows 2 as a fallback (not authored); note each attribute's type."),
                _edit("scene_set_attribute", {"path": "/World/Attr/Box", "name": "size", "value": 1.0},
                      "The cube shrinks.", "Author a size."),
                _read("scene_why_value", {"prim_path": "/World/Attr/Box", "attribute": "size"},
                      "Now size is authored in study.usda, not a fallback."),
                _edit("scene_set_attribute", {"path": "/World/Attr/Box", "name": "note", "value": "inspected",
                                              "create_type": "string"},
                      "Nothing visible: a custom string attribute was added.", "Add your own custom attribute."),
            ],
            "try_yourself": "Why is visibility a token and not a string?",
        },
    }


# Objectives a viewport cannot show (compiling, resolvers, Hydra plugins, exporters): honest pointers instead.
_CODE_ONLY = {
    "3.1": "compiling a plugin against one USD build", "3.2": "building OpenUSD from source",
    "3.3": "registering a custom model kind in plugInfo", "3.4": "designing IsA/API schemas",
    "3.5": "writing a custom Ar resolver", "3.7": "a Hydra scene index plugin", "3.8": "a resolver that emits prims",
    "6.1": "Sdf.ChangeBlock batching", "6.5": "TfDebug, Trace and TfMallocTag",
    "4.7": "writing an exporter", "4.8": "writing an importer", "7.5": "a custom Ar resolver",
    "7.8": "writing an importer",
}


def list_labs() -> dict[str, Any]:
    labs = _labs()
    return {"ok": True, "labs": [{"id": key, "title": lab["title"], "objectives": lab["objectives"],
                                  "steps": len(lab["steps"])} for key, lab in labs.items()],
            "study_stage": f"{STUDY_FOLDER}/{STUDY_STAGE}",
            "start_here": list(FOUNDATIONS),
            "class_labs": {name: f"class session {session}" for name, session in CLASS_LABS.items()},
            "note": "Beginners do the foundation labs (start_here) first, before any exam objective."}


def lab(objective_id: str | None = None, lab_id: str | None = None) -> dict[str, Any]:
    """The lab for an exam objective (e.g. '1.8') or a lab id, with the study-stage setup and how to run steps."""
    labs = _labs()
    if lab_id is not None:
        chosen = labs.get(lab_id)
        if chosen is None:
            return {"ok": False, "error": f"Unknown lab '{lab_id}'. Labs: {', '.join(labs)}."}
        key = lab_id
    elif objective_id is not None:
        objective_id = str(objective_id).strip()
        key = next((name for name, item in labs.items() if objective_id in item["objectives"]), None)
        if key is None:
            topic = _CODE_ONLY.get(objective_id)
            why = (f"Objective {objective_id} is about {topic}: that is code and build work, not something a "
                   "viewport can show." if topic else
                   f"There is no Isaac lab for objective {objective_id} yet.")
            return {"ok": True, "lab": None, "objective": objective_id, "message": why,
                    "instead": ("Learn it from the day's explanation and docs, then use python_show / "
                                "python_practice_list for code, or the day's usd_playground_compose exercise.")}
        chosen = labs[key]
    else:
        return list_labs()
    root = (workspace_root() / STUDY_FOLDER / STUDY_STAGE).as_posix()
    return {
        "ok": True, "lab_id": key, **chosen,
        "setup": [
            f"Isaac Sim must have the study stage open: {root}. Check with scene_summary: stage_name should end "
            f"with {STUDY_FOLDER}/{STUDY_STAGE}.",
            f"If it does not exist yet: in Isaac Sim do File > New, then File > Save As > {root}. Never run labs in "
            "a course scene or the physics test cell.",
            "If a lab group (e.g. /World/Layers) already exists from an earlier run, skip its create steps or delete "
            "the group first (scene_safe_edit with scene_delete).",
        ],
        "how_to_run": [
            "The learner may know nothing yet: teach every entry of 'words' (plain meaning plus an everyday example) "
            "before the concept, and explain every click (which menu or panel, where on screen).",
            "Teach the concept first in plain words; ask the learner to say it back in one sentence.",
            "Do the steps in order, ONE at a time. Before each, say what we are about to do and predict what will "
            "happen; after it, ask the learner what they see in Isaac Sim, then compare with 'look'.",
            "kind 'read': call the tool directly. kind 'edit': scene_safe_edit(tool=..., arguments=...), show the "
            "preview, wait for yes, then call again with confirm=<code>. kind 'ui': the learner does it by hand in "
            "Isaac Sim; wait until they say what they see.",
            "End with 'try_yourself', then the day's quiz.",
        ],
    }
