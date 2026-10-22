import json
import os
import tempfile

from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.service import SceneMCPService


def test_scene_summary_reports_top_level_prims():
    service = SceneMCPService(FakeSceneBridge())
    summary = service.scene_summary()
    assert summary["stage_name"] == "FakeStage"
    assert "/World/Props" in summary["top_level_prims"]


def test_dry_run_create_returns_preview():
    service = SceneMCPService(FakeSceneBridge())
    result = service.create_prim("/World/NewPrim", dry_run=True)
    preview = service.preview_changes(result)
    assert preview["dry_run"] is True
    assert preview["changes"][0]["action"] == "create"
    assert preview["payload"]["type_name"] == "Xform"


def test_move_prim_updates_fake_scene():
    service = SceneMCPService(FakeSceneBridge())
    result = service.move_prim("/World/Props/Cube", translation=(1.0, 2.0, 3.0))
    prim = result.payload["prim"]
    assert prim["translation"] == (1.0, 2.0, 3.0)


def test_variant_selection_updates_fake_scene():
    bridge = FakeSceneBridge()
    service = SceneMCPService(bridge)

    result = service.set_variant_selection("/World/Props", "color", "blue")

    assert result.payload["selections"] == {"color": "blue"}
    assert service.get_variant_selections("/World/Props")["selections"] == {"color": "blue"}


def test_variant_selection_dry_run_does_not_change_scene():
    bridge = FakeSceneBridge()
    service = SceneMCPService(bridge)

    result = service.set_variant_selection("/World/Props", "trim", "sport", dry_run=True)

    assert result.dry_run is True
    assert service.get_variant_selections("/World/Props")["selections"] == {}


def test_timeline_pose_and_clip_controls_fake_scene():
    service = SceneMCPService(FakeSceneBridge())

    pose = service.set_timeline_time(480)
    assert pose["current_time_code"] == 480
    assert pose["playing"] is False

    playing = service.play_timeline(960, 1200)
    assert playing["start_time_code"] == 960
    assert playing["end_time_code"] == 1200
    assert playing["playing"] is True

    stopped = service.stop_timeline()
    assert stopped["playing"] is False


class _NoSaveBridge(FakeSceneBridge):
    """Fake bridge whose stage looks like a real file and whose save must never run."""

    def __init__(self, stage_name: str) -> None:
        super().__init__()
        self._stage_name = stage_name
        self.saves = []

    def save_scene(self, path: str) -> str:
        self.saves.append(path)
        raise AssertionError("dry run must not call the bridge's save")


def test_save_dry_run_in_place_when_path_matches_stage():
    bridge = _NoSaveBridge("C:/Users/Me/Scenes/Shot.usda")
    service = SceneMCPService(bridge)

    preview = service.save_scene(r"c:\users\me\scenes\SHOT.usda", dry_run=True)

    assert bridge.saves == []
    assert preview["dry_run"] is True
    assert preview["in_place"] is True
    assert preview["would"] == "save the root layer in place"
    assert preview["path"] == r"c:\users\me\scenes\SHOT.usda"
    assert any("dirty sublayer" in warning for warning in preview["warnings"])


def test_save_dry_run_strips_file_prefix_from_stage_identifier():
    for identifier in ("file:/C:/Scenes/Shot.usda", "file:///C:/Scenes/Shot.usda", "FILE:C:/Scenes/Shot.usda"):
        service = SceneMCPService(_NoSaveBridge(identifier))
        assert service.save_scene(r"C:\Scenes\Shot.usda", dry_run=True)["in_place"] is True, identifier


def test_save_dry_run_save_as_warns_about_root_layer_only():
    bridge = _NoSaveBridge("C:/Scenes/Shot.usda")
    service = SceneMCPService(bridge)

    preview = service.save_scene("C:/Scenes/Shot_copy.usda", dry_run=True)

    assert bridge.saves == []
    assert preview["in_place"] is False
    assert preview["would"] == "Save As: export a copy of the root layer only"
    text = " ".join(preview["warnings"])
    assert "Only the root layer is copied" in text
    assert "dirty sublayers stay unsaved in memory" in text
    assert "The open stage stays on C:/Scenes/Shot.usda" in text
    json.dumps(preview)


def test_save_dry_run_flags_existing_target_and_anonymous_stage():
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "exists.usda")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("#usda 1.0\n")
        service = SceneMCPService(_NoSaveBridge("anon:0x1234:root.usda"))
        preview = service.save_scene(target, dry_run=True)
    assert preview["in_place"] is False
    assert any("already exists" in warning for warning in preview["warnings"])
    assert any("anonymous" in warning for warning in preview["warnings"])


def test_save_dry_run_on_fake_bridge_does_not_rename_stage():
    service = SceneMCPService(FakeSceneBridge())
    preview = service.save_scene("FakeStage", dry_run=True)
    assert preview["in_place"] is None
    assert any("fake bridge" in warning for warning in preview["warnings"])
    assert any("Relative path" in warning for warning in preview["warnings"])
    assert service.scene_summary()["stage_name"] == "FakeStage"


def test_save_dry_run_relative_path_does_not_claim_copy_only():
    bridge = _NoSaveBridge("C:/Work/proj/scene.usda")
    service = SceneMCPService(bridge)
    preview = service.save_scene("proj/scene.usda", dry_run=True)
    assert bridge.saves == []
    assert preview["in_place"] is None
    assert "working directory" in preview["would"] and "overwrites" in preview["would"]
    assert not preview["would"].startswith("Save As")
    assert any("dirty sublayer" in warning for warning in preview["warnings"])
    uri = service.save_scene("file:/C:/Work/proj/scene.usda", dry_run=True)
    assert uri["in_place"] is True
    anonymous = SceneMCPService(_NoSaveBridge("anon:0x1:root.usda")).save_scene("copy.usda", dry_run=True)
    assert anonymous["in_place"] is False


def test_save_dry_run_requires_a_path():
    service = SceneMCPService(_NoSaveBridge("C:/Scenes/Shot.usda"))
    try:
        service.save_scene("  ", dry_run=True)
    except ValueError as exc:
        assert "required" in str(exc)
    else:
        raise AssertionError("empty path should be rejected")


def test_save_without_dry_run_still_calls_bridge():
    service = SceneMCPService(FakeSceneBridge())
    assert service.save_scene("C:/Scenes/Out.usda") == {"saved_path": "C:/Scenes/Out.usda"}


def test_backend_info_defaults_for_direct_construction():
    info = SceneMCPService(FakeSceneBridge()).backend_info
    assert info["mode"] == "direct"
    assert info["bridge"] == "FakeSceneBridge"
    assert info["silent_fallback"] is False
    custom = SceneMCPService(FakeSceneBridge(), backend_info={"mode": "fake", "bridge": "FakeSceneBridge", "warnings": ["w"]})
    assert custom.backend_info["mode"] == "fake" and custom.backend_info["warnings"] == ["w"]


# --- live insight and light/camera authoring (extended live RPC) ----------------------------------------------
# The HTTP client is exercised with http_bridge.urlopen swapped for an in-process transport: no socket is opened.

import contextlib  # noqa: E402 - appended section
import types  # noqa: E402

from omniverse_scene_mcp import http_bridge, kit_bridge, kit_rpc  # noqa: E402
from omniverse_scene_mcp.http_bridge import HttpSceneBridge  # noqa: E402


class _BasicOnlyServer:
    """Behaves like an old live_scene_control server: only basic methods, others 'Unsupported live scene method'."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge

    def _dispatch(self, payload):
        method = str(payload.get("method", "")).strip().lower()
        if method == "ping":
            return {"ok": True, "result": {"status": "ok"}}
        raise ValueError(f"Unsupported live scene method: {method}")


class _RecordingLiveBridge:
    def __init__(self) -> None:
        self.calls = []

    def capabilities(self):
        return {"bridge": "RecordingLiveBridge"}

    def insight(self, query, args=None):
        self.calls.append(("insight", query, args))
        return {"ok": True, "mode": "live", "query": query, "value": 1.5}

    def create_light(self, path, light_type, **options):
        self.calls.append(("create_light", path, light_type, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def create_camera(self, path, **options):
        self.calls.append(("create_camera", path, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    def aim(self, path, target, **options):
        self.calls.append(("aim", path, target, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}


class _FakeResponse:
    def __init__(self, body) -> None:
        self._data = json.dumps(body).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@contextlib.contextmanager
def _in_process_transport(server):
    """Route HttpSceneBridge RPCs into server._dispatch the way the live pump does (exceptions -> ok False)."""
    calls = []

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append({"url": request.full_url, "method": payload["method"], "params": payload["params"], "timeout": timeout})
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


def test_live_insight_on_fake_scene_is_a_structured_not_supported_result():
    service = SceneMCPService(FakeSceneBridge())
    result = service.live_insight("stage_info")
    assert result["ok"] is False and result["unsupported"] is True and result["fake_scene"] is True
    assert result["needs_bridge_update"] is False
    assert result["mode"] == "live" and result["query"] == "stage_info"
    assert "fake scene" in result["message"]
    unknown = service.live_insight("save")
    assert unknown["ok"] is False and "Unknown insight query" in unknown["message"]
    assert service.live_insight("composition", ["/World"])["ok"] is False
    capabilities = service.live_capabilities()
    assert capabilities["insight_queries"] == [] and "insight" not in capabilities["methods"]
    assert "ping" in capabilities["methods"]
    json.dumps([result, unknown, capabilities])


def test_live_authoring_on_fake_scene_is_not_ok_and_changes_nothing():
    service = SceneMCPService(FakeSceneBridge())
    before = service.scene_summary()
    for result in (
        service.create_light("/World/Key", "DistantLight", intensity=500.0),
        service.create_camera("/World/Cam", focal_length=35.0),
        service.aim("/World/Props/Cube", (0.0, 0.0, 0.0)),
    ):
        assert result.ok is False and result.dry_run is True and result.changes == []
        assert result.payload["unsupported"] is True and result.payload["fake_scene"] is True
        json.dumps(service.preview_changes(result))
    assert service.scene_summary() == before
    for action in (
        lambda: service.create_light("", "SphereLight"),
        lambda: service.create_light("/World/L", " "),
        lambda: service.aim("/World/Props/Cube", ""),
    ):
        try:
            action()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid authoring input should be rejected")


def test_service_live_methods_on_a_bridge_without_them_need_a_bridge_update():
    service = SceneMCPService(types.SimpleNamespace(ping=lambda: "ok"))
    assert service.live_capabilities()["needs_bridge_update"] is True
    insight = service.live_insight("bounds", {"prim_path": "/World"})
    assert insight["needs_bridge_update"] is True and insight["mode"] == "live" and insight["query"] == "bounds"
    light = service.create_light("/World/Key", "SphereLight")
    assert light.ok is False and light.payload["needs_bridge_update"] is True and light.payload["method"] == "create_light"


def test_http_client_turns_old_bridge_errors_into_needs_bridge_update():
    for endpoint, hint in (("http://127.0.0.1:9911", "reload_mcp_bridge.py"), ("http://127.0.0.1:9910", "Composer")):
        client = HttpSceneBridge(endpoint)
        with _in_process_transport(_BasicOnlyServer(None)) as calls:
            results = {
                "capabilities": client.capabilities(),
                "insight": client.insight("stage_info"),
                "create_light": client.create_light("/World/Key", "SphereLight"),
                "create_camera": client.create_camera("/World/Cam"),
                "aim": client.aim("/World/Cam", "/World"),
            }
            assert client.ping() == "ok"
        for method, result in results.items():
            assert result["ok"] is False and result["needs_bridge_update"] is True, method
            assert result["method"] == method and result["endpoint"] == endpoint
            assert hint in result["message"], (method, result["message"])
        assert all(call["url"] == f"{endpoint}/rpc" for call in calls)
        service = SceneMCPService(client)
        with _in_process_transport(_BasicOnlyServer(None)):
            insight = service.live_insight("stage_info")
            light = service.create_light("/World/Key", "SphereLight")
        assert insight["needs_bridge_update"] is True and insight["mode"] == "live"
        assert light.ok is False and light.payload["needs_bridge_update"] is True


def test_http_client_still_raises_real_errors_from_an_extended_bridge():
    client = HttpSceneBridge("http://127.0.0.1:9911")
    server = kit_rpc.make_extended_server_class(_BasicOnlyServer)(_RecordingLiveBridge())
    with _in_process_transport(server):
        try:
            client.insight("not_a_query")
        except ValueError as exc:
            assert "Unknown insight query" in str(exc)
        else:
            raise AssertionError("a real insight error must not look like an old bridge")
        try:
            client._rpc_optional("create_light", {"path": "/World/Key", "light_type": "SphereLight", "dryrun": False})
        except ValueError as exc:
            assert "Unknown parameter" in str(exc)
        else:
            raise AssertionError("a rejected parameter must raise, not report needs_bridge_update")


def test_service_forwards_live_authoring_through_the_http_client_with_dry_run_default():
    bridge = _RecordingLiveBridge()
    server = kit_rpc.make_extended_server_class(_BasicOnlyServer)(bridge)
    service = SceneMCPService(HttpSceneBridge("http://127.0.0.1:9911"))
    with _in_process_transport(server) as calls:
        capabilities = service.live_capabilities()
        insight = service.live_insight("explain_value", {"prim_path": "/World/Box", "attribute": "size", "time": 24})
        preview = service.create_light(
            "/World/Key", "RectLight", intensity=800.0, color=(1.0, 0.5, 0.25), aim_at=(0.0, 0.0, 0.0)
        )
        camera = service.create_camera("/World/Cam", clipping_range=(0.1, 500.0), dry_run=False)
        aimed = service.aim("/World/Cam", "/World/Key", up_axis="Z")

    assert capabilities["version"] == "tier5-1" and capabilities["endpoint"] == "http://127.0.0.1:9911"
    assert insight == {"ok": True, "mode": "live", "query": "explain_value", "value": 1.5}
    assert bridge.calls[0] == ("insight", "explain_value", {"prim_path": "/World/Box", "attribute": "size", "time": 24})
    assert preview.ok is True and preview.dry_run is True
    assert preview.changes[0].action == "create_light" and preview.changes[0].path == "/World/Key"
    assert preview.changes[0].after == {
        "light_type": "RectLight",
        "intensity": 800.0,
        "color": (1.0, 0.5, 0.25),
        "aim_at": (0.0, 0.0, 0.0),
    }
    light_options = bridge.calls[1][3]
    assert light_options["dry_run"] is True and light_options["color"] == (1.0, 0.5, 0.25)
    assert camera.ok is True and camera.dry_run is False and bridge.calls[2][2]["clipping_range"] == (0.1, 500.0)
    assert aimed.ok is True and aimed.dry_run is True and bridge.calls[3][2] == "/World/Key"
    sent = {call["method"]: call for call in calls}
    assert sent["create_light"]["params"]["color"] == [1.0, 0.5, 0.25] and "exposure" not in sent["create_light"]["params"]
    assert sent["insight"]["timeout"] == http_bridge.LIVE_TIMEOUT
    assert sent["capabilities"]["timeout"] == http_bridge.DEFAULT_TIMEOUT
    json.dumps([service.preview_changes(result) for result in (preview, camera, aimed)])


def test_live_insight_end_to_end_on_a_temporary_stage():
    try:
        from pxr import Usd, UsdGeom
    except ImportError:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/Box").CreateSizeAttr(2.0)
    context = types.SimpleNamespace(get_stage=lambda: stage, get_selection=None)
    previous = kit_bridge.omni
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        server = kit_rpc.make_extended_server_class(_BasicOnlyServer)(kit_bridge.LiveKitSceneBridge())
    finally:
        kit_bridge.omni = previous
    service = SceneMCPService(HttpSceneBridge("http://127.0.0.1:9911"))
    before = stage.GetRootLayer().ExportToString()
    with _in_process_transport(server):
        value = service.live_insight("explain_value", {"prim_path": "/World/Box", "attribute": "size"})
        missing = service.live_insight("composition", {"prim_path": "/World/Nope"})
    assert value["ok"] is True and value["mode"] == "live" and value["value"] == 2.0
    assert value["stage_identifier"] == stage.GetRootLayer().identifier
    assert missing["ok"] is False and "Prim does not exist" in missing["message"]
    assert stage.GetRootLayer().ExportToString() == before


def test_only_an_explicit_false_dry_run_authors_through_client_and_service():
    """None, 0 and [] stay a preview in HttpSceneBridge and SceneMCPService too, as they do in authoring.py."""
    bridge = _RecordingLiveBridge()
    server = kit_rpc.make_extended_server_class(_BasicOnlyServer)(bridge)
    client = HttpSceneBridge("http://127.0.0.1:9911")
    service = SceneMCPService(client)
    with _in_process_transport(server) as calls:
        for value in (None, 0, []):
            client.create_light("/World/Key", "RectLight", dry_run=value)
            client.create_camera("/World/Cam", dry_run=value)
            client.aim("/World/Cam", "/World/Key", dry_run=value)
            results = (
                service.create_light("/World/Key", "RectLight", dry_run=value),
                service.create_camera("/World/Cam", dry_run=value),
                service.aim("/World/Cam", "/World/Key", dry_run=value),
            )
            assert all(result.ok and result.dry_run is True for result in results), value
        explicit = service.create_light("/World/Key", "RectLight", dry_run=False)
    sent = [call["params"]["dry_run"] for call in calls]
    assert sent[:-1] == [True] * (len(sent) - 1) and sent[-1] is False, sent
    assert all(call[-1]["dry_run"] is True for call in bridge.calls[:-1])
    assert explicit.dry_run is False and bridge.calls[-1][-1]["dry_run"] is False
    # The service normalizes on its own too, for bridges that pass the value through unchanged.
    direct = _RecordingLiveBridge()
    for value in (None, 0, []):
        assert SceneMCPService(direct).create_light("/World/Key", "RectLight", dry_run=value).dry_run is True, value
        assert direct.calls[-1][-1]["dry_run"] is True, value
