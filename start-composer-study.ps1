# Desktop shortcut entry point: a fresh, unsaved Composer stage every launch.
$ErrorActionPreference = 'Stop'
$composerDirectory = Join-Path $PSScriptRoot 'kit-app-template\_build\windows-x86_64\release'
$composerLauncher = Join-Path $composerDirectory 'my_company.my_usd_composer.kit.bat'
if (-not (Test-Path -LiteralPath $composerLauncher)) { throw "Composer build is missing: $composerLauncher" }
$composerEnvNames = @('OMNIVERSE_ROOT','OMNIVERSE_SCENE_MCP_SRC','OMNIVERSE_SCENE_FILE','OMNIVERSE_EMPTY_STAGE_ON_START','OMNIVERSE_SCENE_CONTROL_PORT')
$composerPreviousEnv = @{}
foreach ($composerEnvName in $composerEnvNames) { $composerPreviousEnv[$composerEnvName] = [Environment]::GetEnvironmentVariable($composerEnvName, 'Process') }
try {
    $env:OMNIVERSE_ROOT = $PSScriptRoot
    $env:OMNIVERSE_SCENE_MCP_SRC = Join-Path $PSScriptRoot 'omniverse_scene_mcp\src'
    $env:OMNIVERSE_SCENE_FILE = ''
    $env:OMNIVERSE_EMPTY_STAGE_ON_START = '1'
    # The setup extension's stock scene server moves to 9909; start_composer_bridge.py serves the extended one on 9910.
    $env:OMNIVERSE_SCENE_CONTROL_PORT = '9909'
    & $composerLauncher '--/app/auto_load_usd=' '--/app/content/emptyStageOnStart=false' '--exec' (Join-Path $PSScriptRoot 'composer-study\startup.py') '--exec' (Join-Path $PSScriptRoot 'composer-study\start_composer_bridge.py')
} finally {
    foreach ($composerEnvName in $composerEnvNames) { [Environment]::SetEnvironmentVariable($composerEnvName, $composerPreviousEnv[$composerEnvName], 'Process') }
}
