"""Plain-English guide to the router's MCP tools, grouped by task, with examples and a quick start.

Pure data and string matching: nothing here reads the scene, the network or any file, so help always works,
even when every bridge and knowledge backend is down.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

# Catalog tools that server.py does not register yet. help() marks them "planned"; tests/test_help.py fails once one
# is registered and still listed here, so the flag never goes stale. Every catalog tool is registered today.
PLANNED_TOOLS: frozenset[str] = frozenset()

MAX_SEARCH_RESULTS = 12

LEGEND = {
    "mutates": "True when the tool can change your open scene, write a file, or change saved exam progress.",
    "needs_bridge": "True when the tool needs a live scene app (Isaac Sim 9911 or Composer 9910) to be useful.",
    "planned": "Listed for completeness; this router version does not register the tool yet.",
}

SAFETY = (
    "Preview first: scene edits take dry_run=True to show exactly what would change without changing anything. "
    "Light, camera and material tools preview by default; edits aimed at a prim by name (not path) always preview. "
    "Router edits can be undone with scene_undo_last (it previews first); saving is not undoable. Simulation "
    "controls (sim_play, sim_pause, sim_stop, sim_step) change simulation state, not authored data: use them only "
    "when asked; sim_stop puts simulated prims back."
)


def _tool(name: str, what: str, example: str, mutates: bool, needs_bridge: bool) -> dict[str, Any]:
    return {"name": name, "what": what, "example": example, "mutates": mutates, "needs_bridge": needs_bridge}


# Group name -> entries. Every @app.tool in server.py appears exactly once (tests/test_help.py checks this).
TOOL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "Scene: look": [
        _tool("scene_summary", "Show the open stage: its name, root, prim count, top-level prims and selection.",
              "scene_summary()", False, True),
        _tool("scene_inspect", "Show one prim's type, parent, children, transform and bound material.",
              'scene_inspect(path="/World/Props/Cube")', False, True),
        _tool("scene_children", "List the direct children of one prim.",
              'scene_children(path="/World/Props")', False, True),
        _tool("scene_find", "Find prims whose path, type or material contains some text, optionally under a scope.",
              'scene_find(query="light", scope_path="/World")', False, True),
        _tool("scene_select", "Select prims in the app so 'this' and 'it' refer to them; nothing is written to a layer.",
              'scene_select(paths=["/World/Props/Cube"])', False, True),
        _tool("scene_variants", "Show the variant set selections authored on one prim.",
              'scene_variants(path="/World/Car")', False, True),
        _tool("scene_animation_actions", "List the named ZAZ-965 demo animations and the timeline state.",
              "scene_animation_actions()", False, True),
        _tool("scene_animation_play", "Play or pose one named ZAZ-965 demo animation on the timeline (nothing is authored).",
              'scene_animation_play(action="wheel_spin")', False, True),
        _tool("scene_animation_stop", "Stop the timeline animation and keep the current pose.",
              "scene_animation_stop()", False, True),
    ],
    "Scene: change safely": [
        _tool("scene_create", "Create a prim of a given type at a path; pass dry_run=True first to preview it.",
              'scene_create(path="/World/Ball", type_name="Sphere", dry_run=True)', True, True),
        _tool("scene_move", "Set a prim's translate, rotate (XYZ degrees) and/or scale; preview with dry_run=True.",
              'scene_move(path="/World/Props/Cube", translation=[0, 0, 1], dry_run=True)', True, True),
        _tool("scene_rename", "Rename a prim (its path changes, its type does not); preview with dry_run=True.",
              'scene_rename(path="/World/Props/Cube", new_name="HeroCube", dry_run=True)', True, True),
        _tool("scene_duplicate", "Copy a prim to a new path; preview with dry_run=True.",
              'scene_duplicate(source_path="/World/Props/Cube", target_path="/World/Props/Cube2", dry_run=True)',
              True, True),
        _tool("scene_reparent", "Move a prim under a different parent; preview with dry_run=True.",
              'scene_reparent(path="/World/Props/Cube", new_parent="/World", dry_run=True)', True, True),
        _tool("scene_delete", "Delete a prim and everything under it; always preview with dry_run=True first.",
              'scene_delete(path="/World/Props/Cube2", dry_run=True)', True, True),
        _tool("scene_set_attribute", "Set one attribute such as a light's inputs:intensity or a camera's focalLength; "
              "dry_run=True (the default) shows the change first.",
              'scene_set_attribute(path="/World/RectLight", name="inputs:intensity", value=5000, dry_run=True)',
              True, True),
        _tool("scene_variant_set", "Switch a variant set to another variant; preview with dry_run=True.",
              'scene_variant_set(path="/World/Car", variant_set="color", selection="red", dry_run=True)', True, True),
        _tool("scene_save", "Save the live scene to a USD file; dry_run=True says whether it overwrites or exports a copy.",
              'scene_save(path="C:/Temp/scene_copy.usda", dry_run=True)', True, True),
    ],
    "Live insight": [
        _tool("scene_stage_info", "Read-only stage overview: layer stack, default prim, upAxis, metersPerUnit, time codes "
              "and a prim census (live stage, or the saved file).", "scene_stage_info()", False, False),
        _tool("scene_composition", "Read-only: how one prim is composed (arcs with their LIVERPS letter, variants, "
              "instancing).", 'scene_composition(prim_path="/World/Props/Cube")', False, False),
        _tool("scene_explain_value", "Read-only: which opinion wins for one attribute and why (layer, arc, time samples).",
              'scene_explain_value(prim_path="/World/Props/Cube", attribute="size")', False, False),
        _tool("scene_properties", "Read-only: a prim's attributes, values, relationships and metadata.",
              'scene_properties(prim_path="/World/Props/Cube")', False, False),
        _tool("scene_bounds", "Read-only: world and local bounds, extent, visibility and purpose of one prim.",
              'scene_bounds(prim_path="/World/Props/Cube")', False, False),
        _tool("usd_validate", "Read-only lint of the stage: exam-style checks, OpenUSD validators, or both.",
              'usd_validate(profile="exam")', False, False),
    ],
    "Lights/cameras/materials": [
        _tool("scene_create_light", "Plan a rect, sphere, disk, distant, cylinder or dome light, optionally placed and "
              "aimed; dry_run=True by default.",
              'scene_create_light(path="/World/KeyLight", light_type="rect", intensity=5000, '
              'aim_at="/World/Props/Cube", dry_run=True)', True, True),
        _tool("scene_create_camera", "Plan a camera with a lens and an optional look-at target; dry_run=True by default.",
              'scene_create_camera(path="/World/ShotCam", translation=[0, 2, 10], aim_at="/World/Props/Cube", '
              'dry_run=True)', True, True),
        _tool("scene_aim", "Turn a light, camera or other prim so it faces a prim or a point; dry_run=True by default.",
              'scene_aim(path="/World/KeyLight", target="/World/Props/Cube", dry_run=True)', True, True),
        _tool("scene_create_material", "Create a UsdPreviewSurface material with a color, roughness and metallic; "
              "dry_run=True by default.",
              'scene_create_material(path="/World/Looks/Steel", diffuse_color=[0.8, 0.8, 0.8], roughness=0.2, '
              'metallic=1.0, dry_run=True)', True, True),
        _tool("scene_bind_material", "Bind a material to a prim (material:binding); dry_run=True by default.",
              'scene_bind_material(prim_path="/World/Props/Cube", material_path="/World/Looks/Steel", dry_run=True)',
              True, True),
        _tool("scene_material", "Bind an existing material to a prim (older form of scene_bind_material); preview "
              "with dry_run=True.",
              'scene_material(path="/World/Props/Cube", material_path="/World/Looks/Steel", dry_run=True)', True, True),
    ],
    "Assets & physics": [
        _tool("asset_search", "Search the local Isaac Sim asset pack (robots, environments, props, sensors) by name.",
              'asset_search(query="franka", top_k=5)', False, False),
        _tool("asset_info", "One asset file's path, category, size, thumbnail and header (defaultPrim, upAxis, units).",
              'asset_info(asset_path="Isaac/Props/Blocks/basic_block.usd")', False, False),
        _tool("scene_add_asset", "Add an asset file to the open scene as a referencing Xform, placed and unit-checked; "
              "dry_run=True by default.",
              'scene_add_asset(prim_path="/World/Franka", asset_path="C:/.../franka.usd", translation=[0, 0, 0], '
              'dry_run=True)', True, True),
        _tool("scene_physics_report", "Read-only physics setup: PhysicsScene and gravity, rigid bodies, colliders, "
              "mass, joints, and common mistakes.", "scene_physics_report()", False, False),
        _tool("scene_add_physics_scene", "Add a PhysicsScene with gravity in m/s^2 (nothing simulates without one); "
              "dry_run=True by default.", 'scene_add_physics_scene(path="/physicsScene", dry_run=True)', True, True),
        _tool("scene_make_rigid_body", "Make a prim a rigid body with optional mass and colliders on its geometry; "
              "dry_run=True by default.",
              'scene_make_rigid_body(prim_path="/World/Props/Cube", mass=2.0, dry_run=True)', True, True),
        _tool("scene_make_collider", "Add collision to a gprim or every gprim below a group, with an optional "
              "approximation; dry_run=True by default.",
              'scene_make_collider(prim_path="/World/Props/Cube", dry_run=True)', True, True),
        _tool("scene_remove_physics", "Remove rigid body, collision and mass APIs from a prim; dry_run=True by default.",
              'scene_remove_physics(prim_path="/World/Props/Cube", dry_run=True)', True, True),
    ],
    "Simulation & screenshots": [
        _tool("scene_capture_viewport", "Save a PNG screenshot of the live app's active viewport (camera and render "
              "settings untouched).", "scene_capture_viewport()", True, True),
        _tool("scene_capture_status", "Check whether a viewport screenshot has been written yet.",
              'scene_capture_status(path="C:/.../viewport_20261022_101500_000000.png")', False, True),
        _tool("sim_status", "Is the simulation playing, paused or stopped, and at what time.", "sim_status()", False, True),
        _tool("sim_play", "Play the simulation: physics moves simulated prims until sim_stop (simulation state only).",
              "sim_play()", True, True),
        _tool("sim_pause", "Pause the simulation at the current time.", "sim_pause()", True, True),
        _tool("sim_stop", "Stop the simulation: the timeline resets and simulated prims go back to their start poses.",
              "sim_stop()", True, True),
        _tool("sim_step", "Advance a paused simulation by a number of frames (1-600).", "sim_step(frames=10)", True, True),
    ],
    "Undo": [
        _tool("scene_undo_last", "Undo the most recent scene change made through the router; dry_run=True (the "
              "default) shows what would be restored.", "scene_undo_last(dry_run=True)", True, True),
        _tool("scene_undo_history", "List the recent scene changes made through the router that can be undone.",
              "scene_undo_history()", False, True),
    ],
    "Knowledge & docs": [
        _tool("usd_knowledge", "Ask an OpenUSD question; answers from the bundled offline USD docs when the docs MCP "
              "is down.", 'usd_knowledge(query="What is a payload?")', False, False),
        _tool("usd_docs_search", "Search the bundled offline OpenUSD docs and get titled, cited snippets.",
              'usd_docs_search(query="variant set selection", top_k=5)', False, False),
        _tool("learning_glossary", "Look up an OpenUSD glossary term, or list every term.",
              'learning_glossary(term="LIVERPS")', False, False),
        _tool("kit_knowledge", "Ask the Kit SDK knowledge backend (extensions, apps, settings).",
              'kit_knowledge(query="How do I subscribe to stage events in Kit?")', False, False),
        _tool("omni_ui_knowledge", "Ask the omni.ui knowledge backend (windows, widgets, styles).",
              'omni_ui_knowledge(query="How do I make a slider in omni.ui?")', False, False),
        _tool("isaac_sim_knowledge", "Ask the Isaac Sim knowledge backend (robots, sensors, physics); answers from the "
              "installed extension docs when the docs MCP is down.",
              'isaac_sim_knowledge(query="How do I add a lidar sensor?")', False, False),
        _tool("docs_search", "Search the installed Isaac Sim 6.1 / Kit / omni.ui extension docs offline, with cited "
              "snippets (domain isaac_sim, kit or omni_ui).",
              'docs_search(query="articulation controller", domain="isaac_sim", top_k=5)', False, False),
        _tool("extension_info", "Facts about one installed Kit or Isaac Sim extension: version, docs, dependencies, "
              "deprecation and latest changelog entry.", 'extension_info(name="isaacsim.sensors.rtx")', False, False),
        _tool("bowerbot_knowledge", "Ask Bowerbot about asset and scene assembly; phrases like 'new project X' create "
              "project files on disk.", 'bowerbot_knowledge(query="list projects")', True, False),
        _tool("workspace_context", "Read the shared Omniverse workspace notes and reusable lessons.",
              "workspace_context()", False, False),
        _tool("workspace_document", "Read an allowlisted shared document: new_project_template or mcp_cheatsheet.",
              'workspace_document(name="mcp_cheatsheet")', False, False),
        _tool("workspace_learning_index", "List the learning documents and which files belong to the existing demo.",
              "workspace_learning_index()", False, False),
    ],
    "Exam prep": [
        _tool("learning_exam_day", "Today's NCP-OUSD study-plan lesson (or a given day); the first call saves your plan "
              "start date.", "learning_exam_day(day=1)", True, False),
        _tool("learning_exam_day_complete", "Mark one study-plan day complete and see your streak and next day.",
              "learning_exam_day_complete(day=1)", True, False),
        _tool("learning_exam_set_date", "Re-plan the daily lessons from today for your exam date.",
              'learning_exam_set_date(exam_date="2026-12-01", minutes_per_day=60)', True, False),
        _tool("learning_exam_blueprint", "The exam blueprint: the 8 weighted domains, objectives and readings.",
              "learning_exam_blueprint()", False, False),
        _tool("learning_exam_plan", "A weighted study plan that gives weak or untested domains more time.",
              "learning_exam_plan(days_until_exam=30, minutes_per_day=60)", False, False),
        _tool("learning_exam_quiz", "Practice questions (answers withheld) by domain or objective, or due reviews first.",
              'learning_exam_quiz(domain="Composition", count=5)', False, False),
        _tool("learning_exam_answer", "Grade one practice answer and save it to your exam progress.",
              'learning_exam_answer(question_id="comp-001", chosen=["B"])', True, False),
        _tool("learning_exam_progress", "Your saved accuracy per domain, weakest objectives, reviews due and streak.",
              "learning_exam_progress()", False, False),
        _tool("learning_exam_dashboard", "Your progress dashboard (domains, plan, mocks, coverage, what to focus on), "
              "also saved as exam-prep/dashboard.html.", "learning_exam_dashboard(write=True)", True, False),
        _tool("learning_exam_review", "Spaced-repetition questions due now, most overdue first.",
              "learning_exam_review(limit=10)", False, False),
        _tool("learning_exam_reset", "Clear ALL saved exam progress (only with confirm=True; a backup is kept).",
              "learning_exam_reset(confirm=False)", True, False),
        _tool("learning_mock_exam_start", "Start a TIMED mock exam; the clock starts immediately.",
              "learning_mock_exam_start(count=65, minutes=120)", True, False),
        _tool("learning_mock_exam_questions", "Page through the open mock exam's questions.",
              "learning_mock_exam_questions(offset=10)", False, False),
        _tool("learning_mock_exam_answer", "Record or change one mock-exam answer while the clock runs.",
              'learning_mock_exam_answer(session_id="mock-20261021T090000Z-a1b2c3", question_id=1, chosen="A")', True, False),
        _tool("learning_mock_exam_flag", "Flag a mock-exam question to revisit before finishing.",
              'learning_mock_exam_flag(session_id="mock-20261021T090000Z-a1b2c3", question_id=7)', True, False),
        _tool("learning_mock_exam_status", "Time left and what is unanswered or flagged in a mock exam.",
              "learning_mock_exam_status()", False, False),
        _tool("learning_mock_exam_finish", "Score a mock exam per domain, explain wrong answers and save them to progress.",
              'learning_mock_exam_finish(session_id="mock-20261021T090000Z-a1b2c3")', True, False),
        _tool("learning_mock_exam_abandon", "Close an open mock exam without scoring it.",
              'learning_mock_exam_abandon(session_id="mock-20261021T090000Z-a1b2c3")', True, False),
        _tool("learning_composition_puzzle", "A LIVERPS 'which value wins?' puzzle (easy, medium or hard).",
              'learning_composition_puzzle(difficulty="medium")', False, False),
        _tool("learning_composition_puzzle_answer", "Check a puzzle answer by really composing it, and save the result.",
              'learning_composition_puzzle_answer(puzzle_id="liverps-easy-42", answer="B")', True, False),
    ],
    "Learning": [
        _tool("learning_tutor", "A short lesson, cheat sheet or quiz on one topic (schemas, prims, transforms, ...).",
              'learning_tutor(topic="transforms", style="lesson")', False, False),
        _tool("learning_practice", "A hands-on drill for one topic that uses paths from your scene.",
              'learning_practice(topic="materials")', False, False),
        _tool("learning_practice_start", "Start a step-by-step practice session.",
              'learning_practice_start(topic="selection")', False, False),
        _tool("learning_practice_next", "Go to the next step of the practice session.",
              'learning_practice_next(feedback="done")', False, False),
        _tool("learning_scene_quiz", "A quiz about the selected prim or the current scene.",
              'learning_scene_quiz(topic="schemas")', False, False),
        _tool("learning_study_mode", "What to study next, based on what is selected.",
              'learning_study_mode(topic="materials")', False, False),
        _tool("learning_translate", "Turn a sentence into the exact scene command it would run, without running it.",
              'learning_translate(request="make this twice as big")', False, False),
        _tool("learning_history", "Your recent learning actions and progress.", "learning_history(limit=20)", False, False),
        _tool("learning_cheatsheet", "The current cheat sheet of scene and learning commands.",
              "learning_cheatsheet()", False, False),
        _tool("learning_roadmap", "A simple study roadmap from what you have practiced.", "learning_roadmap()", False, False),
        _tool("usd_playground_compose", "Compose your own USDA layers in a throwaway temp folder and see what wins.",
              'usd_playground_compose(layers={"root.usda": "#usda 1.0\\ndef \\"A\\" {}"}, root="root.usda", '
              'queries=["/A"])', False, False),
    ],
    "Health & connection": [
        _tool("router_dispatch", "The front door: send any sentence and the router picks the right tool; scene edits "
              "by exact path apply unless dry_run=True.",
              'router_dispatch(request="add a rect light above /World/Props/Cube", dry_run=True)', True, False),
        _tool("router_route", "See where a sentence would go (and why) without running it.",
              'router_route(request="paint /World/Props/Cube red")', False, False),
        _tool("router_help", "This guide: tools grouped by task with examples; give a topic to narrow it.",
              'router_help(topic="lights")', False, False),
        _tool("router_status", "Which scene and knowledge backends are configured.", "router_status()", False, False),
        _tool("router_health", "Read-only health check: scene backend, port probes and recent router errors.",
              "router_health()", False, False),
        _tool("scene_capabilities", "What the connected scene bridge can do (live insight, light/camera authoring) "
              "and whether it needs a reload.", "scene_capabilities()", False, False),
        _tool("router_scene_targets", "List the scene apps the router can talk to and which one is active.",
              "router_scene_targets()", False, False),
        _tool("router_set_scene_target", "Switch which app (Isaac Sim, Composer or the offline fake scene) the scene "
              "tools talk to; no scene data changes.", 'router_set_scene_target(target="composer")', False, False),
    ],
}

GROUP_ABOUT = {
    "Scene: look": "Read the open scene without changing it.",
    "Scene: change safely": "Edit prims; every tool here can preview with dry_run=True before it changes anything.",
    "Live insight": "Deep read-only USD answers (composition, value resolution, bounds, validation) from the live "
                    "stage or the saved file.",
    "Lights/cameras/materials": "Light, camera and material authoring; previews by default.",
    "Assets & physics": "Find robots and props in the local asset pack, add them by reference, and set up physics; "
                        "edits preview by default.",
    "Simulation & screenshots": "Play, pause, stop and step the live simulation, and save viewport screenshots; these "
                                "change simulation state or write a PNG, never authored scene data.",
    "Undo": "See and undo the changes the router made.",
    "Knowledge & docs": "Ask questions and search docs; USD, Kit, omni.ui and Isaac Sim answers work offline.",
    "Exam prep": "NCP-OUSD daily lessons, practice questions, timed mock exams and composition puzzles.",
    "Learning": "Lessons, drills, quizzes and a USD playground that never touches your scenes.",
    "Health & connection": "Is everything connected, and which app is the router talking to?",
}

# Sentences router_dispatch understands for each group (scene edits by exact path apply unless dry_run=True).
GROUP_PHRASES = {
    "Scene: look": ["show me the scene", "inspect /World/Props/Cube", "find light", "children /World/Props"],
    "Scene: change safely": [
        "add a sphere at 0 0 2",
        "move /World/Props/Cube by 0 0 1",
        "rename this to HeroCube",
        "set intensity of /World/RectLight to 5000",
    ],
    "Live insight": [],
    "Lights/cameras/materials": [
        "add a rect light above /World/Props/Cube",
        "add a camera looking at /World/Props/Cube",
        "make a red material",
        "paint /World/Props/Cube red",
        "bind /World/Looks/Steel to /World/Props/Cube",
    ],
    "Assets & physics": [
        "add a franka robot",
        "find a carter robot",
        "make /World/Props/Cube a rigid body",
        "add collision to /World/Props/Cube",
        "physics report",
    ],
    "Simulation & screenshots": ["take a screenshot", "play the simulation", "step 10 frames", "stop the simulation"],
    "Undo": ["undo", "confirm undo", "what did you change"],
    "Knowledge & docs": ["what is a payload in USD?", "how do I subscribe to stage events in Kit?"],
    "Exam prep": ["start day 1", "next lesson", "mock exam", "show my dashboard"],
    "Learning": ["teach me schemas", "quiz me on prims", "practice transforms with me"],
    "Health & connection": ["help", "status"],
}

_QUICK_START = (
    ("what can you do", "Shows this guide: every tool grouped by task."),
    ("show me the scene", "Summarizes the open stage: root, prim count and selection."),
    ("inspect /World/Props/Cube", "Shows one prim's type, transform, children and material."),
    ("move /World/Props/Cube by 0 0 1", "Moves a prim; send it with dry_run=True to preview first."),
    ("add a rect light above /World/Props/Cube", "Previews a light placed above a prim and aimed at it."),
    ("paint /World/Props/Cube red", "Previews creating a red material and binding it."),
    ("undo", "Previews undoing the router's last change; 'confirm undo' applies it."),
    ("next lesson", "Opens today's NCP-OUSD study lesson."),
)

# One-word or short topics that name a group.
_GROUP_ALIASES = {
    "Scene: look": ("scene", "look", "looking", "inspect", "inspecting", "find", "selection", "select", "view"),
    "Scene: change safely": ("change", "changes", "edit", "edits", "editing", "safe", "safely", "safety", "dry run",
                             "dry_run", "preview", "create", "move", "rename", "delete", "save", "attribute",
                             "attributes", "set attribute"),
    "Live insight": ("insight", "live insight", "composition", "bounds", "validate", "validation", "lint",
                     "properties", "explain value", "value resolution"),
    "Lights/cameras/materials": ("light", "lights", "lighting", "camera", "cameras", "material", "materials",
                                 "shading", "paint", "color", "colour", "lights cameras materials"),
    "Assets & physics": ("asset", "assets", "asset library", "asset pack", "robot", "robots", "props", "reference",
                         "references", "physics", "rigid body", "rigid bodies", "collider", "colliders", "collision",
                         "gravity"),
    "Simulation & screenshots": ("simulation", "simulate", "sim", "play", "pause", "step", "screenshot",
                                 "screenshots", "capture", "viewport"),
    "Undo": ("undo", "undoing", "history", "revert", "rollback"),
    "Knowledge & docs": ("knowledge", "docs", "documentation", "search", "glossary", "ask", "questions", "kit",
                         "omni.ui", "isaac", "bowerbot", "workspace"),
    "Exam prep": ("exam", "exams", "exam prep", "ncp-ousd", "ncp ousd", "certification", "mock", "mock exam",
                  "daily lesson", "daily lessons", "puzzle", "puzzles", "progress", "dashboard"),
    "Learning": ("learn", "learning", "lesson", "lessons", "tutor", "practice", "drill", "quiz", "quizzes", "study",
                 "playground", "cheat sheet", "cheatsheet", "roadmap"),
    "Health & connection": ("health", "connection", "connect", "connected", "status", "bridge", "bridges", "router",
                            "setup", "troubleshoot", "troubleshooting", "targets", "scene target", "ports"),
}
_STOP_WORDS = {"a", "an", "the", "to", "how", "do", "i", "me", "my", "with", "on", "about", "for", "of", "and", "or",
               "use", "using", "tool", "tools", "can", "you", "what", "is", "are", "in"}


def tool_names() -> list[str]:
    """Every tool name in the catalog, in catalog order."""
    return [entry["name"] for entries in TOOL_CATALOG.values() for entry in entries]


def quick_start() -> list[dict[str, str]]:
    """The 8 most useful sentences to send to router_dispatch, each with what it does."""
    return [{"say": say, "does": does, "tool": "router_dispatch"} for say, does in _QUICK_START]


def resolve_topic(topic: str | None, search: bool = False) -> dict[str, Any] | None:
    """What a help topic names: {"kind": "tool"|"group", "name": ...}, a keyword search, or None.

    Exact tool names, group names and group aliases always match; search=True also matches tools whose name,
    description or example contains every significant word of the topic.
    """
    text = _clean(topic)
    if not text:
        return None
    for group, entries in TOOL_CATALOG.items():
        for entry in entries:
            if text in {entry["name"], entry["name"].replace("_", " ")}:
                return {"kind": "tool", "name": entry["name"], "group": group}
    for group in TOOL_CATALOG:
        if text == _clean(group) or text in _GROUP_ALIASES.get(group, ()):
            return {"kind": "group", "name": group}
    if not search:
        return None
    matches = _search(text)
    return {"kind": "search", "names": [entry["name"] for _, entry in matches]} if matches else None


def help(topic: str | None = None) -> dict[str, Any]:  # noqa: A001 - the MCP tool is called router_help
    """The whole guide grouped by task, or just the group or tool a topic names, with examples."""
    if topic is None or not str(topic).strip():
        return {
            "ok": True,
            "topic": None,
            "message": "Here is everything I can do, grouped by task. Ask 'help lights' or 'help undo' for one group.",
            "groups": [_group_view(group) for group in TOOL_CATALOG],
            "quick_start": quick_start(),
            "legend": dict(LEGEND),
            "safety": SAFETY,
        }
    found = resolve_topic(str(topic), search=True)
    if found is None:
        return {
            "ok": False,
            "topic": topic,
            "message": f"I have no help topic '{topic}'. Try a group name, a tool name, or one word such as "
                       "'lights', 'undo', 'exam' or 'health'.",
            "groups": list(TOOL_CATALOG),
            "quick_start": quick_start(),
        }
    if found["kind"] == "tool":
        group = found["group"]
        entry = _entry_view(next(item for item in TOOL_CATALOG[group] if item["name"] == found["name"]))
        return {
            "ok": True,
            "topic": topic,
            "match": "tool",
            "group": group,
            "tool": entry,
            "examples": [entry["example"]],
            "phrases": list(GROUP_PHRASES.get(group, [])),
            "see_also": [item["name"] for item in TOOL_CATALOG[group] if item["name"] != entry["name"]],
            "safety": SAFETY,
        }
    if found["kind"] == "group":
        view = _group_view(found["name"])
        return {
            "ok": True,
            "topic": topic,
            "match": "group",
            **view,
            "examples": [entry["example"] for entry in view["tools"]],
            "safety": SAFETY,
        }
    matches = _search(_clean(topic))
    return {
        "ok": True,
        "topic": topic,
        "match": "search",
        "count": len(matches),
        "tools": [{**_entry_view(entry), "group": group} for group, entry in matches],
        "message": f"Tools that mention '{topic}'.",
    }


def _group_view(group: str) -> dict[str, Any]:
    return {
        "group": group,
        "about": GROUP_ABOUT.get(group, ""),
        "tools": [_entry_view(entry) for entry in TOOL_CATALOG[group]],
        "phrases": list(GROUP_PHRASES.get(group, [])),
    }


def _entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    view = deepcopy(entry)
    if entry["name"] in PLANNED_TOOLS:
        view["planned"] = True
    return view


def _search(text: str) -> list[tuple[str, dict[str, Any]]]:
    words = [word for word in re.findall(r"[\w.:-]+", text) if word not in _STOP_WORDS]
    if not words:
        return []
    matches: list[tuple[str, dict[str, Any]]] = []
    for group, entries in TOOL_CATALOG.items():
        for entry in entries:
            haystack = " ".join((entry["name"], entry["name"].replace("_", " "), entry["what"], group)).lower()
            if all(word in haystack or word.rstrip("s") in haystack for word in words):
                matches.append((group, entry))
    return matches[:MAX_SEARCH_RESULTS]


def _clean(topic: Any) -> str:
    text = re.sub(r"\s+", " ", str(topic or "").strip().lower()).strip(" ?.!'\"")
    return re.sub(r"^(?:the|about|with|on|for)\s+", "", text)
