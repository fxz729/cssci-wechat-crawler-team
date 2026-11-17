[CmdletBinding()]
param([string]$RunDir = 'output/rollout/full_126')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$manifestPath = Join-Path $RunDir 'run_manifest.json'
$controlPath = Join-Path $RunDir 'run_control.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    [pscustomobject]@{ status='not_started'; run_dir=$RunDir; manifest=$manifestPath } | ConvertTo-Json -Compress
    exit 0
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$counts = @{}
foreach ($journal in $manifest.journals) {
    $name = [string]$journal.status
    if (-not $counts.ContainsKey($name)) { $counts[$name] = 0 }
    $counts[$name]++
}
$currentJournal = $manifest.journals | Where-Object { $_.status -in @('running','media_running','background_running','msaa_running') } | Sort-Object @{ Expression = { if ($_.msaa_started_at) { [datetimeoffset]$_.msaa_started_at } else { [datetimeoffset]::MinValue } }; Descending = $true } | Select-Object -First 1
if ($null -eq $currentJournal) { $currentJournal = $manifest.journals | Where-Object { $_.status -eq 'import_running' } | Sort-Object @{ Expression = { if ($_.msaa_started_at) { [datetimeoffset]$_.msaa_started_at } else { [datetimeoffset]::MinValue } }; Descending = $true } | Select-Object -First 1 }
if ($null -eq $currentJournal) {
    $currentJournal = $manifest.journals | Where-Object { $_.status -in @('paused','needs_verification','cache_partial','needs_capture','background_failed','msaa_failed','msaa_partial','pending_skip_retry') } | Select-Object -First 1
}
$articleCounts = $null
$currentArticle = $null
$msaaState = $null
if ($null -ne $currentJournal) {
    if ($currentJournal.status -in @('msaa_running','msaa_partial','msaa_failed','import_running','pending_skip_retry')) {
        $statePath = Join-Path $currentJournal.base 'msaa\state.json'
        if (Test-Path -LiteralPath $statePath) {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $processedCount = @($state.processed_titles).Count
            $articleCounts = @{ processed = $processedCount }
            $msaaState = [pscustomobject]@{
                processed = $processedCount
                completed = ($state.completed -eq $true)
                stop_reason = [string]$state.stop_reason
                last_processed_title = [string]$state.last_processed_title
            }
        }
    } else {
        $stateFile = if ($currentJournal.phase -eq 'capture-first' -or $currentJournal.status -in @('cache_ready','cache_partial')) { 'capture_cache_state.json' } else { 'capture_state.json' }
        $statePath = Join-Path $currentJournal.base $stateFile
        if (Test-Path -LiteralPath $statePath) {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $articleCounts = @{}
            foreach ($property in $state.jobs.PSObject.Properties) {
                $name = [string]$property.Value.status
                if (-not $articleCounts.ContainsKey($name)) { $articleCounts[$name] = 0 }
                $articleCounts[$name]++
                if ($null -eq $currentArticle -and $name -eq 'running') {
                    $currentArticle = [pscustomobject]@{
                        index = [int]$property.Name
                        title = [string]$property.Value.title
                        attempt = $property.Value.attempt
                        status = $name
                    }
                }
            }
        }
    }
}
$stopRequested = $false
if (Test-Path -LiteralPath $controlPath) {
    $control = Get-Content -LiteralPath $controlPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $stopRequested = ($control.stop_requested -eq $true)
}
[pscustomobject]@{
    run_dir = $RunDir
    expected = $manifest.expected_count
    journal_status_counts = $counts
    current_journal = $currentJournal
    current_article_status_counts = $articleCounts
    current_article = $currentArticle
    msaa_state = $msaaState
    stop_requested = $stopRequested
    run_control = $controlPath
} | ConvertTo-Json -Depth 8
