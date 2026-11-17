[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [int]$Start = 1,
    [int]$MaxJournals = 0,
    [int]$MediaWorkers = 4,
    [int]$WaitSeconds = 45,
    [int]$RetryAttempts = 5,
    [switch]$PlanOnly,
    [string]$Python = 'D:\miniforge3\python.exe'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$RunPath = if ([IO.Path]::IsPathRooted($RunDir)) { $RunDir } else { Join-Path $ProjectRoot $RunDir }
$ControlPath = Join-Path $RunPath 'run_control.json'
$Launcher = Join-Path $PSScriptRoot 'start_msaa_full_run.ps1'
$AutoRetry = Join-Path $PSScriptRoot 'auto_retry_full_run.ps1'
$Backfill = Join-Path $PSScriptRoot 'backfill_msaa_queue.ps1'
$PrepareRetries = Join-Path $PSScriptRoot 'prepare_skipped_retries.py'
$BackfillQueue = Join-Path $RunPath 'journal_backfill_queue.jsonl'

foreach ($path in @($Launcher, $AutoRetry, $Backfill, $PrepareRetries)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "production_script_missing:$path" }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'python_executable_not_found' }

if ($PlanOnly) {
    $manifestPath = Join-Path $RunPath 'run_manifest.json'
    $journalCount = 0
    $statusSummary = @()
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $journals = @($manifest.journals)
        $journalCount = $journals.Count
        $statusSummary = @($journals | Group-Object status | Sort-Object Name | ForEach-Object {
            [pscustomobject]@{ status = $_.Name; count = $_.Count }
        })
    }
    $queueCount = if (Test-Path -LiteralPath $BackfillQueue -PathType Leaf) {
        @(Get-Content -LiteralPath $BackfillQueue -Encoding UTF8 | Where-Object { $_.Trim() }).Count
    } else { 0 }
    $preparePlan = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        (& $Python -X utf8 -B $PrepareRetries --run-dir $RunDir --dry-run | Out-String) | ConvertFrom-Json
    } else {
        [pscustomobject]@{ article_retry_count = 0; article_retries = @() }
    }
    $backfillPlan = if (Test-Path -LiteralPath $BackfillQueue -PathType Leaf) {
        (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Backfill -RunDir $RunDir -PlanOnly | Out-String) | ConvertFrom-Json
    } else {
        [pscustomobject]@{ plan = 'backfill_plan'; pending_count = 0; pending = @() }
    }
    $control = if (Test-Path -LiteralPath $ControlPath -PathType Leaf) {
        Get-Content -LiteralPath $ControlPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else { [pscustomobject]@{ stop_requested = $false } }
    [pscustomobject]@{
        plan = 'production_plan'
        run_dir = $RunPath
        manifest = $manifestPath
        journal_count = $journalCount
        statuses = $statusSummary
        article_retry_count = [int]$preparePlan.article_retry_count
        article_retries = @($preparePlan.article_retries)
        backfill_queue_count = $queueCount
        backfill_pending_count = [int]$backfillPlan.pending_count
        backfill_pending = @($backfillPlan.pending)
        stop_requested = [bool]$control.stop_requested
        media_workers = $MediaWorkers
        retry_attempts = $RetryAttempts
        final_csv = Join-Path $RunPath 'delivery\文章数据_中文表头.csv'
    } | ConvertTo-Json -Depth 8
    exit 0
}

function Test-StopRequested {
    if (-not (Test-Path -LiteralPath $ControlPath -PathType Leaf)) { return $false }
    try {
        $control = Get-Content -LiteralPath $ControlPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($control.stop_requested -eq $true)
    } catch { return $false }
}

function Invoke-HiddenPowerShell([string]$Path, [string[]]$Arguments) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments
    return $LASTEXITCODE
}

Write-Host 'production_full_126_start'
$env:MSAA_PRODUCTION_ORCHESTRATOR = '1'
$launcherArgs = @(
    '-RunDir', $RunDir, '-Start', [string]$Start, '-MediaWorkers', [string]$MediaWorkers,
    '-WaitSeconds', [string]$WaitSeconds, '-RefreshArticle'
)
if ($MaxJournals -gt 0) { $launcherArgs += @('-MaxJournals', [string]$MaxJournals) }
$initialCode = Invoke-HiddenPowerShell $Launcher $launcherArgs
if (Test-StopRequested) { Write-Host 'production_stop_requested'; exit 130 }

$retryCode = 0
if ($initialCode -ne 0) {
    Write-Host ("production_retry_start initial_exit={0}" -f $initialCode)
    $retryCode = Invoke-HiddenPowerShell $AutoRetry @(
        '-RunnerPid', '0', '-RunDir', $RunDir, '-Start', [string]$Start,
        '-MediaWorkers', [string]$MediaWorkers, '-MaxAttempts', [string]$RetryAttempts
    )
    if (Test-StopRequested) { Write-Host 'production_stop_requested'; exit 130 }
}

$backfillCode = 0
if (Test-Path -LiteralPath $BackfillQueue -PathType Leaf) {
    Write-Host 'production_backfill_start'
    $backfillCode = Invoke-HiddenPowerShell $Backfill @(
        '-RunDir', $RunDir, '-MediaWorkers', [string]$MediaWorkers,
        '-WaitSeconds', [string]$WaitSeconds, '-Python', $Python
    )
    if (Test-StopRequested) { Write-Host 'production_stop_requested'; exit 130 }
}

& $Python -X utf8 -B $PrepareRetries --run-dir $RunDir
$prepareCode = $LASTEXITCODE
if (Test-StopRequested) { Write-Host 'production_stop_requested'; exit 130 }

Write-Host 'production_final_export_start'
& $Python -X utf8 -B -m export.msaa_global_csv --run-dir $RunDir --chinese-header-copy
$mergeCode = $LASTEXITCODE

if ($prepareCode -ne 0) { exit $prepareCode }
if ($mergeCode -ne 0) { exit $mergeCode }
$manifestPath = Join-Path $RunPath 'run_manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$remaining = @($manifest.journals | Where-Object { $_.status -ne 'completed' })
if ($remaining.Count -gt 0) {
    $statusSummary = @($manifest.journals | Group-Object status | Sort-Object Name | ForEach-Object {
        [pscustomobject]@{ status = $_.Name; count = $_.Count }
    })
    [pscustomobject]@{
        status = 'production_final_status'
        completed = $false
        remaining_count = $remaining.Count
        statuses = $statusSummary
    } | ConvertTo-Json -Depth 5
    exit 2
}
Write-Host 'production_complete'
exit 0
