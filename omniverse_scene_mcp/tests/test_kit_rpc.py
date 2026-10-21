"""Extended live RPC (kit_rpc) on a fake base server, and live insight on temporary pxr stages with omni.usd faked.

Fixture-free so isaacsim-study/reload_mcp_bridge.py can run it inside Isaac Sim before swapping the bridge. No HTTP
server is started and the open stage is never touched: every stage here is temporary.
"""

import contextlib
import importlib.util
import json
import math
import re
import tempfile
import types
from pathlib import Path

from omniverse_scene_mcp import kit_bridge, kit_rpc
from omniverse_scene_mcp.bridge import BASIC_RPC_METHODS, INSIGHT_QUERIES, LIVE_RPC_METHODS, RPC_VERSION

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - the insight tests below then return early
    Sdf = Usd = UsdGeom = None

_LIVE_SCENE_CONTROL = (
    Path(__file__).resolve().parents[2]
    / "kit-app-template"
    / "source"
    / "extensions"
    / "my_company.my_usd_composer_setup_extension"
    / "my_company"
    / "my_usd_composer_setup_extension"
    / "live_scene_control.py"
)


class _FakeBaseServer:
    """Stands in for live_scene_control.LiveSceneControlServer: same _dispatch contract, no HTTP and no Kit."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self.base_calls: list[str] = []

    def _dispatch(self, payload):
        method = str(payload.get("method", "")).strip().lower()
        self.base_calls.append(method)
        if method == "ping":
            return {"ok": True, "result": {"status": self._bridge.ping()}}
        raise ValueError(f"Unsupported live scene method: {method}")


class _RecordingBridge:
    """Records what the extended server forwards; never touches a stage."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ping(self) -> str:
        return "ok"

    def capabilities(self) -> dict:
        return {"bridge": "RecordingBridge", "stage_open": True}

    def insight(self, query, args=None):
        self.calls.append(("insight", query, args))
        return {"ok": True, "mode": "live", "query": query}

    def create_light(self, path, light_type, **options):
        self.calls.append(("create_light", path, light_type, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def create_camera(self, path, **options):
        self.calls.append(("create_camera", path, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def aim(self, path, target, **options):
        self.calls.append(("aim", path, target, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}


class _PingOnlyBridge:
    def ping(self) -> str:
        return "ok"


class _FakeSelection:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def get_selected_prim_paths(self) -> list[str]:
        return list(self.paths)

    def set_selected_prim_paths(self, paths, _expand) -> None:
        self.paths = list(paths)


class _FakeContext:
    def __init__(self, stage) -> None:
        self._stage = stage
        self.selection = _FakeSelection()

    def get_stage(self):
        return self._stage

    def get_selection(self):
        return self.selection


@contextlib.contextmanager
def _live_bridge(stage):
    """LiveKitSceneBridge on a temporary stage; only Kit's omni.usd context is faked (and always restored)."""
    previous = kit_bridge.omni
    context = _FakeContext(stage)
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        yield kit_bridge.LiveKitSceneBridge(), context
    finally:
        kit_bridge.omni = previous


def _server(bridge):
    return kit_rpc.make_extended_server_class(_FakeBaseServer)(bridge)


def _call(server, method, **params):
    return server._dispatch({"method": method, "params": params})


def _raises(action, *fragments, error=ValueError):
    try:
        action()
    except error as exc:
        message = str(exc)
        missing = [fragment for fragment in fragments if fragment not in message]
        assert not missing, f"{missing} not in {message!r}"
        return message
    raise AssertionError(f"expected {error.__name__} containing {fragments}")


def test_extended_class_subclasses_the_given_base():
    cls = kit_rpc.make_extended_server_class(_FakeBaseServer)
    assert issubclass(cls, _FakeBaseServer)
    assert cls.__name__ == "Extended_FakeBaseServer"
    assert cls.rpc_version == RPC_VERSION == "tier2-1"
    assert set(cls.rpc_methods) == set(BASIC_RPC_METHODS) | set(LIVE_RPC_METHODS)
    _raises(lambda: kit_rpc.make_extended_server_class(object()), "_dispatch", error=TypeError)
    _raises(lambda: kit_rpc.make_extended_server_class(int), "_dispatch", error=TypeError)


def test_capabilities_reports_version_methods_and_queries():
    server = _server(_RecordingBridge())
    response = _call(server, "capabilities")
    assert response["ok"] is True
    result = response["result"]
    assert result["version"] == "tier2-1"
    assert set(result["methods"]) == set(BASIC_RPC_METHODS) | set(LIVE_RPC_METHODS)
    assert result["insight_queries"] == list(INSIGHT_QUERIES)
    assert result["dry_run_default"] is True
    assert result["bridge"] == "RecordingBridge"  # merged from the bridge's own capabilities()
    assert server.base_calls == []
    json.dumps(response)
    _raises(lambda: _call(server, "capabilities", verbose=True), "Unknown parameter", "verbose")


def test_unknown_and_basic_methods_defer_to_the_base_dispatch():
    server = _server(_RecordingBridge())
    assert _call(server, "ping") == {"ok": True, "result": {"status": "ok"}}
    _raises(lambda: _call(server, "make_light"), "Unsupported live scene method: make_light")
    _raises(lambda: _call(server, "__init__"), "Unsupported live scene method")
    assert server.base_calls == ["ping", "make_light", "__init__"]
    # Method names are normalized like the base class does, then served by the extension.
    assert server._dispatch({"method": "  CAPABILITIES "})["result"]["version"] == RPC_VERSION
    _raises(lambda: server._dispatch(["capabilities"]), "JSON object")
    _raises(lambda: server._dispatch({"method": "insight", "params": ["stage_info"]}), "JSON object")
    assert server.base_calls == ["ping", "make_light", "__init__"]


def test_authoring_methods_forward_with_dry_run_true_by_default():
    bridge = _RecordingBridge()
    server = _server(bridge)

    light = _call(server, "create_light", path="/World/Key", light_type="DistantLight", intensity=500, color=[1, 0.9, 0.8])
    assert light == {"ok": True, "result": {"ok": True, "dry_run": True, "path": "/World/Key"}}
    _, path, light_type, options = bridge.calls[-1]
    assert (path, light_type) == ("/World/Key", "DistantLight")
    assert options["dry_run"] is True
    assert options["intensity"] == 500.0 and isinstance(options["intensity"], float)
    assert options["color"] == (1.0, 0.9, 0.8)
    assert options["exposure"] is None and options["aim_at"] is None

    _call(server, "create_camera", path="/World/Cam", focal_length=35, clipping_range=[0.1, 1000], aim_at="/World/Box", dry_run=False)
    _, path, options = bridge.calls[-1]
    assert path == "/World/Cam" and options["dry_run"] is False
    assert options["clipping_range"] == (0.1, 1000.0) and options["aim_at"] == "/World/Box"

    _call(server, "aim", path="/World/Cam", target=[0, 0, 1], up_axis="Z", dry_run="false")
    _, path, target, options = bridge.calls[-1]
    assert target == (0.0, 0.0, 1.0) and options == {"up_axis": "Z", "dry_run": False}
    _call(server, "aim", path="/World/Cam", target="/World/Box")
    assert bridge.calls[-1][2] == "/World/Box" and bridge.calls[-1][3]["dry_run"] is True


def test_authoring_parameters_are_checked_before_forwarding():
    bridge = _RecordingBridge()
    server = _server(bridge)
    bad = [
        ({"path": "/World/L", "light_type": "SphereLight", "dryrun": False}, "Unknown parameter"),
        ({"path": "/World/L", "light_type": "SphereLight", "dry_run": "maybe"}, "dry_run"),
        ({"path": "/World/L", "light_type": "SphereLight", "dry_run": 0}, "dry_run"),
        ({"path": "/World/L", "light_type": "SphereLight", "color": [1, 1]}, "color"),
        ({"path": "/World/L", "light_type": "SphereLight", "intensity": True}, "intensity"),
        ({"path": "/World/L", "light_type": "SphereLight", "intensity": math.nan}, "intensity"),
        ({"path": "/World/L", "light_type": "SphereLight", "intensity": "bright"}, "intensity"),
        ({"path": "", "light_type": "SphereLight"}, "path"),
        ({"path": "/World/L"}, "light_type"),
    ]
    for params, fragment in bad:
        _raises(lambda: _call(server, "create_light", **params), fragment)
    _raises(lambda: _call(server, "aim", path="/World/Cam"), "target")
    _raises(lambda: _call(server, "aim", path="/World/Cam", target=[1, 2]), "target")
    _raises(lambda: _call(server, "create_camera", path="/World/Cam", copy_optics_from=""), "copy_optics_from")
    assert bridge.calls == []


def test_bridge_without_live_methods_gets_needs_bridge_update_results():
    server = _server(_PingOnlyBridge())
    for method, params in (
        ("insight", {"query": "stage_info"}),
        ("create_light", {"path": "/World/L", "light_type": "RectLight"}),
        ("create_camera", {"path": "/World/C"}),
        ("aim", {"path": "/World/C", "target": "/World"}),
    ):
        response = _call(server, method, **params)
        assert response["ok"] is True
        assert response["result"]["ok"] is False and response["result"]["needs_bridge_update"] is True, method
        assert response["result"]["method"] == method
    capabilities = _call(server, "capabilities")["result"]
    assert capabilities["version"] == RPC_VERSION and capabilities["bridge"] == "_PingOnlyBridge"


def test_insight_allowlist_rejects_unknown_queries_without_reaching_the_bridge():
    bridge = _RecordingBridge()
    server = _server(bridge)
    for query in ("snapshot", "save", "__class__", "_require_stage", "stage_info()", "export", "", None, 5, ["stage_info"]):
        _raises(lambda: _call(server, "insight", query=query), "Unknown insight query")
    _raises(lambda: _call(server, "insight", query="stage_info", args=["/World"]), "args must be a JSON object")
    _raises(lambda: _call(server, "insight", query="stage_info", extra=1), "Unknown parameter")
    assert bridge.calls == []
    assert _call(server, "insight", query=" Stage_Info ")["result"]["query"] == "stage_info"
    assert bridge.calls == [("insight", "stage_info", {})]


def test_insight_runner_table_matches_the_published_queries():
    assert tuple(kit_bridge._INSIGHT_RUNNERS) == INSIGHT_QUERIES
    assert tuple(kit_bridge._INSIGHT_ARGS) == INSIGHT_QUERIES
    assert all(callable(runner) for runner in kit_bridge._INSIGHT_RUNNERS.values())
    every_arg = {name for names in kit_bridge._INSIGHT_ARGS.values() for name in names}
    assert every_arg == set(kit_bridge._ARG_PARSERS)


def test_fit_json_trims_oversized_results_and_keeps_plain_json():
    small = kit_bridge._fit_json({"value": (1, 2), "path": Path("x")}, 10_000)
    assert small == {"value": [1, 2], "path": "x"}
    big = {"ok": True, "items": [{"name": f"attr_{index}", "text": "x" * 500} for index in range(5_000)]}
    fitted = kit_bridge._fit_json(big, 20_000)
    assert len(json.dumps(fitted)) <= 20_000
    assert fitted["ok"] is True and fitted["size_truncated"]["max_bytes"] == 20_000
    assert 0 < len(fitted["items"]) < 5_000
    _raises(lambda: kit_bridge._fit_json({"key_" + str(index): index for index in range(5_000)}, 1_000), "narrow the query")


def _layered_scene(folder: Path):
    """props.usda defines /World/Box (size 2); root.usda sublayers it, overrides size to 3, then an UNSAVED edit sets 4."""
    props = Usd.Stage.CreateNew(str(folder / "props.usda"))
    UsdGeom.Xform.Define(props, "/World")
    UsdGeom.Cube.Define(props, "/World/Box").CreateSizeAttr(2.0)
    props.GetRootLayer().Save()
    stage = Usd.Stage.CreateNew(str(folder / "root.usda"))
    stage.GetRootLayer().subLayerPaths.append("./props.usda")
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    UsdGeom.Cube(stage.GetPrimAtPath("/World/Box")).GetSizeAttr().Set(3.0)
    stage.GetRootLayer().Save()
    UsdGeom.Cube(stage.GetPrimAtPath("/World/Box")).GetSizeAttr().Set(4.0)  # live, unsaved
    return stage


def test_insight_runs_every_query_on_the_live_stage_read_only():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    with tempfile.TemporaryDirectory() as tmp:
        stage = _layered_scene(Path(tmp))
        root = stage.GetRootLayer()
        before = root.ExportToString()
        assert root.dirty
        with _live_bridge(stage) as (bridge, context):
            context.selection.paths = ["/World"]
            server = _server(bridge)
            results = {
                query: _call(server, "insight", query=query, args=args)["result"]
                for query, args in (
                    ("stage_info", {}),
                    ("composition", {"prim_path": "/World/Box"}),
                    ("explain_value", {"prim_path": "/World/Box", "attribute": "size"}),
                    ("properties", {"path": "/World/Box", "authored_only": True}),
                    ("bounds", {"prim_path": "/World/Box", "purposes": ["default"]}),
                    ("validate", {"profile": "exam", "max_issues": 10}),
                )
            }
            selection_after = context.selection.get_selected_prim_paths()

        for query, result in results.items():
            json.dumps(result)
            assert result["mode"] == "live" and result["query"] == query, query
            assert result["has_unsaved_changes"] is True, query
            assert [layer["identifier"] for layer in result["unsaved_layers"]] == [root.identifier], query
            assert "warnings" not in result or not any("Read-only violation" in w for w in result["warnings"]), query
        # The live answer includes the unsaved edit (4), not the saved 3 or the sublayer's 2.
        assert results["explain_value"]["value"] == 4.0
        assert results["explain_value"]["winning_opinion"]["layer"] == root.identifier
        assert results["stage_info"]["up_axis"] == "Z"
        assert any(entry["identifier"].endswith("props.usda") for entry in results["stage_info"]["layer_stack"])
        assert results["composition"]["prim_path"] == "/World/Box"
        assert {attr["name"] for attr in results["properties"]["attributes"]} >= {"size"}
        assert results["properties"]["args"] == {"prim_path": "/World/Box", "authored_only": True}
        assert results["bounds"]["world_bound"]["empty"] is False
        assert results["validate"]["profile"] == "exam" and "issues" in results["validate"]
        assert results["validate"]["args"]["max_prims"] == kit_bridge.LIVE_DEFAULT_MAX_PRIMS
        # Read-only: no new opinions, nothing saved, selection untouched.
        assert root.ExportToString() == before
        assert root.dirty
        saved = Path(tmp, "root.usda").read_text(encoding="utf-8")
        assert "size = 3" in saved and "size = 4" not in saved
        assert selection_after == ["/World"]


def test_insight_argument_allowlist_and_errors_on_the_live_stage():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/Box")
    before = stage.GetRootLayer().ExportToString()
    with _live_bridge(stage) as (bridge, _context):
        server = _server(bridge)
        insight = lambda query, **args: _call(server, "insight", query=query, args=args)  # noqa: E731
        _raises(lambda: insight("stage_info", prim_path="/World"), "Unknown argument", "stage_info")
        _raises(lambda: insight("composition", prim_path="/World/Box", __class__=1), "Unknown argument")
        _raises(lambda: insight("composition"), "needs prim_path")
        _raises(lambda: insight("composition", prim_path=7), "prim_path")
        _raises(lambda: insight("composition", prim_path="/World/Box", path="/World/Box"), "given twice")
        _raises(lambda: insight("composition", prim_path="/World/Missing"), "Prim does not exist")
        _raises(lambda: insight("explain_value", prim_path="/World/Box", time=True), "time")
        _raises(lambda: insight("bounds", prim_path="/World/Box", purposes=["everything"]), "purposes")
        _raises(lambda: insight("properties", prim_path="/World/Box", include_values="yes"), "include_values")
        _raises(lambda: insight("validate", profile="strict"), "profile")
        _raises(lambda: insight("validate", max_prims=2.5), "max_prims")
        capped = insight("validate", max_prims=10**9, max_issues=10**9)["result"]["args"]
        assert capped["max_prims"] == kit_bridge.LIVE_MAX_PRIMS and capped["max_issues"] == kit_bridge.LIVE_MAX_ISSUES
        property_path = insight("explain_value", prim_path="/World/Box.size")["result"]
        assert property_path["attribute"] == "size" and property_path["mode"] == "live"
    assert stage.GetRootLayer().ExportToString() == before

    with _live_bridge(None) as (bridge, _context):
        _raises(lambda: _call(_server(bridge), "insight", query="stage_info"), "No live USD stage")


def test_real_live_scene_control_server_when_running_inside_kit():
    """Outside Kit live_scene_control.py cannot import omni.kit.app, so this only runs in Isaac's self-test."""
    if not _LIVE_SCENE_CONTROL.is_file():
        return
    spec = importlib.util.spec_from_file_location("kit_rpc_self_test_scene_control", _LIVE_SCENE_CONTROL)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return
    cls = kit_rpc.make_extended_server_class(module.LiveSceneControlServer)
    assert issubclass(cls, module.LiveSceneControlServer)
    server = cls.__new__(cls)  # no __init__: no Kit context, no HTTP server, nothing started
    server._bridge = _RecordingBridge()
    assert _call(server, "ping") == {"ok": True, "result": {"status": "ok"}}
    assert _call(server, "capabilities")["result"]["version"] == RPC_VERSION
    _raises(lambda: _call(server, "not_a_method"), "Unsupported live scene method")


def test_basic_method_list_matches_live_scene_control_source():
    if not _LIVE_SCENE_CONTROL.is_file():
        return
    source = _LIVE_SCENE_CONTROL.read_text(encoding="utf-8")
    assert set(re.findall(r'if method == "([a-z_]+)"', source)) == set(BASIC_RPC_METHODS)
    assert not set(LIVE_RPC_METHODS) & set(BASIC_RPC_METHODS)


class _QueueingBaseServer(_FakeBaseServer):
    """Like live_scene_control: _submit only queues; Kit's pump runs _dispatch later (here: by hand)."""

    def __init__(self, bridge) -> None:
        super().__init__(bridge)
        self.queued: list[dict] = []

    def _submit(self, payload):
        self.queued.append(payload)
        return {"ok": False, "message": "Timed out waiting for live scene response"}


def test_writes_queued_too_long_are_dropped_not_run_late():
    """A write the caller stopped waiting for (reported as not applied) must not run on a later Kit update."""
    bridge = _RecordingBridge()
    server = kit_rpc.make_extended_server_class(_QueueingBaseServer)(bridge)
    server._submit({"method": "create_light", "params": {"path": "/World/Key", "light_type": "rect", "dry_run": False}})
    server._submit({"method": "ping", "params": {}})
    write, ping = server.queued
    assert isinstance(write["_queued_at"], float) and write["method"] == "create_light"
    late = dict(write, _queued_at=write["_queued_at"] - kit_rpc._STALE_AFTER_S - 1.0)
    expired = server._dispatch(late)["result"]
    assert expired["ok"] is False and expired["written"] is False and "nothing was written" in expired["error"]
    assert bridge.calls == []
    for method, params in (("create_camera", {"path": "/World/Cam"}), ("aim", {"path": "/World/Cam", "target": "/World"})):
        stale = {"method": method, "params": params, "_queued_at": write["_queued_at"] - 60.0}
        assert server._dispatch(stale)["result"]["written"] is False, method
    assert bridge.calls == []
    # Fresh writes, and reads of any age, still run.
    assert server._dispatch(write)["result"] == {"ok": True, "dry_run": False, "path": "/World/Key"}
    assert server._dispatch(dict(ping, _queued_at=ping["_queued_at"] - 60.0)) == {"ok": True, "result": {"status": "ok"}}
    stale_read = {"method": "capabilities", "params": {}, "_queued_at": ping["_queued_at"] - 60.0}
    assert server._dispatch(stale_read)["result"]["version"] == RPC_VERSION
    assert kit_rpc._STALE_AFTER_S < 15.0  # below live_scene_control._submit's wait, so a timeout means 'not applied'


def test_insight_counts_unsaved_edits_in_a_muted_layer():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        sub = Sdf.Layer.CreateNew(str(folder / "sub.usda"))
        sub.Save()
        stage = Usd.Stage.CreateNew(str(folder / "root.usda"))
        stage.GetRootLayer().subLayerPaths.append("./sub.usda")
        stage.GetRootLayer().Save()
        with Usd.EditContext(stage, sub):
            UsdGeom.Xform.Define(stage, "/World")  # unsaved edit in sub.usda
        assert sub.dirty and not stage.GetRootLayer().dirty
        with _live_bridge(stage) as (bridge, _context):
            server = _server(bridge)
            before = _call(server, "insight", query="stage_info")["result"]
            stage.MuteLayer(sub.identifier)
            after = _call(server, "insight", query="stage_info")["result"]
        for result in (before, after):
            assert result["has_unsaved_changes"] is True, result
            assert [layer["identifier"] for layer in result["unsaved_layers"]] == [sub.identifier], result
        assert sub.dirty
        del stage, sub
