"""Read-only router health: loopback TCP probes, router PID check and a redacted stderr tail."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

PROBE_TIMEOUT = 0.3
TAIL_LINES = 20
APPROVAL = "needs your approval"
SCENE_HOSTS = frozenset({"127.0.0.1"})
KNOWLEDGE_HOSTS = frozenset({"127.0.0.1", "localhost"})
_TAIL_BYTES = 64 * 1024
_LINE_CHARS = 400
_MAX_TAIL_LINES = 200
_DEFAULT_PORTS = {"http": 80, "https": 443}

Probe = Callable[[str, int, float], None]

# Bearer first so "token=Bearer abc" cannot leave "abc" behind once the key=value rule has eaten "Bearer".
_REDACTIONS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"""(?i)([\w.\-]*(?:api[_\-]?key|token|password|passwd|secret)[\w.\-]*["']?\s*[:=]\s*)"""
            r"""("[^"]*"|'[^']*'|[^\s,;&"'}\]]+)"""
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"nvapi-[A-Za-z0-9_\-]+"), "nvapi-[REDACTED]"),
    # URL userinfo (scheme://user:password@host).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    # Authorization headers with any scheme (Basic, Token, ...); Bearer was already masked above.
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?!Bearer \[)[A-Za-z]+\s+\S+"), r"\1[REDACTED]"),
    # Command-line flags such as --password SECRET or --api-key=SECRET.
    (re.compile(r"(?i)(--(?:password|passwd|pwd|token|secret|api[-_]?key)(?:=|\s+))\S+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)\b((?:pwd|passphrase|private[_\-]?key)\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+"), r"\1[REDACTED]"),
)


def redact(text: str) -> str:
    """Mask nvapi-*/sk-* keys, Bearer and other Authorization values, URL userinfo, cookies, --password-style
    flags and api_key/token/password/secret/pwd/passphrase/private_key values (URL-encoded text is decoded first)."""
    if "%" in text:
        text = unquote(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def tcp_connect(host: str, port: int, timeout: float) -> None:
    """Open and immediately close a TCP connection; no bytes are sent. Raises OSError when unreachable."""
    with socket.create_connection((host, port), timeout=timeout):
        pass


def probe_endpoint(
    endpoint: str | None,
    allowed_hosts: Iterable[str] = KNOWLEDGE_HOSTS,
    timeout: float = PROBE_TIMEOUT,
    probe: Probe | None = None,
) -> dict[str, Any]:
    """TCP reachability of an endpoint's host:port, only for allowlisted loopback hosts; never sends HTTP."""
    result: dict[str, Any] = {"endpoint": redact(endpoint) if endpoint else None, "probed": False, "reachable": None}
    if not endpoint:
        result["skipped"] = "no endpoint (in-process, fake or offline backend)"
        return result
    try:
        parts = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        host, port = parts.hostname, parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    except ValueError as exc:
        result["skipped"] = f"unparseable endpoint: {exc}"
        return result
    result.update(host=host, port=port)
    allowed = sorted(allowed_hosts)
    if host not in allowed:
        result["skipped"] = f"only {' / '.join(allowed)} endpoints are probed"
        return result
    if port is None:
        result["skipped"] = "endpoint has no port"
        return result
    started = time.perf_counter()
    try:
        (probe or tcp_connect)(host, port, timeout)
    except OSError as exc:
        result.update(probed=True, reachable=False, error=redact(str(exc)) or type(exc).__name__)
    else:
        result.update(probed=True, reachable=True)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["timeout_ms"] = round(timeout * 1000)
    return result


def pid_status(pid_file: str | Path, current_pid: int | None = None, parent_pid: int | None = None) -> dict[str, Any]:
    """Compare the PID recorded by start-isaac-mcp.ps1 with this router process."""
    path = Path(pid_file)
    current = os.getpid() if current_pid is None else current_pid
    parent = os.getppid() if parent_pid is None else parent_pid
    info: dict[str, Any] = {
        "pid_file": str(path),
        "present": path.is_file(),
        "recorded_pid": None,
        "current_pid": current,
        "parent_pid": parent,
        "matches": None,
    }
    if not info["present"]:
        info["note"] = "No router.pid; this router was not started by start-isaac-mcp.ps1 (nothing to compare)."
        return info
    try:
        raw = path.read_bytes()[:256]
        text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig", errors="replace")
        recorded = int(text.strip().strip("\x00"))
    except (OSError, ValueError) as exc:
        info["error"] = f"Could not read a PID: {type(exc).__name__}"
        return info
    info["recorded_pid"] = recorded
    if recorded == current:
        info.update(matches=True, note="router.pid matches this router process.")
    elif recorded == parent:
        info.update(matches=True, note="router.pid records this router's parent (launcher) process.")
    else:
        info.update(
            matches=False,
            note=f"router.pid records PID {recorded} but this router is PID {current}: the file is stale "
            "or a different router instance was started by the launcher.",
        )
    return info


def stderr_tail(log_file: str | Path, lines: int = TAIL_LINES) -> dict[str, Any]:
    """Last lines of the router stderr log, each truncated then redacted."""
    path = Path(log_file)
    count = max(0, min(int(lines), _MAX_TAIL_LINES))
    info: dict[str, Any] = {"path": str(path), "present": path.is_file(), "lines": []}
    if not info["present"]:
        return info
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            chunk = handle.read()
    except OSError as exc:
        info["error"] = f"Could not read log: {type(exc).__name__}"
        return info
    rows = chunk.decode("utf-8", errors="replace").splitlines()
    if start and rows:
        rows = rows[1:]
    tail = rows[-count:] if count else []
    info["lines"] = [redact(row[:_LINE_CHARS]) for row in tail]
    info["size_bytes"] = size
    info["truncated_lines"] = sum(len(row) > _LINE_CHARS for row in tail)
    return info


def router_health(
    scene_backend: Mapping[str, Any] | None = None,
    knowledge_backends: Iterable[Mapping[str, Any]] | None = None,
    *,
    workspace_root: str | Path | None = None,
    pid_file: str | Path | None = None,
    stderr_log: str | Path | None = None,
    timeout: float = PROBE_TIMEOUT,
    probe: Probe | None = None,
    current_pid: int | None = None,
    parent_pid: int | None = None,
    tail_lines: int = TAIL_LINES,
) -> dict[str, Any]:
    """Read-only health report for the running router.

    ok reflects the scene backend only; knowledge MCPs are optional because the router falls back to
    built-in lessons. Suggestions are text labelled 'needs your approval' and are never executed.
    """
    root = Path(workspace_root) if workspace_root else _default_root()
    study = root / "isaacsim-study"
    issues: list[str] = []
    suggestions: list[str] = []

    scene = dict(scene_backend or {"mode": "unknown", "endpoint": None})
    scene["warnings"] = list(scene.get("warnings") or [])
    raw_scene_endpoint = scene.get("endpoint")
    if raw_scene_endpoint:
        scene["endpoint"] = redact(str(raw_scene_endpoint))
    scene["probe"] = probe_endpoint(raw_scene_endpoint, SCENE_HOSTS, timeout, probe)
    if scene.get("silent_fallback"):
        issues.append(f"Scene backend silently fell back to the fake in-memory scene: {scene.get('reason')}.")
        suggestions.append(
            f"{APPROVAL}: launch the live scene (./start-omniverse.ps1 -LaunchComposer for Composer, "
            "./start-isaac-mcp.ps1 for Isaac Sim), then restart the router so it picks up the scene endpoint."
        )
    if scene["probe"]["reachable"] is False:
        issues.append(f"Scene bridge {scene['probe']['endpoint']} did not accept a TCP connection.")
        suggestions.append(
            f"{APPROVAL}: start or reload the scene bridge for {scene['probe']['endpoint']} "
            "(Composer setup extension on 9910, Isaac Sim bridge on 9911); the router does not restart it."
        )

    knowledge: list[dict[str, Any]] = []
    for backend in knowledge_backends or []:
        endpoint = backend.get("endpoint")
        entry = {
            "target": backend.get("target"),
            "backend": backend.get("backend"),
            "enabled": bool(backend.get("enabled", True)),
        }
        if entry["enabled"]:
            entry["probe"] = probe_endpoint(endpoint, KNOWLEDGE_HOSTS, timeout, probe)
        else:
            entry["probe"] = {
                "endpoint": redact(str(endpoint)) if endpoint else None,
                "probed": False,
                "reachable": None,
                "skipped": "backend disabled",
            }
        if entry["probe"]["reachable"] is False:
            where = entry["probe"]["endpoint"]
            issues.append(f"{entry['target']} knowledge MCP at {where} is unreachable; built-in lessons are used.")
            hint = " (./start-omniverse.ps1 starts Bowerbot on 8181)" if entry["target"] == "bowerbot" else ""
            suggestions.append(f"{APPROVAL}: start the {entry['target']} knowledge MCP on {where}{hint}.")
        knowledge.append(entry)

    pid_info = pid_status(pid_file or study / "router.pid", current_pid, parent_pid)
    if pid_info["matches"] is False:
        issues.append(pid_info["note"])
        suggestions.append(
            f"{APPROVAL}: check which process listens on port 9905 (Get-NetTCPConnection -LocalPort 9905 "
            "-State Listen) before restarting; ./stop-omniverse.ps1 also stops Bowerbot (8181) and Composer (9910)."
        )

    return {
        "ok": not scene.get("silent_fallback") and scene["probe"]["reachable"] is not False,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "router": {"pid": pid_info["current_pid"], "python": sys.version.split()[0]},
        "probe_timeout_ms": round(timeout * 1000),
        "scene": scene,
        "knowledge": knowledge,
        "router_pid_file": pid_info,
        "stderr_tail": stderr_tail(stderr_log or study / "router.stderr.log", tail_lines),
        "issues": issues,
        "suggestions": suggestions,
        "actions_taken": [],
        "note": "Read-only check: nothing was restarted or reloaded.",
    }


def _default_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]
