[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [string]$PilotDir = 'output/pilot_20260906',
    [string]$JournalLinks = 'data/sources/CSSCI来源刊公众号主页链接.md',
    [int]$ExpectedCount = 126,
    [int]$CaptureStart = 1,
    [int]$MaxSteps = 2000,
    [double]$WaitSeconds = 60,
    [int]$Attempts = 2,
    [double]$SleepSeconds = 1,
    [int]$MediaWorkers = 4,
    [string]$FiddlerExe = '..\Fiddler\Fiddler.exe',
    [ValidateRange(2,60)][int]$LockPollSeconds = 5,
    [ValidateRange(5,300)][int]$UnlockPollSeconds = 15,
    [ValidateRange(30,600)][int]$StopTimeoutSeconds = 180,
    [ValidateRange(1,10)][int]$MaxUnlockedFailures = 3
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$RunRoot = if ([IO.Path]::IsPathRooted($RunDir)) { [IO.Path]::GetFullPath($RunDir) } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $RunDir)) }
$Ops = Join-Path $RunRoot 'ops'
$null = [IO.Directory]::CreateDirectory($Ops)
$RunId = Get-Date -Format 'yyyyMMddTHHmmss'
$LogPath = Join-Path $Ops "unlock_aware_full_126_$RunId.log"
$StatusPath = Join-Path $Ops "unlock_aware_full_126_$RunId.status.json"
$WorkerPath = Join-Path $Ops "unlock_aware_capture_worker_$RunId.ps1"
$LockPath = Join-Path $RunRoot 'unlock_aware_supervisor.lock'

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class UnlockAwareNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
}
"@

function Test-SessionLocked {
    if (Get-Process -Name 'LogonUI' -ErrorAction SilentlyContinue) { return $true }
    $title = [Text.StringBuilder]::new(256)
    [void][UnlockAwareNative]::GetWindowText([UnlockAwareNative]::GetForegroundWindow(), $title, 256)
    return $title.ToString() -like '*锁屏界面*'
}

function Write-State([string]$Phase, [hashtable]$Extra = @{}) {
    $body = [ordered]@{
        schema_version = 1
        updated_at = (Get-Date).ToString('o')
        phase = $Phase
        supervisor_pid = $PID
        run_dir = $RunRoot
    }
    foreach ($key in $Extra.Keys) { $body[$key] = $Extra[$key] }
    $temporary = "$StatusPath.tmp"
    [IO.File]::WriteAllText($temporary, ($body | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $StatusPath -Force
}

function Quote-PsLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

try {
    $SupervisorLock = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch {
    throw 'unlock_supervisor_already_running'
}

$CaptureScript = Join-Path $PSScriptRoot 'start_capture_first.ps1'
$BackgroundScript = Join-Path $PSScriptRoot 'start_background_only.ps1'
$StopScript = Join-Path $PSScriptRoot 'stop_full_run.ps1'
$WorkerBody = @"
`$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new(`$false)
`$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $(Quote-PsLiteral $ProjectRoot)
& $(Quote-PsLiteral $CaptureScript) -RunDir $(Quote-PsLiteral $RunDir) -PilotDir $(Quote-PsLiteral $PilotDir) -JournalLinks $(Quote-PsLiteral $JournalLinks) -ExpectedCount $ExpectedCount -Start $CaptureStart -MaxJournals 0 -MaxSteps $MaxSteps -WaitSeconds $WaitSeconds -Attempts $Attempts -SleepSeconds $SleepSeconds -MediaWorkers $MediaWorkers -FiddlerExe $(Quote-PsLiteral $FiddlerExe) 2>&1 | Tee-Object -FilePath $(Quote-PsLiteral $LogPath) -Append
exit `$LASTEXITCODE
"@
[IO.File]::WriteAllText($WorkerPath, $WorkerBody, [Text.UTF8Encoding]::new($true))

$ActivePath = Join-Path $Ops 'active_launcher.json'
$active = [ordered]@{
    schema_version = 1
    started_at = (Get-Date).ToString('o')
    launcher_pid = $PID
    launcher = $PSCommandPath
    log = $LogPath
    status_file = $StatusPath
    worker = $WorkerPath
    phase = 'unlock_aware_capture_then_background'
}
$activeTemporary = "$ActivePath.tmp"
[IO.File]::WriteAllText($activeTemporary, ($active | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $activeTemporary -Destination $ActivePath -Force

$captureFailures = 0
$captureCode = 1
try {
    while ($true) {
        while (Test-SessionLocked) {
            Write-State 'waiting_for_unlock' @{ capture_failures = $captureFailures }
            Start-Sleep -Seconds $UnlockPollSeconds
        }
        Write-State 'starting_capture' @{ capture_failures = $captureFailures }
        $child = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $WorkerPath + '"')
        ) -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
        Write-State 'capture_running' @{ capture_pid = $child.Id; capture_failures = $captureFailures }
        $lockedDuringRun = $false
        while (-not $child.HasExited) {
            Start-Sleep -Seconds $LockPollSeconds
            $child.Refresh()
            if (Test-SessionLocked) {
                $lockedDuringRun = $true
                Write-State 'stopping_for_lock' @{ capture_pid = $child.Id; capture_failures = $captureFailures }
                try { & $StopScript -RunDir $RunDir *>> $LogPath } catch {}
                if (-not $child.WaitForExit($StopTimeoutSeconds * 1000)) { throw 'capture_did_not_stop_after_lock' }
                break
            }
        }
        $child.Refresh()
        $captureCode = $child.ExitCode
        if ($lockedDuringRun) {
            Write-State 'waiting_for_unlock' @{ last_capture_exit = $captureCode; capture_failures = $captureFailures }
            continue
        }
        if ($captureCode -in @(0, 2)) { break }
        if ($captureCode -eq 130) {
            Write-State 'paused_by_operator' @{ capture_exit = $captureCode; exit_code = 130 }
            exit 130
        }
        $captureFailures++
        if ($captureFailures -ge $MaxUnlockedFailures) { throw 'capture_failed_while_unlocked' }
        Write-State 'capture_retry_wait' @{ last_capture_exit = $captureCode; capture_failures = $captureFailures }
        Start-Sleep -Seconds 30
    }

    Write-State 'background_running' @{ capture_exit = $captureCode }
    & $BackgroundScript -RunDir $RunDir -PilotDir $PilotDir -JournalLinks $JournalLinks `
        -ExpectedCount $ExpectedCount -Start 1 -MaxJournals 0 -MaxSteps $MaxSteps `
        -WaitSeconds $WaitSeconds -Attempts $Attempts -SleepSeconds $SleepSeconds `
        -MediaWorkers $MediaWorkers 2>&1 | Tee-Object -FilePath $LogPath -Append
    $backgroundCode = $LASTEXITCODE
    $finalCode = if ($backgroundCode -eq 0 -and $captureCode -eq 0) { 0 } `
        elseif ($backgroundCode -in @(0, 2) -and $captureCode -in @(0, 2)) { 2 } else { 1 }
    Write-State 'completed' @{ capture_exit = $captureCode; background_exit = $backgroundCode; exit_code = $finalCode }
    exit $finalCode
} catch {
    ($_ | Out-String) | Add-Content -LiteralPath $LogPath -Encoding UTF8
    Write-State 'failed' @{ reason = $_.Exception.Message; capture_failures = $captureFailures; exit_code = 1 }
    exit 1
} finally {
    if ($null -ne $SupervisorLock) { $SupervisorLock.Dispose() }
}
