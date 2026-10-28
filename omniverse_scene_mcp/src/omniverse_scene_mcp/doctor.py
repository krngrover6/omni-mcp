"""Connection doctor: one read-only report on the router, the Isaac Sim and Composer bridges, and what to do next.

Works when the router is down (python -m omniverse_scene_mcp doctor) and as the always-on router_doctor tool. It
reads listening ports, process command lines, the bridges' capabilities RPC, the router's own health report, file
times and Kit logs. It never starts, stops, reloads, kills or retargets anything; every fix is a step for you.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .bridge import RPC_VERSION
from .health import redact

ROUTER_PORT, ISAAC_PORT, COMPOSER_PORT, STOCK_PORT, BOWERBOT_PORT = 9905, 9911, 9910, 9909, 8181
PORTS = (ROUTER_PORT, ISAAC_PORT, COMPOSER_PORT, STOCK_PORT, BOWERBOT_PORT)
TIMEOUT = 2.0
_LISTEN = re.compile(r"^\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)
_open = build_opener(ProxyHandler({})).open

APPS = {
    ISAAC_PORT: {"app": "Isaac Sim", "key": "isaac", "reload": "isaacsim-study/reload_mcp_bridge.py",
                 "start": "the 'Isaac Sim Study' shortcut (or start-isaacsim.ps1)", "match": ("isaac-sim", "isaacsim")},
    COMPOSER_PORT: {"app": "USD Composer", "key": "composer", "reload": "composer-study/reload_composer_bridge.py",
                    "start": "the 'USD Composer Study' shortcut (or start-omniverse.ps1 -LaunchComposer)",
                    "match": ("my usd composer", "my_usd_composer", "usd_composer", "kit-app-template")},
}


# ------------------------------------------------------------------------------------------------ collectors
def listeners(run: Callable[..., Any] = subprocess.run) -> dict[int, list[int]]:
    """{port: [pid, ...]} for the doctor's ports, from `netstat -ano -p tcp` (read-only)."""
    if os.name != "nt":
        return {}
    try:
        done = run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, list[int]] = {}
    for line in (done.stdout or "").splitlines():
        match = _LISTEN.match(line)
        if not match:
            continue
        port, pid = int(match.group(2)), int(match.group(3))
        if port in PORTS and match.group(1) in ("127.0.0.1", "0.0.0.0", "[::]", "[::1]"):
            found.setdefault(port, [])
            if pid not in found[port]:
                found[port].append(pid)
    return found


def processes(pids: list[int], run: Callable[..., Any] = subprocess.run) -> dict[int, dict[str, Any]]:
    """{pid: {name, command_line, started}} via one read-only Get-CimInstance query."""
    clean = sorted({int(p) for p in pids if isinstance(p, int) and p > 0})
    if os.name != "nt" or not clean:
        return {}
    where = " OR ".join(f"ProcessId={pid}" for pid in clean)
    script = (f"Get-CimInstance Win32_Process -Filter '{where}' | Select-Object ProcessId,Name,CommandLine,"
              "@{n='Started';e={$_.CreationDate.ToString('o')}} | ConvertTo-Json -Compress")
    try:
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, timeout=20, check=False)
        rows = json.loads(done.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    rows = rows if isinstance(rows, list) else [rows]
    return {int(r["ProcessId"]): {"name": r.get("Name"), "command_line": r.get("CommandLine") or "",
                                  "started": r.get("Started")} for r in rows if isinstance(r, dict)}


def bridge_capabilities(port: int, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """The bridge's own capabilities RPC (version, bridge class), or None when it does not answer."""
    from .http_bridge import bridge_request_headers

    request = Request(f"http://127.0.0.1:{port}/rpc", data=json.dumps({"method": "capabilities", "params": {}}).encode(),
                      headers=bridge_request_headers(), method="POST")
    try:
        with _open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {"http_error": exc.code}
    except (URLError, OSError, ValueError):
        return None
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) else {"raw": True}


def router_report(timeout: float = 5.0) -> dict[str, Any] | None:
    """Ask the running router for its read-only health report over MCP. None when it does not answer."""
    url = f"http://127.0.0.1:{ROUTER_PORT}/mcp"
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    session = None

    def post(body: dict[str, Any]) -> Any:
        return _open(Request(url, data=json.dumps(body).encode(), headers=headers, method="POST"), timeout=timeout)

    try:
        with post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "doctor", "version": "1"}}}) as r:
            session = r.headers.get("mcp-session-id")
        if not session:
            return None
        headers["mcp-session-id"] = session
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}).close()
        with post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "router_health", "arguments": {}}}) as r:
            return parse_tool_reply(r.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, OSError, ValueError):
        return None
    finally:
        if session:
            try:
                _open(Request(url, headers=headers, method="DELETE"), timeout=3).close()
            except (HTTPError, URLError, OSError):
                pass


def parse_tool_reply(body: str) -> dict[str, Any] | None:
    """The tool result dict from a tools/call reply body (plain JSON or an SSE 'data:' line)."""
    match = re.search(r"(?m)^data:\s*(\{.*\})\s*$", body or "")
    try:
        outer = json.loads(match.group(1) if match else body)
        text = outer["result"]["content"][0]["text"]
        inner = json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return inner if isinstance(inner, dict) else None


def newest_source_change(root: Path | None = None) -> float | None:
    """Newest modification time of the router package's .py/.json files (code you would load on restart)."""
    package = root or Path(__file__).resolve().parent
    newest = None
    for path in package.rglob("*"):
        if path.suffix in (".py", ".json") and "__pycache__" not in path.parts:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            newest = stamp if newest is None or stamp > newest else newest
    return newest


def last_reload(workspace: Path, key: str) -> dict[str, Any] | None:
    folder = "isaacsim-study" if key == "isaac" else "composer-study"
    try:
        data = json.loads((workspace / folder / "bridge-reload-result.json").read_text(encoding="utf-8"))
        finished = datetime.strptime(str(data.get("finished")), "%Y-%m-%d %H:%M:%S").timestamp()
    except (OSError, ValueError, TypeError):
        return None
    return {"status": data.get("status"), "rpc_version": data.get("rpc_version"), "finished": finished}


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _when(stamp: float | None) -> str | None:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else None


# ------------------------------------------------------------------------------------------------ diagnosis
def is_router(command_line: str, port: int = ROUTER_PORT) -> bool:
    text = command_line or ""
    return ("omniverse_scene_mcp" in text and re.search(r"(^|\s)serve(\s|$)", text) is not None
            and re.search(rf"--port\s+{port}(\s|$)", text) is not None)


def diagnose(
    *,
    workspace: str | Path | None = None,
    ports: Mapping[int, list[int]] | None = None,
    procs: Mapping[int, Mapping[str, Any]] | None = None,
    capabilities: Callable[[int], dict[str, Any] | None] | None = None,
    router_health_report: dict[str, Any] | None | Callable[[], dict[str, Any] | None] = None,
    source_changed: float | None | object = ...,
    reloads: Callable[[Path, str], dict[str, Any] | None] | None = None,
    logs: Callable[[str], dict[str, Any] | None] | None = None,
    inside_router: bool = False,
) -> dict[str, Any]:
    """Read-only diagnosis. All collectors can be injected (tests); by default they read this machine."""
    root = Path(workspace) if workspace else Path(__file__).resolve().parents[3]
    open_ports = dict(ports) if ports is not None else listeners()
    pids = [pid for owners in open_ports.values() for pid in owners]
    info = dict(procs) if procs is not None else processes(pids)
    caps = capabilities or bridge_capabilities
    reload_info = reloads or last_reload
    changed = newest_source_change() if source_changed is ... else source_changed
    steps: list[str] = []
    parts: list[dict[str, Any]] = []

    # --- router
    router: dict[str, Any] = {"part": "router", "port": ROUTER_PORT}
    owners = open_ports.get(ROUTER_PORT, [])
    record_path = root / "isaacsim-study" / "router.pid"
    try:
        recorded = int(record_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        recorded = None
    report: dict[str, Any] | None = None
    if not owners:
        router.update(status="offline", plain="The router is not running, so no AI client can reach the scene tools.")
        steps.append("Start the router: run start-isaac-mcp.ps1 (or start Isaac Sim with its shortcut, which starts it).")
    else:
        owner = owners[0]
        proc = info.get(owner, {})
        ours = is_router(str(proc.get("command_line", "")))
        router["pid"] = owner
        if len(owners) > 1 or not ours:
            router.update(status="unknown_listener", plain=f"Port {ROUTER_PORT} is used by process {owner}, which is not "
                          "the Omniverse router. Nothing was stopped.")
            steps.append(f"Close the program using port {ROUTER_PORT} (process {owner}) yourself, then start the router.")
        else:
            if callable(router_health_report):
                report = router_health_report()
            elif router_health_report is not None:
                report = router_health_report
            elif not inside_router:
                report = router_report()
            router["pid_record"] = "ok" if recorded == owner else ("missing" if recorded is None else f"stale ({recorded})")
            if inside_router:
                router.update(status="healthy", plain="The router is running (this report comes from inside it).")
            elif report is None:
                router.update(status="not_responding", plain="The router process is running but does not answer MCP requests.")
                steps.append("Restart the router: close it (process "
                             f"{owner}) and run start-isaac-mcp.ps1. Check isaacsim-study/router.stderr.log for errors.")
            else:
                router.update(status="healthy", plain="The router is running and answering.")
            started = _epoch(proc.get("started"))
            router["started"] = _when(started)
            if started and isinstance(changed, float) and changed > started + 1:
                router["status"] = "needs_restart" if router["status"] == "healthy" else router["status"]
                router["plain"] += (f" Its code changed on disk at {_when(changed)}, after it started at "
                                    f"{_when(started)}, so new tools are not loaded yet.")
                steps.append(f"Restart the router to load the new code: close process {owner}, then run start-isaac-mcp.ps1.")
            if router["pid_record"] != "ok":
                router["plain"] += " The router.pid record is out of date (the launcher refreshes it next time)."
    target = None
    if report:
        scene = report.get("scene") or {}
        target = {"endpoint": scene.get("endpoint"), "mode": scene.get("mode"),
                  "reachable": (scene.get("probe") or {}).get("reachable")}
        router["scene_target"] = target
    parts.append(router)

    # --- bridges
    running: dict[str, bool] = {}
    for port, app in APPS.items():
        part: dict[str, Any] = {"part": f"{app['key']}_bridge", "app": app["app"], "port": port}
        owner_list = open_ports.get(port, [])
        if not owner_list:
            part.update(status="offline", plain=f"{app['app']} is closed, or its bridge did not start.")
            running[app["key"]] = False
            parts.append(part)
            continue
        running[app["key"]] = True
        owner = owner_list[0]
        cmd = str(info.get(owner, {}).get("command_line", "")).lower()
        part["pid"] = owner
        if cmd and not any(word in cmd for word in app["match"]):
            part["warning"] = f"Port {port} is owned by a process that does not look like {app['app']}."
        reply = caps(port)
        if reply is None:
            part.update(status="not_responding", plain=f"{app['app']} has port {port} open but its bridge does not answer.")
            steps.append(f"In {app['app']}: Window > Script Editor > File > Open {app['reload']} > Run (reloads the bridge).")
        elif reply.get("http_error") == 403:
            part.update(status="refused", plain=f"The {app['app']} bridge refused the request (security guard).")
            steps.append("Check that OMNIVERSE_BRIDGE_TOKEN is the same for the router and the app, or unset in both.")
        else:
            version = reply.get("version")
            part["rpc_version"] = version
            last = reload_info(root, app["key"])
            # The bridge loads the code on disk when the app starts and again on each Script Editor reload,
            # so whichever happened last is when its code was loaded (a fresh launch needs no reload).
            loads = [stamp for stamp in (last and last["finished"], _epoch(info.get(owner, {}).get("started"))) if stamp]
            loaded = max(loads) if loads else None
            stale_code = bool(loaded and isinstance(changed, float) and changed > loaded + 1)
            if version and version != RPC_VERSION:
                part.update(status="needs_reload", plain=f"The {app['app']} bridge runs version {version}; the code on "
                            f"disk is {RPC_VERSION}.")
            elif stale_code:
                part.update(status="needs_reload", plain=f"The {app['app']} bridge is version {version}, but the code changed "
                            f"after it was last loaded ({_when(loaded)}: app start or reload), so new tools are missing.")
            else:
                part.update(status="healthy", plain=f"The {app['app']} bridge answers (version {version}).")
            if part["status"] == "needs_reload":
                steps.append(f"In {app['app']}: Window > Script Editor > File > Open {app['reload']} > Run "
                             "(reloads the bridge; your scene is not changed).")
        if logs is not None:
            try:
                summary = logs(app["key"])
            except Exception:  # noqa: BLE001 - logs are extra
                summary = None
            if summary and summary.get("ok") and summary.get("active"):
                part["recent_errors"] = summary.get("errors", 0)
                if summary.get("errors"):
                    part["plain"] += f" Its log has {summary['errors']} error lines this session (ask: explain the latest errors)."
        parts.append(part)

    # --- wrong target
    if target and target.get("endpoint"):
        aimed = "isaac" if str(ISAAC_PORT) in str(target["endpoint"]) else "composer" if str(COMPOSER_PORT) in str(target["endpoint"]) else "other"
        router["points_at"] = aimed
        if aimed in running and not running[aimed]:
            other = "composer" if aimed == "isaac" else "isaac"
            if running.get(other):
                router["status"] = "wrong_target" if router["status"] == "healthy" else router["status"]
                router["plain"] += f" It points at {aimed}, which is closed, while {other} is open."
                steps.append(f"Point the router at the open app: ask your AI client 'router_set_scene_target {other}'.")

    # --- extras
    if open_ports.get(STOCK_PORT):
        parts.append({"part": "stock_server", "port": STOCK_PORT, "status": "info",
                      "plain": "The stock live_scene_control server is also running on 9909; it is normally stopped."})
    parts.append({"part": "bowerbot", "port": BOWERBOT_PORT, "status": "running" if open_ports.get(BOWERBOT_PORT) else "optional",
                  "plain": "Optional knowledge server; built-in lessons are used when it is off."})

    statuses = [p["status"] for p in parts if p["part"] in ("router", "isaac_bridge", "composer_bridge")]
    healthy_bridge = any(p["status"] == "healthy" for p in parts if p["part"].endswith("_bridge"))
    verdict = ("ready" if router.get("status") == "healthy" and healthy_bridge
               else "router_only" if router.get("status") == "healthy"
               else "needs_attention")
    headline = {
        "ready": "Ready: the router is running and at least one app bridge answers.",
        "router_only": "The router is running, but no app bridge answers yet: open Isaac Sim or Composer.",
        "needs_attention": "Something needs attention; follow the steps below in order.",
    }[verdict]
    if verdict == "router_only" and not steps:
        steps.append("Open Isaac Sim or USD Composer with its study shortcut; the bridge starts with it.")
    for part in parts:
        for key in ("plain", "warning"):
            if isinstance(part.get(key), str):
                part[key] = redact(part[key])
    return {
        "ok": True,
        "verdict": verdict,
        "headline": headline,
        "parts": parts,
        "next_steps": list(dict.fromkeys(steps)),
        "source_code_changed": _when(changed) if isinstance(changed, float) else None,
        "expected_rpc_version": RPC_VERSION,
        "statuses": statuses,
        "note": "Read-only check: nothing was started, stopped, reloaded or retargeted.",
    }


def render_text(result: dict[str, Any]) -> str:
    """The diagnosis as short plain text for a terminal."""
    marks = {"healthy": "OK ", "running": "OK ", "offline": "-- ", "optional": "-- ", "info": " i "}
    lines = [result["headline"], ""]
    for part in result["parts"]:
        mark = marks.get(part["status"], "!! ")
        name = part.get("app") or part["part"].replace("_", " ")
        lines.append(f"{mark} {name} (port {part['port']}): {part['plain']}")
        if part.get("warning"):
            lines.append(f"     note: {part['warning']}")
    if result["next_steps"]:
        lines += ["", "Next steps:"] + [f"  {i}. {s}" for i, s in enumerate(result["next_steps"], 1)]
    lines += ["", result["note"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from . import kit_logs

    def log_summary(key: str) -> dict[str, Any] | None:
        return kit_logs.summary(key)

    result = diagnose(logs=log_summary)
    print(json.dumps(result, indent=2) if "--json" in args else render_text(result))
    return 0 if result["verdict"] == "ready" else 1
