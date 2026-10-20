import os

os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import contextlib
import json
import subprocess
import tempfile
from pathlib import Path

from omniverse_scene_mcp import health

SECRETS = ("nvapi-AbC123xyz_SECRET", "eyJhbGciOi.payload.sig", "sk-live-123", "hunter2", "s3cr3t", "tok-999", "zzTopKey")
SECRET_LINES = [
    f"loaded key {SECRETS[0]} from env",
    f"Authorization: Bearer {SECRETS[1]}",
    f"retrying with api_key={SECRETS[2]}&x=1",
    f'login password="{SECRETS[3]}" ok',
    f"client_secret: '{SECRETS[4]}'",
    f'{{"access_token": "{SECRETS[5]}"}}',
    f"token=Bearer {SECRETS[6]}",
]


class _Probe:
    """Injected TCP probe: reachable hosts:ports in `up`, everything else refuses."""

    def __init__(self, up=()):
        self.up = set(up)
        self.calls = []

    def __call__(self, host, port, timeout):
        self.calls.append((host, port, timeout))
        if (host, port) not in self.up:
            raise ConnectionRefusedError(f"refused {host}:{port}")


def _refuse(*args, **kwargs):
    raise AssertionError("health checks must never run processes or open real sockets")


@contextlib.contextmanager
def _nothing_executes():
    patched = [
        (subprocess, "Popen"),
        (subprocess, "run"),
        (os, "system"),
        (os, "kill"),
        (health.socket, "create_connection"),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]
    for owner, name in patched:
        setattr(owner, name, _refuse)
    try:
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


def _workspace(folder, pid_text=None, log_lines=None):
    study = Path(folder) / "isaacsim-study"
    study.mkdir()
    if pid_text is not None:
        (study / "router.pid").write_text(pid_text, encoding="utf-8")
    if log_lines is not None:
        (study / "router.stderr.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return study


def test_redact_masks_known_secret_shapes():
    redacted = [health.redact(line) for line in SECRET_LINES]
    joined = "\n".join(redacted)
    for secret in SECRETS:
        assert secret not in joined, secret
    assert redacted[0] == "loaded key nvapi-[REDACTED] from env"
    assert redacted[1] == "Authorization: Bearer [REDACTED]"
    assert redacted[2] == "retrying with api_key=[REDACTED]&x=1"
    assert health.redact("plain progress line 42") == "plain progress line 42"


def test_redact_masks_userinfo_auth_schemes_flags_cookies_and_encoded_keys():
    samples = {
        "Authorization: Basic dXNlcjpTRUNSRVQ2": "dXNlcjpTRUNSRVQ2",
        "connecting to http://admin:SECRET7@127.0.0.1:9903/mcp": "SECRET7",
        "kit.exe --password SECRET8 --verbose": "SECRET8",
        "Incorrect API key provided: sk-proj-SECRET9xyz": "SECRET9",
        "Authorization: Token SECRET10": "SECRET10",
        "pwd=SECRET11": "SECRET11",
        "passphrase: SECRET12": "SECRET12",
        "private_key=SECRET13": "SECRET13",
        "GET /cb?access%5Ftoken%3DSECRET14": "SECRET14",
        "Set-Cookie: session=SECRET15; Path=/": "SECRET15",
        "probe http://u:SECRET18@localhost:9902/mcp": "SECRET18",
    }
    for line, secret in samples.items():
        assert secret not in health.redact(line), line
    assert health.redact("http://admin:SECRET7@127.0.0.1:9903/mcp") == "http://[REDACTED]@127.0.0.1:9903/mcp"
    assert health.redact("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"
    for plain in ("http://127.0.0.1:9910/rpc", "file:///C:/scene.usda", "loaded 3 layers in 0.2s"):
        assert health.redact(plain) == plain
    report = health.probe_endpoint("http://u:SECRET18@127.0.0.1:9902/mcp", probe=_Probe(up=set()))
    assert "SECRET18" not in report["endpoint"]


def test_probe_endpoint_only_probes_allowed_loopback_hosts():
    probe = _Probe(up={("localhost", 9902), ("127.0.0.1", 9910)})
    remote = health.probe_endpoint("http://example.com/mcp", probe=probe)
    private = health.probe_endpoint("http://10.0.0.5:9910", probe=probe)
    ipv6 = health.probe_endpoint("http://[::1]:9910", probe=probe)
    knowledge = health.probe_endpoint("http://LOCALHOST:9902/mcp", health.KNOWLEDGE_HOSTS, probe=probe)
    scene_localhost = health.probe_endpoint("http://localhost:9910", health.SCENE_HOSTS, probe=probe)
    scene = health.probe_endpoint("127.0.0.1:9910", health.SCENE_HOSTS, probe=probe)

    assert remote["probed"] is False and "skipped" in remote
    assert private["probed"] is False and ipv6["probed"] is False
    assert scene_localhost["probed"] is False
    assert knowledge["reachable"] is True and knowledge["host"] == "localhost"
    assert scene["reachable"] is True and scene["port"] == 9910
    assert probe.calls == [("localhost", 9902, 0.3), ("127.0.0.1", 9910, 0.3)]


def test_probe_endpoint_reports_unreachable_and_passes_timeout():
    probe = _Probe()
    result = health.probe_endpoint("http://127.0.0.1:9903/mcp", timeout=0.05, probe=probe)
    assert result["probed"] is True and result["reachable"] is False
    assert "refused" in result["error"]
    assert result["timeout_ms"] == 50
    assert probe.calls == [("127.0.0.1", 9903, 0.05)]

    assert health.probe_endpoint(None, probe=probe)["probed"] is False
    assert health.probe_endpoint("http://127.0.0.1:notaport", probe=probe)["probed"] is False
    assert len(probe.calls) == 1


def test_tcp_connect_opens_and_closes_without_sending():
    events = []

    class _Connection:
        def __enter__(self):
            events.append("open")
            return self

        def __exit__(self, *exc):
            events.append("close")
            return False

        def send(self, *args):
            raise AssertionError("no bytes may be sent")

        sendall = send

    def fake_create_connection(address, timeout=None):
        events.append((address, timeout))
        return _Connection()

    original = health.socket.create_connection
    health.socket.create_connection = fake_create_connection
    try:
        health.tcp_connect("127.0.0.1", 9910, health.PROBE_TIMEOUT)
    finally:
        health.socket.create_connection = original
    assert events == [(("127.0.0.1", 9910), 0.3), "open", "close"]


def test_router_health_offline_report_with_redacted_tail():
    log = [f"line {index}" for index in range(30)] + SECRET_LINES
    scene_info = {
        "mode": "http_env",
        "bridge": "HttpSceneBridge",
        "endpoint": "http://127.0.0.1:9911",
        "reason": "OMNIVERSE_SCENE_HTTP_ENDPOINT is set",
        "silent_fallback": False,
        "warnings": [],
    }
    backends = [
        {"target": "usd", "backend": "usd", "endpoint": "http://127.0.0.1:9903/mcp", "enabled": True},
        {"target": "kit", "backend": "kit", "endpoint": "http://localhost:9902/mcp", "enabled": True},
        {"target": "bowerbot", "backend": "bowerbot", "endpoint": "http://127.0.0.1:8181/mcp", "enabled": True},
        {"target": "omni_ui", "backend": "omni_ui", "endpoint": "http://example.com/mcp?api_key=zzTopKey", "enabled": True},
        {"target": "isaac_sim", "backend": "isaac_sim", "endpoint": "", "enabled": False},
    ]
    probe = _Probe(up={("127.0.0.1", 9911), ("127.0.0.1", 9903)})
    with tempfile.TemporaryDirectory() as folder:
        _workspace(folder, pid_text="4242\r\n", log_lines=log)
        with _nothing_executes():
            report = health.router_health(
                scene_info, backends, workspace_root=folder, probe=probe, current_pid=4242, parent_pid=1
            )

    text = json.dumps(report)
    for secret in SECRETS:
        assert secret not in text, secret
    assert report["ok"] is True
    assert report["scene"]["probe"]["reachable"] is True
    by_target = {entry["target"]: entry for entry in report["knowledge"]}
    assert by_target["usd"]["probe"]["reachable"] is True
    assert by_target["kit"]["probe"]["reachable"] is False
    assert by_target["bowerbot"]["probe"]["reachable"] is False
    assert by_target["omni_ui"]["probe"]["probed"] is False
    assert by_target["isaac_sim"]["probe"]["skipped"] == "backend disabled"
    assert ("example.com", 80, 0.3) not in probe.calls
    assert report["router_pid_file"]["matches"] is True
    assert report["router_pid_file"]["recorded_pid"] == 4242
    tail = report["stderr_tail"]["lines"]
    assert len(tail) == 20
    assert tail[-1] == "token=[REDACTED] [REDACTED]"
    assert tail[0] == "line 17"
    assert report["actions_taken"] == []
    assert report["suggestions"] and all(s.startswith("needs your approval: ") for s in report["suggestions"])
    assert any("Bowerbot on 8181" in s for s in report["suggestions"])


def test_router_health_flags_silent_fallback_and_pid_mismatch_without_acting():
    fallback = {
        "mode": "fake_fallback",
        "bridge": "FakeSceneBridge",
        "endpoint": None,
        "reason": "OMNIVERSE_SCENE_MCP_MODE is unset",
        "silent_fallback": True,
        "warnings": ["fake"],
    }
    with tempfile.TemporaryDirectory() as folder:
        _workspace(folder, pid_text="999")
        with _nothing_executes():
            report = health.router_health(fallback, [], workspace_root=folder, current_pid=4242, parent_pid=1)
    assert report["ok"] is False
    assert report["scene"]["probe"]["probed"] is False
    assert report["router_pid_file"]["matches"] is False
    assert any("999" in issue and "4242" in issue for issue in report["issues"])
    assert any("fell back" in issue for issue in report["issues"])
    assert len(report["suggestions"]) == 2
    assert all(s.startswith("needs your approval: ") for s in report["suggestions"])
    assert report["stderr_tail"]["present"] is False
    json.dumps(report)


def test_router_health_unreachable_scene_bridge():
    scene_info = {"mode": "http_autodetect", "bridge": "HttpSceneBridge", "endpoint": "http://127.0.0.1:9910"}

    def timeout_probe(host, port, timeout):
        raise TimeoutError("timed out")

    with tempfile.TemporaryDirectory() as folder:
        with _nothing_executes():
            report = health.router_health(scene_info, None, workspace_root=folder, probe=timeout_probe)
    assert report["ok"] is False
    assert report["scene"]["probe"]["error"] == "timed out"
    assert any("9910" in s and s.startswith("needs your approval") for s in report["suggestions"])
    assert report["router_pid_file"]["present"] is False


def test_router_health_without_inputs_is_safe_and_serializable():
    with tempfile.TemporaryDirectory() as folder:
        with _nothing_executes():
            report = health.router_health(workspace_root=folder)
    assert report["scene"]["mode"] == "unknown"
    assert report["knowledge"] == []
    assert report["router"]["pid"] == os.getpid()
    assert report["probe_timeout_ms"] == 300
    json.dumps(report)


def test_pid_status_variants():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "router.pid"
        assert health.pid_status(path, 10, 1)["present"] is False

        path.write_bytes("24664\r\n".encode("utf-16"))
        assert health.pid_status(path, 24664, 1)["matches"] is True

        path.write_bytes(b"\xef\xbb\xbf77\n")
        parent = health.pid_status(path, 10, 77)
        assert parent["matches"] is True and "parent" in parent["note"]

        path.write_text("not a pid", encoding="utf-8")
        garbage = health.pid_status(path, 10, 1)
        assert garbage["matches"] is None and "error" in garbage

        path.write_text("12\n", encoding="utf-8")
        assert health.pid_status(path, 13, 1)["matches"] is False


def test_stderr_tail_reads_only_the_end_of_large_logs():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "router.stderr.log"
        filler = "x" * 1000
        long_secret = "y" * 500 + " api_key=hidden"
        path.write_text("\n".join([filler] * 100 + ["short", "password=hunter2 " + "z" * 500, long_secret]), encoding="utf-8")
        tail = health.stderr_tail(path, 3)
        assert health.stderr_tail(Path(folder) / "missing.log")["present"] is False
        assert health.stderr_tail(path, 0)["lines"] == []
    assert tail["lines"][0] == "short"
    assert tail["lines"][1] == "password=[REDACTED] " + "z" * 383
    assert tail["lines"][2] == "y" * 400
    assert "hunter2" not in json.dumps(tail)
    assert tail["truncated_lines"] == 2
