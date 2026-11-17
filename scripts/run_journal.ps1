[CmdletBinding()]
param(
 [ValidateSet('run','pause','stop','status')][string]$Action='run',
 [string]$OutputDir='',
 [string]$RequestFile='',
 [string]$Python='D:\miniforge3\python.exe'
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8'
$project=Split-Path -Parent $PSScriptRoot
if(!$OutputDir){$OutputDir=Join-Path $project 'output\session_production'}
if(!(Test-Path -LiteralPath $Python -PathType Leaf)){throw 'python_executable_not_found'}
$arguments=@('-X','utf8','-m','crawl.journal_runner','--output',$OutputDir)
if($Action -eq 'run'){
 if($RequestFile){$arguments+=@('--request-file',$RequestFile)}else{$arguments+='--all-journals'}
}else{$arguments+='--'+$Action}
Push-Location -LiteralPath $project
try{& $Python @arguments; $code=$LASTEXITCODE}finally{Pop-Location}
exit $code
