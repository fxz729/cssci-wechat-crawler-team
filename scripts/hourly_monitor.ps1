[CmdletBinding()]
param(
    [string]$RunDir = 'output/rollout/full_126',
    [int]$IntervalSeconds = 3600,
    [int]$ListenPort = 8899
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$logPath = Join-Path $ProjectRoot (Join-Path $RunDir 'hourly_status.log')
$inboxPath = Join-Path $ProjectRoot 'output/pilot_20260906/capture_inbox.jsonl'
$prevInbox = $null
while ($true) {
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $record = [ordered]@{ time = $ts }
    try {
        $status = & (Join-Path $PSScriptRoot 'show_full_run_status.ps1') | ConvertFrom-Json
        $record.ordinal = $status.current_journal.ordinal
        $record.journal = $status.current_journal.journal_name
        $record.journal_status = $status.current_journal.status
        $record.processed = $status.msaa_state.processed
        $record.completed = $status.msaa_state.completed
        $record.stop_reason = $status.msaa_state.stop_reason
        $record.stop_requested = $status.stop_requested
        $record.status_counts = $status.journal_status_counts
    } catch {
        $record.error = $_.Exception.Message
    }
    $record.fiddler_port_ok = [bool](Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue)
    $record.proxy = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue).ProxyServer
    $record.runner_alive = [bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'msaa_roster_runner' } | Select-Object -First 1)
    if (Test-Path -LiteralPath $inboxPath) {
        $size = (Get-Item -LiteralPath $inboxPath).Length
        $record.inbox_bytes = $size
        if ($null -ne $prevInbox) { $record.inbox_delta = $size - $prevInbox }
        $prevInbox = $size
    }
    ($record | ConvertTo-Json -Depth 6 -Compress) | Add-Content -LiteralPath $logPath -Encoding UTF8
    Start-Sleep -Seconds ([Math]::Max(60, $IntervalSeconds))
}