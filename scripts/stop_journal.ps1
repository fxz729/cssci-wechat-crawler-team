param([string]$OutputDir='')
& (Join-Path $PSScriptRoot 'run_journal.ps1') -Action 'stop' -OutputDir $OutputDir
exit $LASTEXITCODE
