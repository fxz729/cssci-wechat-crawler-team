[CmdletBinding()]
param(
    [int]$Hwnd = 331714,
    [string]$Biz = 'Mzk3NTE3NTU0Ng==',
    [string]$Account = '当代世界社会主义问题',
    [string]$Base = 'output/journal_validation_20260906',
    [string]$PilotDir = 'output/pilot_20260906',
    [string]$FiddlerExe = '..\Fiddler\Fiddler.exe',
    [string]$Indices = '',
    [int]$Attempts = 2,
    [double]$WaitSeconds = 60,
    [int]$Workers = 4,
    [switch]$Force,
    [switch]$StartFiddler
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

function Invoke-Python([string[]]$Arguments) {
    & python '-X' 'utf8' '-B' @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python_step_failed:$LASTEXITCODE"
    }
}

function Resolve-ToolPath([string]$Value) {
    if ([IO.Path]::IsPathRooted($Value)) { return $Value }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Value))
}

$fiddlerPath = Resolve-ToolPath $FiddlerExe
if ($StartFiddler -and -not (Get-Process -Name Fiddler -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath $fiddlerPath)) { throw 'fiddler_exe_not_found' }
    Start-Process -FilePath $fiddlerPath -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Write-Host '[1/4] 检查微信客户端与 Fiddler'
Invoke-Python @(
    'main.py', 'check-wechat-capture',
    '--hwnd', $Hwnd,
    '--pilot-dir', $PilotDir,
    '--fiddler-exe', $fiddlerPath,
    '--listen-port', '8899'
)

Write-Host '[2/4] 客户端历史目录与逐篇字段采集'
$captureArgs = @(
    'main.py', 'crawl-client-history',
    '--base', $Base,
    '--pilot-dir', $PilotDir,
    '--biz', $Biz,
    '--account', $Account,
    '--hwnd', $Hwnd,
    '--attempts', $Attempts,
    '--wait-seconds', $WaitSeconds
)
if ($Indices) { $captureArgs += @('--indices', $Indices) }
if ($Force) { $captureArgs += '--force' }
Invoke-Python $captureArgs

Write-Host '[3/4] 媒体原文件复用/下载'
$DownloadScript = Join-Path $ProjectRoot 'output\journal_validation_20260906\download_bundle.py'
if (-not (Test-Path -LiteralPath $DownloadScript)) { throw 'download_script_not_found' }
Invoke-Python @(
    $DownloadScript,
    '--workers', $Workers
)

Write-Host '[4/4] 严格验收'
Invoke-Python @('main.py', 'audit-client-bundle', '--base', $Base)
Write-Host "完成。文章与媒体索引位于 $(Join-Path $ProjectRoot $Base)；capture_inbox 仅作为本地事件输入，不导出凭据。"
