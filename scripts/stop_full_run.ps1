[CmdletBinding()]
param([string]$RunDir = 'output/rollout/full_126')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot
$controlPath = Join-Path $RunDir 'run_control.json'
& python -X utf8 -B -m utils.run_control request --path $controlPath
if ($LASTEXITCODE -ne 0) { throw 'stop_request_failed' }
Write-Host '已请求温和停止；当前文章将在安全检查点结束后暂停。未强杀微信、Fiddler或Python进程。'