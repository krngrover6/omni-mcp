@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-omniverse.ps1" %*

endlocal
