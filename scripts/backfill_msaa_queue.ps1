[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [int]$MediaWorkers = 4,
    [int]$WaitSeconds = 45,
    [switch]$PlanOnly,
    [string]$Python = 'D:\miniforge3\python.exe'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$RunPath = if ([IO.Path]::IsPathRooted($RunDir)) { $RunDir } else { Join-Path $ProjectRoot $RunDir }
$QueuePath = Join-Path $RunPath 'journal_backfill_queue.jsonl'
$ProgressPath = Join-Path $RunPath 'backfill_progress.json'
$ResultsPath = Join-Path $RunPath 'backfill_results.jsonl'
$ControlPath = Join-Path $RunPath 'run_control.json'
$Launcher = Join-Path $PSScriptRoot 'start_msaa_full_run.ps1'
$LogRoot = Join-Path $RunPath 'logs'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $QueuePath -PathType Leaf)) {
    Write-Host 'backfill_queue_missing'
    exit 0
}
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) { throw 'start_launcher_missing' }

function Read-DoneKeys {
    if (-not (Test-Path -LiteralPath $ProgressPath -PathType Leaf)) { return @{} }
    try {
        $value = Get-Content -LiteralPath $ProgressPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $map = @{}
        foreach ($item in @($value.done)) { $map[[string]$item] = $true }
        return $map
    } catch { return @{} }
}

function Write-DoneKey([string]$Key) {
    $value = if (Test-Path -LiteralPath $ProgressPath -PathType Leaf) {
        Get-Content -LiteralPath $ProgressPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else { [pscustomobject]@{ done = @() } }
    $items = @($value.done) + @($Key) | Select-Object -Unique
    [pscustomobject]@{ done = @($items) } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ProgressPath -Encoding UTF8
}

function Test-StopRequested {
    if (-not (Test-Path -LiteralPath $ControlPath -PathType Leaf)) { return $false }
    try {
        $control = Get-Content -LiteralPath $ControlPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($control.stop_requested -eq $true)
    } catch { return $false }
}

$done = Read-DoneKeys
$records = @(Get-Content -LiteralPath $QueuePath -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
$targets = @()
foreach ($record in $records) {
    $key = ('{0}:{1}' -f [int]$record.ordinal, [string]$record.biz)
    if (-not $done.ContainsKey($key)) {
        $targets += [pscustomobject]@{ Key = $key; Ordinal = [int]$record.ordinal; Biz = [string]$record.biz; Journal = [string]$record.journal_name }
    }
}
$targets = @($targets | Sort-Object Ordinal, Biz)
if ($PlanOnly) {
    [pscustomobject]@{
        plan = 'backfill_plan'
        queue = $QueuePath
        record_count = $records.Count
        completed_key_count = $done.Count
        pending_count = $targets.Count
        pending = @($targets | ForEach-Object {
            [pscustomobject]@{ ordinal = $_.Ordinal; journal_name = $_.Journal; biz = $_.Biz }
        })
    } | ConvertTo-Json -Depth 5
    exit 0
}
$failureCount = 0

foreach ($target in $targets) {
    if (Test-StopRequested) { Write-Host 'backfill_stop_requested'; exit 130 }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $stdout = Join-Path $LogRoot ("backfill_{0:D3}_{1}.log" -f $target.Ordinal, $stamp)
    $stderr = Join-Path $LogRoot ("backfill_{0:D3}_{1}.err.log" -f $target.Ordinal, $stamp)
    Write-Host ("backfill_start ordinal={0} journal={1}" -f $target.Ordinal, $target.Journal)
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Launcher`"",
        '-RunDir', "`"$RunDir`"", '-Start', [string]$target.Ordinal, '-MaxJournals', '1',
        '-Backfill', '-MediaWorkers', [string]$MediaWorkers, '-WaitSeconds', [string]$WaitSeconds, '-RefreshArticle'
    )
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments `
        -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $status = if ($process.ExitCode -eq 0) { 'completed' } else { 'failed' }
    $result = [pscustomobject]@{
        completed_at = [DateTime]::UtcNow.ToString('o')
        ordinal = $target.Ordinal
        biz = $target.Biz
        journal_name = $target.Journal
        status = $status
        exit_code = $process.ExitCode
        log = $stdout
        error_log = $stderr
    }
    Add-Content -LiteralPath $ResultsPath -Value ($result | ConvertTo-Json -Compress) -Encoding UTF8
    if ($status -eq 'completed') {
        Write-DoneKey $target.Key
    } else {
        $failureCount += 1
    }
}
if ($failureCount -gt 0) {
    Write-Host ("backfill_queue_finished failures={0}" -f $failureCount)
    exit 2
}
Write-Host 'backfill_queue_finished'
exit 0
