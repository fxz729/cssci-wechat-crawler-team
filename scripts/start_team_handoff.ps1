[CmdletBinding()]
param(
    [int]$Start = 1,
    [int]$MaxJournals = 0,
    [int]$MediaWorkers = 4,
    [int]$WaitSeconds = 45,
    [string]$Python = '',
    [string]$FiddlerExe = ''
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$ProjectRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $ProjectRoot
function Resolve-Python {
    param([string]$Requested)
    if ($Requested -and (Test-Path -LiteralPath $Requested -PathType Leaf)) { return (Resolve-Path $Requested).Path }
    foreach ($candidate in @('D:\miniforge3\python.exe','C:\Users\11075\miniforge3\python.exe')) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $cmd=Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'python_not_found'
}
function Resolve-Fiddler {
    param([string]$Requested)
    $candidates=@()
    if ($Requested) { $candidates += $Requested }
    $candidates += @((Join-Path $ProjectRoot '..\Fiddler\Fiddler.exe'),'D:\Fiddler\Fiddler.exe','C:\Program Files\Fiddler\Fiddler.exe','C:\Program Files (x86)\Fiddler\Fiddler.exe')
    foreach($candidate in $candidates) { if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path $candidate).Path } }
    throw 'fiddler_executable_not_found：请用 -FiddlerExe 指定 Fiddler.exe'
}
$Python=Resolve-Python $Python
$FiddlerExe=Resolve-Fiddler $FiddlerExe
$env:CSSCI_FIDDLER_EXE=$FiddlerExe
Write-Host "handoff_run_start start=$Start max=$MaxJournals python=$Python fiddler=$FiddlerExe"
$launcherArgs=@('-RunDir','output/rollout/full_126','-Start',[string]$Start,'-MediaWorkers',[string]$MediaWorkers,'-WaitSeconds',[string]$WaitSeconds,'-Python',$Python)
if($MaxJournals -gt 0){$launcherArgs += @('-MaxJournals',[string]$MaxJournals)}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start_full_126_production.ps1') @launcherArgs
exit $LASTEXITCODE
