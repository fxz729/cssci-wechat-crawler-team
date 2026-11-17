[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$FiddlerExe = '',
    [switch]$ActivateFiddlerRules,
    [switch]$SkipInstall
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$ProjectRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $ProjectRoot
function Resolve-Python {
    param([string]$Requested)
    if ($Requested -and (Test-Path -LiteralPath $Requested -PathType Leaf)) { return (Resolve-Path $Requested).Path }
    foreach ($candidate in @('D:\miniforge3\python.exe','C:\Users\11075\miniforge3\python.exe')) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $cmd=Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'python_not_found：请先安装 Python 3.10+，或用 -Python 指定 python.exe'
}
$Python=Resolve-Python $Python
Write-Host "Python：$Python"
$pilot=Join-Path $ProjectRoot 'output\pilot_20260906'; New-Item -ItemType Directory -Path $pilot -Force | Out-Null; $control=Join-Path $pilot 'capture_control.json'; if(-not (Test-Path $control)){ '{"enabled":false,"target_biz":"","expires_at":null,"diagnostic_enabled":false,"diagnostic_expires_at":null,"diagnostic_after_id":0,"diagnostic_all_articles":false,"diagnostic_mid":"","diagnostic_idx":""}' | Set-Content $control -Encoding utf8 }; New-Item -ItemType File -Path (Join-Path $pilot 'capture_inbox.jsonl') -Force | Out-Null
if ($ActivateFiddlerRules) {
    if (-not $FiddlerExe -or -not (Test-Path -LiteralPath $FiddlerExe -PathType Leaf)) { throw 'fiddler_executable_required_for_activation' }
    $fiddlerDir=Split-Path (Resolve-Path $FiddlerExe) -Parent
    $activatePath=Join-Path $fiddlerDir 'Scripts\CustomRules.js'
    & $Python -X utf8 -B -m capture.setup --output-dir $pilot --fiddler-dir $fiddlerDir --target-biz 'MzUzNzUwMzQ0OA==' --activate-path $activatePath
    if($LASTEXITCODE -ne 0){throw "fiddler_rules_activation_failed:$LASTEXITCODE"}
    Write-Host 'Fiddler 规则已按安全校验安装。'
}
if (-not $SkipInstall) {
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip_install_failed:$LASTEXITCODE" }
}
& $Python -X utf8 -B -m compileall -q crawl export parsers sources utils
if ($LASTEXITCODE -ne 0) { throw "compile_failed:$LASTEXITCODE" }
$run=Join-Path $ProjectRoot 'output\rollout\full_126'
New-Item -ItemType Directory -Force -Path $run | Out-Null
$control=Join-Path $run 'run_control.json'
if (-not (Test-Path -LiteralPath $control)) {
    '{"schema_version":1,"stop_requested":false}' | Set-Content -LiteralPath $control -Encoding utf8
}
if (Test-Path -LiteralPath (Join-Path $run 'run_manifest.json')) {
    & $Python -X utf8 -B -m export.msaa_global_csv --run-dir $run --chinese-header-copy
    if ($LASTEXITCODE -notin @(0,2)) { throw "initial_merge_failed:$LASTEXITCODE" }
}
Write-Host '环境准备完成。下一步：.\scripts\start_team_handoff.ps1'
