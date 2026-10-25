"""router-identity.ps1 / start-isaac-mcp.ps1: stale PID records, unknown listeners and wrong scene targets.

Each case runs the real PowerShell helper functions on made-up inputs. Nothing here starts, stops or contacts a
service, and no process is ever killed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
HELPER = WORKSPACE / "router-identity.ps1"
LAUNCHER = WORKSPACE / "start-isaac-mcp.ps1"
ROUTER_CMD = '"C:\\Program Files\\Python312\\python.exe" -m omniverse_scene_mcp serve --transport streamable-http --host 127.0.0.1 --port 9905'
OTHER_CMD = '"C:\\Python\\python.exe" -m http.server 9905'


def _powershell() -> str | None:
    if sys.platform != "win32":
        return None
    return shutil.which("powershell") or shutil.which("pwsh")


def _run(snippet: str):
    """Dot-source the helper, run snippet (which must end in ConvertTo-Json) and return the parsed JSON."""
    shell = _powershell()
    command = f". '{HELPER}'; {snippet}"
    done = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def _resolve(listeners, commands, record, reported="$null"):
    table = "@{" + ";".join(f"{pid}='{cmd}'" for pid, cmd in commands.items()) + "}"
    text = "$null" if record is None else f"'{record}'"
    pids = ",".join(str(pid) for pid in listeners) or "@()"
    snippet = (
        f"$c = {table}; $r = Resolve-RouterListener -ListenerPids @({pids}) -CommandLines $c "
        f"-RecordedPidText {text} -ReportedPid {reported}; $r | ConvertTo-Json -Compress"
    )
    return _run(snippet)


def test_helper_and_launcher_exist_and_launcher_uses_the_helper():
    if not HELPER.is_file() or not LAUNCHER.is_file():
        return
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "router-identity.ps1" in text
    assert "Resolve-RouterListener" in text and "Write-RouterTargetNotice" in text
    # the old PID-only comparison must be gone
    assert "-notin @($routerListener.OwningProcess)" not in text
    # nothing in the helper or the launcher may stop or kill a process
    for name in ("Stop-Process", "taskkill", "Kill()"):
        assert name not in HELPER.read_text(encoding="utf-8")
        assert name not in text


def test_both_scripts_parse_without_syntax_errors():
    shell = _powershell()
    if shell is None or not HELPER.is_file():
        return
    for path in (HELPER, LAUNCHER):
        snippet = (
            f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$e); "
            "@($e).Count | ConvertTo-Json"
        )
        done = subprocess.run([shell, "-NoProfile", "-Command", snippet], capture_output=True, text=True, timeout=60)
        assert done.stdout.strip() == "0", f"{path.name}: {done.stdout} {done.stderr}"


def test_matching_record_is_ok():
    if _powershell() is None or not HELPER.is_file():
        return
    result = _resolve([4242], {4242: ROUTER_CMD}, "4242", reported="4242")
    assert result["Status"] == "ok" and result["Pid"] == 4242


def test_stale_record_of_a_verified_router_is_refreshed():
    if _powershell() is None or not HELPER.is_file():
        return
    result = _resolve([14732], {14732: ROUTER_CMD}, "109716", reported="14732")
    assert result["Status"] == "refresh" and result["Pid"] == 14732
    assert "109716" in result["Reason"]


def test_missing_empty_or_malformed_record_is_refreshed_not_trusted():
    if _powershell() is None or not HELPER.is_file():
        return
    for record in (None, "", "   ", "abc", "-5", "12 34"):
        result = _resolve([7], {7: ROUTER_CMD}, record)
        assert result["Status"] == "refresh", record
        assert "missing or malformed" in result["Reason"]


def test_unrelated_listener_is_refused_and_never_touched():
    if _powershell() is None or not HELPER.is_file():
        return
    result = _resolve([555], {555: OTHER_CMD}, "555")
    assert result["Status"] == "unknown-listener"
    assert "Nothing was stopped" in result["Reason"]


def test_listener_whose_process_cannot_be_read_is_refused():
    if _powershell() is None or not HELPER.is_file():
        return
    # a dead or protected process has no readable command line
    result = _resolve([321], {}, "321")
    assert result["Status"] == "unknown-listener"


def test_a_router_reporting_a_different_pid_is_a_mismatch():
    if _powershell() is None or not HELPER.is_file():
        return
    result = _resolve([100], {100: ROUTER_CMD}, "100", reported="999")
    assert result["Status"] == "identity-mismatch"


def test_several_listeners_are_not_guessed_between():
    if _powershell() is None or not HELPER.is_file():
        return
    result = _resolve([1, 2], {1: ROUTER_CMD, 2: ROUTER_CMD}, "1")
    assert result["Status"] == "identity-mismatch"
    result = _resolve([1, 2], {1: ROUTER_CMD, 2: OTHER_CMD}, "1")
    assert result["Status"] == "unknown-listener"


def test_router_on_another_port_is_not_ours_for_port_9905():
    if _powershell() is None or not HELPER.is_file():
        return
    other_port = ROUTER_CMD.replace("--port 9905", "--port 9906")
    result = _resolve([8], {8: other_port}, "8")
    assert result["Status"] == "unknown-listener"


def test_no_listener():
    if _powershell() is None or not HELPER.is_file():
        return
    assert _resolve([], {}, None)["Status"] == "no-listener"


def test_scene_target_names():
    if _powershell() is None or not HELPER.is_file():
        return
    cases = {
        "http://127.0.0.1:9911": "isaac",
        "http://127.0.0.1:9911/": "isaac",
        "http://127.0.0.1:9910": "composer",
        "http://127.0.0.1:9909": "other",
        "http://127.0.0.1:99110": "other",
        "": "unknown",
    }
    for endpoint, expected in cases.items():
        got = _run(f"Get-SceneTargetName -Endpoint '{endpoint}' | ConvertTo-Json -Compress")
        assert got == expected, endpoint


def test_health_response_parsing_for_json_and_sse_bodies():
    if _powershell() is None or not HELPER.is_file():
        return
    health = {"router": {"pid": 14732}, "scene": {"endpoint": "http://127.0.0.1:9910", "mode": "http_env"}}
    outer = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(health)}]}}
    plain = json.dumps(outer)
    sse = "event: message\r\ndata: " + plain + "\r\n\r\n"
    for body in (plain, sse):
        escaped = body.replace("'", "''")
        got = _run(f"$b = @'\n{escaped}\n'@; ConvertFrom-RouterHealthResponse -Content $b | ConvertTo-Json -Compress")
        assert got["Pid"] == 14732 and got["Endpoint"] == "http://127.0.0.1:9910"


def test_unreadable_health_response_gives_null_not_an_error():
    if _powershell() is None or not HELPER.is_file():
        return
    for body in ("", "not json", "{}", '{"result":{"content":[{"text":"{}"}]}}', 'data: {"error":"hidden tool"}'):
        escaped = body.replace("'", "''")
        got = _run(f"$b = @'\n{escaped}\n'@; $x = ConvertFrom-RouterHealthResponse -Content $b; (@{{ isnull = ($null -eq $x) }}) | ConvertTo-Json -Compress")
        assert got["isnull"] is True, body
