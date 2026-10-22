"""Localhost CSRF / DNS-rebinding guard of the extended scene bridge, over real HTTP on an ephemeral loopback port.

Every server here is built by kit_rpc.make_extended_server_class from a FAKE minimal base server (or, when the
untracked live_scene_control.py can be loaded with a stand-in omni.kit.app, from the real base class) and bound to
127.0.0.1:0, so the live bridges (9909/9910/9911), the router (9905), Kit and every user scene are never touched.
Fixture-free: run by tools/run_tests.py, pytest, or the scratch runner.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omniverse_scene_mcp import http_bridge, kit_rpc
from omniverse_scene_mcp.http_bridge import BridgeForbiddenError, HttpSceneBridge

_LIVE_STACK_PORTS = {9905, 9909, 9910, 9911}
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


class _PingBridge:
    def ping(self) -> str:
        return "ok"

    def capabilities(self) -> dict:
        return {"bridge": "PingBridge"}


class _FakeBaseServer:
    """The parts of live_scene_control.LiveSceneControlServer the guard wraps: _build_server/_build_handler/_submit.

    _submit dispatches synchronously (no Kit pump) and records every payload that reached it.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._bridge = _PingBridge()
        self.dispatched: list[dict] = []

    @property
    def endpoint(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _build_server(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((self._host, self._port), self._build_handler())
        server.scene_control_server = self
        server.daemon_threads = True
        return server

    def _build_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):  # same shape as live_scene_control's Handler
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/rpc":
                    self._send_json(404, {"ok": False, "message": "Unknown endpoint"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except Exception as exc:
                    self._send_json(400, {"ok": False, "message": f"Invalid JSON payload: {exc}"})
                    return
                try:
                    result = parent._submit(payload)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": str(exc)})
                    return
                self._send_json(200, result)

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/health":
                    self._send_json(404, {"ok": False, "message": "Unknown endpoint"})
                    return
                self._send_json(200, {"ok": True, "status": "running", "endpoint": parent.endpoint})

            def log_message(self, *_args) -> None:
                return

            def _send_json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def _submit(self, payload):
        self.dispatched.append(payload)
        return self._dispatch(payload)

    def _dispatch(self, payload):
        method = str(payload.get("method", "")).strip().lower()
        if method == "ping":
            return {"ok": True, "result": {"status": self._bridge.ping()}}
        raise ValueError(f"Unsupported live scene method: {method}")


@contextlib.contextmanager
def _serving(base_cls=_FakeBaseServer):
    """(server object, port) of the extended class built from base_cls, serving on 127.0.0.1:<ephemeral>."""
    server = kit_rpc.make_extended_server_class(base_cls)()
    httpd = server._build_server()
    port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1" and port not in _LIVE_STACK_PORTS
    server._port = port
    thread = threading.Thread(target=httpd.serve_forever, name="bridge-guard-test", daemon=True)
    thread.start()
    try:
        yield server, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _env_token(value):
    previous = os.environ.get(kit_rpc.BRIDGE_TOKEN_ENV)
    if value is None:
        os.environ.pop(kit_rpc.BRIDGE_TOKEN_ENV, None)
    else:
        os.environ[kit_rpc.BRIDGE_TOKEN_ENV] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(kit_rpc.BRIDGE_TOKEN_ENV, None)
        else:
            os.environ[kit_rpc.BRIDGE_TOKEN_ENV] = previous


def _raw(port, method="POST", path="/rpc", body=None, headers=None, host=None):
    """(status, decoded JSON body) of one hand-made request; host replaces the Host header urllib would send."""
    if body is None and method == "POST":
        body = json.dumps({"method": "ping", "params": {}}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8") or "{}")
    finally:
        connection.close()


_JSON = {"Content-Type": "application/json"}


def _assert_refused(server, status, body, reason):
    assert status == 403, (status, body)
    assert body["ok"] is False and body["forbidden"] is True and body["reason"] == reason, body
    assert server.dispatched == [], server.dispatched


def test_router_client_requests_pass_the_guard():
    with _env_token(None), _serving() as (server, port):
        bridge = HttpSceneBridge(f"http://127.0.0.1:{port}")
        assert bridge.ping() == "ok"
        assert bridge.capabilities()["version"] == kit_rpc.RPC_VERSION
        assert HttpSceneBridge(f"http://localhost:{port}").ping() == "ok"
        assert [item["method"] for item in server.dispatched] == ["ping", "capabilities", "ping"]
        assert _raw(port, "GET", "/health")[0] == 200
        assert _raw(port, headers={"Content-Type": "application/json; charset=utf-8"})[0] == 200


def test_router_client_never_sends_origin_and_always_sends_json():
    with _env_token(None):
        assert http_bridge.bridge_request_headers() == {"Content-Type": "application/json"}
    with _env_token("s3cret-value"):
        headers = http_bridge.bridge_request_headers()
        assert headers == {"Content-Type": "application/json", kit_rpc.BRIDGE_TOKEN_HEADER: "s3cret-value"}
    assert not any(name.lower() == "origin" for name in headers)


def test_browser_style_requests_are_refused_and_never_dispatched():
    with _env_token(None), _serving() as (server, port):
        # The CSRF shape: fetch(..., {mode: "no-cors", body: JSON text}) sends text/plain plus Origin and Sec-Fetch-*.
        attack = {"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://evil.example",
                  "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
        _assert_refused(server, *_raw(port, headers=attack), "origin")
        _assert_refused(server, *_raw(port, headers={**_JSON, "Origin": "null"}), "origin")
        _assert_refused(server, *_raw(port, headers={**_JSON, "Origin": f"http://127.0.0.1:{port}"}), "origin")
        _assert_refused(server, *_raw(port, headers={**_JSON, "Sec-Fetch-Site": "same-origin"}), "browser")
        _assert_refused(server, *_raw(port, headers={**_JSON, "Sec-Fetch-Mode": "cors"}), "browser")
        _assert_refused(server, *_raw(port, "GET", "/health", headers={"Origin": "https://evil.example"}), "origin")
        _assert_refused(server, *_raw(port, "GET", "/health", headers={"Sec-Fetch-Dest": "empty"}), "browser")


def test_non_json_content_types_are_refused():
    with _env_token(None), _serving() as (server, port):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                             "application/jsonp", "application/json-patch+json"):
            _assert_refused(server, *_raw(port, headers={"Content-Type": content_type}), "content_type")
        _assert_refused(server, *_raw(port), "content_type")  # no Content-Type at all
        _assert_refused(server, *_raw(port, headers={"Content-Type": "application/json"} | {"content-type": "text/plain"}),
                        "content_type")


def test_foreign_host_headers_are_refused():
    with _env_token(None), _serving() as (server, port):
        for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1 if port < 65535 else 1}",
                     "127.0.0.1", f"localhost.evil.example:{port}", f"0.0.0.0:{port}", ""):
            _assert_refused(server, *_raw(port, headers=_JSON, host=host), "host")
        _assert_refused(server, *_raw(port, "GET", "/health", host=f"rebind.evil.example:{port}"), "host")
        assert _raw(port, headers=_JSON, host=f"LOCALHOST:{port}")[0] == 200
        assert len(server.dispatched) == 1


def test_shared_token_is_required_when_set_in_kit():
    with _env_token("correct-horse"), _serving() as (server, port):
        header = kit_rpc.BRIDGE_TOKEN_HEADER
        _assert_refused(server, *_raw(port, headers=_JSON), "token")
        _assert_refused(server, *_raw(port, headers={**_JSON, header: "wrong"}), "token")
        _assert_refused(server, *_raw(port, headers={**_JSON, header: "correct-horse-battery"}), "token")
        _assert_refused(server, *_raw(port, headers={**_JSON, header: ""}), "token")
        status, body = _raw(port, headers={**_JSON, header: "wrong"})
        assert "correct-horse" not in json.dumps(body)  # the refusal never echoes the token
        assert _raw(port, "GET", "/health")[0] == 200  # the router's /health probes stay token-free
        assert server.dispatched == []
        assert _raw(port, headers={**_JSON, header: "correct-horse"})[0] == 200
        assert HttpSceneBridge(f"http://127.0.0.1:{port}").ping() == "ok"  # the router sends its own env token
        assert len(server.dispatched) == 2


def test_a_403_reaches_the_router_as_a_structured_error():
    with _env_token("kit-only-token"), _serving() as (server, port):
        bridge = HttpSceneBridge(f"http://127.0.0.1:{port}")
        original = http_bridge.bridge_request_headers
        http_bridge.bridge_request_headers = lambda: {"Content-Type": "application/json"}  # router without the token
        try:
            result = bridge.capabilities()
            try:
                bridge.ping()
            except BridgeForbiddenError as exc:
                error = exc
            else:  # pragma: no cover - the guard must refuse
                raise AssertionError("ping without the token was not refused")
        finally:
            http_bridge.bridge_request_headers = original
        assert result["ok"] is False and result["forbidden"] is True and result["status"] == 403, result
        assert result["reason"] == "token" and result["method"] == "capabilities"
        assert kit_rpc.BRIDGE_TOKEN_ENV in result["message"] and "kit-only-token" not in json.dumps(result)
        assert isinstance(error, ValueError) and error.result["reason"] == "token" and "HTTP 403" in str(error)
        assert server.dispatched == []


def test_guard_wraps_the_handler_once_and_keeps_the_base_handler():
    server = kit_rpc.make_extended_server_class(_FakeBaseServer)()
    handler = server._build_handler()
    base_handler = _FakeBaseServer._build_handler(server)
    assert handler._omniverse_bridge_guarded is True and handler.__name__ == "GuardedHandler"
    assert issubclass(handler, BaseHTTPRequestHandler) and handler.__mro__[1].__name__ == base_handler.__name__
    assert kit_rpc.guard_handler_class(handler) is handler


def _load_real_base():
    """live_scene_control.LiveSceneControlServer with a stand-in omni.kit.app (restored at once), or None."""
    if not _LIVE_SCENE_CONTROL.is_file():
        return None
    names = ("omni", "omni.kit", "omni.kit.app")
    saved = {name: sys.modules.get(name) for name in names}
    if saved["omni.kit.app"] is None:
        omni = saved["omni"] or types.ModuleType("omni")
        kit = saved["omni.kit"] or types.ModuleType("omni.kit")
        app = types.ModuleType("omni.kit.app")
        app.get_app = lambda: None
        kit.app = app
        omni.kit = kit
        sys.modules.update({"omni": omni, "omni.kit": kit, "omni.kit.app": app})
    try:
        spec = importlib.util.spec_from_file_location("bridge_guard_live_scene_control", _LIVE_SCENE_CONTROL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module.LiveSceneControlServer


def test_real_live_scene_control_handler_is_guarded_when_the_file_is_present():
    """The real base Handler (untracked submodule file) behind the guard; returns early when it is absent."""
    real = _load_real_base()
    if real is None:
        return

    class SyncRealBase(real):
        def __init__(self) -> None:  # no LiveKitSceneBridge, no Kit pump: dispatch synchronously
            self._host, self._port = "127.0.0.1", 0
            self._bridge = _PingBridge()
            self.dispatched = []

        def _submit(self, payload):
            self.dispatched.append(payload)
            return self._dispatch(payload)

    with _env_token(None), _serving(SyncRealBase) as (server, port):
        assert HttpSceneBridge(f"http://127.0.0.1:{port}").ping() == "ok"
        assert [item["method"] for item in server.dispatched] == ["ping"]
        server.dispatched.clear()
        _assert_refused(server, *_raw(port, headers={"Content-Type": "text/plain", "Origin": "https://x.example"}),
                        "origin")
        _assert_refused(server, *_raw(port, headers={"Content-Type": "text/plain"}), "content_type")
        _assert_refused(server, *_raw(port, headers=_JSON, host=f"evil.example:{port}"), "host")
