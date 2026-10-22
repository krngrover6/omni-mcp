"""Extended live RPC (kit_rpc) on a fake base server, and live insight on temporary pxr stages with omni.usd faked.

Fixture-free so isaacsim-study/reload_mcp_bridge.py can run it inside Isaac Sim before swapping the bridge. No HTTP
server is started and the open stage is never touched: every stage here is temporary.
"""

import contextlib
import importlib.util
import io
import json
import math
import re
import tempfile
import types
from email.message import Message
from http.server import BaseHTTPRequestHandler
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
    assert cls.rpc_version == RPC_VERSION == "tier4-1"
    assert set(cls.rpc_methods) == set(BASIC_RPC_METHODS) | set(LIVE_RPC_METHODS)
    _raises(lambda: kit_rpc.make_extended_server_class(object()), "_dispatch", error=TypeError)
    _raises(lambda: kit_rpc.make_extended_server_class(int), "_dispatch", error=TypeError)


def test_capabilities_reports_version_methods_and_queries():
    server = _server(_RecordingBridge())
    response = _call(server, "capabilities")
    assert response["ok"] is True
    result = response["result"]
    assert result["version"] == "tier4-1"
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


# --- insight time budget -----------------------------------------------------------------------------------------

import time as _time  # noqa: E402

from omniverse_scene_mcp import usd_insight  # noqa: E402


class _BudgetSeeingBridge(_RecordingBridge):
    """Reports the ambient time budget kit_rpc set, as usd_insight's queries do (or raises, to test cleanup)."""

    def __init__(self, fail=False) -> None:
        super().__init__()
        self.fail = fail

    def insight(self, query, args=None):
        self.calls.append(("insight", query, args))
        active = usd_insight.active_budget()
        if self.fail:
            raise ValueError("query failed")
        result = {"ok": True, "query": query, "seen_budget_ms": active.budget_ms if active else None, "elapsed_ms": 7}
        if active is not None:
            result["truncated_by_time"] = False
            result["time_budget"] = active.report(12, False)
        return result


def test_insight_time_budget_defaults_caps_and_argument_checks():
    if Usd is None:  # pragma: no cover - pxr missing: TimeBudget (Tf.Stopwatch) cannot be built
        return
    bridge = _BudgetSeeingBridge()
    server = _server(bridge)
    result = _call(server, "insight", query="stage_info")["result"]
    assert result["budget_ms"] == kit_rpc.INSIGHT_DEFAULT_BUDGET_MS == 3000.0
    assert result["seen_budget_ms"] == 3000.0 and result["truncated_by_time"] is False
    info = result["time_budget"]
    assert info["enforced"] is True and info["limited_by_queue_wait"] is False and info["requested_budget_ms"] == 3000.0
    assert info["processed"] == 12 and info["note"]  # the query's own record is kept, kit_rpc's fields are added
    assert info["elapsed_ms"] >= 0 and result["elapsed_ms"] == 7  # the bridge's own elapsed_ms is kept
    assert bridge.calls[-1] == ("insight", "stage_info", {}) and usd_insight.active_budget() is None
    json.dumps(result)
    # A bridge that reports no timing (older or test bridges) gets its result back unchanged.
    plain = _call(_server(_RecordingBridge()), "insight", query="stage_info", budget_ms=20)["result"]
    assert plain == {"ok": True, "mode": "live", "query": "stage_info"}

    # budget_ms may sit in params or in args; the bridge never sees it (its argument allowlist would reject it).
    result = _call(server, "insight", query="bounds", args={"prim_path": "/World", "budget_ms": 50})["result"]
    assert result["budget_ms"] == result["seen_budget_ms"] == 50.0
    assert bridge.calls[-1] == ("insight", "bounds", {"prim_path": "/World"})
    capped = _call(server, "insight", query="stage_info", budget_ms=10**6)["result"]
    assert capped["budget_ms"] == kit_rpc.INSIGHT_MAX_BUDGET_MS == 10000.0
    assert capped["time_budget"]["requested_budget_ms"] == 1e6 and capped["seen_budget_ms"] == 10000.0
    calls = len(bridge.calls)
    for bad in (0, -5, True, "fast", math.nan, math.inf, [100]):
        _raises(lambda: _call(server, "insight", query="stage_info", budget_ms=bad), "budget_ms")
    _raises(lambda: _call(server, "insight", query="stage_info", budget_ms=5, args={"budget_ms": 5}), "given twice")
    assert len(bridge.calls) == calls

    failing = _server(_BudgetSeeingBridge(fail=True))
    _raises(lambda: _call(failing, "insight", query="stage_info"), "query failed")
    assert usd_insight.active_budget() is None
    capabilities = _call(server, "capabilities")["result"]
    assert capabilities["insight_budget_ms"] == {"default": 3000.0, "max": 10000.0}
    # A bridge without insight still gets the structured needs_bridge_update result.
    lacking = _call(_server(_PingOnlyBridge()), "insight", query="stage_info", budget_ms=100)["result"]
    assert lacking["needs_bridge_update"] is True and "time_budget" not in lacking


def test_insight_time_budget_shrinks_to_what_is_left_of_the_callers_wait():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    bridge = _BudgetSeeingBridge()
    server = kit_rpc.make_extended_server_class(_QueueingBaseServer)(bridge)
    server._submit({"method": "insight", "params": {"query": "stage_info", "budget_ms": 10_000}})
    [queued] = server.queued
    fresh = server._dispatch(queued)["result"]
    assert fresh["budget_ms"] == 10_000.0 and fresh["time_budget"]["limited_by_queue_wait"] is False
    waited = server._dispatch(dict(queued, _queued_at=_time.monotonic() - 12.0))["result"]
    left = (kit_rpc._CALLER_WAIT_S - kit_rpc._RESULT_MARGIN_S - 12.0) * 1000.0
    assert waited["time_budget"]["limited_by_queue_wait"] is True and 0 < waited["budget_ms"] <= left
    assert waited["seen_budget_ms"] == waited["budget_ms"] and waited["time_budget"]["queue_wait_ms"] >= 12_000
    gone = server._dispatch(dict(queued, _queued_at=_time.monotonic() - 60.0))["result"]
    assert gone["budget_ms"] == kit_rpc._MIN_BUDGET_MS and gone["time_budget"]["limited_by_queue_wait"] is True
    assert kit_rpc._CALLER_WAIT_S - kit_rpc._RESULT_MARGIN_S > kit_rpc.INSIGHT_MAX_BUDGET_MS / 1000.0


def _big_live_stage(groups=50, per=100):
    """In-memory /World > G<i> > C<j> cubes: 5,051 prims, built with Sdf in one change block."""
    layer = Sdf.Layer.CreateAnonymous(".usda")
    with Sdf.ChangeBlock():
        world = Sdf.PrimSpec(layer, "World", Sdf.SpecifierDef, "Xform")
        for group_index in range(groups):
            group = Sdf.PrimSpec(world, f"G{group_index}", Sdf.SpecifierDef, "Xform")
            for cube_index in range(per):
                Sdf.PrimSpec(group, f"C{cube_index}", Sdf.SpecifierDef, "Cube")
    return Usd.Stage.Open(layer)


def test_live_insight_on_a_big_stage_stops_at_the_time_budget():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    total = 1 + 50 + 50 * 100
    stage = _big_live_stage()
    before = stage.GetRootLayer().ExportToString()
    with _live_bridge(stage) as (bridge, _context):
        server = _server(bridge)
        insight = lambda query, **params: _call(server, "insight", query=query, **params)["result"]  # noqa: E731
        tiny = {
            "stage_info": insight("stage_info", budget_ms=1),
            "bounds": insight("bounds", args={"prim_path": "/World", "budget_ms": 1}),
            "validate": insight("validate", budget_ms=1),
        }
        roomy = insight("stage_info")
        roomy_bounds = insight("bounds", args={"prim_path": "/World"}, budget_ms=10_000)
        # Every live query reports its budget, elapsed time and truncation (even the one-prim ones).
        for query, args in (
            ("composition", {"prim_path": "/World/G0"}),
            ("explain_value", {"prim_path": "/World/G0/C0", "attribute": "size"}),
            ("properties", {"prim_path": "/World/G0/C0"}),
            ("validate", {"max_prims": 100}),
        ):
            reported = insight(query, args=args)
            assert reported["truncated_by_time"] is False and reported["budget_ms"] == 3000.0, query
            assert reported["time_budget"]["elapsed_ms"] >= 0 and reported["elapsed_ms"] >= 0, query
            assert reported["time_budget"]["enforced"] is True and args.items() <= reported["args"].items(), query
            assert "budget_ms" not in reported["args"], query
    for query, result in tiny.items():
        json.dumps(result)
        assert result["mode"] == "live" and result["query"] == query, query
        assert result["truncated_by_time"] is True and result["time_budget"]["truncated_by_time"] is True, query
        assert result["budget_ms"] == 1.0 and result["time_budget"]["processed"] < total, query
        assert any("time budget" in warning for warning in result["warnings"]), query
        assert "budget_ms" not in result["args"], query
    assert tiny["stage_info"]["prims"]["total"] < total
    assert tiny["bounds"]["world_bound"]["partial"] is True
    assert roomy["truncated_by_time"] is False and roomy["prims"]["total"] == total and roomy["budget_ms"] == 3000.0
    assert "warnings" not in roomy or not any("time budget" in warning for warning in roomy["warnings"])
    assert roomy_bounds["truncated_by_time"] is False and roomy_bounds["time_budget"]["processed"] == total
    assert stage.GetRootLayer().ExportToString() == before and usd_insight.active_budget() is None


# --- tier3-1: safe attribute/material edits and MCP undo over the extended RPC ---------------------------------

from omniverse_scene_mcp.bridge import EDIT_RPC_METHODS, UNDO_RPC_METHODS  # noqa: E402 - appended section


class _RecordingEditBridge(_RecordingBridge):
    """Records the edit/undo calls the extended server forwards; never touches a stage."""

    def set_attribute(self, path, name, value, **options):
        self.calls.append(("set_attribute", path, name, value, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def create_material(self, path, **options):
        self.calls.append(("create_material", path, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def bind_material(self, prim_path, material_path, **options):
        self.calls.append(("bind_material", prim_path, material_path, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": prim_path}

    def undo_last(self, dry_run=True):
        self.calls.append(("undo_last", dry_run))
        return {"ok": True, "dry_run": dry_run}

    def undo_history(self, limit=20):
        self.calls.append(("undo_history", limit))
        return {"ok": True, "count": 0, "entries": []}


def test_edit_and_undo_methods_are_published_in_the_tier3_capabilities():
    assert RPC_VERSION == "tier4-1"
    assert set(EDIT_RPC_METHODS) | set(UNDO_RPC_METHODS) <= set(LIVE_RPC_METHODS)
    assert not (set(EDIT_RPC_METHODS) | set(UNDO_RPC_METHODS)) & set(BASIC_RPC_METHODS)
    assert set(LIVE_RPC_METHODS) <= set(kit_rpc._HANDLERS)
    result = _call(_server(_RecordingEditBridge()), "capabilities")["result"]
    assert result["version"] == "tier4-1" and result["dry_run_default"] is True
    assert result["edit_methods"] == list(EDIT_RPC_METHODS) and result["undo_methods"] == list(UNDO_RPC_METHODS)
    assert set(EDIT_RPC_METHODS) | set(UNDO_RPC_METHODS) <= set(result["methods"])
    # Every write is dropped when it waited too long in Kit's queue; undo_history only reads.
    assert set(EDIT_RPC_METHODS) | {"undo_last"} <= set(kit_rpc._WRITE_METHODS)
    assert "undo_history" not in kit_rpc._WRITE_METHODS


def test_edit_and_undo_methods_forward_a_dry_run_unless_dry_run_is_false():
    bridge = _RecordingEditBridge()
    server = _server(bridge)

    preview = _call(server, "set_attribute", path="/World/Key", name="inputs:intensity", value=5000)["result"]
    assert preview == {"ok": True, "dry_run": True, "path": "/World/Key"}  # missing dry_run: a quiet dry run
    assert bridge.calls[-1] == ("set_attribute", "/World/Key", "inputs:intensity", 5000,
                                {"time": None, "create_type": None, "dry_run": True})
    _call(server, "set_attribute", path="/World/Key", name="inputs:color", value=[1, 0.5, 0], time=24,
          create_type="color3f", dry_run=False)
    assert bridge.calls[-1][4] == {"time": 24.0, "create_type": "color3f", "dry_run": False}
    _call(server, "set_attribute", path="/World/Key", name="size", value=2, dry_run="FALSE")
    assert bridge.calls[-1][4]["dry_run"] is False
    for malformed in (0, 1, "no", "off", "maybe", [], {}):
        result = _call(server, "set_attribute", path="/World/Key", name="size", value=2, dry_run=malformed)["result"]
        assert bridge.calls[-1][4]["dry_run"] is True, malformed
        assert result["dry_run"] is True and "nothing was written" in result["warnings"][-1], malformed

    _call(server, "create_material", path="/World/Looks/Steel", diffuse_color=[0.8, 0.8, 0.8], roughness=0.2,
          metallic=1, emissive_color="#ff8800", create_parent=True)
    _, path, options = bridge.calls[-1]
    assert path == "/World/Looks/Steel" and options["dry_run"] is True and options["create_parent"] is True
    assert options["diffuse_color"] == (0.8, 0.8, 0.8) and options["metallic"] == 1.0
    assert options["emissive_color"] == "#ff8800" and options["opacity"] is None and options["ior"] is None
    _call(server, "create_material", path="/World/Looks/Steel")
    assert bridge.calls[-1][2]["create_parent"] is False  # only an explicit true creates a missing parent

    _call(server, "bind_material", prim_path="/World/Cube", material_path="/World/Looks/Steel", strength="stronger",
          purpose="preview", dry_run=False)
    assert bridge.calls[-1] == ("bind_material", "/World/Cube", "/World/Looks/Steel",
                                {"strength": "stronger", "purpose": "preview", "dry_run": False})
    _call(server, "bind_material", prim_path="/World/Cube", material_path="/World/Looks/Steel")
    assert bridge.calls[-1][3]["dry_run"] is True

    assert _call(server, "undo_last")["result"]["dry_run"] is True and bridge.calls[-1] == ("undo_last", True)
    _call(server, "undo_last", dry_run=False)
    assert bridge.calls[-1] == ("undo_last", False)
    risky = _call(server, "undo_last", dry_run="yes")["result"]
    assert bridge.calls[-1] == ("undo_last", True) and risky["warnings"]
    for limit, expected in ((None, 20), (0, 1), (5, 5), (999, 50)):
        params = {} if limit is None else {"limit": limit}
        _call(server, "undo_history", **params)
        assert bridge.calls[-1] == ("undo_history", expected), limit
    assert server.base_calls == []
    json.dumps(bridge.calls)


def test_edit_and_undo_parameters_are_checked_before_forwarding():
    bridge = _RecordingEditBridge()
    server = _server(bridge)
    bad = [
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1, "dryrun": False}, "Unknown parameter"),
        ("set_attribute", {"path": "", "name": "size", "value": 1}, "path"),
        ("set_attribute", {"path": "/World/Key", "value": 1}, "name"),
        ("set_attribute", {"path": "/World/Key", "name": "x" * 300, "value": 1}, "name"),
        ("set_attribute", {"path": "/World/Key", "name": "size"}, "value"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": math.nan}, "finite"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": [[[[[1]]]]]}, "nested"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": [1, None]}, "value"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1, "time": True}, "time"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1, "time": "later"}, "time"),
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1, "create_type": "f" * 80}, "create_type"),
        ("create_material", {"path": "/World/Looks/M", "diffuse_color": [1, 0]}, "diffuse_color"),
        ("create_material", {"path": "/World/Looks/M", "roughness": "shiny"}, "roughness"),
        ("create_material", {"path": "/World/Looks/M", "create_parent": "maybe"}, "create_parent"),
        ("create_material", {"path": "/World/Looks/M", "shader": "MDL"}, "Unknown parameter"),
        ("bind_material", {"prim_path": "/World/Cube"}, "material_path"),
        ("bind_material", {"prim_path": "/World/Cube", "material_path": "/M", "strength": ""}, "strength"),
        ("undo_last", {"dry_run": False, "force": True}, "Unknown parameter"),
        ("undo_history", {"limit": "5"}, "limit"),
        ("undo_history", {"limit": True}, "limit"),
    ]
    for method, params, fragment in bad:
        _raises(lambda: _call(server, method, **params), fragment)
    assert bridge.calls == []


def test_bridge_without_edit_or_undo_methods_gets_needs_bridge_update_results():
    server = _server(_PingOnlyBridge())
    for method, params in (
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1}),
        ("create_material", {"path": "/World/Looks/M"}),
        ("bind_material", {"prim_path": "/World/Cube", "material_path": "/World/Looks/M"}),
        ("undo_last", {}),
        ("undo_history", {}),
    ):
        result = _call(server, method, **params)["result"]
        assert result["ok"] is False and result["needs_bridge_update"] is True and result["method"] == method, method


def test_queued_edit_and_undo_writes_are_dropped_when_stale():
    bridge = _RecordingEditBridge()
    server = kit_rpc.make_extended_server_class(_QueueingBaseServer)(bridge)
    writes = (
        ("set_attribute", {"path": "/World/Key", "name": "size", "value": 1, "dry_run": False}),
        ("create_material", {"path": "/World/Looks/M", "dry_run": False}),
        ("bind_material", {"prim_path": "/World/Cube", "material_path": "/World/Looks/M", "dry_run": False}),
        ("undo_last", {"dry_run": False}),
    )
    for method, params in writes:
        server._submit({"method": method, "params": params})
    server._submit({"method": "undo_history", "params": {}})
    for payload in server.queued[:-1]:
        late = dict(payload, _queued_at=payload["_queued_at"] - kit_rpc._STALE_AFTER_S - 1.0)
        result = server._dispatch(late)["result"]
        assert result["ok"] is False and result["written"] is False, payload["method"]
    assert bridge.calls == []
    history = server.queued[-1]
    assert server._dispatch(dict(history, _queued_at=history["_queued_at"] - 60.0))["result"]["ok"] is True
    assert bridge.calls == [("undo_history", 20)]


def test_edits_and_undo_round_trip_over_the_rpc_on_a_live_stage():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    from pxr import UsdShade

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Cube").CreateSizeAttr(1.0)
    layer = stage.GetRootLayer()
    original = layer.ExportToString()
    with _live_bridge(stage) as (bridge, context):
        server = _server(bridge)
        result = lambda method, **params: _call(server, method, **params)["result"]  # noqa: E731
        previews = [
            result("set_attribute", path="/World/Cube", name="size", value=3),
            result("create_material", path="/World/Looks/Red", diffuse_color=[1, 0, 0], create_parent=True),
            result("set_attribute", path="/World/Cube", name="size", value=4, dry_run=0),
            result("undo_last"),
        ]
        assert layer.ExportToString() == original  # previews never write
        assert all(item["dry_run"] is True for item in previews[:3]), previews
        assert previews[1]["would_create"]["creates_parent"] == {"path": "/World/Looks", "type": "Scope"}
        assert previews[3]["ok"] is False  # nothing recorded yet

        sized = result("set_attribute", path="/World/Cube", name="size", value=3, dry_run=False)
        made = result("create_material", path="/World/Looks/Red", diffuse_color=[1, 0, 0], create_parent=True,
                      dry_run=False)
        bound = result("bind_material", prim_path="/World/Cube", material_path="/World/Looks/Red", dry_run="false")
        assert sized["ok"] and sized["written"] and sized["undoable"] is True, sized
        assert made["ok"] and made["created"]["created_parent"] == "/World/Looks", made
        assert bound["ok"] and bound["written"], bound
        assert stage.GetPrimAtPath("/World/Cube").GetAttribute("size").Get() == 3.0
        assert stage.GetPrimAtPath("/World/Looks").GetTypeName() == "Scope"
        material, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/World/Cube")).ComputeBoundMaterial()
        assert material and str(material.GetPath()) == "/World/Looks/Red"
        assert context.selection.paths == []  # edits never change the selection

        history = result("undo_history", limit=10)
        assert history["count"] == 3 and history["entries"][0]["label"].startswith("Bind /World/Looks/Red")
        before_undo = layer.ExportToString()
        assert result("undo_last")["would_undo"]["label"].startswith("Bind") and layer.ExportToString() == before_undo
        for _ in range(3):
            undone = result("undo_last", dry_run=False)
            assert undone["ok"] is True and undone["verified"] is True, undone
            assert "not undoable" in undone["note"]
        assert layer.ExportToString() == original
        json.dumps([previews, sized, made, bound, history])


# --- localhost CSRF / DNS-rebinding guard, socket-free (fake connections), so it also runs in Kit's self-test --------


class _FakeConnection:
    """Socket stand-in for BaseHTTPRequestHandler: reads one raw request, collects the raw response."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.sent = bytearray()

    def makefile(self, mode, *_args, **_kwargs):
        assert "r" in mode, mode
        return io.BytesIO(self._raw)

    def sendall(self, data) -> None:
        self.sent.extend(data)

    def settimeout(self, _timeout) -> None:
        return None

    def setsockopt(self, *_args) -> None:
        return None


class _GuardBaseServer(_FakeBaseServer):
    """_FakeBaseServer plus a live_scene_control-shaped HTTP handler; _submit records and dispatches synchronously."""

    def __init__(self, bridge) -> None:
        super().__init__(bridge)
        self.submitted: list = []

    def _submit(self, payload):
        self.submitted.append(payload)
        return self._dispatch(payload)

    def _build_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self._send_json(200, parent._submit(json.loads(self.rfile.read(length).decode("utf-8") or "{}")))

            def do_GET(self) -> None:  # noqa: N802
                self._send_json(200, {"ok": True, "status": "running"})

            def log_message(self, *_args) -> None:
                return

            def _send_json(self, status, payload) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


_GUARD_PORT = 47123  # only a label in the fake server's address: nothing binds or connects to it


def _guarded_request(handler_cls, method="POST", path="/rpc", headers=None, body=None):
    """(status, JSON body) of one raw request run through handler_cls on a fake connection (no socket)."""
    if body is None and method == "POST":
        body = b'{"method": "ping", "params": {}}'
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    connection = _FakeConnection(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b""))
    fake_server = types.SimpleNamespace(server_address=("127.0.0.1", _GUARD_PORT))
    handler_cls(connection, ("127.0.0.1", 50000), fake_server)
    head, _, payload = bytes(connection.sent).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), json.loads(payload.decode("utf-8") or "{}")


@contextlib.contextmanager
def _guard_env(value):
    """OMNIVERSE_BRIDGE_TOKEN set to value (None: unset) for the block, always restored."""
    environ = kit_rpc.os.environ
    previous = environ.get(kit_rpc.BRIDGE_TOKEN_ENV)
    if value is None:
        environ.pop(kit_rpc.BRIDGE_TOKEN_ENV, None)
    else:
        environ[kit_rpc.BRIDGE_TOKEN_ENV] = value
    try:
        yield
    finally:
        if previous is None:
            environ.pop(kit_rpc.BRIDGE_TOKEN_ENV, None)
        else:
            environ[kit_rpc.BRIDGE_TOKEN_ENV] = previous


def test_bridge_request_refusal_rules():
    def refusal(command, port=9911, token=None, **values):
        message = Message()
        for name, value in values.items():
            message[name.replace("_", "-")] = value
        return kit_rpc.bridge_request_refusal(command, message, port, token)

    good = {"Host": "127.0.0.1:9911", "Content_Type": "application/json"}
    assert refusal("POST", **good) is None
    assert refusal("POST", Host="localhost:9911", Content_Type="application/json; charset=utf-8") is None
    assert refusal("GET", Host="127.0.0.1:9911") is None
    assert refusal("POST", **good, Origin="https://evil.example")[0] == "origin"
    assert refusal("GET", Host="127.0.0.1:9911", Origin="null")[0] == "origin"
    assert refusal("POST", **good, Sec_Fetch_Mode="no-cors")[0] == "browser"
    assert refusal("POST", **good, Sec_Fetch_Site="cross-site")[0] == "browser"
    assert refusal("POST", Host="127.0.0.1:9911", Content_Type="text/plain")[0] == "content_type"
    assert refusal("POST", Host="127.0.0.1:9911")[0] == "content_type"
    assert refusal("POST", Host="evil.example:9911", Content_Type="application/json")[0] == "host"
    assert refusal("POST", Host="127.0.0.1:9910", Content_Type="application/json")[0] == "host"
    assert refusal("POST", Content_Type="application/json")[0] == "host"
    assert refusal("POST", port=None, **good)[0] == "host"
    assert refusal("POST", token="t0ken", **good)[0] == "token"
    assert refusal("POST", token="t0ken", X_Omniverse_Bridge_Token="nope", **good)[0] == "token"
    assert refusal("POST", token="t0ken", X_Omniverse_Bridge_Token="t0ken", **good) is None
    assert refusal("GET", token="t0ken", Host="127.0.0.1:9911") is None  # /health stays token-free
    assert "t0ken" not in refusal("POST", token="t0ken", X_Omniverse_Bridge_Token="nope", **good)[1]


def test_guarded_handler_refuses_browser_requests_before_dispatch():
    server = kit_rpc.make_extended_server_class(_GuardBaseServer)(_PingOnlyBridge())
    handler = server._build_handler()
    assert handler._omniverse_bridge_guarded and kit_rpc.guard_handler_class(handler) is handler
    host = {"Host": f"127.0.0.1:{_GUARD_PORT}"}
    json_headers = {**host, "Content-Type": "application/json"}
    with _guard_env(None):
        assert _guarded_request(handler, headers=json_headers) == (200, {"ok": True, "result": {"status": "ok"}})
        assert [item["method"] for item in server.submitted] == ["ping"]
        server.submitted.clear()
        attack = {**host, "Content-Type": "text/plain", "Origin": "https://evil.example", "Sec-Fetch-Mode": "no-cors"}
        for request_headers, reason in (
            (attack, "origin"),
            ({**json_headers, "Sec-Fetch-Site": "cross-site"}, "browser"),
            ({**host, "Content-Type": "text/plain"}, "content_type"),
            ({**json_headers, "Host": f"evil.example:{_GUARD_PORT}"}, "host"),
        ):
            status, body = _guarded_request(handler, headers=request_headers)
            assert status == 403 and body["forbidden"] is True and body["reason"] == reason, (reason, body)
        assert _guarded_request(handler, "GET", "/health", headers={**host, "Origin": "https://x.example"})[0] == 403
        assert _guarded_request(handler, "GET", "/health", headers=host)[0] == 200
    with _guard_env("shared-secret"):
        assert _guarded_request(handler, headers=json_headers)[1]["reason"] == "token"
        wrong = {**json_headers, kit_rpc.BRIDGE_TOKEN_HEADER: "guess"}
        assert _guarded_request(handler, headers=wrong)[1]["reason"] == "token"
        assert server.submitted == []
        right = {**json_headers, kit_rpc.BRIDGE_TOKEN_HEADER: "shared-secret"}
        assert _guarded_request(handler, headers=right)[0] == 200
        assert len(server.submitted) == 1


def test_real_live_scene_control_handler_refuses_browser_requests_inside_kit():
    """Inside Kit: the real base Handler behind the guard refuses without queueing (no Kit pump is waited on)."""
    if not _LIVE_SCENE_CONTROL.is_file():
        return
    spec = importlib.util.spec_from_file_location("kit_rpc_guard_scene_control", _LIVE_SCENE_CONTROL)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return
    cls = kit_rpc.make_extended_server_class(module.LiveSceneControlServer)
    server = cls.__new__(cls)  # no __init__: no Kit context, no HTTP server, nothing started
    server._host, server._port, server._bridge = "127.0.0.1", _GUARD_PORT, _PingOnlyBridge()
    submitted = []
    server._submit = submitted.append  # a refused request must never get here
    handler = server._build_handler()
    host = {"Host": f"127.0.0.1:{_GUARD_PORT}"}
    with _guard_env(None):
        for request_headers in (
            {**host, "Content-Type": "text/plain", "Origin": "https://evil.example"},
            {**host, "Content-Type": "text/plain"},
            {"Host": f"evil.example:{_GUARD_PORT}", "Content-Type": "application/json"},
        ):
            assert _guarded_request(handler, headers=request_headers)[0] == 403
        assert _guarded_request(handler, "GET", "/health", headers=host)[0] == 200
    assert submitted == []
