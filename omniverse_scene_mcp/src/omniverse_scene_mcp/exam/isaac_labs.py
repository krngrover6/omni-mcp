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


def _labs() -> dict[str, dict[str, Any]]:
    props = f"{_assets()}/Props/Blocks"
    franka = f"{_assets()}/Robots/FrankaRobotics/FrankaPanda/franka.usd"
    return {
        "surprising_renders": {
            "title": "Why did it vanish? Visibility, purpose and stale extents",
            "objectives": ["5.5", "6.4", "5.6"],
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
            "study_stage": f"{STUDY_FOLDER}/{STUDY_STAGE}"}


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
            "Teach the concept first in plain words; ask the learner to say it back in one sentence.",
            "Do the steps in order, ONE at a time. Before each, say what we are about to do and predict what will "
            "happen; after it, ask the learner what they see in Isaac Sim, then compare with 'look'.",
            "kind 'read': call the tool directly. kind 'edit': scene_safe_edit(tool=..., arguments=...), show the "
            "preview, wait for yes, then call again with confirm=<code>.",
            "End with 'try_yourself', then the day's quiz.",
        ],
    }
