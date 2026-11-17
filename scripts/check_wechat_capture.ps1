[CmdletBinding()]
param(
    [int]$Hwnd = 331714,
    [string]$PilotDir = 'output/pilot_20260906',
    [string]$FiddlerExe = '..\Fiddler\Fiddler.exe',
    [int]$ListenPort = 8899
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
if ([IO.Path]::IsPathRooted($FiddlerExe)) { $fiddlerPath = $FiddlerExe }
else { $fiddlerPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $FiddlerExe)) }
& python '-X' 'utf8' '-B' 'main.py' 'check-wechat-capture' '--hwnd' $Hwnd '--pilot-dir' $PilotDir '--fiddler-exe' $fiddlerPath '--listen-port' $ListenPort
exit $LASTEXITCODE
