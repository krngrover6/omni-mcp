# Router watchdog: restarts the Omniverse MCP router (port 9905) when it has died.
#
# Why: the router died several times during study sessions (Nov 2026), and every time the user had to notice it,
# run start-isaac-mcp.ps1 and reconnect. This script checks the port every -IntervalSeconds and, after two failed
# checks in a row, runs the workspace's start-isaac-mcp.ps1 (which starts ONLY the router, never Isaac or Composer)
# in its own minimized window, so it survives this shell closing.
#
# Usage (from the workspace folder):
#   powershell -ExecutionPolicy Bypass -File omniverse_scene_mcp\tools\router_watchdog.ps1           # keep watching
#   powershell -ExecutionPolicy Bypass -File omniverse_scene_mcp\tools\router_watchdog.ps1 -Once     # check once
# Stop it: close its window (or Ctrl+C). After a restart, reconnect your MCP client (/mcp in Claude Code).
# Safety: at most -MaxRestartsPerHour restarts, then it stops and says so (a router that keeps crashing needs a look
# at isaacsim-study\router.stderr.log, not more restarts). Only one watchdog runs at a time.
[CmdletBinding()]
param(
    [int]$IntervalSeconds = 30,
    [int]$MaxRestartsPerHour = 4,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$startScript = Join-Path $workspace 'start-isaac-mcp.ps1'
$logFile = Join-Path $workspace 'isaacsim-study\router-watchdog.log'
$port = 9905

function Write-Log([string]$message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $message
    Write-Host $line
    try { Add-Content -LiteralPath $logFile -Value $line -Encoding utf8 } catch { }
}

function Test-RouterUp {
    $listening = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    return $listening.Count -gt 0
}

function Start-Router {
    if (-not (Test-Path -LiteralPath $startScript)) {
        Write-Log "Cannot restart: $startScript is missing."
        return $false
    }
    Start-Process -FilePath 'powershell.exe' -WorkingDirectory $workspace -WindowStyle Minimized -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startScript
    ) | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if (Test-RouterUp) {
            Write-Log "Router is back on port $port. Reconnect your MCP client (/mcp in Claude Code)."
            return $true
        }
    }
    Write-Log "Router did not come back within 60 s. See isaacsim-study\router.stderr.log."
    return $false
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\OmniverseRouterWatchdog')
if (-not $mutex.WaitOne(0)) {
    Write-Host 'A router watchdog is already running; nothing to do.'
    exit 0
}
try {
    if ($Once) {
        if (Test-RouterUp) { Write-Host "Router is up on port $port."; exit 0 }
        Write-Log 'Router is down; starting it once.'
        if (Start-Router) { exit 0 } else { exit 1 }
    }
    Write-Log "Watching the router on port $port every $IntervalSeconds s (max $MaxRestartsPerHour restarts/hour)."
    $misses = 0
    $restarts = New-Object System.Collections.Generic.List[datetime]
    while ($true) {
        if (Test-RouterUp) {
            $misses = 0
        } else {
            $misses++
            if ($misses -ge 2) {
                $cutoff = (Get-Date).AddHours(-1)
                $recent = @($restarts | Where-Object { $_ -gt $cutoff })
                if ($recent.Count -ge $MaxRestartsPerHour) {
                    Write-Log "Router died $($recent.Count) times within an hour; stopping the watchdog. Check isaacsim-study\router.stderr.log."
                    exit 2
                }
                Write-Log 'Router is down (two checks in a row); restarting it.'
                $restarts.Add((Get-Date))
                [void](Start-Router)
                $misses = 0
            }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
