[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [string]$PilotDir = 'output/pilot_20260906',
    [string]$JournalLinks = 'data/sources/CSSCI来源刊公众号主页链接.md',
    [int]$ExpectedCount = 126,
    [int]$Start = 1,
    [int]$MaxJournals = 0,
    [int]$MaxSteps = 2000,
    [double]$WaitSeconds = 60,
    [int]$Attempts = 2,
    [double]$SleepSeconds = 1,
    [int]$MediaWorkers = 4,
    [string]$FiddlerExe = '..\Fiddler\Fiddler.exe'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$controlPath = Join-Path $RunDir 'run_control.json'
Write-Host "运行控制文件：$controlPath"
$common = @{
    RunDir=$RunDir; PilotDir=$PilotDir; JournalLinks=$JournalLinks; ExpectedCount=$ExpectedCount
    Start=$Start; MaxJournals=$MaxJournals; MaxSteps=$MaxSteps; WaitSeconds=$WaitSeconds
    Attempts=$Attempts; SleepSeconds=$SleepSeconds; MediaWorkers=$MediaWorkers
}
& (Join-Path $PSScriptRoot 'start_capture_first.ps1') @common -FiddlerExe $FiddlerExe
$captureCode = $LASTEXITCODE
if ($captureCode -notin @(0, 2)) {
    Write-Host "capture_first_failed：返回码=$captureCode；已保留断点，本次不进入后台物化。"
    exit $captureCode
}
if ($captureCode -eq 2) {
    Write-Host 'capture_first_partial：已保存全部可验证缓存和独立补采索引；继续物化完整缓存并导出 CSV。'
} else {
    Write-Host '126 刊前台缓存全部通过，立即进入 background-only。'
}
& (Join-Path $PSScriptRoot 'start_background_only.ps1') @common
$backgroundCode = $LASTEXITCODE
if ($backgroundCode -notin @(0, 2)) {
    exit $backgroundCode
}
if ($captureCode -eq 2 -or $backgroundCode -eq 2) {
    Write-Host 'full_run_partial：CSV 已按可验证缓存导出；仍有独立补采项，不宣称全量完成。'
    exit 2
}
exit 0
