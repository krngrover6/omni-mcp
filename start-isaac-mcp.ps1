# Start the existing shared router, explicitly targeting the Isaac Sim bridge.
$ErrorActionPreference = 'Stop'

function Wait-IsaacRouterReady {
    $routerUrl = 'http://127.0.0.1:9905/mcp'
    $routerDeadline = (Get-Date).AddSeconds(60)
    $routerInitialize = @{
        jsonrpc = '2.0'; id = 1; method = 'initialize'
        params = @{
            protocolVersion = '2025-06-18'
            capabilities = @{}
            clientInfo = @{name='isaac-study-launcher';version='1.0'}
        }
    } | ConvertTo-Json -Depth 5 -Compress
    while ((Get-Date) -lt $routerDeadline) {
        try {
            $routerResponse = Invoke-WebRequest -UseBasicParsing -Uri $routerUrl `
                -Method Post -ContentType 'application/json' `
                -Headers @{Accept='application/json, text/event-stream'} `
                -Body $routerInitialize -TimeoutSec 3
            if ($routerResponse.StatusCode -eq 200 -and
                $routerResponse.Content -match '"serverInfo"\s*:') {
                $routerSessionId = $routerResponse.Headers['mcp-session-id']
                if ($routerSessionId) {
                    try {
                        Invoke-WebRequest -UseBasicParsing -Uri $routerUrl -Method Delete `
                            -Headers @{'mcp-session-id'=[string]$routerSessionId;Accept='application/json, text/event-stream'} `
                            -TimeoutSec 3 | Out-Null
                    } catch { }
                }
                Write-Host 'MCP router ready. Starting Isaac Sim next.'
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw 'MCP router did not initialize within 60 seconds. See isaacsim-study/router.stderr.log.'
}

. (Join-Path $PSScriptRoot 'router-identity.ps1')

# After the router answers, say which app its scene tools point at. Never switches the target on its own.
function Write-RouterTargetNotice {
    $reported = Get-RouterReportedInfo
    if ($null -eq $reported) {
        Write-Host 'Could not read the router''s scene target (router_health unavailable). Ask your MCP client for router_health.' -ForegroundColor Yellow
        return
    }
    $targetName = Get-SceneTargetName -Endpoint $reported.Endpoint
    if ($targetName -eq 'isaac') {
        Write-Host "Router scene target: Isaac Sim ($($reported.Endpoint))."
    } else {
        Write-Host "Router scene target is '$targetName' ($($reported.Endpoint)), not Isaac Sim (http://127.0.0.1:9911)." -ForegroundColor Yellow
        Write-Host 'Isaac Sim will still start. To point the scene tools at Isaac Sim, ask your MCP client: router_set_scene_target isaac' -ForegroundColor Yellow
    }
}

$routerPidFile = Join-Path $PSScriptRoot 'isaacsim-study\router.pid'
$routerListener = @(Get-NetTCPConnection -LocalPort 9905 -State Listen -ErrorAction SilentlyContinue)
if ($routerListener.Count) {
    $routerOwners = @($routerListener.OwningProcess | Sort-Object -Unique)
    $routerCommands = @{}
    foreach ($routerOwner in $routerOwners) {
        $routerProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$routerOwner" -ErrorAction SilentlyContinue
        if ($routerProcessInfo) { $routerCommands[[int]$routerOwner] = [string]$routerProcessInfo.CommandLine }
    }
    $routerReported = Get-RouterReportedInfo
    $routerDecision = Resolve-RouterListener -ListenerPids $routerOwners -CommandLines $routerCommands `
        -RecordedPidText $(if (Test-Path -LiteralPath $routerPidFile) { Get-Content -LiteralPath $routerPidFile -Raw } else { $null }) `
        -ReportedPid $(if ($routerReported) { $routerReported.Pid } else { $null })
    switch ($routerDecision.Status) {
        'ok' { }
        'refresh' {
            Write-Host $routerDecision.Reason
            $routerDecision.Pid | Set-Content -LiteralPath $routerPidFile
        }
        default { throw "$($routerDecision.Reason) Close that program or run stop-omniverse.ps1 yourself, then start again." }
    }
    Write-Host "Omniverse router already listening on port 9905 (PID $($routerDecision.Pid))."
    Wait-IsaacRouterReady
    Write-RouterTargetNotice
    return
}
$routerPython = Join-Path $env:ProgramFiles 'Python312\python.exe'
if (-not (Test-Path -LiteralPath $routerPython)) { throw "Missing Python: $routerPython" }
$routerPackages = Join-Path $PSScriptRoot 'bowerbot-venv\Lib\site-packages'
$routerPreviousPath = $env:PYTHONPATH
$routerPreviousEndpoint = $env:OMNIVERSE_SCENE_HTTP_ENDPOINT
try {
    $env:PYTHONPATH = @(
        (Join-Path $PSScriptRoot 'omniverse_scene_mcp\src'),
        $routerPackages,
        (Join-Path $routerPackages 'win32'),
        (Join-Path $routerPackages 'win32\lib'),
        (Join-Path $routerPackages 'pywin32_system32')
    ) -join ';'
    $env:OMNIVERSE_SCENE_HTTP_ENDPOINT = 'http://127.0.0.1:9911'
    $routerProcess = Start-Process -FilePath $routerPython -ArgumentList @(
        '-m', 'omniverse_scene_mcp', 'serve', '--transport', 'streamable-http',
        '--host', '127.0.0.1', '--port', '9905'
    ) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $PSScriptRoot 'isaacsim-study\router.stdout.log') `
      -RedirectStandardError (Join-Path $PSScriptRoot 'isaacsim-study\router.stderr.log')
    $routerProcess.Id | Set-Content -LiteralPath $routerPidFile
    Write-Host "Started Omniverse router for Isaac Sim (PID $($routerProcess.Id))."
} finally {
    $env:PYTHONPATH = $routerPreviousPath
    $env:OMNIVERSE_SCENE_HTTP_ENDPOINT = $routerPreviousEndpoint
}
Wait-IsaacRouterReady
Write-RouterTargetNotice
