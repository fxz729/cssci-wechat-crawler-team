param([string]$OutputDir='')
& (Join-Path $PSScriptRoot 'run_journal.ps1') -Action 'status' -OutputDir $OutputDir
exit $LASTEXITCODE
