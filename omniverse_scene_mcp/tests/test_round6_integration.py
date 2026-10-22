"""Round 6 integration: the learning helpers (Python snippets, Python practice, error explanations, homework checks,
the scene tour and the class notes) wired through every layer (server tool -> router -> service -> HTTP client ->
extended RPC tier5-1 -> Kit bridge), "show_python" on the editing tools, and the plain-English router phrases.

Offline: a test-mode server over a temp workspace, the in-memory fake scene or an in-process HTTP transport into the
extended RPC over a temporary pxr stage (only Kit's omni modules are faked). Every socket is refused and the real
exam-prep folder is checked after every server (test_twelve_gaps_integration's _guard). The class notes come from a
temporary folder (OMNIVERSE_CLASS_NOTES_ROOT). Plain fixture-free test_* functions, so the stdlib runner runs them.
"""

import os

# The stdlib test runner may not load conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp import kit_rpc, nl_tier5, python_links
from omniverse_scene_mcp.bridge import HOMEWORK_RPC_METHODS, LIVE_RPC_METHODS, RPC_VERSION
from omniverse_scene_mcp.exam import python_practice
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.kit_bridge import LiveKitSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService
from test_round5_integration import _live, _live_stage
from test_twelve_gaps_integration import COMPOSER, ISAAC, _BasicServer, _env, _in_process_transport, _layers_text, _server

try:
    from pxr import Usd, UsdGeom
except ImportError:  # pragma: no cover - the pxr tests below then return early
    Usd = UsdGeom = None

WORKSPACE = Path(__file__).resolve().parents[2]
NEW_TOOLS = {
    "python_show",
    "python_topics",
    "python_practice_list",
    "python_practice_get",
    "python_practice_run",
    "explain_error",
    "assignment_list",
    "assignment_check",
    "scene_explain",
    "class_sessions",
    "class_ask",
    "class_search",
}
SHOW_PYTHON_TOOLS = (
    "router_dispatch", "scene_create", "scene_delete", "scene_duplicate", "scene_rename", "scene_move",
    "scene_material", "scene_variant_set", "scene_set_attribute", "scene_create_light", "scene_create_camera",
    "scene_aim", "scene_create_material", "scene_bind_material", "scene_add_asset", "scene_add_physics_scene",
    "scene_make_rigid_body", "scene_make_collider",
)
SRT = """1
00:00:01,000 --> 00:00:05,000
Today we add a ground plane to the stage so the boxes have a floor.

2
00:00:06,000 --> 00:00:10,000
Then we group the shelves under one Xform called Shelves.
"""
HELPER_PHRASES = {
    "show me the python for moving a cube": "python_show",
    "how do I write python to add a light": "python_show",
    "python code for creating a camera": "python_show",
    "explain this error: NameError: name 'stage' is not defined": "explain_error",
    "check my homework": "assignment_check",
    "check my assignment": "assignment_check",
    "what's next on my homework": "assignment_check",
    "explain my scene": "scene_explain",
    "what did the teacher say about ground plane": "class_ask",
    "give me a python exercise": "python_practice_get",
    "list python exercises": "python_practice_list",
    "python topics": "python_topics",
    "list assignments": "assignment_list",
    "list class sessions": "class_sessions",
}


# --------------------------------------------------------------------------- helpers


@contextlib.contextmanager
def _classes():
    """A temporary class folder with one session's subtitles, used by every ClassNotes built while the block runs."""
    with tempfile.TemporaryDirectory() as tmp:
        session = Path(tmp) / "Session 2"
        session.mkdir()
        (session / "Session 2 recording.srt").write_text(SRT, encoding="utf-8")
        with _env(OMNIVERSE_CLASS_NOTES_ROOT=tmp):
            yield Path(tmp)


def _homework_file(root: Path) -> Path:
    """A small saved scene inside the temp workspace: a floor and a cube, nowhere near a finished homework."""
    path = root / "homework" / "hw.usda"
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/World")
    floor = UsdGeom.Cube.Define(stage, "/World/Floor")
    floor.AddScaleOp().Set((10.0, 10.0, 0.05))
    UsdGeom.Cube.Define(stage, "/World/Box")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    stage.GetRootLayer().Save()
    return path


def _route(router, text):
    return router.route(text)


# --------------------------------------------------------------------------- registration and wiring


def test_round6_tools_are_registered_read_only_and_catalogued():
    with _server() as call:
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert NEW_TOOLS <= set(tools) and len(tools) >= 110, len(tools)
        for name in NEW_TOOLS - {"python_practice_run"}:
            text = " ".join(tools[name].description.split())
            assert text.startswith("Read-only"), name
        run = " ".join(tools["python_practice_run"].description.split())
        assert "throwaway temporary copy" in run and "record=False saves nothing" in run
        for name in SHOW_PYTHON_TOOLS:
            properties = tools[name].parameters["properties"]
            assert properties["show_python"]["default"] is False, name
            assert "show_python=True" in tools[name].description, name
        # The dry-run defaults of the edit tools are unchanged.
        for name in ("scene_create_light", "scene_set_attribute", "scene_add_asset", "scene_make_collider"):
            assert tools[name].parameters["properties"]["dry_run"]["default"] is True, name
        assert tools["assignment_check"].parameters["properties"]["assignment_id"]["default"] == "isaac_session02_warehouse"
    catalog = {entry["name"]: entry for entries in tool_guide.TOOL_CATALOG.values() for entry in entries}
    assert NEW_TOOLS <= set(tool_guide.TOOL_CATALOG["Learning Python & homework"][i]["name"]
                            for i in range(len(tool_guide.TOOL_CATALOG["Learning Python & homework"])))
    assert catalog["python_practice_run"]["mutates"] is True  # it saves the graded attempt to exam progress
    for name in NEW_TOOLS - {"python_practice_run"}:
        assert catalog[name]["mutates"] is False, name
    assert tool_guide.help("homework")["group"] == "Learning Python & homework"
    assert tool_guide.help("python")["group"] == "Learning Python & homework"


def test_homework_rpc_methods_line_up_across_the_protocol_the_rpc_and_every_bridge():
    assert RPC_VERSION == "tier7-1"
    assert set(HOMEWORK_RPC_METHODS) == {"assignment_check", "scene_tour"}
    assert set(HOMEWORK_RPC_METHODS) <= set(LIVE_RPC_METHODS) <= set(kit_rpc._HANDLERS)
    for owner in (FakeSceneBridge, HttpSceneBridge, LiveKitSceneBridge, SceneMCPService):
        missing = [name for name in HOMEWORK_RPC_METHODS if not callable(getattr(owner, name, None))]
        assert not missing, (owner.__name__, missing)
    for name in HOMEWORK_RPC_METHODS:  # read-only: never dropped as stale writes, always time-budgeted
        assert name not in kit_rpc._WRITE_METHODS and name in kit_rpc._BUDGETED_METHODS, name


def test_extended_rpc_parses_homework_params_strictly_and_budgets_the_tour():
    calls = []

    class Recorder:
        def assignment_check(self, assignment_id):
            calls.append(("assignment_check", assignment_id))
            return {"ok": True, "assignment": assignment_id}

        def scene_tour(self, focus_path, *, depth, max_prims):
            calls.append(("scene_tour", focus_path, depth, max_prims))
            return {"ok": True, "truncated_by_time": False, "sections": []}

    server = kit_rpc.make_extended_server_class(_BasicServer)(Recorder())
    answer = server._dispatch({"method": "assignment_check", "params": {}})["result"]
    assert answer == {"ok": True, "assignment": "isaac_session02_warehouse"}
    tour = server._dispatch({"method": "scene_tour", "params": {"focus_path": "/World", "budget_ms": 500}})["result"]
    assert calls[-1] == ("scene_tour", "/World", 3, 400)
    if tour.get("time_budget"):  # with pxr the ambient TimeBudget was enforced and recorded
        assert tour["time_budget"]["budget_ms"] == 500.0 and tour["truncated_by_time"] is False
    server._dispatch({"method": "scene_tour", "params": {"depth": 0, "max_prims": 1}})
    assert calls[-1] == ("scene_tour", None, 0, 1)
    for bad in (
        {"method": "assignment_check", "params": {"assignment_id": "", "x": 1}},
        {"method": "assignment_check", "params": {"assignment_id": 3}},
        {"method": "scene_tour", "params": {"depth": -1}},
        {"method": "scene_tour", "params": {"depth": True}},
        {"method": "scene_tour", "params": {"max_prims": 0}},
        {"method": "scene_tour", "params": {"max_prims": 10 ** 9}},
        {"method": "scene_tour", "params": {"focus_path": ""}},
        {"method": "scene_tour", "params": {"budget_ms": -5}},
    ):
        try:
            server._dispatch(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    lacking = kit_rpc.make_extended_server_class(_BasicServer)(object())._dispatch({"method": "scene_tour", "params": {}})
    assert lacking["result"]["needs_bridge_update"] is True and lacking["result"]["method"] == "scene_tour"
    info = server._dispatch({"method": "capabilities", "params": {}})["result"]
    assert info["version"] == "tier7-1" and info["homework_methods"] == list(HOMEWORK_RPC_METHODS)


# --------------------------------------------------------------------------- show_python


def test_show_python_is_off_by_default_and_never_changes_the_edit_result():
    cases = (
        ("scene_create", {"path": "/World/Ball", "type_name": "Sphere", "dry_run": True}),
        ("scene_move", {"path": "/World/Props/Cube", "translation": [0, 0, 1], "dry_run": True}),
        ("scene_rename", {"path": "/World/Props/Cube", "new_name": "Box", "dry_run": True}),
        ("scene_delete", {"path": "/World/Props/Cube", "dry_run": True}),
        ("scene_set_attribute", {"path": "/World/Props/Cube", "name": "size", "value": 2.0}),
        ("scene_create_light", {"path": "/World/Key", "light_type": "rect", "intensity": 5000}),
        ("scene_create_material", {"path": "/World/Looks/Red", "diffuse_color": [1, 0, 0]}),
        ("scene_make_rigid_body", {"prim_path": "/World/Props/Cube", "mass": 2.0}),
    )
    with _server() as call:
        bridge = call.router._scene._bridge
        state = repr(bridge._state())
        for name, arguments in cases:
            plain = call(name, **arguments)
            off = call(name, **arguments, show_python=False)
            assert plain == off and "python" not in plain, name
            shown = call(name, **arguments, show_python=True)
            assert {key: value for key, value in shown.items() if key != "python"} == plain, name
            assert shown["python"]["ok"] is True and shown["python"]["code"].strip(), (name, shown["python"])
        assert repr(bridge._state()) == state, "dry runs and snippets change nothing"
        moved = call("scene_move", path="/World/Props/Cube", translation=[0, 0, 1], dry_run=True, show_python=True)
        assert moved["python"]["action"] == "move" and "SetTranslate" in moved["python"]["code"]
        assert moved["python"]["args"]["prim_path"] == "/World/Props/Cube"
        # A snippet problem is reported inside 'python' and never fails the edit itself.
        odd = call("scene_create", path="/World/Odd", type_name="Weird", dry_run=True, show_python=True)
        assert odd["python"]["ok"] is False and "type_name" in odd["python"]["error"]
        bad = call("scene_create_light", path="/World/Key", light_type="rect", color=[1, 2], show_python=True)
        assert bad["ok"] is False and bad["applied"] is False and bad["python"]["ok"] is True
        assert call("scene_remove_physics", prim_path="/World/Props/Cube").get("python") is None


def test_router_dispatch_show_python_for_scene_edits_only():
    with _server() as call:
        state = repr(call.router._scene._bridge._state())
        result = call("router_dispatch", request="move /World/Props/Cube by 0 0 1", dry_run=True, show_python=True)
        assert result["python"]["ok"] is True and result["python"]["action"] == "move"
        assert "relative move" in result["python"]["note"]
        plain = call("router_dispatch", request="move /World/Props/Cube by 0 0 1", dry_run=True)
        assert "python" not in plain and plain["payload"] == result["payload"]
        other = call("router_dispatch", request="teach me schemas", show_python=True)
        assert other["python"]["ok"] is False
        assert repr(call.router._scene._bridge._state()) == state
    snippet = python_links.python_for_command({"action": "paint", "path": "/World/Cube", "color": (1.0, 0.0, 0.0)})
    assert snippet["ok"] is True and snippet["action"] == "create_material" and snippet["args"]["bind_to"] == "/World/Cube"
    assert python_links.python_for_command(None)["ok"] is False
    assert python_links.python_for_tool("scene_reparent", {"path": "/World/A"})["ok"] is False


# --------------------------------------------------------------------------- Python snippets and practice


def test_python_show_and_topics():
    with _server() as call:
        shown = call("python_show", action="scene_create_light", args={"light_type": "dome"})
        assert shown["ok"] is True and shown["action"] == "create_light" and "UsdLux" in shown["code"]
        assert all(row["explanation"] for row in shown["lines"]) and isinstance(shown["exam_link"], list)
        standalone = call("python_show", action="move", context="standalone")
        assert standalone["ok"] is True and "stage.Save()" in standalone["code"]
        assert call("python_show", action="fly_to_the_moon")["ok"] is False
        assert call("python_show", action="move", context="browser")["ok"] is False
        topics = call("python_topics")
        assert topics["ok"] is True and topics["count"] >= 20 and topics["groups"]


def test_python_practice_records_progress_only_in_memory_and_picks_the_next_exercise():
    if Usd is None or getattr(__import__("pxr"), "__file__", None) is None:
        return  # practice runs need an installed usd-core (a namespace-package pxr, as in Kit's libs, cannot run)
    with _server() as call:
        listing = call("python_practice_list")
        assert listing["ok"] is True and listing["count"] == 24 and listing["passed_count"] == 0
        first = listing["next_exercise"]
        assert all("starter_code" not in row and "solution" not in row for row in listing["exercises"])
        exercise = call("python_practice_get")
        assert exercise["id"] == first and "solution" not in exercise and exercise["why_this_one"]
        assert call("python_practice_list", level="expert")["ok"] is False
        solution = call("python_practice_get", exercise_id=first, reveal=True)["solution"]
        wrong = call("python_practice_run", exercise_id=first, code="x = 1\n", record=False)
        assert wrong["ok"] is True and wrong["passed"] is False and wrong["progress"]["saved"] is False
        right = call("python_practice_run", exercise_id=first, code=solution)
        assert right["ok"] is True and right["passed"] is True, right
        assert isinstance(right["progress"], dict) and right["progress"].get("ok") is not False
        after = call("python_practice_list")
        assert after["passed_count"] == 1 and after["next_exercise"] != first
        assert call("python_practice_get")["id"] == after["next_exercise"]
        history = call("learning_exam_progress")["recent"]
        assert history[0]["question_id"] == first and history[0]["source"] == python_practice.SOURCE
        assert call.router._scene is not None
        unknown = call("python_practice_run", exercise_id="nope", code="pass")
        assert unknown["ok"] is False and unknown["progress"]["saved"] is False


# --------------------------------------------------------------------------- errors


def test_explain_error_tool_explains_without_running_anything():
    with _server() as call:
        text = "Traceback (most recent call last):\n  File \"<string>\", line 3\nAttributeError: 'NoneType' object has no attribute 'GetAttribute'"
        result = call("explain_error", text=text, code="prim = None\nprim.GetAttribute('size')\n")
        assert result["ok"] is True and result["exception"] == "AttributeError" and result["how_to_fix"]
        offline = call("explain_error", text="NameError: name 'stage' is not defined", search_docs=False)
        assert offline["ok"] is True and "related_docs" not in offline
        empty = call("explain_error", text="")
        assert empty["ok"] is False or empty["kind"]


# --------------------------------------------------------------------------- homework and scene tour


def test_assignment_and_scene_tour_on_the_fake_scene_say_why_and_suggest_scene_file():
    with _server() as call:
        listing = call("assignment_list")
        assert listing["ok"] is True and listing["default"] == "isaac_session02_warehouse" and listing["count"] >= 1
        for name in ("assignment_check", "scene_explain"):
            result = call(name)
            assert result["ok"] is False and result["needs_bridge_update"] is False, (name, result)
            assert "fake scene" in result["live_fallback"]["reason"] and "scene_file=" in result["message"], name
            assert "how_to_enable" not in result, name
        assert call("scene_explain", depth=-1)["ok"] is False
        assert call("scene_explain", max_prims=0)["ok"] is False
        unknown = call("assignment_check", assignment_id="no_such_homework", scene_file="missing.usda")
        assert unknown["ok"] is False


def test_assignment_check_and_scene_explain_read_a_saved_file_without_touching_it():
    if Usd is None:
        return
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        path = _homework_file(root)
        before = (path.stat().st_mtime_ns, path.read_bytes())
        report = call("assignment_check", scene_file="homework/hw.usda")
        assert report["ok"] is True and report["mode"] == "saved_file" and report["results"], report
        assert report["passed"] is False and report["next_step"] and report["scene_file"].endswith("hw.usda")
        tour = call("scene_explain", scene_file=str(path))
        assert tour["ok"] is True and tour["mode"] == "saved_file" and tour["sections"] and tour["overview"]
        focused = call("scene_explain", scene_file=str(path), focus_path="/World/Box", depth=1)
        assert focused["ok"] is True
        assert (path.stat().st_mtime_ns, path.read_bytes()) == before
        outside = call("assignment_check", scene_file=str(WORKSPACE / "nowhere" / "x.usda"))
        assert outside["ok"] is False and outside["mode"] == "saved_file"
        # The sentence form reads the same file.
        said = call.dispatch("check my homework in homework/hw.usda")
        assert said["tool"] == "assignment_check" and said["ok"] is True and said["mode"] == "saved_file"


def test_older_bridges_get_needs_bridge_update_and_the_scene_file_hint():
    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(COMPOSER))) as call:
        with _in_process_transport(_BasicServer(FakeSceneBridge())):
            for name in ("assignment_check", "scene_explain"):
                result = call(name)
                assert result["ok"] is False and result["needs_bridge_update"] is True, (name, result)
                assert result["how_to_enable"] and "reload_composer_bridge.py" in json.dumps(result), name
                assert "scene_file=" in result["message"] and "Reload the bridge" in result["message"], name
            capabilities = call("scene_capabilities")
            assert capabilities["needs_bridge_update"] is True
            assert set(HOMEWORK_RPC_METHODS) <= set(capabilities["missing_live_methods"])


def test_live_homework_check_and_scene_tour_through_the_rpc_change_nothing():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        stage = _live_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (_b, _s, sent):
            capabilities = call("scene_capabilities")
            assert capabilities["rpc_version"] == "tier7-1" and capabilities["needs_bridge_update"] is False
            assert capabilities["homework"]["available"] is True
            report = call("assignment_check")
            assert report["ok"] is True and report["mode"] == "live" and report["read_only"] is True, report
            assert report["assignment"] == "isaac_session02_warehouse" and report["results"]
            assert report["note"].startswith("Read from the LIVE stage")
            tour = call("scene_explain", focus_path="/World", depth=2)
            assert tour["ok"] is True and tour["mode"] == "live" and tour["action"] == "scene_explain", tour
            assert tour["sections"] and "time_budget" in tour
            said = call.dispatch("explain my scene")
            assert said["tool"] == "scene_explain" and said["ok"] is True and said["mode"] == "live"
            nxt = call.dispatch("what's next on my homework")
            assert nxt["tool"] == "assignment_check" and nxt["ok"] is True and nxt["answer"] == nxt["next_step"]
            methods = [item["method"] for item in sent]
            assert "assignment_check" in methods and "scene_tour" in methods
            assert all(item["params"].get("dry_run") is None for item in sent if item["method"] in HOMEWORK_RPC_METHODS)
        assert _layers_text(stage) == original, "the homework check and the tour only read the stage"


# --------------------------------------------------------------------------- class notes


def test_class_notes_tools_read_the_configured_folder():
    with _classes() as folder:
        with _server() as call:
            sessions = call("class_sessions")
            assert sessions["ok"] is True and sessions["count"] == 1 and sessions["sessions"][0]["session"] == 2
            assert str(folder) in sessions["sources"]["roots"]
            answer = call("class_ask", question="ground plane")
            assert answer["ok"] is True and answer["answer_passages"][0]["session"] == 2
            hits = call("class_search", query="shelves xform", session=2)
            assert hits["ok"] is True and hits["count"] >= 1 and hits["results"][0]["source"] == "subtitles"
            assert call("class_search", query="quantum chromodynamics")["ok"] is False
            said = call.dispatch("what did the teacher say about ground plane")
            assert said["tool"] == "class_ask" and said["ok"] is True and said["answer_passages"]
        assert sorted(p.name for p in folder.rglob("*")) == ["Session 2", "Session 2 recording.srt"]


# --------------------------------------------------------------------------- natural-language routing


def test_learning_helper_phrases_route_to_the_new_tools_and_change_nothing():
    with _classes(), _server() as call:
        bridge = call.router._scene._bridge
        state = repr(bridge._state())
        for phrase, tool in HELPER_PHRASES.items():
            decision = call.router.route(phrase)
            assert decision.target is RouteTarget.LEARN and "Learning helper" in decision.reason, (phrase, decision)
            payload = call.dispatch(phrase)
            assert payload["tool"] == tool and payload["read_only"] is True and payload["executed"] is True, phrase
            json.dumps(payload)
        shown = call.dispatch("show me the python for moving a cube")
        assert shown["ok"] is True and shown["action"] == "move" and "SetTranslate" in shown["code"]
        light = call.dispatch("how do I write python to add a dome light")
        assert light["action"] == "create_light" and light["args"]["light_type"] == "dome"
        standalone = call.dispatch("show me the standalone python to add a reference")
        assert standalone["context"] == "standalone" and "Usd.Stage.Open" in standalone["code"]
        error = call.dispatch("explain this error: NameError: name 'stage' is not defined")
        assert error["ok"] is True and error["exception"] == "NameError"
        pasted = call.dispatch("Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'size'")
        assert pasted["tool"] == "explain_error" and pasted["exception"] == "KeyError"
        exercise = call.dispatch("give me an easy python exercise")
        assert exercise["ok"] is True and exercise["level"] == "beginner" and "solution" not in exercise
        assert repr(bridge._state()) == state, "learning-helper sentences never change the scene"


def test_existing_phrases_keep_their_routes():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    expected = {
        "move /World/Props/Cube by 0 0 1": RouteTarget.SCENE,
        "add a rect light above /World/Props/Cube": RouteTarget.SCENE,
        "make /World/Props/Cube a rigid body": RouteTarget.SCENE,
        "take a screenshot": RouteTarget.SCENE,
        "teach me schemas": RouteTarget.LEARN,
        "practice transforms with me": RouteTarget.LEARN,
        "show my dashboard": RouteTarget.LEARN,
        "help": RouteTarget.LEARN,
        "what is a payload in USD?": RouteTarget.USD,
    }
    for phrase, target in expected.items():
        decision = router.route(phrase)
        assert decision.target is target, (phrase, decision)
        assert "Learning helper" not in decision.reason, phrase
    for phrase in ("what is in the kitchen scene?", "explain composition arcs", "what is the error", "quiz me on prims",
                   "find a carter robot", "add a sphere at 0 0 2", "explain /World/Cube", "start day 1"):
        assert nl_tier5.parse(phrase) is None, phrase


def test_router_without_server_helpers_answers_the_pure_ones_and_names_the_rest():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    shown = asyncio.run(router.handle("show me the python for moving a cube")).payload
    assert shown["ok"] is True and shown["tool"] == "python_show" and shown["executed"] is True
    error = asyncio.run(router.handle("explain this error: KeyError: 'x'")).payload
    assert error["tool"] == "explain_error" and error["ok"] is True
    homework = asyncio.run(router.handle("check my homework")).payload
    assert homework["executed"] is False and "assignment_check(" in homework["message"]
    notes = asyncio.run(router.handle("what did the teacher say about ground plane")).payload
    assert notes["executed"] is False and notes["tool"] == "class_ask"


def test_nl_tier5_parses_targets_values_and_contexts():
    spec = nl_tier5.parse("give me the code to move /World/Cube to 1 2 3")
    assert spec["action"] == "move" and spec["args"] == {"prim_path": "/World/Cube", "translate": (1.0, 2.0, 3.0)}
    spec = nl_tier5.parse("how do i create a sphere with python")
    assert spec["action"] == "create_prim" and spec["args"]["type_name"] == "Sphere"
    spec = nl_tier5.parse("show me the code to scale the cube by 2")
    assert spec["action"] == "scale" and spec["args"]["scale"] == (2.0, 2.0, 2.0)
    assert nl_tier5.parse("I got an error: TypeError: bad")["text"] == "TypeError: bad"
    assert nl_tier5.parse("what's next on my homework")["next_step_only"] is True
    assert nl_tier5.parse("describe the scene under /World/Shelves")["focus_path"] == "/World/Shelves"
    assert nl_tier5.parse("what did we learn in session 2")["session"] == 2



def test_nl_tier5_stays_fast_on_long_runs_of_dots_and_spaces():
    # parse() runs on every router request; its trailing-punctuation regex backtracked cubically (0.7 s at 800
    # dots, hours at 10 000), which froze the whole router.
    import time

    for text in ("x" + "." * 20_000 + "x", "x" + " " * 20_000 + "x", "x" + "!?. " * 5_000 + "x",
                 "show me the python for moving " + "1" * 200_000, "check my homework in " + "a." * 100_000):
        started = time.perf_counter()
        nl_tier5.parse(text)
        assert time.perf_counter() - started < 0.5, text[:40]
    assert nl_tier5.parse("show me the python for moving a cube please")["action"] == "move"
    assert nl_tier5.parse("check my homework please!!")["kind"] == "assignment_check"
    pasted = "explain this error: " + "NameError: name 'x' is not defined\n" * 2_000
    assert nl_tier5.parse(pasted)["kind"] == "explain_error"  # long pasted errors are not cut by the phrase cap


def test_show_python_snippets_never_retarget_the_users_prim_and_keep_hex_colours():
    # scene_aim on a light used to give UsdGeom.Camera.Define(stage, <the light's path>): running it turned the light
    # into a camera.
    aimed = python_links.python_for_tool("scene_aim", {"path": "/World/Lights/Key", "target": [0, 0, 0]})
    assert aimed["ok"] is True and "/World/Lights/Key" not in aimed["code"], aimed["code"]
    assert aimed["args"]["prim_path"] == "/World/MyCamera" and "does not touch /World/Lights/Key" in aimed["note"]
    light = python_links.python_for_tool("scene_create_light", {"path": "/World/Key", "light_type": "rect",
                                                                "color": "#ff8000"})
    assert light["args"]["color"] == [1.0, 128 / 255.0, 0.0], light["args"]
    material = python_links.python_for_tool("scene_create_material", {"path": "/World/Looks/Red",
                                                                      "diffuse_color": "#f00"})
    assert material["args"]["color"] == [1.0, 0.0, 0.0], material["args"]


def test_show_python_for_set_attribute_uses_the_attributes_real_type():
    # 2.0 for a double attribute (radius) was guessed as 'float', so the snippet raised "already has type double".
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        stage = _live_stage(tmp)
        UsdGeom.Sphere.Define(stage, "/World/Ball")
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage):
            shown = call("scene_set_attribute", path="/World/Ball", name="radius", value=2.0, show_python=True)
            assert shown["ok"] is True and shown["preview"] is True, shown
            assert shown["python"]["ok"] is True and shown["python"]["args"]["value_type"] == "double", shown["python"]
            assert "Sdf.ValueTypeNames.Double" in shown["python"]["code"]
        assert _layers_text(stage) == original


# --------------------------------------------------------------------------- packaging and self-tests


def test_package_data_and_bridge_reload_self_tests_include_round6_files():
    pyproject = (WORKSPACE / "omniverse_scene_mcp" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"omniverse_scene_mcp" = ["data/*.json", "data/assignments/*.json"]' in pyproject
    assert '"omniverse_scene_mcp.exam" = ["data/*.json"]' in pyproject
    package = WORKSPACE / "omniverse_scene_mcp" / "src" / "omniverse_scene_mcp"
    assert (package / "data" / "error_patterns.json").is_file()
    assert list((package / "data" / "assignments").glob("*.json"))
    assert (package / "exam" / "data" / "python_exercises.json").is_file()
    for script in (WORKSPACE / "isaacsim-study" / "reload_mcp_bridge.py", WORKSPACE / "composer-study" / "reload_composer_bridge.py"):
        if script.is_file():
            text = script.read_text(encoding="utf-8")
            assert "test_scene_tour.py" in text and "test_assignments.py" in text, script.name
