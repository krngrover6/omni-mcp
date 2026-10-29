"""doctor: read-only connection diagnosis with injected ports, processes, bridge replies and file times."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

from omniverse_scene_mcp import doctor
from omniverse_scene_mcp.bridge import RPC_VERSION

ROUTER_CMD = '"C:\\Python312\\python.exe" -m omniverse_scene_mcp serve --transport streamable-http --host 127.0.0.1 --port 9905'
ISAAC_CMD = 'C:\\Users\\me\\Omniverse\\isaacsim-6.1.0\\kit\\kit.exe isaacsim.exp.full.kit'
COMPOSER_CMD = 'C:\\Users\\me\\kit-app-template\\_build\\release\\kit\\kit.exe my_company.my_usd_composer.kit'
T0 = datetime(2026, 10, 27, 9, 0).timestamp()


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).astimezone().isoformat()


def _run(tmp: Path, *, ports, procs=None, caps=None, report=None, changed=T0, reloads=None, logs=None, inside=False):
    (tmp / "isaacsim-study").mkdir(exist_ok=True)
    return doctor.diagnose(
        workspace=tmp, ports=ports, procs=procs or {}, capabilities=caps or (lambda port: None),
        router_health_report=report, source_changed=changed, reloads=reloads or (lambda root, key: None),
        logs=logs, inside_router=inside)


def _part(result, name):
    return next(p for p in result["parts"] if p["part"] == name)


def _health(endpoint="http://127.0.0.1:9911"):
    return {"scene": {"endpoint": endpoint, "mode": "http_env", "probe": {"reachable": True}}}


def test_everything_closed_says_start_the_router():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={})
        assert result["verdict"] == "needs_attention"
        assert _part(result, "router")["status"] == "offline"
        assert _part(result, "isaac_bridge")["status"] == "offline"
        assert result["next_steps"][0].startswith("Start the router")


def test_ready_when_router_and_a_bridge_are_current():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "isaacsim-study").mkdir()
        (root / "isaacsim-study" / "router.pid").write_text("100", encoding="utf-8")
        result = _run(root, ports={9905: [100], 9911: [200]},
                      procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 + 60)},
                             200: {"command_line": ISAAC_CMD, "started": _iso(T0 - 600)}},
                      caps=lambda port: {"version": RPC_VERSION}, report=_health(),
                      reloads=lambda r, key: {"finished": T0 + 30, "rpc_version": RPC_VERSION})
        assert result["verdict"] == "ready", result
        router = _part(result, "router")
        assert router["status"] == "healthy" and router["pid_record"] == "ok" and router["points_at"] == "isaac"
        assert _part(result, "isaac_bridge")["status"] == "healthy"
        assert result["next_steps"] == []


def test_old_bridge_version_or_code_newer_than_its_reload_needs_a_script_editor_reload():
    with tempfile.TemporaryDirectory() as tmp:
        base = dict(ports={9905: [100], 9910: [300]},
                    procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 + 60)}, 300: {"command_line": COMPOSER_CMD}},
                    report=_health("http://127.0.0.1:9910"))
        old = _run(Path(tmp), caps=lambda port: {"version": "tier5-1"}, **base)
        part = _part(old, "composer_bridge")
        assert part["status"] == "needs_reload" and "tier5-1" in part["plain"]
        assert any("composer-study/reload_composer_bridge.py" in s for s in old["next_steps"])
        stale = _run(Path(tmp), caps=lambda port: {"version": RPC_VERSION},
                     reloads=lambda r, key: {"finished": T0 - 3600, "rpc_version": RPC_VERSION}, **base)
        assert _part(stale, "composer_bridge")["status"] == "needs_reload"
        assert "after it was last loaded" in _part(stale, "composer_bridge")["plain"]
        # Seen live: the headline said "open Isaac Sim or Composer" although the app was open and only needed a reload.
        assert stale["verdict"] == "needs_reload" and "reload" in stale["headline"], stale["headline"]
        assert "open Isaac Sim or Composer" not in stale["headline"]


def test_app_launched_after_the_code_changed_is_current_without_a_reload():
    # Seen live: Isaac restarted after a PC crash loads the new code, though its last Script Editor reload is older.
    with tempfile.TemporaryDirectory() as tmp:
        base = dict(ports={9905: [100], 9911: [200]}, caps=lambda port: {"version": RPC_VERSION}, report=_health(),
                    reloads=lambda r, key: {"finished": T0 - 3600, "rpc_version": RPC_VERSION})
        fresh = _run(Path(tmp), procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 + 60)},
                                       200: {"command_line": ISAAC_CMD, "started": _iso(T0 + 120)}}, **base)
        assert _part(fresh, "isaac_bridge")["status"] == "healthy", fresh
        old = _run(Path(tmp), procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 + 60)},
                                     200: {"command_line": ISAAC_CMD, "started": _iso(T0 - 7200)}}, **base)
        assert _part(old, "isaac_bridge")["status"] == "needs_reload"  # started and reloaded before the change


def test_router_started_before_the_code_changed_needs_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9905: [100]},
                      procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 - 3600)}}, report=_health())
        router = _part(result, "router")
        assert router["status"] == "needs_restart" and "new tools are not loaded" in router["plain"]
        assert any("Restart the router" in s for s in result["next_steps"])
        assert router["pid_record"] == "missing"


def test_unknown_program_on_the_router_port_is_never_stopped():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9905: [555]}, procs={555: {"command_line": "python -m http.server 9905"}})
        router = _part(result, "router")
        assert router["status"] == "unknown_listener" and "Nothing was stopped" in router["plain"]
        assert "kill" not in json.dumps(result).lower()


def test_router_that_does_not_answer():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9905: [100]}, procs={100: {"command_line": ROUTER_CMD}},
                      report=lambda: None)
        assert _part(result, "router")["status"] == "not_responding"
        assert any("router.stderr.log" in s for s in result["next_steps"])


def test_router_pointing_at_the_closed_app_is_a_wrong_target():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9905: [100], 9911: [200]},
                      procs={100: {"command_line": ROUTER_CMD}, 200: {"command_line": ISAAC_CMD}},
                      caps=lambda port: {"version": RPC_VERSION}, report=_health("http://127.0.0.1:9910"))
        router = _part(result, "router")
        assert router["status"] == "wrong_target" and router["points_at"] == "composer"
        assert any("router_set_scene_target isaac" in s for s in result["next_steps"])


def test_bridge_problems_refused_silent_and_odd_owner():
    with tempfile.TemporaryDirectory() as tmp:
        refused = _run(Path(tmp), ports={9911: [200]}, procs={200: {"command_line": ISAAC_CMD}},
                       caps=lambda port: {"http_error": 403})
        assert _part(refused, "isaac_bridge")["status"] == "refused"
        silent = _run(Path(tmp), ports={9911: [200]}, procs={200: {"command_line": "notepad.exe"}})
        part = _part(silent, "isaac_bridge")
        assert part["status"] == "not_responding" and "does not look like Isaac Sim" in part["warning"]


def test_log_errors_and_the_stock_server_are_reported():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9911: [200], 9909: [201]}, procs={200: {"command_line": ISAAC_CMD}},
                      caps=lambda port: {"version": RPC_VERSION},
                      logs=lambda key: {"ok": True, "active": True, "errors": 4})
        assert _part(result, "isaac_bridge")["recent_errors"] == 4
        assert _part(result, "stock_server")["status"] == "info"
        assert result["verdict"] == "needs_attention"


def test_inside_router_mode_does_not_call_itself():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(Path(tmp), ports={9905: [100]}, procs={100: {"command_line": ROUTER_CMD, "started": _iso(T0 + 5)}},
                      report=_health(), inside=True)
        assert _part(result, "router")["status"] == "healthy"
        assert result["verdict"] == "router_only" and "Open Isaac Sim or USD Composer" in result["next_steps"][0]


def test_collectors_parse_netstat_and_process_json():
    netstat = ("Active Connections\n\n  Proto  Local Address  Foreign Address  State  PID\n"
               "  TCP    127.0.0.1:9905   0.0.0.0:0   LISTENING   14732\n"
               "  TCP    0.0.0.0:9911     0.0.0.0:0   LISTENING   200\n"
               "  TCP    [::]:9910        [::]:0      LISTENING   300\n"
               "  TCP    127.0.0.1:5555   0.0.0.0:0   LISTENING   9\n"
               "  TCP    127.0.0.1:9905   127.0.0.1:6000  ESTABLISHED  14732\n")
    fake = lambda *a, **k: types.SimpleNamespace(stdout=netstat)
    if sys.platform == "win32":
        assert doctor.listeners(run=fake) == {9905: [14732], 9911: [200], 9910: [300]}
        one = lambda *a, **k: types.SimpleNamespace(stdout=json.dumps({"ProcessId": 5, "Name": "kit.exe", "CommandLine": "x", "Started": "t"}))
        assert doctor.processes([5], run=one) == {5: {"name": "kit.exe", "command_line": "x", "started": "t"}}
        bad = lambda *a, **k: types.SimpleNamespace(stdout="not json")
        assert doctor.processes([5], run=bad) == {}
        assert doctor.processes([], run=one) == {}


def test_tool_reply_parsing_and_router_command_lines():
    inner = {"scene": {"endpoint": "http://127.0.0.1:9910"}}
    outer = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}}
    assert doctor.parse_tool_reply(json.dumps(outer)) == inner
    assert doctor.parse_tool_reply("event: message\ndata: " + json.dumps(outer) + "\n\n") == inner
    assert doctor.parse_tool_reply("garbage") is None
    assert doctor.is_router(ROUTER_CMD) and not doctor.is_router(ROUTER_CMD.replace("9905", "9906"))
    assert not doctor.is_router("python -m http.server 9905")


def test_text_report_lists_parts_and_numbered_steps():
    with tempfile.TemporaryDirectory() as tmp:
        text = doctor.render_text(_run(Path(tmp), ports={}))
        assert "router (port 9905)" in text and "Next steps:" in text and "  1. Start the router" in text
        assert "nothing was started" in text


def test_doctor_command_runs_without_the_mcp_library():
    src = Path(doctor.__file__).resolve().parents[1]
    code = ("import sys; sys.path.insert(0, r'%s'); import omniverse_scene_mcp.doctor, omniverse_scene_mcp.__main__; "
            "print('mcp' in sys.modules, 'omniverse_scene_mcp.server' in sys.modules)" % src)
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "False False", done.stderr
