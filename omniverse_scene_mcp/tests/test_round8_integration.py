"""Round 8 integration: the project starter, the layer manager, scene checkpoints, the Kit log reader, the asset health
check, the journal-to-script helper and the project report wired through every layer (server tool -> router ->
service -> HTTP client -> extended RPC tier7-1 -> Kit bridge), plus the plain-English phrases in nl_tier7.py.

Offline: a test-mode server over a temp workspace, the in-memory fake scene or an in-process HTTP transport into the
extended RPC over a temporary pxr stage (only Kit's omni modules are faked). Every socket is refused and the real
exam-prep folder is checked after every server (test_twelve_gaps_integration's _guard). Kit logs come from a temp
folder ($OMNIVERSE_KIT_LOG_DIRS); projects, checkpoints and reports are written only inside the temp workspace. Plain
fixture-free test_* functions, so the stdlib runner runs them.
"""

import os

# The stdlib test runner may not load conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import json
import re
import tempfile
import time
from pathlib import Path

from omniverse_scene_mcp import checkpoints, kit_rpc, nl_tier5, nl_tier6, nl_tier7
from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp import router as router_module
from omniverse_scene_mcp.bridge import (
    HEALTH_RPC_METHODS,
    LAYER_READ_RPC_METHODS,
    LAYER_RPC_METHODS,
    LAYER_WRITE_RPC_METHODS,
    LIVE_RPC_METHODS,
    RPC_VERSION,
)
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.journal import JOURNALED_TOOLS
from omniverse_scene_mcp.kit_bridge import LiveKitSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService
from test_kit_logs import ISAAC as ISAAC_LOG_APP
from test_kit_logs import header_lines, line, py_traceback, write_log
from test_round5_integration import _live, _live_stage
from test_round6_integration import HELPER_PHRASES
from test_round7_integration import KEPT as ROUND7_KEPT
from test_round7_integration import PHRASES as ROUND7_PHRASES
from test_twelve_gaps_integration import COMPOSER, ISAAC, _BasicServer, _env, _in_process_transport, _layers_text, _server

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - the pxr tests below then return early
    Sdf = Usd = UsdGeom = None

TESTS = Path(__file__).resolve().parent
WORKSPACE = TESTS.parents[1]
PROJECT_TOOLS = ("project_templates", "project_plan", "project_create", "project_list")
LAYER_READ_TOOLS = ("scene_layers", "scene_where_is", "scene_flatten_preview")
LAYER_WRITE_TOOLS = ("scene_add_sublayer", "scene_remove_sublayer", "scene_set_edit_target", "scene_mute_layer",
                     "scene_unmute_layer", "scene_move_opinions")
CHECKPOINT_WRITE_TOOLS = ("scene_checkpoint", "scene_restore_checkpoint", "scene_prune_checkpoints")
LOG_TOOLS = ("kit_logs", "kit_log_errors", "kit_explain_recent_errors", "kit_log_summary")
READ_ONLY_TOOLS = ("project_templates", "project_list", *LAYER_READ_TOOLS, "scene_checkpoints", "scene_diff",
                   *LOG_TOOLS, "usd_asset_health", "usd_health_checks", "scene_journal_to_script")
NEW_TOOLS = {*PROJECT_TOOLS, *LAYER_READ_TOOLS, *LAYER_WRITE_TOOLS, *CHECKPOINT_WRITE_TOOLS, "scene_checkpoints",
             "scene_diff", *LOG_TOOLS, "usd_asset_health", "usd_health_checks", "scene_journal_to_script",
             "project_report"}
NEW_GROUPS = ("Projects", "Layers", "Checkpoints", "Logs & errors", "Asset health", "Reports")
PHRASES = {
    "start a new digital twin project called factory_twin": ("project_create", RouteTarget.LEARN),
    "what project templates are there": ("project_templates", RouteTarget.LEARN),
    "list my projects": ("project_list", RouteTarget.LEARN),
    "show the layer stack": ("scene_layers", RouteTarget.SCENE),
    "which layer sets /World/X.size": ("scene_where_is", RouteTarget.SCENE),
    "add a sublayer lighting.usda": ("scene_add_sublayer", RouteTarget.SCENE),
    "mute layer X": ("scene_mute_layer", RouteTarget.SCENE),
    "save a checkpoint": ("scene_checkpoint", RouteTarget.SCENE),
    "what changed since the last checkpoint": ("scene_diff", RouteTarget.SCENE),
    "restore checkpoint 3": ("scene_restore_checkpoint", RouteTarget.SCENE),
    "show isaac errors": ("kit_logs", RouteTarget.LEARN),
    "any errors in composer": ("kit_logs", RouteTarget.LEARN),
    "explain the latest errors": ("kit_explain_recent_errors", RouteTarget.LEARN),
    "check asset health": ("usd_asset_health", RouteTarget.SCENE),
    "is this asset ok": ("usd_asset_health", RouteTarget.SCENE),
    "turn what I did into a script": ("scene_journal_to_script", RouteTarget.SCENE),
    "make a project report": ("project_report", RouteTarget.LEARN),
}
# Phrases that keep their old routes and never parse as a round-8 request.
KEPT = (
    "new project Kitchen", "open project Kitchen", "package scene", "how do I create a new project?",
    "how do i start a new project with a dome light?", "how do sublayers work?", "what does a layer offset do?",
    "what is an edit target?", "how do I flatten a stage?", "explain this error: KeyError: 'x'", "what is the error",
    "undo", "what did you change", "what did you change today", "physics report", "check my homework", "save",
    "show the change journal", "what changed in my scene", "mute", "restore", "show me the python for moving a cube",
    "explain my scene", "inspect my robot", "add a franka robot", "add a sphere at 0 0 2", "help", "status",
    "move /World/Props/Cube by 0 0 1", "teach me sublayers", "quiz me on layers", "what is a layer stack?",
)


# --------------------------------------------------------------------------- helpers


def _layer_stage(tmp: str):
    """The live test stage plus a sublayer layers/look.usda (an over that sets /World/Box/Geom.size to 3) and an
    empty, saved layers/lighting.usda that is not in the stack yet."""
    stage = _live_stage(tmp)
    layers = Path(tmp) / "layers"
    layers.mkdir()
    look = Sdf.Layer.CreateNew(str(layers / "look.usda"))
    over = Sdf.CreatePrimInLayer(look, "/World/Box/Geom")
    over.specifier = Sdf.SpecifierOver
    attr = Sdf.AttributeSpec(over, "size", Sdf.ValueTypeNames.Double)
    attr.default = 3.0
    look.Save()
    Sdf.Layer.CreateNew(str(layers / "lighting.usda")).Save()
    stage.GetRootLayer().subLayerPaths.append("./layers/look.usda")
    stage.GetRootLayer().Save()
    return stage


def _size(stage):
    return stage.GetPrimAtPath("/World/Box/Geom").GetAttribute("size").Get()


def _project_scene(root: Path) -> Path:
    """A saved project scene inside the temp workspace: PROJECT_CONTEXT.md, scene.usda and one sublayer."""
    project = root / "demo_proj"
    (project / "layers").mkdir(parents=True)
    (project / "PROJECT_CONTEXT.md").write_text("# Demo project\n\n## Goal\n\nLearn checkpoints.\n", encoding="utf-8")
    sub = Sdf.Layer.CreateNew(str(project / "layers" / "layout.usda"))
    cube = Sdf.CreatePrimInLayer(sub, "/World/Cube")
    cube.specifier = Sdf.SpecifierDef
    cube.typeName = "Cube"
    size = Sdf.AttributeSpec(cube, "size", Sdf.ValueTypeNames.Double)
    size.default = 1.0
    sub.Save()
    scene = Sdf.Layer.CreateNew(str(project / "scene.usda"))
    scene.subLayerPaths.append("./layers/layout.usda")
    world = Sdf.CreatePrimInLayer(scene, "/World")
    world.specifier = Sdf.SpecifierDef
    world.typeName = "Xform"
    scene.defaultPrim = "World"
    scene.Save()
    return project / "scene.usda"


def _set_size(path: Path, value: float) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"size = [0-9.]+", f"size = {value:g}", text), encoding="utf-8")


@contextlib.contextmanager
def _kit_log_folder():
    """A temp Kit log folder with one Isaac Sim session: a warning and a Python traceback (invented content)."""
    with tempfile.TemporaryDirectory() as root:
        lines = header_lines() + [
            line("Warning", "omni.rtx", "Texture not found: demo.png", second=2),
            *py_traceback(3),
            line("Error", "omni.physx.plugin", "PhysX error: invalid inertia tensor on /World/Box", second=4),
        ]
        write_log(root, ISAAC_LOG_APP, "kit_20261022_101010.log", lines, mtime=time.time() - 30)
        with _env(OMNIVERSE_KIT_LOG_DIRS=root):
            yield Path(root)


def _corpus():
    """Every sentence the other tests send through route()/dispatch()/handle() (string literals), plus the round-6
    and round-7 phrase lists and the help phrases of the older groups."""
    pattern = re.compile(r"""(?:\.route|\.dispatch|\.handle|_route)\(\s*(?P<q>["'])(?P<text>[^"'\\\n]{1,200})(?P=q)""")
    found = set()
    for path in TESTS.glob("test_*.py"):
        if path.name in ("test_round8_integration.py", "test_nl_tier7.py"):
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group("text"))
    found.update(HELPER_PHRASES)
    found.update(ROUND7_PHRASES)
    found.update(ROUND7_KEPT)
    for group, phrases in tool_guide.GROUP_PHRASES.items():
        if group not in NEW_GROUPS:
            found.update(phrases)
    found.update(item["say"] for item in tool_guide.quick_start())
    return sorted(found)


# --------------------------------------------------------------------------- registration and wiring


def test_round8_tools_are_registered_with_safe_defaults_and_catalogued():
    with _server() as call:
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert NEW_TOOLS <= set(tools) and len(NEW_TOOLS) == 26 and len(tools) == 169 + len(__import__("omniverse_scene_mcp.features", fromlist=["FEATURES"]).FEATURES), len(tools)
        for name in (*LAYER_WRITE_TOOLS, *CHECKPOINT_WRITE_TOOLS, "project_create"):
            assert tools[name].parameters["properties"]["dry_run"]["default"] is True, name
            description = " ".join(tools[name].description.split())
            assert description.startswith("DRY RUN BY DEFAULT") and "preview to the user first" in description, name
        for name in LAYER_WRITE_TOOLS:
            assert "changes the user's open scene" in " ".join(tools[name].description.split()), name
        for name in READ_ONLY_TOOLS:
            assert " ".join(tools[name].description.split()).startswith("Read-only"), name
        report = tools["project_report"].parameters["properties"]
        assert report["write"]["default"] is False and report["overwrite"]["default"] is False
        assert report["include_assignment"]["default"] is False
        assert "PREVIEW BY DEFAULT" in tools["project_report"].description
        assert "IMPORTANT" in tools["scene_restore_checkpoint"].description
        assert tools["kit_logs"].parameters["properties"]["app"]["default"] == "current"
        assert tools["usd_asset_health"].parameters["properties"]["budget_ms"]["default"] == 8000
        assert tools["scene_flatten_preview"].parameters["properties"]["max_chars"]["default"] == 4000
        assert tools["scene_prune_checkpoints"].parameters["properties"]["keep"]["default"] == 20
    catalog = {entry["name"]: (group, entry) for group, entries in tool_guide.TOOL_CATALOG.items() for entry in entries}
    assert NEW_TOOLS <= set(catalog) and set(NEW_GROUPS) <= set(tool_guide.TOOL_CATALOG)
    for name in (*LAYER_WRITE_TOOLS, *CHECKPOINT_WRITE_TOOLS, "project_create"):
        assert catalog[name][1]["mutates"] is True and "dry_run=True" in catalog[name][1]["example"], name
    for name in READ_ONLY_TOOLS:
        assert catalog[name][1]["mutates"] is False, name
    for name, group in (("project_create", "Projects"), ("scene_layers", "Layers"), ("scene_diff", "Checkpoints"),
                        ("kit_logs", "Logs & errors"), ("usd_asset_health", "Asset health"),
                        ("project_report", "Reports")):
        assert catalog[name][0] == group, name
    for topic, group in (("layers", "Layers"), ("checkpoint", "Checkpoints"), ("logs", "Logs & errors"),
                         ("asset health", "Asset health"), ("projects", "Projects"), ("report", "Reports"),
                         ("health", "Health & connection"), ("undo", "Undo")):
        assert tool_guide.help(topic)["group"] == group, topic
    # Every help phrase of a new group is understood by the router (none falls through to UNKNOWN).
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for group in NEW_GROUPS:
        for phrase in tool_guide.GROUP_PHRASES[group]:
            assert router.route(phrase).target is not RouteTarget.UNKNOWN, (group, phrase)
            assert nl_tier7.parse(phrase) is not None, phrase


def test_tier7_rpc_methods_line_up_across_the_protocol_the_rpc_and_every_bridge():
    assert RPC_VERSION == "tier8-1"
    assert set(LAYER_RPC_METHODS) == set(LAYER_READ_RPC_METHODS) | set(LAYER_WRITE_RPC_METHODS)
    assert HEALTH_RPC_METHODS == ("asset_health",)
    assert set(LAYER_RPC_METHODS) | set(HEALTH_RPC_METHODS) <= set(LIVE_RPC_METHODS) <= set(kit_rpc._HANDLERS)
    for owner in (FakeSceneBridge, HttpSceneBridge, LiveKitSceneBridge, SceneMCPService):
        missing = [name for name in (*LAYER_RPC_METHODS, *HEALTH_RPC_METHODS) if not callable(getattr(owner, name, None))]
        assert not missing, (owner.__name__, missing)
    for name in LAYER_WRITE_RPC_METHODS:  # writes: dropped when stale, never time-budgeted
        assert name in kit_rpc._WRITE_METHODS and name not in kit_rpc._BUDGETED_METHODS, name
    for name in (*LAYER_READ_RPC_METHODS, *HEALTH_RPC_METHODS):  # reads: budgeted like insight, never "writes"
        assert name in kit_rpc._BUDGETED_METHODS and name not in kit_rpc._WRITE_METHODS, name
    journaled = {*LAYER_WRITE_TOOLS, *CHECKPOINT_WRITE_TOOLS, "project_create"}
    assert journaled <= JOURNALED_TOOLS
    assert not ({*READ_ONLY_TOOLS, "project_report", "project_plan"} & JOURNALED_TOOLS)


def test_extended_rpc_parses_layer_and_health_params_strictly():
    calls = []

    class Recorder:
        def layer_stack(self):
            calls.append(("stack",))
            return {"ok": True, "time_budget": {}}

        def layer_where_is(self, prim_path, attribute=None, *, time=None):
            calls.append(("where", prim_path, attribute, time))
            return {"ok": True}

        def layer_flatten_preview(self, prim_path, *, max_chars):
            calls.append(("flatten", prim_path, max_chars))
            return {"ok": True}

        def layer_add_sublayer(self, layer_path, *, position, create_if_missing, relative, allow_outside, offset,
                               scale, dry_run):
            calls.append(("add", layer_path, position, create_if_missing, relative, allow_outside, offset, scale,
                          dry_run))
            return {"ok": True, "dry_run": dry_run}

        def layer_remove_sublayer(self, layer, *, discard_unsaved_edits, dry_run):
            calls.append(("remove", layer, discard_unsaved_edits, dry_run))
            return {"ok": True}

        def layer_set_edit_target(self, layer, *, dry_run):
            calls.append(("target", layer, dry_run))
            return {"ok": True}

        def layer_mute(self, layer, *, discard_unsaved_edits, dry_run):
            calls.append(("mute", layer, discard_unsaved_edits, dry_run))
            return {"ok": True}

        def layer_unmute(self, layer, *, dry_run):
            calls.append(("unmute", layer, dry_run))
            return {"ok": True}

        def layer_move_opinions(self, prim_path, src_layer, dst_layer, *, include_children, properties, allow_changes,
                                dry_run):
            calls.append(("move", prim_path, src_layer, dst_layer, include_children, properties, allow_changes, dry_run))
            return {"ok": True}

        def asset_health(self, root_path, *, max_prims):
            calls.append(("health", root_path, max_prims))
            return {"ok": True}

    server = kit_rpc.make_extended_server_class(_BasicServer)(Recorder())
    dispatch = server._dispatch
    stack = dispatch({"method": "layer_stack", "params": {"budget_ms": 500}})["result"]
    assert calls[-1] == ("stack",) and stack["time_budget"]["budget_ms"] == 500.0
    dispatch({"method": "layer_where_is", "params": {"prim_path": "/World/Cube", "attribute": "size", "time": 3}})
    assert calls[-1] == ("where", "/World/Cube", "size", 3.0)
    dispatch({"method": "layer_where_is", "params": {"prim_path": "/World/Cube", "time": "default"}})
    assert calls[-1] == ("where", "/World/Cube", None, "default")
    dispatch({"method": "layer_flatten_preview", "params": {"prim_path": "/World/Cube"}})
    assert calls[-1] == ("flatten", "/World/Cube", 4000)
    dispatch({"method": "layer_add_sublayer", "params": {"layer_path": "layers/a.usda"}})
    assert calls[-1] == ("add", "layers/a.usda", "strongest", False, True, False, 0.0, 1.0, True)  # missing = dry run
    odd = dispatch({"method": "layer_add_sublayer", "params": {"layer_path": "a.usda", "position": 2, "dry_run": "no"}})
    assert calls[-1][2] == 2 and calls[-1][-1] is True and "dry_run='no'" in odd["result"]["warnings"][0]
    dispatch({"method": "layer_remove_sublayer", "params": {"layer": "a.usda", "dry_run": False}})
    assert calls[-1] == ("remove", "a.usda", False, False)
    dispatch({"method": "layer_set_edit_target", "params": {"layer": "root"}})
    assert calls[-1] == ("target", "root", True)
    dispatch({"method": "layer_mute", "params": {"layer": "a.usda", "discard_unsaved_edits": True}})
    assert calls[-1] == ("mute", "a.usda", True, True)
    dispatch({"method": "layer_unmute", "params": {"layer": "a.usda", "dry_run": "false"}})
    assert calls[-1] == ("unmute", "a.usda", False)
    dispatch({"method": "layer_move_opinions", "params": {"prim_path": "/World/Cube", "src_layer": "root",
                                                         "dst_layer": "a.usda", "properties": ["size"]}})
    assert calls[-1] == ("move", "/World/Cube", "root", "a.usda", True, ["size"], False, True)
    dispatch({"method": "asset_health", "params": {"root_path": "/World", "budget_ms": 900}})
    assert calls[-1] == ("health", "/World", 50_000)
    for bad in (
        {"method": "layer_stack", "params": {"extra": 1}},
        {"method": "layer_where_is", "params": {}},
        {"method": "layer_where_is", "params": {"prim_path": "/A", "time": "soon"}},
        {"method": "layer_flatten_preview", "params": {}},
        {"method": "layer_flatten_preview", "params": {"prim_path": "/"}},
        {"method": "layer_flatten_preview", "params": {"prim_path": "/A", "max_chars": 50}},
        {"method": "layer_add_sublayer", "params": {}},
        {"method": "layer_add_sublayer", "params": {"layer_path": "a.usda", "position": "middle"}},
        {"method": "layer_add_sublayer", "params": {"layer_path": "a.usda", "create_if_missing": "maybe"}},
        {"method": "layer_add_sublayer", "params": {"layer_path": "a.usda", "scale": "big"}},
        {"method": "layer_remove_sublayer", "params": {"layer": ""}},
        {"method": "layer_mute", "params": {"layer": "a\0b"}},
        {"method": "layer_move_opinions", "params": {"prim_path": "/A", "src_layer": "root"}},
        {"method": "layer_move_opinions", "params": {"prim_path": "/A", "src_layer": "a", "dst_layer": "b",
                                                     "properties": []}},
        {"method": "asset_health", "params": {"max_prims": 0}},
        {"method": "asset_health", "params": {"budget_ms": -1}},
    ):
        try:
            dispatch(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    lacking = kit_rpc.make_extended_server_class(_BasicServer)(object())._dispatch({"method": "layer_stack", "params": {}})
    assert lacking["result"]["needs_bridge_update"] is True and lacking["result"]["method"] == "layer_stack"
    info = dispatch({"method": "capabilities", "params": {}})["result"]
    assert info["version"] == "tier8-1" and info["layer_write_methods"] == list(LAYER_WRITE_RPC_METHODS)
    assert info["layer_read_methods"] == list(LAYER_READ_RPC_METHODS) and info["health_methods"] == ["asset_health"]


# --------------------------------------------------------------------------- layer manager


def test_layer_tools_on_the_fake_scene_and_older_bridges_say_why_and_change_nothing():
    with _server() as call:
        state = repr(call.router._scene._bridge._state())
        for name, arguments in (
            ("scene_add_sublayer", {"layer_path": "layers/a.usda"}),
            ("scene_remove_sublayer", {"layer": "a.usda", "dry_run": False}),
            ("scene_set_edit_target", {"layer": "root"}),
            ("scene_mute_layer", {"layer": "a.usda"}),
            ("scene_unmute_layer", {"layer": "a.usda"}),
            ("scene_move_opinions", {"prim_path": "/World/Props/Cube", "src_layer": "root", "dst_layer": "a.usda"}),
        ):
            result = call(name, **arguments)
            assert result["ok"] is False and result["applied"] is False and result.get("fake_scene") is True, (name, result)
            assert "Nothing was changed" in result["message"], name
        assert call("scene_add_sublayer", layer_path="  ")["ok"] is False
        # The read-only ones fall back to the live scene's saved file, which the fake scene does not have.
        layers = call("scene_layers")
        assert layers["ok"] is False and "fake scene" in layers["live_fallback"]["reason"] and "scene_file=" in layers["message"]
        assert call("scene_flatten_preview", prim_path="/")["ok"] is False
        assert call("scene_flatten_preview", prim_path="/World", max_chars=10)["ok"] is False
        assert repr(call.router._scene._bridge._state()) == state
        tried = call("scene_journal")["entries"]  # previews are never journaled; the refused apply is an attempt
        assert [(entry["tool"], entry["ok"]) for entry in tried] == [("scene_remove_sublayer", False)], tried
    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(COMPOSER))) as call:
        with _in_process_transport(_BasicServer(FakeSceneBridge())):
            old = call("scene_mute_layer", layer="a.usda")
            assert old["ok"] is False and old["needs_bridge_update"] is True and old["how_to_enable"], old
            capabilities = call("scene_capabilities")
            assert set(LAYER_RPC_METHODS) | {"asset_health"} <= set(capabilities["missing_live_methods"])
            assert capabilities["layers"]["available"] is False and capabilities["health"]["available"] is False


def test_live_layer_manager_reads_previews_applies_and_puts_back():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        stage = _layer_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (_b, _s, sent):
            capabilities = call("scene_capabilities")
            assert capabilities["rpc_version"] == "tier8-1" and capabilities["needs_bridge_update"] is False
            assert capabilities["layers"]["available"] is True and capabilities["health"]["available"] is True
            listed = call("scene_layers")
            assert listed["ok"] is True and listed["mode"] == "live" and listed["read_only"] is True, listed
            names = [layer["display_name"] for layer in listed["layers"]]
            assert any("look.usda" in name for name in names) and listed["layer_count"] >= 2
            where = call("scene_where_is", prim_path="/World/Box/Geom.size")
            assert where["ok"] is True and where["mode"] == "live" and "look.usda" in json.dumps(where), where
            flat = call("scene_flatten_preview", prim_path="/World/Box")
            assert flat["ok"] is True and "size = 3" in flat["usda"] and flat["mode"] == "live", flat
            assert _layers_text(stage) == original

            preview = call("scene_add_sublayer", layer_path="layers/lighting.usda")
            assert preview["ok"] is True and preview["preview"] is True and preview["applied"] is False, preview
            assert "Kit's Layer window" in preview["kit_note"] and _layers_text(stage) == original
            assert call("scene_journal")["count"] == 0
            added = call("scene_add_sublayer", layer_path="layers/lighting.usda", position="weakest", dry_run=False)
            assert added["ok"] is True and added["applied"] is True, added
            assert list(stage.GetRootLayer().subLayerPaths)[-1].endswith("lighting.usda")
            assert any("Kit's Layer window" in warning for warning in added["warnings"])
            assert added["undo"]["function"] == "remove_sublayer" and "Put it back with" in added["message"]
            removed = call("scene_remove_sublayer", layer="lighting.usda", dry_run=False)
            assert removed["applied"] is True and removed["restore_with"]["function"] == "add_sublayer", removed
            assert [str(item) for item in stage.GetRootLayer().subLayerPaths] == ["./layers/look.usda"]

            assert _size(stage) == 3.0
            muted = call("scene_mute_layer", layer="look.usda", dry_run=False)
            assert muted["applied"] is True and stage.IsLayerMuted(muted["layer"]) and _size(stage) != 3.0, muted
            unmuted = call("scene_unmute_layer", layer="look.usda", dry_run=False)
            assert unmuted["applied"] is True and _size(stage) == 3.0, unmuted

            target = call("scene_set_edit_target", layer="look.usda", dry_run=False)
            assert target["applied"] is True and stage.GetEditTarget().GetLayer().identifier.endswith("look.usda")
            back = call("scene_set_edit_target", layer=target["restore_with"]["layer_identifier"], dry_run=False)
            assert back["applied"] is True and stage.GetEditTarget().GetLayer() == stage.GetRootLayer(), back

            plan = call("scene_move_opinions", prim_path="/World/Box/Geom", src_layer="look.usda", dst_layer="root")
            assert plan["ok"] is True and plan["preview"] is True and plan["plan"]["destination_is"] == "stronger", plan
            moved = call("scene_move_opinions", prim_path="/World/Box/Geom", src_layer="look.usda", dst_layer="root",
                         dry_run=False)
            assert moved["applied"] is True and moved["undoable"] is True and moved["undo"]["recorded"] is True, moved
            assert "size = 3" in stage.GetRootLayer().ExportToString()
            assert call("scene_undo_last", dry_run=False)["applied"] is True
            assert stage.GetRootLayer().GetAttributeAtPath("/World/Box/Geom.size") is None and _size(stage) == 3.0

            sentences = call("scene_journal", limit=50)["sentences"]
            text = " ".join(sentences)
            assert "added the sublayer" in text and "muted the layer" in text and "moved the opinions" in text, sentences
            assert len(sentences) == 8, sentences  # add, remove, mute, unmute, 2 edit targets, move, undo
            # A sentence only ever previews, even with dry_run=False.
            said = call.dispatch("add a sublayer layers/lighting.usda", dry_run=False)
            assert said["preview"] is True and said["applied"] is False and said["ok"] is True, said
            assert "scene_add_sublayer(" in said["apply_with"] and "dry_run=False" in said["apply_with"]
            muted_said = call.dispatch("mute layer look.usda", dry_run=False)
            assert muted_said["preview"] is True and not stage.IsLayerMuted(muted["layer"]), muted_said
            shown = call.dispatch("show the layer stack")
            assert shown["tool"] == "scene_layers" and shown["ok"] is True and shown["read_only"] is True
            which = call.dispatch("which layer sets /World/Box/Geom.size")
            assert which["tool"] == "scene_where_is" and which["ok"] is True, which
            assert len(call("scene_journal", limit=50)["entries"]) == 8  # sentences never wrote anything
            flags = [item["params"]["dry_run"] for item in sent if item["method"] in LAYER_WRITE_RPC_METHODS]
            assert flags[0] is True and flags[-2:] == [True, True], flags
        assert [str(item) for item in stage.GetRootLayer().subLayerPaths] == ["./layers/look.usda"]


def test_layer_reads_open_a_saved_file_read_only():
    if Usd is None:
        return
    with _server() as call:
        scene = _project_scene(Path(os.environ["OMNIVERSE_ROOT"]))
        before = (scene.stat().st_mtime_ns, scene.read_bytes())
        listed = call("scene_layers", scene_file="demo_proj/scene.usda")
        assert listed["ok"] is True and listed["mode"] == "saved_file" and listed["layer_count"] >= 2, listed
        where = call("scene_where_is", prim_path="/World/Cube", attribute="size", scene_file=str(scene))
        assert where["ok"] is True and "layout.usda" in json.dumps(where)
        flat = call("scene_flatten_preview", prim_path="/World/Cube", scene_file=str(scene), max_chars=300)
        assert flat["ok"] is True and flat["usda"] and flat["mode"] == "saved_file"
        assert call("scene_layers", scene_file=str(WORKSPACE / "nowhere.usda"))["ok"] is False
        assert (scene.stat().st_mtime_ns, scene.read_bytes()) == before


# --------------------------------------------------------------------------- checkpoints


def test_checkpoints_preview_create_diff_restore_and_prune_inside_the_project_only():
    if Usd is None:
        return
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        scene = _project_scene(root)
        folder = scene.parent / checkpoints.FOLDER_NAME
        rel = "demo_proj/scene.usda"
        planned = call("scene_checkpoint", scene_file=rel, note="first")
        assert planned["ok"] is True and planned["dry_run"] is True and planned["created"] is False, planned
        assert not folder.exists()
        made = call("scene_checkpoint", scene_file=rel, note="first", dry_run=False)
        assert made["ok"] is True and made["created"] is True and len(made["files"]) == 2, made
        assert (folder / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
        listed = call("scene_checkpoints", scene_file=rel)
        assert listed["count"] == 1 and listed["checkpoints"][0]["note"] == "first"
        layout = scene.parent / "layers" / "layout.usda"
        _set_size(layout, 5.0)
        changed = call("scene_diff", scene_file=rel)
        assert changed["ok"] is True and "size" in json.dumps(changed), changed
        restore = call("scene_restore_checkpoint", checkpoint=1, scene_file=rel)
        assert restore["ok"] is True and restore["dry_run"] is True and restore["message"].startswith("IMPORTANT:"), restore
        assert restore["app_reload_warning"] == checkpoints.APP_RELOAD_WARNING == restore["warnings"][0]
        assert "size = 5" in layout.read_text(encoding="utf-8")
        done = call("scene_restore_checkpoint", checkpoint=1, scene_file=rel, dry_run=False)
        assert done["ok"] is True and done["dry_run"] is False and done["safety_checkpoint"], done
        assert "IMPORTANT" in done["message"] and done["important"] == checkpoints.APP_RELOAD_WARNING
        assert "size = 1" in layout.read_text(encoding="utf-8")
        assert call("scene_checkpoints", scene_file=rel)["count"] == 2  # the safety checkpoint taken first
        pruned = call("scene_prune_checkpoints", scene_file=rel, keep=1)
        assert pruned["ok"] is True and pruned["dry_run"] is True and call("scene_checkpoints", scene_file=rel)["count"] == 2
        assert call("scene_prune_checkpoints", scene_file=rel, keep=1, dry_run=False)["ok"] is True
        assert call("scene_checkpoints", scene_file=rel)["count"] == 1
        assert call("scene_prune_checkpoints", scene_file=rel, keep=0)["ok"] is False
        # The fake scene has no saved file to default to; a scene outside the workspace is refused.
        fake = call("scene_checkpoint")
        assert fake["ok"] is False and "not saved to a local USD file" in fake["message"], fake
        assert call("scene_checkpoints", scene_file=str(WORKSPACE.parent / "x.usda"))["ok"] is False
        restore_fake = call("scene_restore_checkpoint", checkpoint=1)
        assert restore_fake["ok"] is False and restore_fake["message"].startswith("IMPORTANT:")
        said = call.dispatch("restore checkpoint 1", dry_run=False)
        assert said["preview"] is True and said["applied"] is False and said["tool"] == "scene_restore_checkpoint"
        assert said["arguments"]["dry_run"] is True and "dry_run=False" in said["apply_with"]
        saved = call.dispatch("save a checkpoint", dry_run=False)
        assert saved["preview"] is True and saved["tool"] == "scene_checkpoint" and saved["arguments"]["dry_run"] is True
        tools = [(entry["tool"], entry["ok"]) for entry in call("scene_journal", limit=50)["entries"]]
        assert ("scene_checkpoint", True) in tools and ("scene_restore_checkpoint", True) in tools, tools
        assert ("scene_prune_checkpoints", True) in tools and len(tools) == 3, tools
        sentences = " ".join(call("scene_journal", limit=50)["sentences"])
        assert "restored the checkpoint" in sentences and "Reload the scene in the app" in sentences
        tip = call("scene_save", path="C:/tmp/Copy.usda", dry_run=True)
        assert tip["dry_run"] is True and "scene_checkpoint(" in tip["tip"]
    assert not (WORKSPACE / checkpoints.FOLDER_NAME).exists()


# --------------------------------------------------------------------------- Kit logs


def test_kit_log_tools_read_only_the_configured_log_folder():
    with _kit_log_folder() as root, _server() as call:
        before = {path: path.stat().st_mtime_ns for path in root.rglob("*.log")}
        logs = call("kit_logs", app="isaac", level="error")
        assert logs["ok"] is True and logs["app"] == "isaac" and logs["count"] >= 2, logs
        assert all(entry["level"] in ("error", "fatal") for entry in logs["entries"])
        assert logs["log"]["name"] == "kit_20261022_101010.log"
        current = call("kit_logs")  # 'current' follows the scene target: the fake scene falls back to Isaac Sim
        assert current["ok"] is True and current["app"] == "isaac" and "Isaac Sim log" in current["notes"][0]
        assert call("kit_logs", app="composer")["ok"] is False
        assert call("kit_logs", app="C:/Windows/notepad.log")["ok"] is False  # never an arbitrary path
        assert call("kit_logs", level="loud")["ok"] is False and call("kit_logs", lines=0)["ok"] is False
        watch = call("kit_log_errors", app="isaac")
        assert watch["ok"] is True and watch["count"] == 0 and watch["cursor"]
        explained = call("kit_explain_recent_errors", app="isaac", limit=3)
        assert explained["ok"] is True and explained["errors"] and explained["errors"][0]["explanation"]["summary"]
        summary = call("kit_log_summary", app="isaac")
        assert summary["ok"] is True and summary["errors"] >= 2, summary
        said = call.dispatch("show isaac errors")
        assert said["tool"] == "kit_logs" and said["ok"] is True and said["level"] == "error", said
        assert call.dispatch("explain the latest errors")["tool"] == "kit_explain_recent_errors"
        assert {path: path.stat().st_mtime_ns for path in root.rglob("*.log")} == before


# --------------------------------------------------------------------------- asset health


def test_asset_health_reads_a_file_the_live_stage_and_falls_back_on_the_fake_scene():
    if Usd is None:
        return
    with _server() as call:
        scene = _project_scene(Path(os.environ["OMNIVERSE_ROOT"]))
        before = (scene.stat().st_mtime_ns, scene.read_bytes())
        report = call("usd_asset_health", scene_file="demo_proj/scene.usda")
        assert report["ok"] is True and report["mode"] == "saved_file" and 0 <= report["score"] <= 100, report
        assert (scene.stat().st_mtime_ns, scene.read_bytes()) == before
        assert call("usd_asset_health", budget_ms=5)["ok"] is False
        fake = call("usd_asset_health")
        assert fake["ok"] is False and "scene_file=" in fake["message"]
        checks = call("usd_health_checks")
        assert checks["ok"] is True and checks["count"] == len(checks["checks"]) > 5
        said = call.dispatch("is demo_proj/scene.usda ok")
        assert said["tool"] == "usd_asset_health" and said["ok"] is True and said["score"] == report["score"], said
    with tempfile.TemporaryDirectory() as tmp:
        stage = _layer_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (_b, _s, sent):
            live = call("usd_asset_health", root_path="/World")
            assert live["ok"] is True and live["mode"] == "live" and "time_budget" in live, live
            assert [item["params"].get("budget_ms") for item in sent if item["method"] == "asset_health"] == [8000.0]
        assert _layers_text(stage) == original


# --------------------------------------------------------------------------- projects, journal script and report


def test_project_tools_plan_create_list_and_never_overwrite():
    if Usd is None:
        return
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        templates = call("project_templates")
        assert templates["ok"] is True and "digital_twin" in {row["name"] for row in templates["templates"]}
        plan = call("project_plan", name="factory_twin", template="digital_twin")
        assert plan["ok"] is True and plan["dry_run"] is True and not (root / "factory_twin").exists()
        preview = call("project_create", name="factory_twin", template="digital_twin")
        assert preview["ok"] is True and preview["dry_run"] is True and not (root / "factory_twin").exists()
        said = call.dispatch("start a new digital twin project called factory_twin", dry_run=False)
        assert said["preview"] is True and said["tool"] == "project_create" and said["ok"] is True, said
        assert said["arguments"] == {"name": "factory_twin", "template": "digital_twin", "dry_run": True}
        assert not (root / "factory_twin").exists()
        made = call("project_create", name="factory_twin", template="digital_twin", goal="Learn layers",
                    dry_run=False)
        assert made["ok"] is True and (root / "factory_twin" / "root.usda").is_file(), made
        again = call("project_create", name="factory_twin", dry_run=False)
        assert again["ok"] is False and "already exists" in again["error"]
        assert call("project_create", name="../escape", dry_run=False)["ok"] is False
        listed = call("project_list")
        assert listed["ok"] is True and [row["name"] for row in listed["projects"]] == ["factory_twin"], listed
        assert call.dispatch("list my projects")["count"] == 1
        journaled = [(entry["tool"], entry["ok"]) for entry in call("scene_journal")["entries"]]
        # Newest first: the refused '../escape' and 'already exists' attempts, then the real create; never previews.
        assert journaled == [("project_create", False), ("project_create", False), ("project_create", True)], journaled
        sentence = call("scene_journal", tool="project_create", limit=10)["sentences"][-1]
        assert "created the new project factory_twin" in sentence and "files on disk" in sentence, sentence


def test_journal_to_script_replays_applied_edits_only():
    with _server() as call:
        call("scene_move", path="/World/Props/Cube", translation=[0, 0, 2], dry_run=True)
        empty = call("scene_journal_to_script")
        assert empty["ok"] is False and empty["code"] is None and empty["read_only"] is True
        call("scene_move", path="/World/Props/Cube", translation=[0, 0, 2], dry_run=False)
        script = call("scene_journal_to_script", context="standalone", title="My day")
        assert script["ok"] is True and "/World/Props/Cube" in script["code"] and "My day" in script["code"], script
        compile(script["code"], "<journal>", "exec")
        assert script["journal_entries_read"] == 1
        assert call("scene_journal_to_script", limit=0)["ok"] is False
        said = call.dispatch("turn what I did into a script")
        assert said["tool"] == "scene_journal_to_script" and said["ok"] is True and said["code"], said


def test_project_report_previews_and_writes_only_into_the_project_docs_folder():
    if Usd is None:
        return
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        assert call("project_create", name="cell", template="robot_cell", goal="A robot cell", dry_run=False)["ok"]
        before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        preview = call("project_report", project_dir="cell")
        assert preview["ok"] is True and preview["dry_run"] is True and preview["written"] == [], preview
        assert preview["gathered"]["tour"] == "ok" and preview["gathered"]["health"] == "ok", preview["gathered"]
        assert preview["gathered"]["assignment"] == "skipped" and "<html" in preview["html"].lower()
        assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == before
        said = call.dispatch("make a project report for cell", dry_run=False)
        assert said["preview"] is True and said["written"] == [] and "write=True" in said["apply_with"], said
        written = call("project_report", project_dir="cell", title="My robot cell", write=True)
        assert written["ok"] is True and len(written["written"]) == 2, written
        assert all(Path(path).parent == root / "cell" / "docs" for path in written["written"])
        assert "My robot cell" in (root / "cell" / "docs" / "report.md").read_text(encoding="utf-8")
        again = call("project_report", project_dir="cell", write=True)
        assert again["ok"] is False and "overwrite=True" in again["error"]
        assert call("project_report", project_dir="cell", write=True, overwrite=True)["ok"] is True
        outside = call("project_report", project_dir=str(WORKSPACE), write=True)
        assert outside["ok"] is False and outside["written"] == []
        assert call("project_report", project_dir="no_such_project")["ok"] is False
        no_folder = call("project_report", write=True, include_tour=False, include_health=False)
        assert no_folder["ok"] is False and "project_dir" in no_folder["error"]


# --------------------------------------------------------------------------- natural-language routing


def test_round8_phrases_route_to_the_new_tools():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for phrase, (tool, target) in PHRASES.items():
        decision = router.route(phrase)
        assert decision.target is target, (phrase, decision)
        spec = nl_tier7.parse(phrase)
        assert spec is not None and router_module._ROUND8_TOOLS[spec["kind"]] == tool, (phrase, spec)
        assert nl_tier6.parse(phrase) is None and nl_tier5.parse(phrase) is None, phrase
    # Without the server's helpers the pure answers still work and the rest name their tool.
    templates = asyncio.run(router.handle("what project templates are there")).payload
    assert templates["executed"] is True and templates["count"] >= 5
    plan = asyncio.run(router.handle("start a new warehouse project called shelf_study")).payload
    assert plan["preview"] is True and plan["dry_run"] is True and plan["template"] == "warehouse", plan
    logs = asyncio.run(router.handle("show isaac errors")).payload
    assert logs["executed"] is False and "kit_logs(" in logs["message"]
    for phrase in [p for p, (_tool, _target) in PHRASES.items() if nl_tier7.parse(p)["kind"] in nl_tier7.PREVIEW_KINDS]:
        payload = asyncio.run(router.handle(phrase, dry_run=False)).payload
        assert payload["preview"] is True and payload["applied"] is False and payload["dry_run"] is True, phrase


def test_nl_tier7_parses_values():
    assert nl_tier7.parse("start a new digital twin project called factory_twin") == {
        "kind": "project_create", "name": "factory_twin", "template": "digital_twin"}
    assert nl_tier7.parse("create a project called cell_01 from the robot cell template")["template"] == "robot_cell"
    assert nl_tier7.parse("start a new flux capacitor project called x") is None
    assert nl_tier7.parse("which layer sets /World/X.size") == {"kind": "where_is", "prim_path": "/World/X",
                                                                "attribute": "size"}
    assert nl_tier7.parse("which layer sets /World/X")["attribute"] is None
    spec = nl_tier7.parse("add a new sublayer layers/lighting.usda as the weakest")
    assert spec == {"kind": "add_sublayer", "layer_path": "layers/lighting.usda", "position": "weakest",
                    "create_if_missing": True}
    assert nl_tier7.parse("unmute layer lighting.usda") == {"kind": "unmute_layer", "layer": "lighting.usda"}
    assert nl_tier7.parse("save a checkpoint called before lights")["note"] == "before lights"
    assert nl_tier7.parse("what changed since checkpoint 2") == {"kind": "diff", "a": 2, "b": "current"}
    assert nl_tier7.parse("restore checkpoint 3") == {"kind": "restore", "checkpoint": 3}
    assert nl_tier7.parse("go back to the previous checkpoint")["checkpoint"] == "previous"
    assert nl_tier7.parse("any warnings in composer") == {"kind": "kit_logs", "app": "composer", "level": "warning"}
    assert nl_tier7.parse("explain the latest isaac errors")["app"] == "isaac"
    assert nl_tier7.parse("check asset health on /World/Robot")["root_path"] == "/World/Robot"
    assert nl_tier7.parse("is robots/arm.usda ok")["scene_file"] == "robots/arm.usda"
    assert nl_tier7.parse("make a project report for factory_twin")["project_dir"] == "factory_twin"
    assert nl_tier7.parse("please show the layer stack!")["kind"] == "layer_stack"


def test_existing_routes_are_unchanged_by_round8():
    """Every phrase the other tests send (round 6 and round 7 corpora included) routes exactly as without nl_tier7."""
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    phrases = _corpus()
    assert len(phrases) >= 89, len(phrases)
    after = {phrase: router.route(phrase) for phrase in phrases}
    original = nl_tier7.parse
    nl_tier7.parse = lambda _text: None
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier7.parse = original
    changed = [(phrase, before[phrase].target, after[phrase].target, after[phrase].reason)
               for phrase in phrases if before[phrase] != after[phrase]]
    assert not changed, changed
    parsed = [phrase for phrase in phrases if nl_tier7.parse(phrase) is not None]
    assert not parsed, parsed
    for phrase in KEPT:
        assert nl_tier7.parse(phrase) is None, phrase
    assert router.route("new project Kitchen").target is not RouteTarget.LEARN
    assert router.route("how do sublayers work?").target is RouteTarget.USD


def test_nl_tier7_stays_fast_on_long_input():
    for text in ("x" + "." * 20_000, "start a " + "new " * 140 + "project called x", "which layer sets /" + "A/" * 280,
                 "add a sublayer " + "a" * 580 + ".usda", "mute layer " + "x " * 290, "save a checkpoint called " + "a " * 280,
                 "show " + "isaac " * 99 + "errors", "is " + "a" * 590 + " ok", "turn " + "what i did " * 55 + "into a script",
                 "restore checkpoint " + "1" * 580, "check asset health on " + "/A" * 280):
        started = time.perf_counter()
        nl_tier7.parse(text)
        assert time.perf_counter() - started < 0.5, text[:40]
    assert nl_tier7.parse("show the layer stack " + "?" * 1000) is None  # over the phrase cap


# --------------------------------------------------------------------------- packaging and self-tests


def test_bridge_reload_self_tests_include_round8_files():
    for name in ("test_layer_manager.py", "test_asset_health.py"):
        assert (TESTS / name).is_file(), name
    for script in (WORKSPACE / "isaacsim-study" / "reload_mcp_bridge.py",
                   WORKSPACE / "composer-study" / "reload_composer_bridge.py"):
        if script.is_file():
            text = script.read_text(encoding="utf-8")
            assert "test_layer_manager.py" in text and "test_asset_health.py" in text, script.name
    smoke = (TESTS / "test_asset_health.py").read_text(encoding="utf-8")
    assert "if _IN_KIT:" in smoke  # the real-asset smoke tests return at once inside Kit
    templates = WORKSPACE / "omniverse_scene_mcp" / "src" / "omniverse_scene_mcp" / "data" / "project_templates.json"
    assert json.loads(templates.read_text(encoding="utf-8"))["templates"]
    pyproject = (WORKSPACE / "omniverse_scene_mcp" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"data/*.json"' in pyproject  # project_templates.json ships with the package
