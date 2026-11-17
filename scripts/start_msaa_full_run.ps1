[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [string]$JournalLinks = 'data/sources/CSSCI来源刊公众号主页链接.md',
    [int]$Start = 1,
    [int]$MaxJournals = 0,
    [string]$CaptureInbox = 'output/pilot_20260906/capture_inbox.jsonl',
    [string]$SeedDir = 'output/v12_validation/manual_ui_capture',
    [string]$SeedBiz = 'MzUzNzUwMzQ0OA==',
    [int]$MediaWorkers = 4,
    [int]$ProfileWaitSeconds = 30,
    [int]$DelaySeconds = 2,
    [int]$WaitSeconds = 60,
    [int]$ScrollDelta = -600,
    [int]$MaxIdleScrolls = 300,
    [switch]$ImportOnly,
    [switch]$PreflightOnly,
    [switch]$RefreshArticle = $true,
    [switch]$Backfill,
    [string]$Python = 'D:\miniforge3\python.exe',
    [string]$FiddlerExe = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'python_executable_not_found'
}
& $Python -X utf8 -B -c "from sources.fixed_roster import load_fixed_roster; r=load_fixed_roster(r'$JournalLinks',126); print('固定名单校验通过：'+str(r.count))"
if ($LASTEXITCODE -ne 0) { throw 'fixed_roster_check_failed' }

if (-not $FiddlerExe -and $env:CSSCI_FIDDLER_EXE) { $FiddlerExe = $env:CSSCI_FIDDLER_EXE }
if (-not $FiddlerExe) { $FiddlerExe = '..\Fiddler\Fiddler.exe' }
$fiddlerPath = if ([IO.Path]::IsPathRooted($FiddlerExe)) {
    $FiddlerExe
} else {
    [IO.Path]::GetFullPath((Join-Path $ProjectRoot $FiddlerExe))
}
if (-not (Test-Path -LiteralPath $fiddlerPath -PathType Leaf)) {
    throw 'fiddler_executable_not_found'
}
$fiddler = Get-Process -Name 'Fiddler' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $fiddler) {
    $fiddler = Start-Process -FilePath $fiddlerPath -WindowStyle Hidden -PassThru
    Write-Host "已启动 Fiddler；PID=$($fiddler.Id)"
    Start-Sleep -Seconds 3
}
$pilotRoot = if ([IO.Path]::IsPathRooted($CaptureInbox)) {
    Split-Path -Parent $CaptureInbox
} else {
    [IO.Path]::GetFullPath((Join-Path $ProjectRoot (Split-Path -Parent $CaptureInbox)))
}
if (-not (Test-Path -LiteralPath (Join-Path $pilotRoot 'capture_control.json') -PathType Leaf)) {
    throw 'capture_control_missing'
}

if (-not $PreflightOnly -and -not $ImportOnly) {
    $prepareScript = Join-Path $PSScriptRoot 'prepare_skipped_retries.py'
    if (-not (Test-Path -LiteralPath $prepareScript -PathType Leaf)) {
        throw 'prepare_skipped_retries_missing'
    }
    $runManifest = if ([IO.Path]::IsPathRooted($RunDir)) {
        Join-Path $RunDir 'run_manifest.json'
    } else {
        Join-Path $ProjectRoot (Join-Path $RunDir 'run_manifest.json')
    }
    if (Test-Path -LiteralPath $runManifest -PathType Leaf) {
        & $Python -X utf8 -B $prepareScript --run-dir $RunDir
        if ($LASTEXITCODE -ne 0) { throw "prepare_skipped_retries_failed:$LASTEXITCODE" }
        $cachedImporter = Join-Path $PSScriptRoot 'import_cached_msaa.py'
        if (-not (Test-Path -LiteralPath $cachedImporter -PathType Leaf)) {
            throw 'import_cached_msaa_missing'
        }
        & $Python -X utf8 -B $cachedImporter --run-dir $RunDir --min-rows 20 --media-workers 0
        if ($LASTEXITCODE -ne 0) { throw "import_cached_msaa_failed:$LASTEXITCODE" }
    } else {
        Write-Host '运行清单尚未创建，跳过完成标记与 skipped 预处理。'
    }
}

# 先把上次已经生成但尚未进入总表的 bundle 导入，确保换机/续跑不会遗漏历史结果。
if (-not $PreflightOnly) {
    & $Python -X utf8 -B -m export.msaa_global_csv --run-dir $RunDir --chinese-header-copy
    $preMergeCode = $LASTEXITCODE
    if ($preMergeCode -notin @(0, 2)) { throw "pre_merge_failed:$preMergeCode" }
    Write-Host "启动前总 CSV 合并完成：exit=$preMergeCode"
}

function Test-CaptureKeeperRunning([string]$PidPath) {
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) { return $false }
    $keeperPid = (Get-Content -LiteralPath $PidPath -Raw -ErrorAction SilentlyContinue).Trim()
    if ($keeperPid -notmatch '^[0-9]+$') { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$keeperPid" -ErrorAction SilentlyContinue
    return ($null -ne $process -and $process.CommandLine -match 'keep_capture_window\.py')
}

if (-not $ImportOnly -and -not $PreflightOnly) {
    $keeperPidPath = Join-Path $pilotRoot 'capture_window_keeper.pid'
    if (-not (Test-CaptureKeeperRunning $keeperPidPath)) {
        $keeperScript = Join-Path $PSScriptRoot 'keep_capture_window.py'
        if (-not (Test-Path -LiteralPath $keeperScript -PathType Leaf)) { throw 'capture_keeper_script_missing' }
        $runPath = if ([IO.Path]::IsPathRooted($RunDir)) { $RunDir } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $RunDir)) }
        $keeperArgs = @(
            '-X', 'utf8', '-B', ('"' + $keeperScript + '"'),
            '--output-dir', ('"' + $pilotRoot + '"'),
            '--run-control', ('"' + (Join-Path $runPath 'run_control.json') + '"')
        )
        $keeper = Start-Process -FilePath $Python -ArgumentList $keeperArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $pilotRoot 'capture_window_keeper.log') `
            -RedirectStandardError (Join-Path $pilotRoot 'capture_window_keeper.log.err')
        Set-Content -LiteralPath $keeperPidPath -Value $keeper.Id -Encoding ascii
        Write-Host "抓包窗口续期守护已启动：PID=$($keeper.Id)"
    }
}

Write-Host 'MSAA 全量模式启动：名称搜索 → H5 主页窗口 → 逐篇采集 → bundle/CSV 导入'
Write-Host "运行目录：$RunDir｜起始刊：$Start｜Fiddler PID=$($fiddler.Id)"
$extraArgs = @('--skip-completed')
if ($ImportOnly) { $extraArgs += '--import-only' }
if ($PreflightOnly) { $extraArgs += '--preflight-only' }
if ($RefreshArticle) { $extraArgs += '--refresh-article' }
if ($Backfill) { $extraArgs += @('--backfill', '--start-from-top') }
& $Python -X utf8 -B -m crawl.msaa_roster_runner `
    --run-dir $RunDir `
    --journal-links $JournalLinks `
    --start $Start `
    --max-journals $MaxJournals `
    --capture-inbox $CaptureInbox `
    --seed-dir $SeedDir `
    --seed-biz $SeedBiz `
    --profile-wait-seconds $ProfileWaitSeconds `
    --delay-seconds $DelaySeconds `
    --wait-seconds $WaitSeconds `
    --scroll-delta $ScrollDelta `
    --max-idle-scrolls $MaxIdleScrolls `
    --media-workers $MediaWorkers `
    @extraArgs
$runCode = $LASTEXITCODE
if ($PreflightOnly) { exit $runCode }
$mergeArgs = @("-X", "utf8", "-B", "-m", "export.msaa_global_csv", "--run-dir", $RunDir)
if ($runCode -eq 0) { $mergeArgs += "--chinese-header-copy" }
& $Python @mergeArgs
$mergeCode = $LASTEXITCODE
if ($runCode -ne 0) { exit $runCode }
exit $mergeCode
