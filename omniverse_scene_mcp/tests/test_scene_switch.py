"""runtime.switch_scene_target and SceneMCPService.swap_bridge: Composer <-> Isaac Sim <-> fake without a restart.

Offline: probes and live bridges are injected stand-ins, every call passes environ explicitly (the stdlib runner does
not load conftest.py), and _NoSockets fails a test that would open a connection.
"""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.error import URLError

from omniverse_scene_mcp import http_bridge, runtime
from omniverse_scene_mcp.bridge import RPC_VERSION
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter
from omniverse_scene_mcp.runtime import (
    TEST_ENV,
    build_router,
    probe_scene_health,
    scene_targets,
    switch_scene_target,
)
from omniverse_scene_mcp.service import SceneMCPService

TEST_MODE = {TEST_ENV: "1"}
LIVE_MODE: dict[str, str] = {}  # not test mode: only injected probes keep these tests offline
COMPOSER = "http://127.0.0.1:9910"
ISAAC = "http://127.0.0.1:9911"


class _NoSockets:
    """Any socket.create_connection or urlopen call inside this block fails the test."""

    def __enter__(self) -> _NoSockets:
        self._saved = (socket.create_connection, runtime.urlopen, http_bridge.urlopen)

        def refuse(*_args, **_kwargs):
            raise AssertionError("a socket was opened")

        socket.create_connection = refuse
        runtime.urlopen = refuse
        http_bridge.urlopen = refuse
        return self

    def __exit__(self, *_exc: object) -> None:
        socket.create_connection, runtime.urlopen, http_bridge.urlopen = self._saved


class _LiveStandIn(FakeSceneBridge):
    """Stands in for an HttpSceneBridge: a fake scene with an endpoint and extended capabilities, no sockets."""

    def __init__(self, endpoint: str, stage_name: str) -> None:
        super().__init__()
        self.endpoint = endpoint
        self._stage_name = stage_name

    def capabilities(self) -> dict:
        return {
            "ok": True,
            "bridge": "LiveKitSceneBridge",
            "version": RPC_VERSION,
            "methods": ["ping", "capabilities", "insight"],
            "insight_queries": ["stage_info"],
            "endpoint": self.endpoint,
        }


def _factory(stage_names: dict[str, str]):
    created: list[_LiveStandIn] = []

    def build(endpoint: str) -> _LiveStandIn:
        bridge = _LiveStandIn(endpoint, stage_names[endpoint])
        created.append(bridge)
        return bridge

    build.created = created  # type: ignore[attr-defined]
    return build


def _up(probed: list | None = None):
    def probe(endpoint: str, timeout: float) -> dict:
        if probed is not None:
            probed.append((endpoint, timeout))
        return {"reachable": True, "probed": True, "status": "running"}

    return probe


def _down(endpoint: str, timeout: float) -> dict:
    return {"reachable": False, "probed": True, "error": "ConnectionRefusedError: [WinError 10061]"}


def test_scene_targets_defaults_and_loopback_only_overrides():
    assert scene_targets({}) == {"isaac": ISAAC, "composer": COMPOSER, "fake": None}
    custom = scene_targets(
        {"OMNIVERSE_COMPOSER_SCENE_ENDPOINT": " http://127.0.0.1:9920/ ", "OMNIVERSE_ISAAC_SCENE_ENDPOINT": "http://127.0.0.1:9921"}
    )
    assert custom == {"isaac": "http://127.0.0.1:9921", "composer": "http://127.0.0.1:9920", "fake": None}
    for bad in (
        "http://localhost:9920",
        "http://10.0.0.5:9910",
        "https://127.0.0.1:9910",
        "http://user:pw@127.0.0.1:9910",
        "http://127.0.0.1:9910/rpc",
        "http://127.0.0.1:9910?x=1",
        "http://127.0.0.1",
        "http://127.0.0.1:99999",
        "127.0.0.1:9910",
        "garbage",
    ):
        assert scene_targets({"OMNIVERSE_COMPOSER_SCENE_ENDPOINT": bad})["composer"] == COMPOSER, bad


def test_switch_swaps_the_same_service_and_the_router_keeps_working():
    router = build_router(TEST_MODE)
    service = router._scene
    probed: list = []
    factory = _factory({COMPOSER: "ComposerStage", ISAAC: "IsaacStage"})
    with _NoSockets():
        result = switch_scene_target(service, "composer", environ=TEST_MODE, probe=_up(probed), bridge_factory=factory)
        assert result["ok"] and result["changed"] and result["reachable"], result
        assert probed == [(COMPOSER, 0.3)]
        assert result["previous"]["mode"] == "test" and result["previous"]["bridge"] == "FakeSceneBridge"
        current = result["current"]
        assert current["endpoint"] == COMPOSER and current["mode"] == "http_switch" and current["target"] == "composer"
        assert result["capabilities"]["ok"] and result["capabilities"]["version"] == RPC_VERSION
        assert router._scene is service and service.backend_info["mode"] == "http_switch"
        assert router.scene_summary()["stage_name"] == "ComposerStage"
        answer = asyncio.run(router.handle("show me the scene"))
        assert answer.payload["ok"] and answer.payload["result"]["stage_name"] == "ComposerStage"

        result = switch_scene_target(router, "isaac", environ=TEST_MODE, probe=_up(probed), bridge_factory=factory)
        assert result["ok"] and result["previous"]["endpoint"] == COMPOSER and result["current"]["endpoint"] == ISAAC
        assert router.scene_summary()["stage_name"] == "IsaacStage"
        assert service.live_capabilities()["endpoint"] == ISAAC
    json.dumps(result)


def test_switch_refuses_an_unreachable_target_unless_forced():
    service = SceneMCPService(FakeSceneBridge())
    before = service.bridge_info()
    factory = _factory({ISAAC: "IsaacStage"})
    with _NoSockets():
        refused = switch_scene_target(service, "isaac", environ=LIVE_MODE, probe=_down, bridge_factory=factory)
        assert not refused["ok"] and not refused["changed"] and refused["reachable"] is False, refused
        assert "force=True" in refused["error"] and "start-isaacsim.ps1" in refused["error"]
        assert "WinError 10061" in refused["health"]["error"]
        assert service.bridge_info() == before and factory.created == [] and "capabilities" not in refused

        forced = switch_scene_target(
            service, "isaac", force=True, environ=LIVE_MODE, probe=_down, bridge_factory=factory
        )
    assert forced["ok"] and forced["changed"] and forced["forced"] and forced["reachable"] is False, forced
    assert forced["current"]["endpoint"] == ISAAC and "capabilities" not in forced
    assert any("did not answer" in warning for warning in forced["warnings"])
    assert any("force=True" in warning for warning in service.backend_info["warnings"])

    # Once the bridge answers, switching again keeps the same bridge and clears the forced-switch warning.
    bridge = service._bridge
    with _NoSockets():
        later = switch_scene_target(service, "isaac", environ=LIVE_MODE, probe=_up(), bridge_factory=factory)
    assert later["ok"] and not later["changed"] and service._bridge is bridge and len(factory.created) == 1, later
    assert service.backend_info["warnings"] == [] and "answered /health" in service.backend_info["reason"]
    assert later["capabilities"]["endpoint"] == ISAAC


def test_test_mode_never_opens_a_socket():
    service = SceneMCPService(FakeSceneBridge())
    with _NoSockets():
        result = switch_scene_target(service, "composer", environ=TEST_MODE)
        assert not result["ok"] and result["health"]["probed"] is False and TEST_ENV in result["error"], result
        assert service.bridge_info()["bridge"] == "FakeSceneBridge"

        forced = switch_scene_target(service, "composer", force=True, environ=TEST_MODE)
        assert forced["ok"] and forced["current"]["bridge"] == "HttpSceneBridge", forced
        assert forced["current"]["endpoint"] == COMPOSER and "capabilities" not in forced

        # Reachable by an injected probe, but the real HttpSceneBridge is never asked for its capabilities.
        isaac = switch_scene_target(service, "isaac", environ=TEST_MODE, probe=_up())
        assert isaac["ok"] and isaac["current"]["bridge"] == "HttpSceneBridge" and "capabilities" not in isaac
        assert any("opens no sockets" in warning for warning in isaac["warnings"])

        back = switch_scene_target(service, "fake", environ=TEST_MODE)
        assert back["ok"] and back["current"]["bridge"] == "FakeSceneBridge" and back["capabilities"]["fake_scene"]


def test_switch_to_fake_keeps_an_existing_fake_scene_and_its_edits():
    service = SceneMCPService(_LiveStandIn(COMPOSER, "ComposerStage"))
    with _NoSockets():
        result = switch_scene_target(service, "fake", environ=LIVE_MODE)
        assert result["ok"] and result["changed"] and result["reachable"] is None, result
        assert result["current"]["mode"] == "fake" and result["current"]["endpoint"] is None
        assert result["capabilities"]["fake_scene"] is True
        assert any("in-memory" in warning for warning in service.backend_info["warnings"])
        created = service.create_prim("/World/Kept", dry_run=False)
        assert created.ok
        again = switch_scene_target(service, "in-memory", environ=LIVE_MODE)
        assert again["ok"] and not again["changed"] and again["target"] == "fake", again
        assert service.inspect_prim("/World/Kept")["path"] == "/World/Kept"


def test_unknown_targets_and_aliases():
    service = SceneMCPService(FakeSceneBridge())
    before = service.bridge_info()
    with _NoSockets():
        for bad in ("blender", "", None, 9910, "http://127.0.0.1:9910"):
            result = switch_scene_target(service, bad, environ=LIVE_MODE, probe=_up())
            assert not result["ok"] and "Unknown scene target" in result["error"], result
            assert sorted(result["targets"]) == ["composer", "fake", "isaac"]
        assert service.bridge_info() == before
        factory = _factory({ISAAC: "IsaacStage", COMPOSER: "ComposerStage"})
        isaac = switch_scene_target(service, "  Isaac   Sim ", environ=LIVE_MODE, probe=_up(), bridge_factory=factory)
        assert isaac["ok"] and isaac["target"] == "isaac"
        composer = switch_scene_target(service, "USD Composer", environ=LIVE_MODE, probe=_up(), bridge_factory=factory)
        assert composer["ok"] and composer["target"] == "composer" and composer["current"]["endpoint"] == COMPOSER


def test_dry_run_probes_but_never_swaps():
    service = SceneMCPService(FakeSceneBridge())
    before = service.bridge_info()
    probed: list = []
    factory = _factory({COMPOSER: "ComposerStage"})
    with _NoSockets():
        preview = switch_scene_target(
            service, "composer", dry_run=True, environ=LIVE_MODE, probe=_up(probed), bridge_factory=factory
        )
        fake_preview = switch_scene_target(service, "fake", dry_run=True, environ=LIVE_MODE)
    assert preview["ok"] and preview["dry_run"] and preview["would_change"] and not preview["changed"], preview
    assert probed == [(COMPOSER, 0.3)] and factory.created == [] and "capabilities" not in preview
    assert fake_preview["ok"] and fake_preview["would_change"] is False
    assert service.bridge_info() == before


def test_switching_to_the_current_endpoint_keeps_the_bridge():
    service = SceneMCPService(FakeSceneBridge())
    factory = _factory({COMPOSER: "ComposerStage"})
    with _NoSockets():
        first = switch_scene_target(service, "composer", environ=LIVE_MODE, probe=_up(), bridge_factory=factory)
        bridge = service._bridge
        second = switch_scene_target(service, "composer", environ=LIVE_MODE, probe=_up(), bridge_factory=factory)
    assert first["changed"] and not second["changed"] and second["ok"], second
    assert service._bridge is bridge and len(factory.created) == 1
    assert second["capabilities"]["endpoint"] == COMPOSER


def test_a_failing_or_odd_probe_counts_as_unreachable():
    service = SceneMCPService(FakeSceneBridge())

    def broken(endpoint: str, timeout: float):
        raise OSError("probe exploded")

    with _NoSockets():
        result = switch_scene_target(service, "composer", environ=LIVE_MODE, probe=broken)
        assert not result["ok"] and "probe exploded" in result["health"]["error"], result
        result = switch_scene_target(service, "composer", environ=LIVE_MODE, probe=lambda endpoint, timeout: "yes")
        assert not result["ok"] and result["reachable"] is False
        result = switch_scene_target(service, "composer", environ=LIVE_MODE, probe=lambda endpoint, timeout: False)
        assert not result["ok"] and result["health"] == {"reachable": False, "probed": True}
    assert service.bridge_info()["bridge"] == "FakeSceneBridge"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


def test_probe_scene_health_only_targets_loopback_and_parses_health():
    calls: list = []

    def fake_urlopen(request, timeout: float):
        # The probe sends a Request with the bridge headers (JSON type, never Origin, the token when set).
        url = getattr(request, "full_url", request)
        headers = {key.lower(): value for key, value in getattr(request, "header_items", lambda: [])()}
        assert "origin" not in headers and headers.get("content-type") == "application/json", headers
        calls.append((url, timeout))
        if not bodies:
            raise URLError("refused")
        return _Response(bodies.pop(0))

    with _NoSockets():
        for remote in ("http://10.0.0.1:9910", "http://localhost:9910", "https://127.0.0.1:9910"):
            assert probe_scene_health(remote) == {
                "reachable": False,
                "probed": False,
                "error": "only http://127.0.0.1:<port> endpoints are probed",
            }
        saved = runtime.urlopen
        runtime.urlopen = fake_urlopen
        try:
            bodies = [b'{"ok": true, "status": "running", "endpoint": "http://127.0.0.1:9910"}', b'{"ok": false}', b"<html>"]
            assert probe_scene_health(COMPOSER + "/") == {"reachable": True, "probed": True, "status": "running"}
            assert probe_scene_health(COMPOSER)["reachable"] is False
            assert probe_scene_health(COMPOSER)["reachable"] is False  # not JSON
            down = probe_scene_health(ISAAC)
        finally:
            runtime.urlopen = saved
    assert down["reachable"] is False and down["probed"] and "URLError" in down["error"]
    assert calls[0] == (COMPOSER + "/health", 0.3) and calls[-1] == (ISAAC + "/health", 0.3)


def test_swap_bridge_validates_and_reports_bridge_info():
    service = SceneMCPService(FakeSceneBridge())
    before = service.bridge_info()
    assert before["bridge"] == "FakeSceneBridge" and before["endpoint"] is None and before["mode"] == "direct"
    for bad in (None, object(), _LiveStandIn.capabilities):
        try:
            service.swap_bridge(bad)  # type: ignore[arg-type]
        except TypeError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted as a bridge")
    assert service.bridge_info() == before
    live = _LiveStandIn(COMPOSER, "ComposerStage")
    previous = service.swap_bridge(live)
    assert previous == before
    info = service.bridge_info()
    assert info["bridge"] == "_LiveStandIn" and info["endpoint"] == COMPOSER
    assert info["mode"] == "direct" and info["reason"] == "Bridge swapped in with swap_bridge"
    assert service.scene_summary()["stage_name"] == "ComposerStage" and service.live_capabilities()["ok"]
    service.swap_bridge(FakeSceneBridge(), {"mode": "fake", "bridge": "FakeSceneBridge", "warnings": ["w"]})
    assert service.bridge_info()["mode"] == "fake" and service.bridge_info()["warnings"] == ["w"]
    info = service.bridge_info()
    info["warnings"].append("mutated copy")
    assert service.bridge_info()["warnings"] == ["w"]
    json.dumps(service.bridge_info())


def test_router_built_directly_follows_a_swap_too():
    service = SceneMCPService(FakeSceneBridge())
    router = OmniverseRouter(service)
    with _NoSockets():
        result = switch_scene_target(
            router, "composer", environ=LIVE_MODE, probe=_up(), bridge_factory=_factory({COMPOSER: "ComposerStage"})
        )
    assert result["ok"] and router._scene is service
    payload = asyncio.run(router.handle("inspect /World/Props/Cube")).payload
    assert payload["ok"] and payload["result"]["path"] == "/World/Props/Cube"
    assert router.scene_summary()["stage_name"] == "ComposerStage"


def test_loopback_openers_never_follow_a_redirect_to_another_endpoint():
    """A 127.0.0.1 listener that answers 3xx must not send the router on to another host/port (review-12 S4)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from omniverse_scene_mcp.http_bridge import HttpSceneBridge

    hits: list[str] = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b'{"ok": true, "status": "elsewhere", "result": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *_args: object) -> None:
            return None

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)]
    location = f"http://127.0.0.1:{servers[0].server_port}/elsewhere"

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args: object) -> None:
            return None

    servers.append(ThreadingHTTPServer(("127.0.0.1", 0), Redirect))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    endpoint = f"http://127.0.0.1:{servers[1].server_port}"
    try:
        health = probe_scene_health(endpoint, timeout=5.0)
        assert health["reachable"] is False and "302" in health["error"], health
        service = SceneMCPService(FakeSceneBridge())
        result = switch_scene_target(service, "composer", environ={"OMNIVERSE_COMPOSER_SCENE_ENDPOINT": endpoint})
        assert result["endpoint"] == endpoint and result["ok"] is False and result["changed"] is False, result
        assert result["reachable"] is False
        try:
            HttpSceneBridge(endpoint).ping()
        except ValueError as exc:
            assert "302" in str(exc)
        else:
            raise AssertionError("a redirected RPC must fail")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert hits == []
