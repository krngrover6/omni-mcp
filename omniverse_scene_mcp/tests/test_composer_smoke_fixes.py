"""Regressions from a live smoke test of the router against USD Composer (no Isaac libraries, no default prim)."""
from __future__ import annotations

import ast
import asyncio
import re
import tempfile
import types
from pathlib import Path

from omniverse_scene_mcp import joint_control as jc, live_pick as lp, motion_recorder as mr, safe_edit, sdg, sensors
from omniverse_scene_mcp.bridge import PrimRecord, SceneSnapshot
from omniverse_scene_mcp.exam import class_quiz as cq
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.features import FEATURES
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.service import SceneMCPService

SRC = Path(__file__).resolve().parents[1] / "src" / "omniverse_scene_mcp"

# ------------------------------------------------------------------------------------------- 1. hints name real tools

# A name after "call" / "use" / "run" or inside "(or ...)" in a message string must be a tool the router has.
# These are not router tools but real names a beginner types elsewhere (Isaac Python API, ROS 2 programs).
_NOT_TOOLS = {
    "add_default_ground_plane",  # Isaac Python: World.scene.add_default_ground_plane()
    "get_data",  # Isaac Python: IMUSensor(...).get_data()
    "rmw_zenoh_cpp", "rqt_image_view", "setup_ros_env", "teleop_twist_keyboard",  # ROS 2 programs and settings
}
_HINT = re.compile(r"(?:\b[Cc]all|\b[Uu]se|\b[Rr]un|\(or)\s+([a-z][a-z0-9]*_[a-z0-9_]+)\b(?!\s*=)")


def _tool_names() -> set[str]:
    server = (SRC / "server.py").read_text(encoding="utf-8")
    return set(FEATURES) | set(re.findall(r'@app\.tool\(name="([a-z0-9_]+)"', server))


def _message_strings(tree: ast.AST):
    """String constants that are not docstrings (docstrings may name the Python functions themselves)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def test_hints_only_name_tools_that_exist():
    # Seen live in Composer: "Call capture_status(path)", "Press Play (or simulation_play)", "use set_drive_targets"
    # named tools that do not exist (the real ones: scene_capture_status, sim_play, robot_drive_targets).
    tools = _tool_names()
    assert {"scene_capture_status", "sim_play", "robot_drive_targets", "sensor_create"} <= tools
    dead = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _message_strings(tree):
            for match in _HINT.finditer(node.value):
                name = match.group(1)
                if name not in tools and name not in _NOT_TOOLS:
                    dead.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert not dead, "messages name tools that do not exist: " + ", ".join(dead)


def test_stopped_timeline_hint_names_sim_play_and_robot_drive_targets():
    # Seen live in Composer: the stopped-timeline refusal said "simulation_play" and "set_drive_targets".
    saved = (jc._isaac_problem, jc._robot_problem, jc._timeline_state)
    jc._isaac_problem = lambda **_: None
    jc._robot_problem = lambda _root: None
    jc._timeline_state = lambda: ("stopped", None)
    try:
        reason = jc.live_joint_state("/World/franka")["reason"]
    finally:
        jc._isaac_problem, jc._robot_problem, jc._timeline_state = saved
    assert "sim_play" in reason and "robot_drive_targets" in reason and "simulation_play" not in reason, reason


# ------------------------------------------------------------------------------------- 2. sdg plan, missing folder

def test_sdg_plan_says_plainly_that_the_project_folder_is_missing():
    # Seen live in Composer: a missing project_dir leaked "[WinError 2] The system cannot find the file specified".
    missing = Path(tempfile.mkdtemp()) / "no_such_project"
    plan = sdg.plan_sdg(str(missing))
    assert plan["ok"] is False and not plan["written"], plan
    assert "does not exist" in plan["error"] and "existing folder" in plan["error"], plan
    assert "WinError" not in plan["error"] and "dataset" in plan["error"], plan


# --------------------------------------------------------------------------- 3. Isaac-only tools notice Composer

def _kit_finder(*present: str):
    """A _kit_module stand-in: Kit's own modules exist, plus only the names in `present`."""
    timeline = types.SimpleNamespace(get_timeline_interface=lambda: types.SimpleNamespace(
        is_playing=lambda: True, is_stopped=lambda: False))
    usd = types.SimpleNamespace(get_context=lambda: types.SimpleNamespace(get_stage=lambda: None))
    modules = {"omni.timeline": timeline, "omni.usd": usd}
    modules.update({name: types.ModuleType(name) for name in present})
    return lambda name: modules.get(name)


def test_isaac_problem_only_speaks_inside_kit_without_isaac():
    # Seen live in Composer: robot and sensor tools only said "Press Play" in an app with no Isaac libraries.
    assert jc._isaac_problem(kit_module=lambda _name: None) is None  # outside Kit: callers keep their own refusal
    assert jc._isaac_problem(kit_module=_kit_finder()) == jc.NEEDS_ISAAC  # Composer
    assert jc._isaac_problem(kit_module=_kit_finder("isaacsim.core.prims")) is None  # Isaac Sim
    assert jc._isaac_problem(sensors=True, kit_module=_kit_finder()) == jc.NEEDS_ISAAC
    assert jc._isaac_problem(sensors=True, kit_module=_kit_finder("isaacsim.sensors.experimental.rtx")) is None
    assert "Isaac Sim" in jc.NEEDS_ISAAC and "router_set_scene_target('isaac')" in jc.NEEDS_ISAAC


def test_robot_tools_refuse_in_composer_before_the_timeline_check():
    # Seen live in Composer: robot_joint_state and robot_live_pick said only "Press Play".
    saved = jc._kit_module
    jc._kit_module = _kit_finder()  # Kit is there (timeline playing), Isaac's libraries are not
    try:
        results = [
            jc.live_joint_state("/World/franka"),
            jc.live_move("/World/franka", {"joint1": 10}),
            jc.live_move("/World/franka", {"joint1": 10}, dry_run=False),
            jc.live_spin("/World/franka", {"wheel": 90}),
            mr.record_start("/World/franka"),
        ]
        pick = lp.live_pick("/World/franka", "/World/Cube", [0.3, 0.3, 0.05])
    finally:
        jc._kit_module = saved
        mr._ACTIVE.clear()
        lp._JOB.clear()
    for result in results:
        assert result["ok"] is False and result["reason"] == jc.NEEDS_ISAAC, result
        assert not result.get("applied")
    assert pick["ok"] is False and pick["error"] == jc.NEEDS_ISAAC, pick


def test_sensor_create_refuses_in_composer_even_as_a_dry_run():
    # Seen live in Composer: the dry run planned happily and invited dry_run=False.
    saved = sensors._kit_module
    sensors._kit_module = _kit_finder()
    try:
        preview = sensors.create_sensor_live("imu", "/World/Link", name="Imu")
        real = sensors.create_sensor_live("imu", "/World/Link", name="Imu", dry_run=False)
    finally:
        sensors._kit_module = saved
    assert preview["ok"] is False and preview["dry_run"] and preview["error"] == jc.NEEDS_ISAAC, preview
    assert real["ok"] is False and real["error"] == jc.NEEDS_ISAAC and "next_step" not in real, real


def test_robot_tools_say_when_the_path_is_not_a_robot():
    # Seen live in Composer: a plain Xform given as robot_root got "Press Play" instead of "not a robot".
    try:
        from pxr import Usd, UsdGeom, UsdPhysics
    except ImportError:  # pxr is an optional extra: skip without it
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World/Box")
    UsdGeom.Xform.Define(stage, "/World/Bot")
    UsdPhysics.ArticulationRootAPI.Apply(UsdGeom.Xform.Define(stage, "/World/Bot/base").GetPrim())
    finder = _kit_finder("isaacsim.core.experimental.prims")
    usd = types.SimpleNamespace(get_context=lambda: types.SimpleNamespace(get_stage=lambda: stage))
    saved = jc._kit_module
    jc._kit_module = lambda name: usd if name == "omni.usd" else finder(name)
    try:
        assert "/World/Box is not a robot (no articulation root)" in jc._robot_problem("/World/Box")
        assert jc._robot_problem("/World/Bot") is None  # the articulation root sits on a child: still a robot
        assert jc._robot_problem("/World/Missing") is None  # the prim check says that more precisely
        state = jc.live_joint_state("/World/Box")
        pick = lp.live_pick("/World/Box", "/World/Bot", [0.3, 0.3, 0.05])
    finally:
        jc._kit_module = saved
        lp._JOB.clear()
    assert not state["ok"] and "is not a robot" in state["reason"] and "Play" not in state["reason"], state
    assert not pick["ok"] and "is not a robot" in pick["error"], pick


# -------------------------------------------------------------------------------------- 4. typeless prims stay so

def test_typeless_prims_are_not_reported_as_xform():
    # Seen live in Composer: an implicitly created parent (no type) showed as "Xform" in scene_delete / scene_inspect.
    assert HttpSceneBridge._prim_from_dict({"path": "/World/McpSmoke"}).type_name == ""
    assert HttpSceneBridge._prim_from_dict({"path": "/World/McpSmoke", "type_name": None}).type_name == ""
    try:
        from pxr import Usd
    except ImportError:  # pxr is an optional extra: the live-bridge half needs it
        return
    from omniverse_scene_mcp import kit_bridge

    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/McpSmoke/Box", "Cube")  # /World and /World/McpSmoke come out typeless
    context = types.SimpleNamespace(get_stage=lambda: stage, get_selection=lambda: types.SimpleNamespace(
        get_selected_prim_paths=lambda: [], set_selected_prim_paths=lambda *_: None))
    previous = kit_bridge.omni
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        service = SceneMCPService(kit_bridge.LiveKitSceneBridge())
        inspected = service.inspect_prim("/World/McpSmoke")
        preview = service.delete_prim("/World/McpSmoke", dry_run=True)
        box = service.inspect_prim("/World/McpSmoke/Box")
    finally:
        kit_bridge.omni = previous
    assert inspected["type_name"] == "" and preview.changes[0].before["type_name"] == "", inspected
    assert box["type_name"] == "Cube"


# --------------------------------------------------------------------------- 5. scene_create names new parents

def test_create_preview_lists_the_parents_it_will_also_create():
    # Seen live in Composer: creating /World/McpSmoke/Box also made /World/McpSmoke (typeless), unmentioned.
    service = SceneMCPService(FakeSceneBridge())  # the fake scene has /World but no /World/McpSmoke
    preview = service.create_prim("/World/McpSmoke/Box", "Cube", dry_run=True)
    assert preview.payload["also_creates_parents"] == ["/World/McpSmoke"], preview.payload
    assert "no type" in preview.payload["parents_note"] and "Xform" in preview.payload["parents_note"]
    plain = service.create_prim("/World/Props/NewCube", "Cube", dry_run=True)
    assert "also_creates_parents" not in plain.payload, plain.payload


# ----------------------------------------------------------------- 6. scene_summary with no default prim ("/")

class _RootStageBridge:
    """A Composer-like stage: no default prim, Kit's cameras and /Render at the root, plus /World."""

    def snapshot(self):
        roots = ["/OmniverseKit_Persp", "/OmniverseKit_Front", "/OmniverseKit_Top", "/OmniverseKit_Right", "/Render",
                 "/World"]
        prims = [PrimRecord(path=path, type_name="Camera", parent=None) for path in roots]
        prims.append(PrimRecord(path="/World/McpSmoke", type_name="", parent="/World"))
        return SceneSnapshot(stage_name="anon", root_path="/", selection=[], prim_count=len(prims), prims=prims)


def test_scene_summary_lists_root_prims_when_there_is_no_default_prim():
    # Seen live in Composer: top_level_prims was [] although the stage root had 5 prims.
    summary = SceneMCPService(_RootStageBridge()).scene_summary()
    assert "/World" in summary["top_level_prims"] and len(summary["top_level_prims"]) == 6, summary
    assert "/World/McpSmoke" not in summary["top_level_prims"]
    assert len(summary["kit_own_prims"]) == 5 and "Kit's own viewport cameras" in summary["note"], summary


# ----------------------------------------------------------------- 7. safe_edit preview: no "dry_run=False" hints

def test_safe_edit_preview_drops_the_inner_dry_run_false_next_step():
    # Seen live in Composer: the preview showed the inner "Call again with dry_run=False", against the confirm code.
    inner = {"ok": True, "dry_run": True, "next_step": "Dry run: nothing changed. Call again with dry_run=False.",
             "payload": {"path": "/World/Box", "next": "Then call with dry_run=False to apply."},
             "steps": [{"next_step": "use dry_run=False"}], "message": "Would move /World/Box."}

    async def call(name, args):
        if name == "scene_move":
            return inner
        raise ValueError("not in this fake")

    schema = {"properties": {"path": {}, "dry_run": {}}}
    result = asyncio.run(safe_edit.safe_edit(call, lambda _tool: schema, "scene_move", {"path": "/World/Box"}))
    assert result["ok"] and result["step"] == "preview", result
    shown = result["preview"]
    assert "dry_run=False" not in str(shown) and shown["message"] == "Would move /World/Box.", shown
    assert shown["payload"]["path"] == "/World/Box" and "confirm=" in result["next"]
    assert "next_step" in inner  # the wrapped tool's own result is not changed


# ------------------------------------------------------------------ 8. composition diagram: real Mermaid <br/>

def test_composition_diagram_keeps_mermaid_line_breaks():
    # Seen live in Composer: the label was HTML-escaped after joining, so Mermaid showed a literal "&lt;br/&gt;".
    try:
        from pxr import Sdf, Usd
    except ImportError:  # pxr is an optional extra: skip without it
        return
    from omniverse_scene_mcp import composition_diagram as cd

    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Lamp", "Xform").CreateAttribute("intensity", Sdf.ValueTypeNames.Double).Set(5.0)
    result = cd.diagram(stage, "/World/Lamp", "intensity")
    assert result["ok"], result
    assert "<br/>" in result["mermaid"] and "&lt;br/&gt;" not in result["mermaid"], result["mermaid"]
    assert cd._quote('a <b> "c"') == "a &lt;b&gt; 'c'"  # the parts themselves are still escaped


# ------------------------------------------------------------------------ 9. class quiz skips filler sentences

def test_class_quiz_rejects_vague_and_short_sentences():
    # Seen live in Composer: "Do not try to use the ____ which are over here." became a question.
    assert cq._quality("Do not try to use the payload which are over here.", "payload") == cq.REJECT
    assert cq._quality("You can load the payload and everything.", "payload") == cq.REJECT  # under 8 words, vague
    assert cq._quality("Then we just open the payload window in the menu bar.", "payload") == cq.REJECT  # no verb
    good = "A payload can be switched off, so heavy files only load when they are needed."
    assert cq._quality(good, "payload") >= 2
