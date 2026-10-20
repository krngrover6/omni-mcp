import os

# The stdlib test runner has no conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import io
import json
import socket
import tempfile
import urllib.request

from omniverse_scene_mcp import runtime
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.router import NullKnowledgeBackend, RouteTarget
from omniverse_scene_mcp.server import build_server


@contextlib.contextmanager
def _no_network():
    """Make every socket connect and urlopen raise, and record any attempt."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(repr(args[-1:] or kwargs))
        raise AssertionError(f"network access attempted: {args!r}")

    patched = [
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (urllib.request, "urlopen"),
        (runtime, "urlopen"),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]
    for owner, name in patched:
        setattr(owner, name, refuse)
    try:
        yield attempts
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


@contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _RecordingEnv(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = []

    def get(self, key, default=None):
        self.read.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.append(key)
        return super().__getitem__(key)


def _never_detect():
    raise AssertionError("the 127.0.0.1:9910 health probe must not run")


def _tool(app, name):
    tool = app._tool_manager.get_tool(name)
    assert tool is not None, name
    return tool


def test_server_builds():
    app = build_server()
    assert app is not None


def test_test_switch_is_set_at_import():
    assert runtime.is_test_mode()


def test_build_server_under_test_switch_never_opens_a_socket():
    with tempfile.TemporaryDirectory() as root:
        with _env(
            OMNIVERSE_SCENE_MCP_TEST="1",
            OMNIVERSE_SCENE_HTTP_ENDPOINT="http://127.0.0.1:9910",
            OMNIVERSE_SCENE_MCP_MODE="live",
            OMNIVERSE_ROOT=root,
        ):
            with _no_network() as attempts:
                app = build_server()
                health = _tool(app, "router_health").fn()
                summary = _tool(app, "scene_summary").fn()
    assert attempts == []
    assert health["scene"]["mode"] == "test"
    assert health["scene"]["bridge"] == "FakeSceneBridge"
    assert health["scene"]["probe"]["probed"] is False
    assert all(entry["probe"]["probed"] is False for entry in health["knowledge"])
    assert summary["stage_name"] == "FakeStage"
    json.dumps(health)


def test_test_switch_never_reads_endpoint_env_or_probes():
    env = _RecordingEnv(
        OMNIVERSE_SCENE_MCP_TEST="1",
        OMNIVERSE_SCENE_HTTP_ENDPOINT="http://127.0.0.1:9911",
        OMNIVERSE_SCENE_MCP_MODE="live",
    )
    info = runtime.choose_scene_backend(env, detect_endpoint=_never_detect)
    assert info["mode"] == "test" and info["endpoint"] is None and info["silent_fallback"] is False
    assert "OMNIVERSE_SCENE_HTTP_ENDPOINT" not in env.read

    env.read.clear()
    builders = ("build_usd_backend", "build_kit_backend", "build_omni_ui_backend", "build_isaac_sim_backend", "build_bowerbot_backend")
    originals = {name: getattr(runtime, name) for name in builders}

    def upstream_builder(*args, **kwargs):
        raise AssertionError("upstream knowledge backends must not be built under the test switch")

    try:
        for name in builders:
            setattr(runtime, name, upstream_builder)
        with _no_network() as attempts:
            router = runtime.build_router(env, detect_endpoint=_never_detect)
    finally:
        for name, original in originals.items():
            setattr(runtime, name, original)
    assert attempts == []
    assert "OMNIVERSE_SCENE_HTTP_ENDPOINT" not in env.read
    assert isinstance(router._scene._bridge, FakeSceneBridge)
    assert router._scene.backend_info["mode"] == "test"


def test_test_switch_uses_offline_knowledge_backends():
    router = runtime.build_router({"OMNIVERSE_SCENE_MCP_TEST": "1"}, detect_endpoint=_never_detect)
    assert all(isinstance(backend, NullKnowledgeBackend) for backend in router._backends.values())
    assert all(item["endpoint"] is None for item in router.backend_status()["backends"])
    answer = asyncio.run(router.ask_backend(RouteTarget.USD, "what is a prim?"))
    assert answer["ok"] is False and answer["backend"] == "usd"


def test_is_test_mode_off_values():
    for value in ("", "0", "false", "No", " off "):
        assert runtime.is_test_mode({"OMNIVERSE_SCENE_MCP_TEST": value}) is False
    assert runtime.is_test_mode({}) is False
    assert runtime.is_test_mode({"OMNIVERSE_SCENE_MCP_TEST": "yes"}) is True


def test_backend_choice_http_endpoint_from_env():
    info = runtime.choose_scene_backend(
        {"OMNIVERSE_SCENE_HTTP_ENDPOINT": " http://127.0.0.1:9911 "}, detect_endpoint=_never_detect
    )
    assert info["mode"] == "http_env"
    assert info["bridge"] == "HttpSceneBridge"
    assert info["endpoint"] == "http://127.0.0.1:9911"
    assert info["silent_fallback"] is False and info["warnings"] == []


def test_backend_choice_autodetected_endpoint_and_ignored_mode():
    info = runtime.choose_scene_backend({"OMNIVERSE_SCENE_MCP_MODE": "fake"}, detect_endpoint=lambda: "http://127.0.0.1:9910")
    assert info["mode"] == "http_autodetect"
    assert info["endpoint"] == "http://127.0.0.1:9910"
    assert any("OMNIVERSE_SCENE_MCP_MODE=fake is ignored" in warning for warning in info["warnings"])


def test_backend_choice_live_kit_mode():
    info = runtime.choose_scene_backend(
        {"OMNIVERSE_SCENE_MCP_MODE": "Live", "OMNIVERSE_USD_CONTEXT_NAME": "ctx"}, detect_endpoint=lambda: None
    )
    assert info["mode"] == "live_kit"
    assert info["bridge"] == "LiveKitSceneBridge"
    assert info["usd_context_name"] == "ctx"
    assert info["silent_fallback"] is False


def test_backend_choice_deliberate_fake_versus_silent_fallback():
    deliberate = runtime.choose_scene_backend({"OMNIVERSE_SCENE_MCP_MODE": "fake"}, detect_endpoint=lambda: None)
    assert deliberate["mode"] == "fake" and deliberate["silent_fallback"] is False

    fallback = runtime.choose_scene_backend({}, detect_endpoint=lambda: None)
    assert fallback["mode"] == "fake_fallback"
    assert fallback["silent_fallback"] is True
    assert "OMNIVERSE_SCENE_MCP_MODE is unset" in fallback["reason"]
    assert fallback["warnings"]

    typo = runtime.choose_scene_backend({"OMNIVERSE_SCENE_MCP_MODE": "liev"}, detect_endpoint=lambda: None)
    assert typo["mode"] == "fake_fallback" and "'liev'" in typo["reason"]


def test_build_scene_service_attaches_backend_info_without_connecting():
    with _no_network() as attempts:
        service = runtime.build_scene_service(
            {"OMNIVERSE_SCENE_HTTP_ENDPOINT": "http://127.0.0.1:9911"}, detect_endpoint=_never_detect
        )
        fallback = runtime.build_scene_service({}, detect_endpoint=lambda: None)
    assert attempts == []
    assert isinstance(service._bridge, HttpSceneBridge)
    assert service.backend_info["mode"] == "http_env"
    assert fallback.backend_info["mode"] == "fake_fallback"
    info = fallback.backend_info
    info["warnings"].append("mutated copy")
    assert "mutated copy" not in fallback.backend_info["warnings"]


def test_detect_live_scene_endpoint_handles_errors_offline():
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    original = runtime.urlopen
    try:
        def reset(*args, **kwargs):
            raise ConnectionResetError("reset by peer")

        runtime.urlopen = reset
        assert runtime._detect_live_scene_endpoint() is None
        runtime.urlopen = lambda *args, **kwargs: _Response(b"[1, 2]")
        assert runtime._detect_live_scene_endpoint() is None
        runtime.urlopen = lambda *args, **kwargs: _Response(b"not json")
        assert runtime._detect_live_scene_endpoint() is None
        runtime.urlopen = lambda *args, **kwargs: _Response(b'{"ok": true}')
        assert runtime._detect_live_scene_endpoint() == runtime.AUTO_SCENE_ENDPOINT
    finally:
        runtime.urlopen = original


def test_loopback_scene_calls_ignore_http_proxy_settings():
    """With HTTP(S)_PROXY set, the scene probe and bridge RPCs still connect to 127.0.0.1, never the proxy."""
    from omniverse_scene_mcp import http_bridge

    hosts = []

    def record_and_refuse(address, *args, **kwargs):
        hosts.append(address[0])
        raise ConnectionRefusedError("refused by test")

    saved_env = {key: os.environ.get(key) for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")}
    original_connect = socket.create_connection
    for key in saved_env:
        os.environ.pop(key, None)
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://proxy.invalid:3128"
    socket.create_connection = record_and_refuse
    try:
        assert runtime._detect_live_scene_endpoint() is None
        try:
            HttpSceneBridge("http://127.0.0.1:9910").ping()
        except ValueError:
            pass
        else:
            raise AssertionError("ping should fail when the connection is refused")
    finally:
        socket.create_connection = original_connect
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert hosts and set(hosts) == {"127.0.0.1"}, hosts
    assert callable(runtime.urlopen) and callable(http_bridge.urlopen)


def test_scene_save_tool_dry_run_previews_without_saving():
    with _no_network() as attempts:
        app = build_server()
    assert attempts == []
    tool = _tool(app, "scene_save")
    assert tool.parameters["properties"]["dry_run"]["default"] is False
    preview = tool.fn(path="C:/tmp/Copy.usda", dry_run=True)
    assert preview["dry_run"] is True
    assert preview["would"] == "Save As: export a copy of the root layer only"
    assert _tool(app, "scene_summary").fn()["stage_name"] == "FakeStage"


def test_router_health_tool_is_registered():
    app = build_server()
    names = {tool.name for tool in app._tool_manager.list_tools()}
    assert {"router_health", "scene_save", "router_status"} <= names
    assert _tool(app, "router_health").parameters.get("properties", {}) == {}
