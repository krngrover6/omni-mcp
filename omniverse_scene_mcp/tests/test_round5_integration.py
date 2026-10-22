"""Round 5 integration: the asset library, UsdPhysics setup, viewport screenshots, simulation controls and the exam
dashboard, wired through every layer (server tool -> router -> service -> HTTP client -> extended RPC -> Kit bridge).

Offline: a test-mode server over a temp workspace, a synthetic asset pack in a temp folder, the in-memory fake scene
or an in-process HTTP transport into the extended RPC over a temporary pxr stage (only Kit's omni modules are faked).
Every socket is refused and the real exam-prep folder is checked after every server (test_twelve_gaps_integration's
_guard). Plain fixture-free test_* functions, so the stdlib runner runs them too.
"""

import os

# The stdlib test runner may not load conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import contextlib
import io
from dataclasses import asdict
import json
import tempfile
from pathlib import Path
from urllib.error import HTTPError

from omniverse_scene_mcp import asset_library, http_bridge, kit_bridge, kit_commands, kit_live_extras, kit_rpc, runtime
from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp import nl_tier4
from omniverse_scene_mcp.asset_library import AssetLibrary
from omniverse_scene_mcp.bridge import (
    ASSET_RPC_METHODS,
    CAPTURE_RPC_METHODS,
    LIVE_RPC_METHODS,
    PHYSICS_RPC_METHODS,
    RPC_VERSION,
    SIMULATION_RPC_METHODS,
    TIER4_RPC_METHODS,
)
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.kit_bridge import LiveKitSceneBridge
from omniverse_scene_mcp.service import SceneMCPService
from omniverse_scene_mcp.undo import UndoStack
from test_asset_library import _pack
from test_kit_live_extras import FakePhysX, FakeTimeline, FakeViewport, _fake_kit, _physx_module, _settle
from test_kit_live_extras import _temp_root, _timeline_module, _utility
from test_twelve_gaps_integration import (
    COMPOSER,
    ISAAC,
    REAL_EXAM_PREP,
    _BasicServer,
    _env,
    _exam_prep_state,
    _guard,
    _in_process_transport,
    _layers_text,
    _live_kit_bridge,
    _server,
)

try:
    from pxr import Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - the live-stage tests below then return early
    Usd = UsdGeom = UsdPhysics = None

_IN_KIT = "carb" in __import__("sys").modules or "omni.kit.app" in __import__("sys").modules
NEW_TOOLS = {
    "asset_search",
    "asset_info",
    "scene_add_asset",
    "scene_physics_report",
    "scene_add_physics_scene",
    "scene_make_rigid_body",
    "scene_make_collider",
    "scene_remove_physics",
    "scene_capture_viewport",
    "scene_capture_status",
    "sim_status",
    "sim_play",
    "sim_pause",
    "sim_stop",
    "sim_step",
    "learning_exam_dashboard",
}
PREVIEW_TOOLS = (
    "scene_add_asset",
    "scene_add_physics_scene",
    "scene_make_rigid_body",
    "scene_make_collider",
    "scene_remove_physics",
)
SIM_CHANGERS = ("sim_play", "sim_pause", "sim_stop", "sim_step")


# --------------------------------------------------------------------------- helpers


@contextlib.contextmanager
def _assets():
    """A synthetic asset pack in a temp folder, used by every AssetLibrary() built while the block runs."""
    with tempfile.TemporaryDirectory() as tmp:
        pack = _pack(tmp)
        asset_library.clear_cache()
        try:
            with _env(OMNIVERSE_ISAAC_ASSETS_ROOT=pack):
                yield pack
        finally:
            asset_library.clear_cache()


def _content(pack: Path) -> Path:
    return pack / "Assets" / "Isaac" / "9.9"


def _tree(root: Path):
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _live_stage(tmp: str):
    stage = Usd.Stage.CreateNew(str(Path(tmp) / "live.usda"))
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Box")
    UsdGeom.Cube.Define(stage, "/World/Box/Geom")
    stage.SetEditTarget(stage.GetRootLayer())
    return stage


@contextlib.contextmanager
def _live(stage):
    """HttpSceneBridge(ISAAC) -> in-process extended RPC -> a real LiveKitSceneBridge on stage; Kit undo faked off."""
    with _live_kit_bridge(stage) as (bridge, selection):
        bridge._undo_stack = UndoStack()
        rpc = kit_rpc.make_extended_server_class(_Round5Server)(bridge)
        with _in_process_transport(rpc) as sent:
            yield bridge, selection, sent


class _Round5Server(_BasicServer):
    """The basic stand-in server plus 'find', which the router's name resolution needs."""

    def _dispatch(self, payload):
        if str(payload.get("method", "")).strip().lower() == "find":
            params = payload.get("params") or {}
            prims = self._bridge.find_prims(params["query"], scope_path=params.get("scope_path"))
            return {"ok": True, "result": {"prims": [asdict(prim) for prim in prims]}}
        return super()._dispatch(payload)


def _no_kit_commands():
    """kit_commands must not reach a real omni.kit.commands here (there is none outside Kit; keep it that way)."""
    assert kit_commands._OMNI_KIT_COMMANDS is None or _IN_KIT


# --------------------------------------------------------------------------- registration and dry-run defaults


def test_round5_tools_are_registered_with_dry_run_defaults_and_honest_descriptions():
    with _server() as call:
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert NEW_TOOLS <= set(tools) and len(tools) >= 98, len(tools)
        for name in PREVIEW_TOOLS:
            properties = tools[name].parameters["properties"]
            assert properties["dry_run"]["default"] is True, name
            description = " ".join(tools[name].description.split())
            assert description.startswith("DRY RUN BY DEFAULT"), name
            assert "changes the user's open scene" in description and "preview to the user first" in description, name
        for name in SIM_CHANGERS:
            description = " ".join(tools[name].description.split())
            assert description.startswith("CHANGES THE SIMULATION STATE") and "not authored scene data" in description
            assert "dry_run" not in tools[name].parameters["properties"], name
        for name in ("asset_search", "asset_info", "scene_physics_report", "scene_capture_status", "sim_status"):
            text = tools[name].description
            assert text.startswith("Read-only") or "Nothing changes" in text or "nothing changes" in text, name
        assert "does not change the open scene" in " ".join(tools["scene_capture_viewport"].description.split())
        assert tools["learning_exam_dashboard"].parameters["properties"]["write"]["default"] is True
    assert NEW_TOOLS <= set(tool_guide.tool_names())


def test_tier4_methods_line_up_across_the_protocol_the_rpc_and_every_bridge():
    assert RPC_VERSION == "tier5-1"
    assert set(TIER4_RPC_METHODS) == set(ASSET_RPC_METHODS + PHYSICS_RPC_METHODS + CAPTURE_RPC_METHODS
                                         + SIMULATION_RPC_METHODS)
    assert set(TIER4_RPC_METHODS) <= set(LIVE_RPC_METHODS) <= set(kit_rpc._HANDLERS)
    for owner in (FakeSceneBridge, HttpSceneBridge, LiveKitSceneBridge):
        missing = [name for name in TIER4_RPC_METHODS if not callable(getattr(owner, name, None))]
        assert not missing, (owner.__name__, missing)
    for name in ("asset_search", "asset_info", "add_asset", "physics_report", "add_physics_scene", "make_rigid_body",
                 "make_collider", "remove_physics", "capture_viewport", "capture_status", *SIMULATION_RPC_METHODS):
        assert callable(getattr(SceneMCPService, name, None)), name
    # A write that waited too long in Kit's queue is dropped, like the older write methods.
    for name in ("add_reference", "make_rigid_body", "simulation_play", "simulation_step", "capture_viewport"):
        assert name in kit_rpc._WRITE_METHODS, name
    assert "physics_report" not in kit_rpc._WRITE_METHODS and "simulation_status" not in kit_rpc._WRITE_METHODS


def test_http_client_sends_dry_run_true_unless_dry_run_is_exactly_false():
    client = HttpSceneBridge(ISAAC)
    server = kit_rpc.make_extended_server_class(_BasicServer)(FakeSceneBridge())
    with _guard(), _in_process_transport(server) as sent:
        for value in (None, 0, [], "false", True):
            client.add_reference("/World/A", "C:/a.usda", dry_run=value)
            client.add_physics_scene(dry_run=value)
            client.make_rigid_body("/World/A", dry_run=value)
            client.make_collider("/World/A", dry_run=value)
            client.remove_physics("/World/A", dry_run=value)
        assert {call["params"]["dry_run"] for call in sent} == {True}
        sent.clear()
        client.make_rigid_body("/World/Props/Cube", dry_run=False)
        assert sent[0]["params"]["dry_run"] is False


def test_extended_rpc_parses_strictly_and_a_missing_or_malformed_dry_run_is_a_dry_run():
    calls = []

    class Recorder:
        def add_reference(self, *args, **kwargs):
            calls.append(("add_reference", args, kwargs))
            return {"ok": True, "dry_run": kwargs["dry_run"]}

        def make_rigid_body(self, *args, **kwargs):
            calls.append(("make_rigid_body", args, kwargs))
            return {"ok": True, "dry_run": kwargs["dry_run"]}

        def simulation_step(self, frames):
            calls.append(("simulation_step", (frames,), {}))
            return {"ok": True}

    server = kit_rpc.make_extended_server_class(_BasicServer)(Recorder())
    answer = server._dispatch({"method": "add_reference", "params": {"prim_path": "/World/A", "asset_path": "C:/a.usda"}})
    assert answer["result"]["dry_run"] is True and "warnings" not in answer["result"]
    odd = server._dispatch({"method": "make_rigid_body", "params": {"prim_path": "/World/A", "dry_run": "maybe"}})
    assert odd["result"]["dry_run"] is True and "nothing was written" in odd["result"]["warnings"][-1]
    assert calls[1][2]["kinematic"] is False and calls[1][2]["add_collider"] is True
    server._dispatch({"method": "add_reference", "params": {"prim_path": "/World/B", "asset_path": "C:/b.usda",
                                                             "scale": 2, "instanceable": True}})
    assert calls[-1][2]["scale"] == 2.0 and calls[-1][2]["instanceable"] is True
    server._dispatch({"method": "simulation_step", "params": {}})
    assert calls[-1] == ("simulation_step", (1,), {})
    for bad in (
        {"method": "add_reference", "params": {"prim_path": "/World/A", "asset_path": "C:/a.usda", "extra": 1}},
        {"method": "add_reference", "params": {"prim_path": "/World/A"}},
        {"method": "make_rigid_body", "params": {"prim_path": "/World/A", "mass": "heavy"}},
        {"method": "simulation_step", "params": {"frames": 0}},
        {"method": "simulation_step", "params": {"frames": True}},
        {"method": "simulation_play", "params": {"now": True}},
    ):
        try:
            server._dispatch(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    lacking = server._dispatch({"method": "physics_report", "params": {}})["result"]
    assert lacking["needs_bridge_update"] is True and lacking["method"] == "physics_report"
    info = server._dispatch({"method": "capabilities", "params": {}})["result"]
    assert info["version"] == "tier5-1" and info["physics_methods"] == list(PHYSICS_RPC_METHODS)
    assert info["simulation_methods"] == list(SIMULATION_RPC_METHODS)


# --------------------------------------------------------------------------- fake scene, end to end


def test_assets_on_the_fake_scene_search_preview_add_and_undo():
    with _assets() as pack, _server() as call:
        before_pack = _tree(pack)
        found = call("asset_search", query="arm robot", top_k=3)
        assert found["ok"] is True and found["target"] == "assets"
        top = found["results"][0]
        assert top["relative_path"] == "Isaac/Robots/Acme/Arm/arm.usda" and top["path"].endswith("arm.usda")
        assert call("asset_search", query="x", top_k=True)["ok"] is False
        info = call("asset_info", asset_path="Isaac/Props/Shapes/cube.usda")
        assert info["ok"] is True and info["category"] == "Props" and info["path"].endswith("cube.usda")
        missing = call("asset_info", asset_path="Isaac/Props/nope.usda")
        assert missing["ok"] is False and missing["message"]

        scene = call.router._scene
        before = scene.scene_summary()
        preview = call("scene_add_asset", prim_path="/World/Arm", asset_path="Isaac/Robots/Acme/Arm/arm.usda",
                       translation=[1, 2, 3])
        assert preview["ok"] is True and preview["dry_run"] is True and preview["applied"] is False
        assert preview["preview"] is True and "nothing in the open scene changed" in preview["message"]
        plan = preview["payload"]["would_create"]
        assert plan["reference"]["asset_path"] == top["path"] and plan["translation"] == [1.0, 2.0, 3.0]
        assert scene.scene_summary() == before and scene._bridge.get_prim("/World/Arm") is None

        applied = call("scene_add_asset", prim_path="/World/Arm", asset_path=top["path"], translation=[1, 2, 3],
                       dry_run=False)
        assert applied["ok"] is True and applied["applied"] is True and applied["dry_run"] is False
        prim = scene._bridge.get_prim("/World/Arm")
        assert prim is not None and prim.translation == (1.0, 2.0, 3.0)
        assert scene._bridge.get_attribute("/World/Arm", "references")[0]["asset_path"] == top["path"]
        undone = call("scene_undo_last", dry_run=False)
        assert undone["ok"] is True and undone["applied"] is True and scene._bridge.get_prim("/World/Arm") is None

        absent = call("scene_add_asset", prim_path="/World/X", asset_path="Isaac/Robots/Acme/Arm/arm_v2.usda")
        assert absent["ok"] is False and absent["applied"] is False and "not found" in absent["message"]
        assert any(path.endswith("arm.usda") for path in absent["suggestions"])
        bad = call("scene_add_asset", prim_path="/World/X", asset_path=top["path"], scale=[1, 2])
        assert bad["ok"] is False and "scale" in bad["message"]
        assert _tree(pack) == before_pack, "the asset pack must only be read"


def test_physics_and_screenshots_say_not_supported_on_the_fake_scene_and_change_nothing():
    with _server() as call:
        bridge = call.router._scene._bridge
        state = repr(bridge._state())
        for name, arguments in (
            ("scene_add_physics_scene", {}),
            ("scene_make_rigid_body", {"prim_path": "/World/Props/Cube", "dry_run": False}),
            ("scene_make_collider", {"prim_path": "/World/Props/Cube"}),
            ("scene_remove_physics", {"prim_path": "/World/Props/Cube", "dry_run": False}),
        ):
            result = call(name, **arguments)
            assert result["ok"] is False and result["applied"] is False and result["fake_scene"] is True, name
            assert "not supported on the in-memory fake scene" in result["message"], name
            assert "how_to_enable" not in result, name
        report = call("scene_physics_report")
        assert report["ok"] is False and report["mode"] == "saved_file" and "live_fallback" in report
        assert "fake scene" in report["message"]
        shot = call("scene_capture_viewport")
        assert shot["ok"] is False and shot["unsupported"] is True and shot["fake_scene"] is True
        assert call("scene_capture_viewport", output_dir="relative/dir")["ok"] is False
        assert call("scene_capture_status", path="")["ok"] is False
        assert repr(bridge._state()) == state
        capabilities = call("scene_capabilities")
        assert capabilities["assets"]["available"] is True and capabilities["physics"]["available"] is False
        assert capabilities["simulation"]["available"] is True and capabilities["needs_bridge_update"] is False


def test_simulation_controls_on_the_fake_timeline():
    with _server() as call:
        status = call("sim_status")
        assert status["ok"] and status["stopped"] and status["physics_available"] is False
        played = call("sim_play")
        assert played["playing"] is True and "no physics" in played["note"]
        refused = call("sim_step", frames=5)
        assert refused["ok"] is False and "pause" in refused["message"]
        assert call("sim_pause")["paused"] is True
        stepped = call("sim_step", frames=1000)
        assert stepped["ok"] and stepped["frames"] == 600 and stepped["capped"] is True
        assert stepped["current_time_code"] == 600.0
        assert call("sim_step", frames=0)["ok"] is False
        stopped = call("sim_stop")
        assert stopped["stopped"] is True and stopped["current_time_code"] == 0.0
        # Simulation state is not an authored edit: nothing went on the undo history.
        assert call("scene_undo_history")["count"] == 0


def test_dashboard_tool_writes_only_inside_the_temp_workspace_and_write_false_writes_nothing():
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        target = root / "exam-prep" / "dashboard.html"
        quiet = call("learning_exam_dashboard", write=False)
        assert quiet["ok"] is True and quiet["dashboard"]["written"] is False and not target.exists()
        assert [row["name"] for row in quiet["domains"]] and "focus_next" in quiet
        saved = call("learning_exam_dashboard")
        assert saved["dashboard"]["written"] is True and Path(saved["dashboard"]["path"]) == target.resolve()
        page = target.read_text(encoding="utf-8")
        assert "<html" in page.lower()
        assert "<script" not in page.lower()
        nl = call.dispatch("show my dashboard", dry_run=True)
        assert nl["style"] == "dashboard" and nl["executed"] is True and nl["dashboard"]["written"] is False
        assert call.router.route("show my dashboard").target.value == "learn"


def test_dashboard_in_test_mode_never_writes_the_real_exam_prep_folder():
    from omniverse_scene_mcp.exam.progress import ProgressStore

    before = _exam_prep_state()
    with _env(OMNIVERSE_SCENE_MCP_TEST="1", OMNIVERSE_ROOT=None, OMNIVERSE_EXAM_PREP_DIR=None):
        result = runtime.exam_dashboard_result(ProgressStore(memory_only=True), write=True)
    assert result["ok"] is True and result["dashboard"]["written"] is False and result["dashboard"]["skipped"] is True
    assert "never written" in result["dashboard"]["message"]
    with _env(OMNIVERSE_SCENE_MCP_TEST="1", OMNIVERSE_EXAM_PREP_DIR=str(REAL_EXAM_PREP)):
        again = runtime.exam_dashboard_result(ProgressStore(memory_only=True), write=True)
    assert again["dashboard"]["written"] is False
    assert _exam_prep_state() == before


# --------------------------------------------------------------------------- router sentences


def test_router_sentences_preview_assets_and_run_simulation_only_when_asked():
    with _assets(), _server() as call:
        bridge = call.router._scene._bridge
        state = repr(bridge._state())
        added = call.dispatch("add an arm robot")
        assert added["ok"] is True and added["action"] == "add_asset" and added["preview"] is True
        assert added["applied"] is False and added["dry_run"] is True and added["path"] == "/World/Arm"
        assert added["apply_with"]["tool"] == "scene_add_asset" and added["apply_with"]["arguments"]["dry_run"] is False
        assert added["asset"]["relative_path"] == "Isaac/Robots/Acme/Arm/arm.usda"
        shelf = call.dispatch("add a warehouse shelf at 0 0 0")
        assert shelf["action"] == "add_asset" and shelf["payload"]["would_create"]["translation"] == [0.0, 0.0, 0.0]
        assert shelf["asset"]["name"].lower().startswith(("sm_rackshelf", "warehouse_shelves"))
        found = call.dispatch("find a rover robot")
        assert found["action"] == "asset_search" and found["results"][0]["name"] == "rover_v1.usda"
        assert found["preview"] is True and found["candidates"]
        assert repr(bridge._state()) == state, "sentences only preview assets"
        # No confident asset: the plain create runs as before.
        plane = call.dispatch("add an airplane", dry_run=True)
        assert plane["dry_run"] is True and plane["changes"][0]["action"] == "create"
        assert call.dispatch("find cube")["result"]["query"] == "cube"  # still the scene's own prim search

        preview = call.dispatch("play the simulation", dry_run=True)
        assert preview["preview"] is True and bridge._timeline_state["playing"] is False
        assert call.dispatch("play the simulation")["applied"] is True and bridge._timeline_state["playing"] is True
        assert call.dispatch("pause the simulation")["paused"] is True
        assert call.dispatch("step 10 frames")["frames"] == 10
        assert call.dispatch("stop the simulation")["stopped"] is True
        assert call.dispatch("simulation status")["ok"] is True
        shot = call.dispatch("take a screenshot", dry_run=True)
        assert shot["preview"] is True and shot["action"] == "capture_viewport"
        assert call.dispatch("physics report")["unsupported"] is True
        named = call.dispatch("make the cube a rigid body")
        assert named["ok"] is False and named["dry_run"] is True  # by name: previewed (and the fake has no physics)
        for phrase in ("add a franka robot", "take a screenshot", "play the simulation", "step 10 frames",
                       "make /World/Box a rigid body", "add collision to /World/Box", "physics report",
                       "find a carter robot"):
            assert call.router.route(phrase).target.value == "scene", phrase
        assert nl_tier4.parse_asset_find("search for a tutorial on lights") is None  # still a learning request
        assert call.router.route("search for a tutorial on lights").target.value == "learn"


# --------------------------------------------------------------------------- live stage through the extended RPC


def test_live_assets_and_physics_preview_byte_identical_then_apply_and_undo():
    if Usd is None or UsdPhysics is None:
        return
    _no_kit_commands()
    with tempfile.TemporaryDirectory() as tmp, _assets() as pack:
        stage = _live_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (bridge, _sel, sent):
            capabilities = call("scene_capabilities")
            assert capabilities["rpc_version"] == "tier5-1" and capabilities["needs_bridge_update"] is False
            assert capabilities["assets"]["available"] and capabilities["physics"]["available"]
            assert capabilities["viewport_capture"]["available"] and capabilities["simulation"]["available"]

            previews = [
                call("scene_add_asset", prim_path="/World/Cube1", asset_path="Isaac/Props/Shapes/cube_y_cm.usda",
                     correct_units=True),
                call("scene_add_physics_scene"),
                call("scene_make_rigid_body", prim_path="/World/Box", mass=2.0),
                call("scene_make_collider", prim_path="/World/Box"),
                call("scene_remove_physics", prim_path="/World/Box"),
            ]
            for result in previews:
                assert result["ok"] is True and result["dry_run"] is True and result["applied"] is False, result
            assert _layers_text(stage) == original, "dry runs must leave every layer byte-identical"
            plan = previews[0]["payload"]["would_create"]
            assert plan["reference"]["asset_path"].endswith("cube_y_cm.usda")
            assert plan["rotation"] == [90.0, 0.0, 0.0] and plan["scale"] == [0.01, 0.01, 0.01]  # Y-up cm -> Z-up m
            assert all(item["params"].get("dry_run") is True for item in sent if "dry_run" in item["params"])

            added = call("scene_add_asset", prim_path="/World/Cube1", asset_path=plan["reference"]["asset_path"],
                         correct_units=True, dry_run=False)
            assert added["ok"] is True and added["applied"] is True and added["undoable"] is True
            prim = stage.GetPrimAtPath("/World/Cube1")
            assert prim.IsValid() and prim.HasAuthoredReferences()
            scene = call("scene_add_physics_scene", dry_run=False)
            assert scene["applied"] is True and stage.GetPrimAtPath("/physicsScene").IsA(UsdPhysics.Scene)
            body = call("scene_make_rigid_body", prim_path="/World/Box", mass=2.0, dry_run=False)
            assert body["applied"] is True and stage.GetPrimAtPath("/World/Box").HasAPI(UsdPhysics.RigidBodyAPI)
            assert stage.GetPrimAtPath("/World/Box/Geom").HasAPI(UsdPhysics.CollisionAPI)
            report = call("scene_physics_report")
            assert report["ok"] is True and report["mode"] == "live" and report["has_physics_scene"] is True
            assert report["read_only"] is True

            for _ in range(3):  # rigid body, physics scene, asset: newest first
                assert call("scene_undo_last", dry_run=False)["applied"] is True
            assert not stage.GetPrimAtPath("/World/Box").HasAPI(UsdPhysics.RigidBodyAPI)
            assert not stage.GetPrimAtPath("/physicsScene").IsValid() and not stage.GetPrimAtPath("/World/Cube1").IsValid()
            assert _layers_text(stage) == original

            # The router: an exact path with dry_run=False applies, a name only previews.
            named = call.dispatch("make the box a rigid body")
            assert named["ok"] is True and named["preview"] is True and named["resolved_from_name"]["path"] == "/World/Box"
            assert _layers_text(stage) == original
            exact = call.dispatch("add collision to /World/Box/Geom")
            assert exact["applied"] is True and stage.GetPrimAtPath("/World/Box/Geom").HasAPI(UsdPhysics.CollisionAPI)
        assert nl_tier4.parse_asset_add("add a cube shape") is None  # 'cube' is a plain prim word, never an asset
        assert pack.is_dir()


def test_live_screenshot_and_simulation_through_the_rpc_with_fake_kit_modules():
    if Usd is None or _IN_KIT:
        return
    timeline = FakeTimeline(start=0.0, current=0.0)
    viewport = FakeViewport()
    utility = _utility(viewport)
    with tempfile.TemporaryDirectory() as tmp:
        stage = _live_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage):
            with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(FakePhysX()),
                           omni_kit_viewport_utility=utility):
                shot = call("scene_capture_viewport", output_dir=tmp)
                assert shot["ok"] is True and shot["status"] == "pending" and shot["path"].startswith(tmp)
                assert viewport.camera_switches == [] and viewport.writes == []
                assert call("scene_capture_status", path=shot["path"])["ready"] is False
                _settle(shot["path"])
                assert call("scene_capture_status", path=shot["path"])["ready"] is True
                assert call("sim_status")["stopped"] is True
                assert call("sim_play")["playing"] is True
                assert call("sim_pause")["paused"] is True
                stepped = call("sim_step", frames=3)
                assert stepped["ok"] and stepped["via"] == "physx" and stepped["frames"] == 3
                assert call("sim_stop")["stopped"] is True
                with _temp_root() as captures:  # the default capture folder goes under this temp root
                    nl = call.dispatch("take a screenshot")
                    assert nl["ok"] is True and nl["status"] == "pending" and nl["path"].startswith(captures)
                    assert call.dispatch("is my screenshot ready")["action"] == "capture_status"
            kit_live_extras._PENDING.clear()
            assert _layers_text(stage) == original, "screenshots and simulation author nothing"
            # Without Kit's modules the live bridge says so instead of failing.
            assert call("sim_status")["message"] == "Kit not available"


# --------------------------------------------------------------------------- older bridges and the guard


def test_older_bridges_get_needs_bridge_update_for_every_new_method():
    with _assets() as pack, _server(scene=lambda: SceneMCPService(HttpSceneBridge(COMPOSER))) as call:
        rpc = _BasicServer(FakeSceneBridge())
        with _in_process_transport(rpc):
            asset = str(_content(pack) / "Isaac" / "Props" / "Shapes" / "cube.usda")
            results = {
                "scene_add_asset": call("scene_add_asset", prim_path="/World/C", asset_path=asset),
                "scene_add_physics_scene": call("scene_add_physics_scene"),
                "scene_make_rigid_body": call("scene_make_rigid_body", prim_path="/World/Props/Cube"),
                "scene_make_collider": call("scene_make_collider", prim_path="/World/Props/Cube"),
                "scene_remove_physics": call("scene_remove_physics", prim_path="/World/Props/Cube"),
                "scene_capture_viewport": call("scene_capture_viewport"),
                "scene_capture_status": call("scene_capture_status", path="C:/x.png"),
                "sim_play": call("sim_play"),
                "sim_pause": call("sim_pause"),
                "sim_stop": call("sim_stop"),
                "sim_step": call("sim_step"),
                "scene_physics_report": call("scene_physics_report", mode="live"),
            }
            for name, result in results.items():
                assert result["ok"] is False and result["needs_bridge_update"] is True, (name, result)
                assert result["how_to_enable"], name
                assert "reload_composer_bridge.py" in json.dumps(result), name
            assert call("sim_status")["needs_bridge_update"] is True
            capabilities = call("scene_capabilities")
            assert capabilities["needs_bridge_update"] is True
            assert set(TIER4_RPC_METHODS) <= set(capabilities["missing_live_methods"])


def test_a_403_from_the_bridge_guard_is_reported_and_nothing_runs():
    def forbidden(request, timeout=None):
        body = json.dumps({"ok": False, "forbidden": True, "reason": "token", "message": "Refused"}).encode()
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call:
        previous = http_bridge.urlopen
        http_bridge.urlopen = forbidden
        try:
            for result in (call("sim_play"), call("scene_make_rigid_body", prim_path="/World/A", dry_run=False),
                           call("scene_capture_viewport")):
                assert result["ok"] is False and "403" in json.dumps(result) and "OMNIVERSE_BRIDGE_TOKEN" in json.dumps(result)
        finally:
            http_bridge.urlopen = previous


def test_runtime_health_probes_send_the_bridge_headers_and_the_token():
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, *_args):
            return b'{"ok": true, "status": "running"}'

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return Response()

    previous = runtime.urlopen
    runtime.urlopen = fake_urlopen
    try:
        with _env(OMNIVERSE_BRIDGE_TOKEN="round5-test-token"):
            assert runtime.probe_scene_health(ISAAC)["reachable"] is True
            assert runtime._detect_live_scene_endpoint() == runtime.AUTO_SCENE_ENDPOINT
        with _env(OMNIVERSE_BRIDGE_TOKEN=None):
            runtime.probe_scene_health(COMPOSER)
    finally:
        runtime.urlopen = previous
    first, second, third = seen
    for request in (first, second):
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers[kit_rpc.BRIDGE_TOKEN_HEADER.lower()] == "round5-test-token" and "origin" not in headers
        assert request.get_method() == "GET" and request.full_url.endswith("/health")
    assert kit_rpc.BRIDGE_TOKEN_HEADER.lower() not in {key.lower() for key, _ in third.header_items()}


def test_kit_bridge_dry_runs_default_true_and_never_touch_the_stage():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _assets() as pack:
        stage = _live_stage(tmp)
        original = _layers_text(stage)
        asset = str(_content(pack) / "Isaac" / "Props" / "Shapes" / "cube.usda")
        with _live_kit_bridge(stage) as (bridge, _selection):
            for result in (
                bridge.add_reference("/World/C", asset),
                bridge.add_physics_scene(),
                bridge.make_rigid_body("/World/Box"),
                bridge.make_collider("/World/Box"),
                bridge.remove_physics("/World/Box"),
                bridge.physics_report(),
            ):
                assert result["ok"] is True and result.get("dry_run", True) is True and result["mode"] == "live", result
            assert _layers_text(stage) == original
            info = bridge.capabilities()
            assert info["version"] == "tier5-1" and info["physics"]["available"] and info["assets"]["available"]
        service = SceneMCPService(FakeSceneBridge())
        for result in (service.add_physics_scene(), service.make_rigid_body("/World/Props/Cube"),
                       service.make_collider("/World/Props/Cube"), service.remove_physics("/World/Props/Cube")):
            assert result.dry_run is True and result.ok is False  # the fake scene: unsupported, never written
    assert isinstance(kit_bridge.LIVE_NOTE, str)


def test_plain_prim_and_light_words_never_become_asset_searches():
    # 'find a mesh' / 'find an xform' were the router's scene search before round 5 and must stay so; lights too.
    for phrase in ("find a mesh", "find an xform", "find a red cube", "find a prim named Cube", "find a light",
                   "find any prims with physics", "look for a camera", "search for a material"):
        assert nl_tier4.parse_asset_find(phrase) is None, phrase
    for phrase, query in (("find a carter robot", "carter robot"), ("find a cube asset", "cube"),
                          ("find cube assets", "cube"), ("find a cube in the library", "cube"),
                          ("search the asset library for cubes", "cubes")):
        assert nl_tier4.parse_asset_find(phrase) == {"query": query}, phrase
    for phrase in ("add a sun", "add a sky", "add a dome", "add a spotlight", "add a sunlight", "add an hdri"):
        assert nl_tier4.parse_asset_add(phrase) is None, phrase
    from omniverse_scene_mcp.router import OmniverseRouter

    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    found = router._dispatch_scene("find an xform", dry_run=False)
    assert found.get("action") != "asset_search" and found["result"]["query"] == "an xform", found


def test_asset_picks_match_whole_path_words_not_letters_inside_other_words():
    from omniverse_scene_mcp.router import OmniverseRouter

    covers = OmniverseRouter._asset_covers
    for query, path in (("tree", "Isaac/Environments/Modular_Warehouse/Props/warehouse_a01_h10m_streetlight_01.usd"),
                        ("sun", "Isaac/Robots/NVIDIA/NovaCarterDevKit/parts/Countersunk_Bolt_14_20_x_05_JHD.usd"),
                        ("hdri", "Isaac/Environments/Office/Props/SM_Flashdrive.usd"),
                        ("rug", "Isaac/Props/PackingTable/props/sm_whitecorrugatedbox_b04_brown_01.usd"),
                        ("car", "Isaac/Robots/NVIDIA/Carter/carter_v1.usd")):
        assert not covers(query, {"relative_path": path}), (query, path)
    for query, path in (("franka robot", "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"),
                        ("arm robot", "Isaac/Robots/Acme/Arm/arm.usda"),
                        ("warehouse shelf", "Isaac/Environments/Simple_Warehouse/Props/SM_RackShelf_01.usd"),
                        ("shelf", "Isaac/Environments/Warehouse/Props/rackshelf_01.usd"),
                        ("lidar", "Isaac/Sensors/Slamtec/RPLidar_S2e.usd"),
                        ("pallets", "Isaac/Props/Pallet/pallet.usd"),
                        ("ur10", "Isaac/Robots/UniversalRobots/ur10/ur10.usd"),
                        ("plant", "Isaac/Environments/Office/Props/SM_Plant01.usd")):
        assert covers(query, {"relative_path": path}), (query, path)
