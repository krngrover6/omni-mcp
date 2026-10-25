# Helpers for start-isaac-mcp.ps1: decide whether the process listening on the router port is our router,
# and which scene bridge it points at. Pure functions; nothing here starts, stops or kills a process.

# True only for `python -m omniverse_scene_mcp serve ... --port <Port>`.
function Test-OurRouterCommandLine {
    param([string]$CommandLine, [int]$Port = 9905)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    return ($CommandLine -match 'omniverse_scene_mcp') -and
        ($CommandLine -match '(^|\s)serve(\s|$)') -and
        ($CommandLine -match "--port\s+$Port(\s|$)")
}

# Reads router.pid text. Returns the PID, or $null when the record is missing, empty or malformed.
function ConvertTo-RecordedPid {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return $null }
    $parsed = 0
    if ([int]::TryParse($Text.Trim(), [ref]$parsed) -and $parsed -gt 0) { return $parsed }
    return $null
}

# Decides what to do about a listener on the router port.
#   ListenerPids   : process ids owning the listening socket
#   CommandLines   : hashtable pid -> command line (missing or empty = process not readable / already gone)
#   RecordedPidText: raw text of router.pid ($null when the file is missing)
#   ReportedPid    : PID the router reported about itself over MCP ($null when it could not be asked)
# Status: 'ok' (record already correct), 'refresh' (ours, record stale: caller rewrites it),
#         'unknown-listener' (not our router: caller must refuse, never kill), 'identity-mismatch'.
function Resolve-RouterListener {
    param(
        [int[]]$ListenerPids,
        [hashtable]$CommandLines,
        [AllowNull()][string]$RecordedPidText,
        [AllowNull()][object]$ReportedPid,
        [int]$Port = 9905
    )
    $pids = @($ListenerPids | Sort-Object -Unique)
    if ($pids.Count -eq 0) { return [pscustomobject]@{ Status = 'no-listener'; Pid = $null; Reason = "Nothing is listening on port $Port." } }
    foreach ($owner in $pids) {
        $command = $CommandLines[$owner]
        if (-not (Test-OurRouterCommandLine -CommandLine $command -Port $Port)) {
            return [pscustomobject]@{
                Status = 'unknown-listener'; Pid = $owner
                Reason = "Port $Port is used by process $owner, which is not the Omniverse scene router (or its command line cannot be read). Nothing was stopped."
            }
        }
    }
    if ($pids.Count -gt 1) {
        return [pscustomobject]@{ Status = 'identity-mismatch'; Pid = $null; Reason = "Several processes ($($pids -join ', ')) listen on port $Port; refusing to guess which one is the router." }
    }
    $owner = $pids[0]
    if ($null -ne $ReportedPid -and [int]$ReportedPid -ne $owner) {
        return [pscustomobject]@{
            Status = 'identity-mismatch'; Pid = $owner
            Reason = "The router on port $Port says it is process $ReportedPid but the listener is process $owner."
        }
    }
    $recorded = ConvertTo-RecordedPid -Text $RecordedPidText
    if ($recorded -eq $owner) { return [pscustomobject]@{ Status = 'ok'; Pid = $owner; Reason = 'The PID record matches the verified router.' } }
    $why = if ($null -eq $recorded) { 'The PID record is missing or malformed' } else { "The PID record says $recorded" }
    return [pscustomobject]@{ Status = 'refresh'; Pid = $owner; Reason = "$why, but the verified router is process $owner. The record will be refreshed." }
}

# Names the scene bridge a router endpoint points at.
function Get-SceneTargetName {
    param([AllowNull()][string]$Endpoint)
    if ([string]::IsNullOrWhiteSpace($Endpoint)) { return 'unknown' }
    if ($Endpoint -match ':9911(/|$)') { return 'isaac' }
    if ($Endpoint -match ':9910(/|$)') { return 'composer' }
    return 'other'
}

# Turns the HTTP body of a tools/call router_health answer (plain JSON or an SSE "data:" line) into
# @{ Pid; Endpoint; Mode }. Returns $null when the body does not contain a health report.
function ConvertFrom-RouterHealthResponse {
    param([AllowNull()][string]$Content)
    if ([string]::IsNullOrWhiteSpace($Content)) { return $null }
    try {
        $json = $Content
        $match = [regex]::Match($Content, '(?m)^data:\s*(\{.*\})\s*$')
        if ($match.Success) { $json = $match.Groups[1].Value }
        $outer = $json | ConvertFrom-Json
        $text = $outer.result.content[0].text
        if (-not $text) { return $null }
        $health = $text | ConvertFrom-Json
        if ($null -eq $health.router -or $null -eq $health.scene) { return $null }
        return @{ Pid = $health.router.pid; Endpoint = [string]$health.scene.endpoint; Mode = [string]$health.scene.mode }
    } catch {
        return $null
    }
}

# Asks the running router (read-only router_health) who it is and where it points. $null when it cannot answer.
function Get-RouterReportedInfo {
    param([string]$Url = 'http://127.0.0.1:9905/mcp', [int]$TimeoutSec = 5)
    $headers = @{ Accept = 'application/json, text/event-stream' }
    $sessionId = $null
    try {
        $initialize = @{
            jsonrpc = '2.0'; id = 1; method = 'initialize'
            params = @{ protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{ name = 'isaac-study-launcher'; version = '1.0' } }
        } | ConvertTo-Json -Depth 5 -Compress
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Post -ContentType 'application/json' `
            -Headers $headers -Body $initialize -TimeoutSec $TimeoutSec
        $sessionId = [string]$response.Headers['mcp-session-id']
        if (-not $sessionId) { return $null }
        $headers['mcp-session-id'] = $sessionId
        Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Post -ContentType 'application/json' -Headers $headers `
            -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' -TimeoutSec $TimeoutSec | Out-Null
        $call = @{ jsonrpc = '2.0'; id = 2; method = 'tools/call'; params = @{ name = 'router_health'; arguments = @{} } } |
            ConvertTo-Json -Depth 5 -Compress
        $health = Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Post -ContentType 'application/json' `
            -Headers $headers -Body $call -TimeoutSec $TimeoutSec
        return ConvertFrom-RouterHealthResponse -Content ([string]$health.Content)
    } catch {
        return $null
    } finally {
        if ($sessionId) {
            try { Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Delete -Headers $headers -TimeoutSec 3 | Out-Null } catch { }
        }
    }
}
