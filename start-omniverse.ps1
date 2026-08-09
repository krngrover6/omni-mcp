param(
    [switch]$LaunchComposer
)

$ErrorActionPreference = "Stop"

function Test-PortListening {
    param(
        [int]$Port
    )

    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
}

function Wait-PortListening {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-BackgroundProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden | Out-Null
}

function Stop-PortListener {
    param(
        [int]$Port
    )

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        if ($connection.OwningProcess -gt 0) {
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

$root = Split-Path -Parent $PSCommandPath
$kitRoot = Join-Path $root "kit-app-template"
$omniverseRoot = Join-Path $root "omniverse_scene_mcp"
$bowerbotExe = Join-Path $root "bowerbot-venv\Scripts\bowerbot.exe"
$pythonExe = "python"

if (-not (Test-Path $kitRoot)) {
    throw "Missing kit-app-template folder: $kitRoot"
}

if (-not (Test-Path $omniverseRoot)) {
    throw "Missing omniverse_scene_mcp folder: $omniverseRoot"
}

if (-not (Test-Path $bowerbotExe)) {
    throw "Missing Bowerbot executable: $bowerbotExe"
}

$env:PYTHONPATH = (Join-Path $omniverseRoot "src")

Write-Host "Starting Omniverse stack..."

if (Test-PortListening -Port 8181) {
    Write-Host "Bowerbot already listening on 8181"
}
else {
    Start-BackgroundProcess -FilePath $bowerbotExe -Arguments @() -WorkingDirectory $root
    Write-Host "Started Bowerbot on http://127.0.0.1:8181/mcp"
}

if ($LaunchComposer) {
    $env:OMNIVERSE_ROOT = $root
    $env:OMNIVERSE_SCENE_MCP_SRC = (Join-Path $omniverseRoot "src")
    $env:OMNIVERSE_SCENE_CONTROL_HOST = "127.0.0.1"
    $env:OMNIVERSE_SCENE_CONTROL_PORT = "9910"
    $env:OMNIVERSE_SCENE_HTTP_ENDPOINT = "http://127.0.0.1:9910"
    Start-Process -FilePath (Join-Path $kitRoot "repo.bat") -ArgumentList @("launch") -WorkingDirectory $kitRoot
    Write-Host "Launching USD Composer..."
    if (Wait-PortListening -Port 9910 -TimeoutSeconds 60) {
        Write-Host "Composer live scene control endpoint ready on http://127.0.0.1:9910/rpc"
    }
    else {
        Write-Host "Composer live scene control endpoint did not come up within 60 seconds."
        Remove-Item Env:OMNIVERSE_ROOT -ErrorAction SilentlyContinue
        Remove-Item Env:OMNIVERSE_SCENE_MCP_SRC -ErrorAction SilentlyContinue
        Remove-Item Env:OMNIVERSE_SCENE_CONTROL_HOST -ErrorAction SilentlyContinue
        Remove-Item Env:OMNIVERSE_SCENE_CONTROL_PORT -ErrorAction SilentlyContinue
        Remove-Item Env:OMNIVERSE_SCENE_HTTP_ENDPOINT -ErrorAction SilentlyContinue
    }
}
else {
    Remove-Item Env:OMNIVERSE_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:OMNIVERSE_SCENE_MCP_SRC -ErrorAction SilentlyContinue
    Remove-Item Env:OMNIVERSE_SCENE_CONTROL_HOST -ErrorAction SilentlyContinue
    Remove-Item Env:OMNIVERSE_SCENE_CONTROL_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:OMNIVERSE_SCENE_HTTP_ENDPOINT -ErrorAction SilentlyContinue
}

if (Test-PortListening -Port 9905) {
    if ($LaunchComposer) {
        Write-Host "Restarting router on 9905 so it picks up the live Composer endpoint..."
        Stop-PortListener -Port 9905
    }
    else {
        Write-Host "Router already listening on 9905"
    }
}
if (-not (Test-PortListening -Port 9905)) {
    Start-BackgroundProcess `
        -FilePath $pythonExe `
        -Arguments @("-m", "omniverse_scene_mcp", "serve", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "9905") `
        -WorkingDirectory $omniverseRoot
    Write-Host "Started Omniverse router on http://127.0.0.1:9905/mcp"
}

if (-not $LaunchComposer) {
    Write-Host "USD Composer not launched. Re-run with -LaunchComposer if you want live scene awareness in Composer."
}

Write-Host ""
Write-Host "Ready. Use Codex against the router or open USD Composer with -LaunchComposer for live scene awareness."
