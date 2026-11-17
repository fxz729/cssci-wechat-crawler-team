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
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
& (Join-Path $PSScriptRoot 'check_full_run.ps1') -Phase capture-first -RunDir $RunDir -PilotDir $PilotDir -JournalLinks $JournalLinks -ExpectedCount $ExpectedCount -FiddlerExe $FiddlerExe
if ($LASTEXITCODE -ne 0) { throw 'capture_first_preflight_failed' }
& python -X utf8 -B main.py crawl-client-roster `
    --phase capture-first --run-dir $RunDir --journal-links $JournalLinks `
    --expected-count $ExpectedCount --start $Start --max-journals $MaxJournals `
    --max-steps $MaxSteps --wait-seconds $WaitSeconds --attempts $Attempts `
    --sleep-seconds $SleepSeconds --media-workers $MediaWorkers
exit $LASTEXITCODE
