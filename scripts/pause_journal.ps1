param([string]$OutputDir='')
& (Join-Path $PSScriptRoot 'run_journal.ps1') -Action 'pause' -OutputDir $OutputDir
exit $LASTEXITCODE
