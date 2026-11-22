# Start the router watchdog automatically when you log in to Windows (or stop doing that with -Uninstall).
#
# It puts one shortcut, "Omniverse Router Watchdog.lnk", in your own Startup folder (shell:startup). No admin
# rights, no Task Scheduler, nothing system-wide: delete the shortcut (or run with -Uninstall) and it is gone.
# At login the shortcut opens router_watchdog.ps1 in a minimized window; the watchdog starts the router if it is down
# and keeps it running (max 4 restarts an hour). Close that window to stop it for the session.
#
#   powershell -ExecutionPolicy Bypass -File omniverse_scene_mcp\tools\install_router_watchdog.ps1
#   powershell -ExecutionPolicy Bypass -File omniverse_scene_mcp\tools\install_router_watchdog.ps1 -Uninstall
[CmdletBinding()]
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Omniverse Router Watchdog.lnk'
$watchdog = Join-Path $PSScriptRoot 'router_watchdog.ps1'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

if ($Uninstall) {
    if (Test-Path -LiteralPath $link) {
        Remove-Item -LiteralPath $link
        Write-Host "Removed $link. The watchdog no longer starts at login."
    } else {
        Write-Host 'Nothing to remove: the watchdog was not set to start at login.'
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $watchdog)) { throw "Missing $watchdog" }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$watchdog`""
$shortcut.WorkingDirectory = $workspace
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = 'Keeps the Omniverse MCP router (port 9905) running. Remove with install_router_watchdog.ps1 -Uninstall.'
$shortcut.Save()
Write-Host "Installed: $link"
Write-Host 'From the next login the watchdog starts by itself. To start it now, run router_watchdog.ps1 once.'
