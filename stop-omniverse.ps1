param()

$ErrorActionPreference = "Stop"

function Get-ListeningProcessIds {
    param(
        [int[]]$Ports
    )

    $pids = New-Object System.Collections.Generic.List[int]
    foreach ($port in $Ports) {
        $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($connection in $connections) {
            if ($connection.OwningProcess -gt 0) {
                $null = $pids.Add([int]$connection.OwningProcess)
            }
        }
    }

    return $pids | Select-Object -Unique
}

$ports = @(8181, 9905)
$pids = Get-ListeningProcessIds -Ports $ports

if (-not $pids) {
    Write-Host "No Omniverse launcher processes found on ports 8181 or 9905."
    exit 0
}

foreach ($pid in $pids) {
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($null -ne $proc) {
        Write-Host "Stopping process $($proc.ProcessName) (PID $pid)..."
        Stop-Process -Id $pid -Force
    }
}

Write-Host "Stopped Omniverse launcher processes."
