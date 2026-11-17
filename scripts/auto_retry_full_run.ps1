[CmdletBinding()]
param(
    [int]$RunnerPid = 4688,
    [string]$RunDir = 'output/rollout/full_126',
    [int]$Start = 6,
    [int]$MediaWorkers = 4,
    [int]$PollSeconds = 60,
    [int]$MaxAttempts = 5,
    [string]$Python = 'D:\miniforge3\python.exe'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$OuterLauncher = (Resolve-Path (Join-Path $PSScriptRoot 'start_msaa_full_run.ps1')).Path
$PrepareScript = Join-Path $ProjectRoot 'scripts\prepare_skipped_retries.py'
$RunPath = Join-Path $ProjectRoot $RunDir
$ManifestPath = Join-Path $RunPath 'run_manifest.json'
$ControlPath = Join-Path $RunPath 'run_control.json'
$LogRoot = Join-Path $RunPath 'logs'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

function Test-StopRequested {
    if (-not (Test-Path -LiteralPath $ControlPath)) { return $false }
    try {
        $control = Get-Content -LiteralPath $ControlPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($control.stop_requested -eq $true)
    } catch {
        return $false
    }
}

function Prepare-SkippedRetries {
    if (-not (Test-Path -LiteralPath $PrepareScript -PathType Leaf)) {
        throw 'prepare_skipped_retries_missing'
    }
    & $Python -X utf8 -B $PrepareScript --run-dir $RunPath
    if ($LASTEXITCODE -ne 0) {
        throw "prepare_skipped_retries_failed:$LASTEXITCODE"
    }
}

function Get-PendingJournals {
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    return @($manifest.journals | Where-Object { $_.status -notin @('completed', 'completed_with_skips', 'needs_backfill') })
}

if ($RunnerPid -gt 0) {
    Write-Host "自动重试看门狗已启动：等待首轮 PID $RunnerPid 结束"
    while ($true) {
        if (Test-StopRequested) {
            Write-Host '检测到 stop_requested=true，看门狗停止。'
            exit 130
        }
        if (-not (Get-Process -Id $RunnerPid -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    }
} else {
    Write-Host 'initial_runner_already_exited：直接检查未完成期刊。'
}

Write-Host '首轮采集进程已退出，检查未完成期刊。'
Start-Sleep -Seconds 20
for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    if (Test-StopRequested) {
        Write-Host '检测到 stop_requested=true，取消自动重试。'
        exit 130
    }
    Prepare-SkippedRetries
    $pending = @(Get-PendingJournals)
    if ($pending.Count -eq 0) {
        Write-Host '全部期刊已完成或已进入独立补采名单，看门狗结束。'
        exit 0
    }

    $pendingText = ($pending | ForEach-Object { "$($_.ordinal):$($_.status)" }) -join ', '
    $freshPending = @($pending | Where-Object { $_.status -eq 'pending' })
    $startCandidates = if ($freshPending.Count -gt 0) { $freshPending } else { $pending }
    $startOrdinal = ($startCandidates | ForEach-Object { [int]$_.ordinal } | Sort-Object | Select-Object -First 1)
    if ($null -eq $startOrdinal) { $startOrdinal = $Start }
    Write-Host "自动重试第 $attempt/$MaxAttempts 轮；最早未完成刊=$startOrdinal；未完成：$pendingText"
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $stdout = Join-Path $LogRoot "auto_retry_${stamp}_attempt${attempt}.log"
    $stderr = Join-Path $LogRoot "auto_retry_${stamp}_attempt${attempt}.err.log"
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$OuterLauncher`"",
        '-Start', [string]$startOrdinal, '-MediaWorkers', [string]$MediaWorkers, '-WaitSeconds', '45', '-RefreshArticle'
    )
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments `
        -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Write-Host "自动重试轮次结束：exit_code=$($process.ExitCode)｜log=$stdout"
    Start-Sleep -Seconds 30
}

$remaining = @(Get-PendingJournals)
if ($remaining.Count -gt 0) {
    $remainingText = ($remaining | ForEach-Object { "$($_.ordinal):$($_.status)" }) -join ', '
    Write-Host "自动重试达到上限，仍有未完成项：$remainingText"
    exit 2
}
Write-Host '自动重试全部完成。'
exit 0
