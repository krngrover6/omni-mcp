$ErrorActionPreference = "Continue"

$usdaFile = "C:\Users\krngr\Downloads\OpenUSD_Lab-Assets\OpenUSD_Lab-Assets\OpenUSD_Lab\Assemblies\Factory_Complete.usda"
$blenderExe = "C:\Program Files\Blender Foundation\Blender 4.3\blender.exe"
$exportScript = "C:\Users\krngr\Downloads\Omniverse\usd_to_glb.py"
$viewerDir = "C:\Users\krngr\Downloads\Omniverse\factory-viewer"
$logFile = "C:\Users\krngr\Downloads\Omniverse\watch_and_deploy.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

function Do-ExportAndDeploy {
    Write-Log "Change detected -> exporting via Blender..."
    & $blenderExe --background --python $exportScript *> $null
    if (-not (Test-Path "$viewerDir\factory.glb")) {
        Write-Log "ERROR: export failed, glb not found. Skipping deploy."
        return
    }
    Write-Log "Export done. Deploying to Vercel (production)..."
    Push-Location $viewerDir
    $result = vercel --prod --yes 2>&1 | Out-String
    Pop-Location
    Write-Log "Deploy finished."
    Write-Log $result
}

Write-Log "Watching for changes to: $usdaFile"
Write-Log "Save in USD Composer (Ctrl+S) to trigger an automatic re-export + redeploy."

$folder = Split-Path $usdaFile
$file = Split-Path $usdaFile -Leaf

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $folder
$watcher.Filter = $file
$watcher.NotifyFilter = [System.IO.NotifyFilters]::LastWrite
$watcher.EnableRaisingEvents = $true

$lastTrigger = Get-Date "2000-01-01"
$debounceSeconds = 5

while ($true) {
    $result = $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::Changed, 2000)
    if ($result.TimedOut) { continue }

    $now = Get-Date
    if (($now - $lastTrigger).TotalSeconds -lt $debounceSeconds) { continue }
    $lastTrigger = $now

    Start-Sleep -Seconds 3
    Do-ExportAndDeploy
}
