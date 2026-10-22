"""Tier 3 integration: safe attribute/material edits, MCP undo, offline Kit/Isaac Sim docs, the tool guide and
scene-target switching, wired through every layer (server tool -> router -> service -> bridge -> RPC -> Kit bridge).

Everything runs offline on a test-mode server: the in-memory fake scene, or an in-process HTTP transport into the
extended live RPC over a temporary pxr stage (only Kit's omni.usd context is faked), and a synthetic Isaac Sim docs
tree in a temp folder. Sockets are refused, and the real exam-prep folder is checked (listing and mtimes) after every
server. Plain fixture-free test_* functions, so they also run under the stdlib runner.
"""

import os

# The stdlib test runner may not load conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import inspect
import json
import socket
import tempfile
import types
import urllib.request
from dataclasses import asdict
from pathlib import Path

from omniverse_scene_mcp import edits, http_bridge, kit_bridge, kit_commands, kit_rpc, runtime
from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp import offline_docs_knowledge
from omniverse_scene_mcp import server as server_module
from omniverse_scene_mcp.bridge import BASIC_RPC_METHODS, EDIT_RPC_METHODS, LIVE_RPC_METHODS, RPC_VERSION, UNDO_RPC_METHODS
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.kit_bridge import LiveKitSceneBridge
from omniverse_scene_mcp.offline_docs_knowledge import OfflineDocsKnowledge, OfflineDocsKnowledgeBackend
from omniverse_scene_mcp.offline_knowledge import FallbackKnowledgeBackend
from omniverse_scene_mcp.router import NullKnowledgeBackend, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService
from omniverse_scene_mcp.undo import UndoStack

try:
    from pxr import Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - the live-stage tests below then return early
    Usd = UsdGeom = UsdShade = None

REAL_EXAM_PREP = Path(__file__).resolve().parents[2] / "exam-prep"
EDIT_TOOLS = ("scene_set_attribute", "scene_create_material", "scene_bind_material")
NEW_TOOLS = {
    *EDIT_TOOLS,
    "scene_undo_last",
    "scene_undo_history",
    "router_help",
    "router_scene_targets",
    "router_set_scene_target",
    "docs_search",
    "extension_info",
}
ISAAC = "http://127.0.0.1:9911"
COMPOSER = "http://127.0.0.1:9910"


# --------------------------------------------------------------------------- helpers


def _exam_prep_state():
    """Listing and mtimes of the real exam-prep folder (None when it does not exist)."""
    if not REAL_EXAM_PREP.is_dir():
        return None
    return {entry.name: (entry.stat().st_mtime_ns, entry.stat().st_size) for entry in REAL_EXAM_PREP.iterdir()}


@contextlib.contextmanager
def _guard():
    """Refuse every socket connection and check that the real exam-prep folder is untouched."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(repr(args[-1:] or kwargs))
        raise AssertionError(f"network access attempted: {args!r}")

    async def refuse_async(*args, **kwargs):
        refuse(*args, **kwargs)

    patched = [
        (socket, "create_connection", refuse),
        (socket.socket, "connect", refuse),
        (socket.socket, "connect_ex", refuse),
        (urllib.request, "urlopen", refuse),
        (runtime, "urlopen", refuse),
        (http_bridge, "urlopen", refuse),
        (asyncio, "open_connection", refuse_async),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in patched]
    before = _exam_prep_state()
    # The Windows event loop builds its self-pipe with socket.connect, so create it before patching.
    loop = asyncio.new_event_loop()
    for owner, name, replacement in patched:
        setattr(owner, name, replacement)
    try:
        yield loop
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)
        loop.close()
    assert attempts == [], attempts
    assert _exam_prep_state() == before, "the real exam-prep folder changed"


@contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _server(scene=None, docs_root=None):
    """A test-mode server over a temp workspace; yields call(name, **arguments). scene() may replace the fake scene."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data").mkdir()
        with _env(
            OMNIVERSE_SCENE_MCP_TEST="1",
            OMNIVERSE_ROOT=root,
            OMNIVERSE_USD_KNOWLEDGE_DATA=root / "data",
            OMNIVERSE_EXAM_PREP_DIR=None,
            OMNIVERSE_KIT_DOCS_ROOT=docs_root or (root / "no-isaac-install"),
        ):
            offline_docs_knowledge.clear_cache()
            with _guard() as loop:
                original = server_module.build_router
                built = {}

                def build_router(*args, **kwargs):
                    router = original(*args, **kwargs)
                    if scene is not None:
                        router._scene = scene()
                    built["router"] = router
                    return router

                server_module.build_router = build_router
                try:
                    app = server_module.build_server()
                finally:
                    server_module.build_router = original

                def call(tool, /, **arguments):  # positional-only: some tools take an argument called name
                    result = loop.run_until_complete(app._tool_manager.call_tool(tool, arguments))
                    json.dumps(result)  # every tool result must be JSON-ready
                    return result

                def dispatch(request, dry_run=False):
                    return call("router_dispatch", request=request, dry_run=dry_run)["payload"]

                call.app, call.loop, call.router, call.dispatch = app, loop, built["router"], dispatch
                try:
                    yield call
                finally:
                    offline_docs_knowledge.clear_cache()


class _FakeResponse:
    def __init__(self, body) -> None:
        self._data = json.dumps(body).encode("utf-8")

    def read(self, *_args):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@contextlib.contextmanager
def _in_process_transport(server):
    """HttpSceneBridge RPCs go straight into server._dispatch (exceptions -> ok False, like the live pump)."""
    calls = []

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append({"url": request.full_url, "method": payload["method"], "params": payload["params"]})
        try:
            body = server._dispatch(payload)
        except Exception as exc:  # noqa: BLE001 - mirrors LiveSceneControlServer._pump
            body = {"ok": False, "message": str(exc)}
        return _FakeResponse(body)

    previous = http_bridge.urlopen
    http_bridge.urlopen = fake_urlopen
    try:
        yield calls
    finally:
        http_bridge.urlopen = previous


class _BasicServer:
    """An old live_scene_control server: a few basic methods on its bridge, else 'Unsupported live scene method'."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge

    def _dispatch(self, payload):
        method = str(payload.get("method", "")).strip().lower()
        params = payload.get("params") or {}
        if method == "ping":
            return {"ok": True, "result": {"status": self._bridge.ping()}}
        if method == "snapshot":
            return {"ok": True, "result": asdict(self._bridge.snapshot())}
        if method == "selection":
            return {"ok": True, "result": {"selection": self._bridge.get_selection()}}
        if method == "inspect":
            prim = self._bridge.get_prim(params["path"])
            return {"ok": True, "result": {"prim": asdict(prim) if prim else None}}
        if method == "material":
            return {"ok": True, "result": {"prim": asdict(self._bridge.set_material(params["path"], params["material_path"]))}}
        raise ValueError(f"Unsupported live scene method: {method}")


class _Tier2Server(_BasicServer):
    """An extended bridge from before tier3-1: capabilities without the edit/undo methods, which it rejects."""

    def _dispatch(self, payload):
        if str(payload.get("method", "")).strip().lower() == "capabilities":
            old = [name for name in LIVE_RPC_METHODS if name not in EDIT_RPC_METHODS + UNDO_RPC_METHODS]
            return {"ok": True, "result": {
                "ok": True, "bridge": "LiveKitSceneBridge", "version": "tier2-1",
                "methods": list(BASIC_RPC_METHODS) + old, "insight_queries": ["stage_info"],
                "authoring": {"available": True}, "stage_open": True, "dry_run_default": True,
            }}
        return super()._dispatch(payload)


class _FakeSelection:
    def __init__(self) -> None:
        self.paths = []

    def get_selected_prim_paths(self):
        return list(self.paths)

    def set_selected_prim_paths(self, paths, _expand):
        self.paths = list(paths)


@contextlib.contextmanager
def _live_kit_bridge(stage):
    """A real LiveKitSceneBridge on a temporary stage; only Kit's omni.usd context is faked (always restored)."""
    selection = _FakeSelection()
    context = types.SimpleNamespace(get_stage=lambda: stage, get_selection=lambda: selection)
    previous = kit_bridge.omni
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        yield kit_bridge.LiveKitSceneBridge(), selection
    finally:
        kit_bridge.omni = previous


def _layers_text(stage):
    """Every layer of the stage, session layer included, as text: a dry run must leave all of them identical."""
    return tuple(layer.ExportToString() for layer in stage.GetLayerStack(includeSessionLayers=True))


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _docs_install(root: Path) -> Path:
    """A tiny Isaac Sim install: one sensor extension, one deprecated one and omni.ui, each with docs."""
    install = root / "isaacsim-6.1.0"
    _write(install, "VERSION", "6.1.0-rc.26\n")
    lidar = "exts/isaacsim.sensors.demo_lidar"
    _write(install, f"{lidar}/config/extension.toml", (
        '[package]\ntitle = "Demo Lidar"\nversion = "2.4.0"\ndescription = "Rotating lidar sensors for robots."\n'
        'keywords = ["isaac", "lidar"]\nchangelog = "docs/CHANGELOG.md"\nreadme = "docs/Overview.md"\n\n'
        '[dependencies]\n"omni.kit.uiapp" = {}\n\n[[python.module]]\nname = "isaacsim.sensors.demo_lidar"\n'))
    _write(install, f"{lidar}/docs/Overview.md", (
        "# Overview\n\n## Rotating lidar\n\nCreate a rotating lidar with the zzspinner sensor config and attach it "
        "to a robot base link.\n"))
    _write(install, f"{lidar}/docs/CHANGELOG.md", "# Changelog\n\n## [2.4.0] - 2026-05-01\n### Fixed\n- Fixed beam count.\n")
    old = "extsDeprecated/isaacsim.core.oldapi"
    _write(install, f"{old}/config/extension.toml", (
        '[package]\ntitle = "Old Core"\nversion = "5.3.2"\ndescription = "Old core APIs."\n\n'
        '[deprecation]\nwarning = "Deprecated since Isaac Sim 6.0.0 in favor of isaacsim.core.experimental.*"\n'))
    _write(install, f"{old}/docs/Overview.md", "# Overview\n\nThe old ArticulationController drives joints.\n")
    ui = "extscache/omni.ui-3.3.1+00c488ae.wx64.r.cp312"
    _write(install, f"{ui}/config/extension.toml", (
        '[package]\ntitle = "UI Framework"\nversion = "3.3.1"\ndescription = "The Omniverse UI Framework"\n\n'
        '[[python.module]]\nname = "omni.ui"\n'))
    _write(install, f"{ui}/docs/Overview.md", "# Overview\n\nA VStack lays out child widgets in a zzcolumn.\n")
    return install


def _tree_state(root: Path):
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _refused_probe(log):
    async def probe(host, port, timeout):
        log.append((host, port, timeout))
        return "connection refused (simulated)"

    return probe


# --------------------------------------------------------------------------- registration and dry-run defaults


def test_new_tools_are_registered_with_dry_run_defaults_and_honest_descriptions():
    with _server() as call:
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert NEW_TOOLS <= set(tools) and len(tools) >= 82
        for name in (*EDIT_TOOLS, "scene_undo_last"):
            properties = tools[name].parameters["properties"]
            assert properties["dry_run"]["default"] is True, name
            description = " ".join(tools[name].description.split())
            assert description.startswith("DRY RUN BY DEFAULT"), name
            assert "changes the user's open scene" in description and "preview" in description, name
            assert "\n " not in tools[name].description, name
        assert tools["scene_create_material"].parameters["properties"]["create_parent"]["default"] is True
        for name in NEW_TOOLS - {*EDIT_TOOLS, "scene_undo_last"}:
            assert "dry_run" not in tools[name].parameters["properties"], name
            assert tools[name].description.strip(), name
        assert "saving is not undoable" in tools["scene_undo_last"].description.lower()
        for name in ("scene_create_light", "scene_create_camera", "scene_aim"):
            assert "not on Kit's undo stack" not in tools[name].description, name  # they are undoable now
        assert tools["router_set_scene_target"].parameters["properties"]["force"]["default"] is False
        assert tools["docs_search"].parameters["properties"]["domain"]["default"] == "isaac_sim"


def test_every_layer_defaults_to_a_dry_run():
    layers = {
        SceneMCPService: ("set_attribute", "create_material", "bind_material", "undo_last"),
        FakeSceneBridge: ("set_attribute", "create_material", "bind_material", "undo_last"),
        HttpSceneBridge: ("set_attribute", "create_material", "bind_material", "undo_last"),
        LiveKitSceneBridge: ("set_attribute", "create_material", "bind_material", "undo_last"),
    }
    for owner, methods in layers.items():
        for method in methods:
            default = inspect.signature(getattr(owner, method)).parameters["dry_run"].default
            assert default is True, (owner.__name__, method)
    for function in (edits.set_attribute, edits.create_material, edits.bind_material, kit_commands.undo_last,
                     UndoStack.undo_last):
        assert inspect.signature(function).parameters["dry_run"].default is True, function.__qualname__
    assert inspect.signature(edits.create_material).parameters["create_parent"].default is False

    class Recorder:
        def __init__(self):
            self.calls = []

        def set_attribute(self, path, name, value, **options):
            self.calls.append(options["dry_run"])
            return {"ok": True, "dry_run": options["dry_run"]}

        create_material = lambda self, path, **options: self._note(options)  # noqa: E731
        bind_material = lambda self, prim, material, **options: self._note(options)  # noqa: E731

        def undo_last(self, dry_run=True):
            self.calls.append(dry_run)
            return {"ok": True, "dry_run": dry_run}

        def _note(self, options):
            self.calls.append(options["dry_run"])
            return {"ok": True, "dry_run": options["dry_run"]}

    recorder = Recorder()
    service = SceneMCPService(recorder)
    for value in (None, 0, [], "no", True):
        assert service.set_attribute("/World/A", "size", 1, dry_run=value).dry_run is True, value
        assert service.create_material("/World/Looks/M", dry_run=value).dry_run is True, value
        assert service.bind_material("/World/A", "/World/Looks/M", dry_run=value).dry_run is True, value
        assert service.undo_last(dry_run=value)["dry_run"] is True, value
    assert recorder.calls and all(flag is True for flag in recorder.calls), recorder.calls
    assert service.set_attribute("/World/A", "size", 1, dry_run=False).dry_run is False
    assert recorder.calls[-1] is False
    # The HTTP client sends dry_run=true unless it was given exactly False.
    client = HttpSceneBridge(ISAAC)
    server = kit_rpc.make_extended_server_class(_BasicServer)(Recorder())
    with _in_process_transport(server) as sent:
        for value in (None, 0, [], "false"):
            client.set_attribute("/World/A", "size", 1, dry_run=value)
            client.create_material("/World/Looks/M", dry_run=value)
            client.bind_material("/World/A", "/World/Looks/M", dry_run=value)
            client.undo_last(dry_run=value)
    assert {call["params"]["dry_run"] for call in sent} == {True}


# --------------------------------------------------------------------------- fake scene, end to end


def test_fake_scene_edits_preview_apply_and_undo_through_the_server_tools():
    bridge = FakeSceneBridge()
    with _server(scene=lambda: SceneMCPService(bridge)) as call:
        original = bridge.snapshot()
        created = call("scene_create", path="/World/Key", type_name="RectLight")
        assert created["ok"] is True and created["dry_run"] is False
        lit = bridge.snapshot()

        preview = call("scene_set_attribute", path="/World/Key", name="inputs:intensity", value=5000)
        assert preview["ok"] is True and preview["dry_run"] is True and preview["applied"] is False
        assert preview["preview"] is True and "only after they agree" in preview["message"]
        assert preview["payload"]["would_change"] is True and preview["payload"]["fake_scene"] is True
        assert bridge.get_attribute("/World/Key", "inputs:intensity") is None and bridge.snapshot() == lit
        applied = call("scene_set_attribute", path="/World/Key", name="inputs:intensity", value=5000, dry_run=False)
        assert applied["applied"] is True and "scene_undo_last" in applied["message"]
        assert bridge.get_attribute("/World/Key", "inputs:intensity") == 5000

        material = call("scene_create_material", path="/World/Looks/Red", diffuse_color=[255, 0, 0], roughness=0.4)
        assert material["preview"] is True and material["payload"]["would_create"]["creates_parent"] == {
            "path": "/World/Looks", "type": "Scope"}
        assert material["payload"]["would_create"]["inputs"]["diffuseColor"]["value"] == [1.0, 0.0, 0.0]
        assert bridge.get_prim("/World/Looks") is None
        refused = call("scene_create_material", path="/World/Looks/Red", create_parent=False)
        assert refused["ok"] is False and "Parent prim does not exist" in refused["message"]
        made = call("scene_create_material", path="/World/Looks/Red", diffuse_color="#ff0000", dry_run=False)
        assert made["applied"] is True and bridge.get_prim("/World/Looks/Red").type_name == "Material"
        assert bridge.get_prim("/World/Looks/Red/PreviewSurface").type_name == "Shader"

        bind_preview = call("scene_bind_material", prim_path="/World/Props/Cube", material_path="/World/Looks/Red")
        assert bind_preview["preview"] is True and bridge.get_prim("/World/Props/Cube").material is None
        not_material = call("scene_bind_material", prim_path="/World/Props/Cube", material_path="/World/Props",
                            dry_run=False)
        assert not_material["ok"] is False and "not a Material" in not_material["message"]
        bound = call("scene_bind_material", prim_path="/World/Props/Cube", material_path="/World/Looks/Red",
                     dry_run=False)
        assert bound["applied"] is True and bridge.get_prim("/World/Props/Cube").material == "/World/Looks/Red"

        history = call("scene_undo_history")
        assert history["ok"] is True and history["count"] == 4 and history["fake_scene"] is True
        assert [entry["label"] for entry in history["entries"]] == [
            "Bind /World/Looks/Red to /World/Props/Cube",
            "Create material /World/Looks/Red",
            "Set inputs:intensity on /World/Key",
            "Create RectLight /World/Key",
        ]
        before_undo = bridge.snapshot()
        undo_preview = call("scene_undo_last")
        assert undo_preview["ok"] is True and undo_preview["dry_run"] is True and undo_preview["applied"] is False
        assert undo_preview["preview"] is True and undo_preview["would_undo"]["label"].startswith("Bind")
        assert "not undoable" in undo_preview["note"] and bridge.snapshot() == before_undo
        for _ in range(4):
            undone = call("scene_undo_last", dry_run=False)
            assert undone["ok"] is True and undone["applied"] is True and "not undoable" in undone["note"]
        assert bridge.snapshot() == original and bridge.get_attribute("/World/Key", "inputs:intensity") is None
        nothing = call("scene_undo_last", dry_run=False)
        assert nothing["ok"] is False and nothing["applied"] is False and "Nothing to undo" in nothing["message"]

        capabilities = call("scene_capabilities")
        assert capabilities["needs_bridge_update"] is False and capabilities["live_insight"]["available"] is False
        assert capabilities["scene_edits"]["available"] is True and capabilities["scene_edits"]["in_memory"] is True
        assert capabilities["undo"]["available"] is True and "fake scene" in capabilities["message"]


def test_router_dispatch_reaches_the_new_service_methods_on_the_fake_scene():
    bridge = FakeSceneBridge()
    with _server(scene=lambda: SceneMCPService(bridge)) as call:
        dispatch = call.dispatch
        call("scene_create", path="/World/Key", type_name="RectLight")
        set_light = dispatch("set intensity of /World/Key to 5000")
        assert set_light["ok"] is True and set_light["applied"] is True and set_light["attribute_name"] == "inputs:intensity"
        assert bridge.get_attribute("/World/Key", "inputs:intensity") == 5000.0
        previewed = dispatch("set exposure of /World/Key to 2", dry_run=True)
        assert previewed["applied"] is False and bridge.get_attribute("/World/Key", "inputs:exposure") is None
        assert dispatch("set intensity of /World/Props/Cube to 5")["ok"] is False  # a light setting on a cube

        before_paint = bridge.snapshot()
        paint = dispatch("paint /World/Props/Cube red")
        assert paint["ok"] is True and paint["applied"] is False and paint["preview"] is True
        assert bridge.snapshot() == before_paint
        assert [step["tool"] for step in paint["steps"]] == ["scene_create_material", "scene_bind_material"]
        for step in paint["steps"]:
            assert call(step["tool"], **step["arguments"])["applied"] is True, step
        assert bridge.get_prim("/World/Props/Cube").material == "/World/Looks/RedMaterial"

        changes = dispatch("what did you change")
        assert changes["ok"] is True and changes["action"] == "undo_history" and changes["count"] == 4
        undo = dispatch("undo")
        assert undo["ok"] is True and undo["applied"] is False and undo["command"] == "confirm undo"
        assert bridge.get_prim("/World/Props/Cube").material == "/World/Looks/RedMaterial"
        confirmed = dispatch("confirm undo")
        assert confirmed["ok"] is True and confirmed["applied"] is True
        assert bridge.get_prim("/World/Props/Cube").material is None
        rebound = dispatch("bind /World/Looks/RedMaterial to /World/Props/Cube")
        assert rebound["ok"] is True and rebound["payload"]["action"] == "bind_material"
        assert bridge.get_prim("/World/Props/Cube").material == "/World/Looks/RedMaterial"
        missing = dispatch("bind /World/Looks/Nope to /World/Props/Cube")
        assert missing["ok"] is False and "Material prim does not exist" in missing["message"]
        assert "Nothing was changed" in missing["message"]
        taken = dispatch("create material /World/Looks/RedMaterial roughness 0.5")  # refused while parsing
        assert taken["ok"] is False and "already exists" in taken["message"]
        deep = dispatch("create material /World/Nope/Deep/Mat roughness 0.5")  # refused by the service's preview
        assert deep["ok"] is False and "Parent prim does not exist" in deep["message"]
        assert "Nothing was created" in deep["message"] and bridge.get_prim("/World/Nope") is None

        blue = dispatch("make a blue material")
        assert blue["ok"] is True and blue["applied"] is False and blue["apply_with"]["tool"] == "scene_create_material"
        assert blue["payload"]["would_create"]["material"] == "/World/Looks/BlueMaterial"
        assert bridge.get_prim("/World/Looks/BlueMaterial") is None
        guide = dispatch("help")
        assert guide["ok"] is True and guide["style"] == "help" and len(guide["groups"]) == 12


# --------------------------------------------------------------------------- live bridge over the extended RPC


def test_live_bridge_edits_and_undo_end_to_end_with_byte_identical_dry_runs():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Cube").CreateSizeAttr(1.0)
    original = _layers_text(stage)
    with _live_kit_bridge(stage) as (bridge, selection):
        rpc = kit_rpc.make_extended_server_class(_BasicServer)(bridge)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _in_process_transport(rpc) as sent:
            capabilities = call("scene_capabilities")
            assert capabilities["rpc_version"] == RPC_VERSION == "tier5-1" and capabilities["needs_bridge_update"] is False
            assert capabilities["scene_edits"]["available"] is True and capabilities["undo"]["available"] is True
            assert "how_to_enable" not in capabilities

            previews = [
                call("scene_set_attribute", path="/World/Cube", name="size", value=2),
                call("scene_set_attribute", path="/World/Cube", name="primvars:displayColor", value=[[1, 0, 0]]),
                call("scene_create_material", path="/World/Looks/Steel", diffuse_color=[0.8, 0.8, 0.8], metallic=1),
                call("scene_undo_last"),
            ]
            assert _layers_text(stage) == original, "a dry run changed a layer"
            assert all(item["ok"] and item["dry_run"] is True and item["applied"] is False for item in previews[:3])
            assert previews[0]["payload"]["before"]["value"] == 1.0 and previews[0]["payload"]["after"] == 2.0
            assert previews[2]["payload"]["would_create"]["creates_parent"]["path"] == "/World/Looks"
            assert previews[3]["ok"] is False  # nothing recorded yet

            sized = call("scene_set_attribute", path="/World/Cube", name="size", value=2, dry_run=False)
            routed = call.dispatch("set size of /World/Cube to 3")
            made = call("scene_create_material", path="/World/Looks/Steel", diffuse_color=[0.8, 0.8, 0.8], metallic=1,
                        dry_run=False)
            bound = call("scene_bind_material", prim_path="/World/Cube", material_path="/World/Looks/Steel",
                         strength="strongerThanDescendants", dry_run=False)
            for result in (sized, made, bound):
                assert result["ok"] and result["applied"] is True and result["undoable"] is True, result
                assert "scene_undo_last" in result["message"]
            assert routed["ok"] is True and routed["applied"] is True and routed["attribute_name"] == "size"
            cube = stage.GetPrimAtPath("/World/Cube")
            assert cube.GetAttribute("size").Get() == 3.0
            assert str(UsdShade.MaterialBindingAPI(cube).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Steel"
            assert selection.paths == []

            history = call("scene_undo_history", limit=10)
            assert history["count"] == 4 and history["entries"][0]["label"] == "Bind /World/Looks/Steel to /World/Cube"
            routed_undo = call.dispatch("undo")
            assert routed_undo["applied"] is False and routed_undo["would_undo"]["label"].startswith("Bind")
            for _ in range(4):
                undone = call("scene_undo_last", dry_run=False)
                assert undone["ok"] is True and undone["verified"] is True and undone["applied"] is True, undone
            assert _layers_text(stage) == original
            assert call("scene_undo_last", dry_run=False)["ok"] is False
    methods = {item["method"] for item in sent}
    assert {"capabilities", "set_attribute", "create_material", "bind_material", "undo_last", "undo_history"} <= methods
    assert all(item["url"] == f"{ISAAC}/rpc" for item in sent)


def test_old_bridges_report_needs_bridge_update_and_change_nothing():
    bridge = FakeSceneBridge()
    for endpoint, hint in ((ISAAC, "reload_mcp_bridge.py"), (COMPOSER, "reload_composer_bridge.py")):
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(endpoint))) as call:
            with _in_process_transport(_BasicServer(bridge)) as sent:
                before = bridge.snapshot()
                for name, arguments in (
                    ("scene_set_attribute", {"path": "/World/Props/Cube", "name": "size", "value": 2, "dry_run": False}),
                    ("scene_create_material", {"path": "/World/Looks/M", "dry_run": False}),
                    ("scene_bind_material", {"prim_path": "/World/Props/Cube", "material_path": "/World/Looks/M",
                                             "purpose": "preview", "dry_run": False}),
                ):
                    result = call(name, **arguments)
                    assert result["ok"] is False and result["applied"] is False, name
                    assert result["needs_bridge_update"] is True and result["how_to_enable"], name
                    assert hint in result["message"], (name, result["message"])
                for name in ("scene_undo_last", "scene_undo_history"):
                    result = call(name)
                    assert result["ok"] is False and result["needs_bridge_update"] is True, name
                    assert result["how_to_enable"] and "not undoable" in result["note"], name
                assert bridge.snapshot() == before
                # A plain binding still works there, through the basic 'material' method.
                preview = call("scene_bind_material", prim_path="/World/Props/Cube", material_path="/World/Looks/M")
                assert preview["ok"] is True and preview["dry_run"] is True and preview["fallback"] == "set_material"
                assert bridge.get_prim("/World/Props/Cube").material is None
                applied = call("scene_bind_material", prim_path="/World/Props/Cube", material_path="/World/Looks/M",
                               dry_run=False)
                assert applied["ok"] is True and applied["applied"] is True and applied["fallback"] == "set_material"
                assert bridge.get_prim("/World/Props/Cube").material == "/World/Looks/M"
                bridge.set_material("/World/Props/Cube", None)
            assert "material" in {item["method"] for item in sent}

    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call:
        with _in_process_transport(_Tier2Server(bridge)):
            capabilities = call("scene_capabilities")
    assert capabilities["needs_bridge_update"] is True and capabilities["how_to_enable"]
    assert set(EDIT_RPC_METHODS) | set(UNDO_RPC_METHODS) <= set(capabilities["missing_live_methods"])
    assert capabilities["scene_edits"]["available"] is False and "predates" in capabilities["scene_edits"]["reason"]
    assert capabilities["undo"]["available"] is False and capabilities["rpc_version"] == "tier2-1"
    assert capabilities["expected_rpc_version"] == RPC_VERSION and "set_attribute" in capabilities["message"]


# --------------------------------------------------------------------------- scene targets


def test_scene_target_switching_uses_injected_probes_and_never_opens_a_socket():
    with _guard():
        service = SceneMCPService(FakeSceneBridge())
        down = runtime.switch_scene_target(service, "Isaac Sim", probe=lambda endpoint, timeout: False)
        assert down["ok"] is False and down["changed"] is False and "force=True" in down["error"]
        assert service.bridge_info()["bridge"] == "FakeSceneBridge"
        up = runtime.switch_scene_target(service, "isaac", probe=lambda e, t: {"reachable": True, "status": "ok"})
        assert up["ok"] is True and up["changed"] is True and up["current"]["endpoint"] == ISAAC
        assert up["current"]["mode"] == "http_switch" and "capabilities" not in up  # test mode: no RPC
        preview = runtime.switch_scene_target(service, "composer", dry_run=True, probe=lambda e, t: True)
        assert preview["ok"] is True and preview["would_change"] is True and service.bridge_info()["endpoint"] == ISAAC
        back = runtime.switch_scene_target(service, "fake")
        assert back["ok"] is True and back["current"]["bridge"] == "FakeSceneBridge"
        assert back["capabilities"]["fake_scene"] is True

    if Usd is None:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/Cube").CreateSizeAttr(1.0)
    original = _layers_text(stage)
    with _live_kit_bridge(stage) as (bridge, _selection), _server() as call:
        targets = call("router_scene_targets")
        assert targets["ok"] is True and targets["active"] == "fake"
        assert {item["name"]: item["endpoint"] for item in targets["targets"]} == {
            "isaac": ISAAC, "composer": COMPOSER, "fake": None}
        refused = call("router_set_scene_target", target="composer")
        assert refused["ok"] is False and "not probed" in refused["message"] and call("router_scene_targets")["active"] == "fake"
        unknown = call("router_set_scene_target", target="blender")
        assert unknown["ok"] is False and "Unknown scene target" in unknown["message"]

        forced = call("router_set_scene_target", target="USD Composer", force=True)
        assert forced["ok"] is True and forced["changed"] is True and forced["current"]["endpoint"] == COMPOSER
        assert "Scene tools now talk to" in forced["message"] and forced["warnings"]
        assert call("router_scene_targets")["active"] == "composer"
        rpc = kit_rpc.make_extended_server_class(_BasicServer)(bridge)
        with _in_process_transport(rpc) as sent:
            capabilities = call("scene_capabilities")
            assert capabilities["backend"]["mode"] == "http_switch" and capabilities["backend"]["target"] == "composer"
            assert capabilities["rpc_version"] == RPC_VERSION and capabilities["scene_edits"]["available"] is True
            preview = call("scene_set_attribute", path="/World/Cube", name="size", value=5)
            assert preview["ok"] is True and preview["dry_run"] is True and _layers_text(stage) == original
        assert sent and all(item["url"] == f"{COMPOSER}/rpc" for item in sent)
        assert call.router._scene.bridge_info()["endpoint"] == COMPOSER  # every tool follows the switch, no restart
        home = call("router_set_scene_target", target="fake")
        assert home["ok"] is True and call("router_scene_targets")["active"] == "fake"
        assert call("scene_summary")["stage_name"] == "FakeStage"


# --------------------------------------------------------------------------- help


def test_help_catalog_covers_every_registered_tool_and_router_help_answers():
    with _server() as call:
        registered = {tool.name for tool in call.app._tool_manager.list_tools()}
        catalog = tool_guide.tool_names()
        assert set(catalog) == registered and len(catalog) == len(set(catalog))
        assert tool_guide.PLANNED_TOOLS == frozenset()
        by_name = {entry["name"]: entry for entries in tool_guide.TOOL_CATALOG.values() for entry in entries}
        for name in (*EDIT_TOOLS, "scene_undo_last"):
            assert by_name[name]["mutates"] is True and by_name[name]["needs_bridge"] is True, name
        for name in ("scene_undo_history", "router_help", "router_scene_targets", "router_set_scene_target",
                     "docs_search", "extension_info"):
            assert by_name[name]["mutates"] is False, name
        guide = call("router_help")
        assert guide["ok"] is True and len(guide["groups"]) == 12 and len(guide["quick_start"]) == 8
        assert not any(tool.get("planned") for group in guide["groups"] for tool in group["tools"])
        undo = call("router_help", topic="undo")
        assert undo["match"] == "group" and {tool["name"] for tool in undo["tools"]} == {"scene_undo_last", "scene_undo_history"}
        docs = call("router_help", topic="docs_search")
        assert docs["match"] == "tool" and docs["group"] == "Knowledge & docs" and "extension_info" in docs["see_also"]
        assert "undo" in guide["safety"].lower()


# --------------------------------------------------------------------------- offline Kit / Isaac Sim docs


def test_docs_tools_and_knowledge_fallback_answer_offline_from_a_temp_install():
    with tempfile.TemporaryDirectory() as tmp:
        install = _docs_install(Path(tmp))
        before = _tree_state(install)
        with _server(docs_root=install) as call:
            hits = call("docs_search", query="rotating lidar zzspinner", top_k=3)
            assert hits["ok"] is True and hits["domain"] == "isaac_sim" and hits["isaac_sim_version"] == "6.1.0"
            assert hits["results"][0]["extension"] == "isaacsim.sensors.demo_lidar"
            assert ":" in hits["results"][0]["source_path"] and "docs MCPs are not needed" in hits["note"]
            ui = call("docs_search", query="zzcolumn widgets", domain="omni.ui")
            assert ui["ok"] is True and ui["domain"] == "omni_ui" and ui["results"][0]["extension"] == "omni.ui"
            assert call("docs_search", query="zzqx nothing", domain="kit")["ok"] is False
            bad = call("docs_search", query="lidar", domain="blender")
            assert bad["ok"] is False and "Unknown docs domain" in bad["message"]

            info = call("extension_info", name="isaacsim.sensors.demo_lidar")
            assert info["ok"] is True and info["version"] == "2.4.0" and info["domain"] == "isaac_sim"
            assert "2.4.0" in info["changelog_latest"]["heading"] and "beam count" in info["changelog_latest"]["text"]
            old = call("extension_info", name="isaacsim.core.oldapi")
            assert old["ok"] is True and old["deprecated"] is True and "6.0.0" in old["deprecation_warning"]
            missing = call("extension_info", name="omni.isaac.demo_lidar")
            assert missing["ok"] is False and missing["found"] is False and "renamed" in missing["message"]
            assert call("extension_info", name=" ")["ok"] is False
            # Test mode: the router's Kit / Isaac Sim / OmniUI backends never open a socket or read the install.
            assert isinstance(call.router._backends[RouteTarget.ISAAC_SIM], NullKnowledgeBackend)

        endpoints = {
            "OMNIVERSE_KIT_MCP_ENDPOINT": "http://127.0.0.1:9902/mcp",
            "OMNIVERSE_OMNI_UI_MCP_ENDPOINT": "http://127.0.0.1:9901/mcp",
            "OMNIVERSE_ISAAC_SIM_MCP_ENDPOINT": "http://127.0.0.1:9904/mcp",
        }
        with _env(**endpoints), _guard() as loop:
            probes = []
            shared = runtime.build_docs_knowledge(roots=install)
            assert set(shared) == {"kit", "isaac_sim", "omni_ui"}
            backend = runtime.build_docs_backend_with_fallback("isaac", shared["isaac_sim"], probe=_refused_probe(probes))
            assert isinstance(backend, FallbackKnowledgeBackend) and backend.name == "isaac_sim"
            assert isinstance(backend.offline, OfflineDocsKnowledgeBackend) and backend.offline.knowledge is shared["isaac_sim"]
            answer = loop.run_until_complete(backend.ask("how do I make a rotating lidar zzspinner?"))
            assert answer["ok"] is True and answer["endpoint"] == "offline" and answer["source"] == "offline_docs"
            assert "simulated" in answer["primary_error"] and "isaac_sim docs MCP is not running" in answer["note"]
            assert probes and all(host in ("127.0.0.1", "::1") and port == 9904 for host, port, _ in probes)

            router = runtime.build_router({}, detect_endpoint=lambda: None, docs_knowledge=shared)
            for target, port in ((RouteTarget.KIT, 9902), (RouteTarget.OMNI_UI, 9901), (RouteTarget.ISAAC_SIM, 9904)):
                wired = router._backends[target]
                assert isinstance(wired, FallbackKnowledgeBackend), target
                assert wired.offline.knowledge is shared[target.value] and wired.endpoint.endswith(f":{port}/mcp")
                wired._probe = _refused_probe(probes)  # never touch a real port
            routed = loop.run_until_complete(router.handle("how do I attach the demo lidar sensor in isaac sim?"))
            assert routed.decision.target is RouteTarget.ISAAC_SIM
            assert routed.payload["ok"] is True and routed.payload["endpoint"] == "offline"
            assert routed.payload["tool"] in {"search", "extension_info"}
            test_mode = runtime.build_router({"OMNIVERSE_SCENE_MCP_TEST": "1"}, docs_knowledge=shared)
            assert all(isinstance(test_mode._backends[t], NullKnowledgeBackend)
                       for t in (RouteTarget.KIT, RouteTarget.OMNI_UI, RouteTarget.ISAAC_SIM))
        offline_docs_knowledge.clear_cache()
        assert _tree_state(install) == before, "the docs tools must only read the install"


def test_new_rpc_methods_line_up_across_the_protocol_the_rpc_and_the_bridges():
    assert RPC_VERSION == "tier5-1"
    new = set(EDIT_RPC_METHODS) | set(UNDO_RPC_METHODS)
    assert new <= set(LIVE_RPC_METHODS) and new <= set(kit_rpc._HANDLERS)
    for owner in (FakeSceneBridge, HttpSceneBridge, LiveKitSceneBridge, SceneMCPService):
        missing = [name for name in new if not callable(getattr(owner, name, None))]
        assert not missing, (owner.__name__, missing)
    fake = FakeSceneBridge().capabilities()
    assert new <= set(fake["methods"]) and fake["edits"]["available"] and fake["undo"]["available"]
    json.dumps(fake)
