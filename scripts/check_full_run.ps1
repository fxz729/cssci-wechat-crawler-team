[CmdletBinding()]
param(
    [ValidateSet('capture-first','background-only')]
    [string]$Phase = 'capture-first',
    [string]$RunDir = 'output/rollout/full_126',
    [string]$PilotDir = 'output/pilot_20260906',
    [string]$JournalLinks = 'data/sources/CSSCI来源刊公众号主页链接.md',
    [int]$ExpectedCount = 126,
    [string]$FiddlerExe = '..\Fiddler\Fiddler.exe'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
& python -X utf8 -B -c "from sources.fixed_roster import load_fixed_roster; r=load_fixed_roster(r'$JournalLinks',$ExpectedCount); print('固定名单校验通过：'+str(r.count))"
if ($LASTEXITCODE -ne 0) { throw 'fixed_roster_check_failed' }
if ($Phase -eq 'background-only') {
    Write-Host "后台阶段预检通过；不需要微信、搜一搜或 Fiddler；运行目录=$RunDir"
    exit 0
}
$fiddlerPath = if ([IO.Path]::IsPathRooted($FiddlerExe)) { $FiddlerExe } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $FiddlerExe)) }
if (-not (Test-Path -LiteralPath $fiddlerPath -PathType Leaf)) { throw 'fiddler_executable_not_found' }
$pilotRoot = if ([IO.Path]::IsPathRooted($PilotDir)) { $PilotDir } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $PilotDir)) }
$activationPath = Join-Path $pilotRoot 'capture_activation.json'
$controlPath = Join-Path $pilotRoot 'capture_control.json'
if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf) -or -not (Test-Path -LiteralPath $controlPath -PathType Leaf)) {
    throw 'capture_activation_missing'
}
$activation = Get-Content -LiteralPath $activationPath -Raw -Encoding UTF8 | ConvertFrom-Json
$control = Get-Content -LiteralPath $controlPath -Raw -Encoding UTF8 | ConvertFrom-Json
$activeRules = [string]$activation.active_path
$targetBiz = [string]$control.target_biz
if ([string]::IsNullOrWhiteSpace($activeRules) -or [string]::IsNullOrWhiteSpace($targetBiz)) {
    throw 'capture_activation_invalid'
}
$refreshOutput = @(& python -X utf8 -B capture/setup.py `
    --fiddler-dir (Split-Path -Parent $fiddlerPath) --output-dir $pilotRoot `
    --target-biz $targetBiz --activate-path $activeRules 2>&1)
if ($LASTEXITCODE -ne 0) {
    $refreshOutput | ForEach-Object { Write-Host $_ }
    throw 'capture_rules_refresh_failed'
}
$refreshOutput | ForEach-Object { Write-Host $_ }
$fiddler = Get-Process -Name 'Fiddler' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $fiddler) {
    $fiddler = Start-Process -FilePath $fiddlerPath -WindowStyle Hidden -PassThru
    Write-Host "已启动 Fiddler；PID=$($fiddler.Id)"
    Start-Sleep -Seconds 3
}
$ensureCaptureOutput = @(& powershell -NoProfile -File (Join-Path $ProjectRoot 'crawl\ensure_fiddler_capture.ps1') -ListenPort 8899 2>&1)
if ($LASTEXITCODE -ne 0) {
    $ensureCaptureOutput | ForEach-Object { Write-Host $_ }
    throw 'fiddler_capture_reenable_failed'
}
$ensureCaptureOutput | ForEach-Object { Write-Host $_ }
$ensureText = & powershell -NoProfile -File (Join-Path $ProjectRoot 'crawl\ensure_search_window.ps1')
if ($LASTEXITCODE -ne 0) { throw 'search_window_recovery_failed' }
Write-Host $ensureText
$inspectionText = & powershell -NoProfile -File (Join-Path $ProjectRoot 'crawl\profile_switch.ps1') -InspectOnly
if ($LASTEXITCODE -ne 0) { throw 'search_window_inspection_failed' }
$inspection = $inspectionText | ConvertFrom-Json
$searchWindows = @($inspection.windows | Where-Object { $_.search_input -eq $true })
if ($searchWindows.Count -eq 0) { throw 'search_window_required' }
if ($searchWindows.Count -gt 1) { throw 'ambiguous_search_window' }
$hwnd = [int]$searchWindows[0].browser_hwnd
if ($hwnd -le 0) { throw 'invalid_browser_window' }
$healthPassed = $false
$lastHealth = @()
for ($attempt = 1; $attempt -le 15; $attempt++) {
    $lastHealth = @(& python -X utf8 -B main.py check-wechat-capture --hwnd $hwnd --pilot-dir $PilotDir --fiddler-exe $fiddlerPath --listen-port 8899 2>&1)
    if ($LASTEXITCODE -eq 0) { $healthPassed = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $healthPassed) {
    $lastHealth | ForEach-Object { Write-Host $_ }
    throw 'capture_health_failed'
}
$lastHealth | ForEach-Object { Write-Host $_ }
Write-Host "前台缓存预检通过；搜一搜 HWND=$hwnd；Fiddler PID=$($fiddler.Id)；运行目录=$RunDir"
