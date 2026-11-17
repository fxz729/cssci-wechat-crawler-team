[CmdletBinding()]
param(
    [int]$ListenPort = 8899,
    [int]$WaitSeconds = 10
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
if ($ListenPort -lt 1 -or $ListenPort -gt 65535) { throw 'invalid_fiddler_port' }
if ($WaitSeconds -lt 1 -or $WaitSeconds -gt 30) { throw 'invalid_wait_bound' }

$internetSettings = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
function Test-FiddlerProxy {
    $settings = Get-ItemProperty -LiteralPath $internetSettings
    if ([int]$settings.ProxyEnable -ne 1) { return $false }
    $server = [string]$settings.ProxyServer
    $accepted = @("127.0.0.1:$ListenPort", "localhost:$ListenPort")
    $parts = @($server.Split(';') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    if ($parts.Count -eq 1 -and $parts[0] -notmatch '=') { return $parts[0] -in $accepted }
    $routes = @{}
    foreach ($part in $parts) {
        if ($part -notmatch '=') { continue }
        $pair = $part.Split('=', 2)
        $routes[$pair[0].Trim()] = $pair[1].Trim()
    }
    return $routes['http'] -in $accepted -and $routes['https'] -in $accepted
}

if (Test-FiddlerProxy) {
    [pscustomobject]@{ success=$true; changed=$false; listen_port=$ListenPort } | ConvertTo-Json -Compress
    exit 0
}

$fiddlers = @(Get-Process -Name 'Fiddler' -ErrorAction SilentlyContinue)
if ($fiddlers.Count -ne 1) { throw 'single_fiddler_process_required' }
$fiddler = $fiddlers[0]
if ($fiddler.MainWindowHandle -eq 0) { throw 'fiddler_window_required' }

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class FiddlerCaptureNative {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int maxCount);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int maxCount);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);
}
"@
$targetText = '系统代理已更改。单击以重新启用捕获。'
$matches = [Collections.Generic.List[IntPtr]]::new()
$callback = [FiddlerCaptureNative+EnumWindowsProc]{
    param([IntPtr]$hwnd, [IntPtr]$lParam)
    $text = [Text.StringBuilder]::new(512)
    $class = [Text.StringBuilder]::new(256)
    [void][FiddlerCaptureNative]::GetWindowText($hwnd, $text, $text.Capacity)
    [void][FiddlerCaptureNative]::GetClassName($hwnd, $class, $class.Capacity)
    [uint32]$pid = 0
    [void][FiddlerCaptureNative]::GetWindowThreadProcessId($hwnd, [ref]$pid)
    if ($pid -eq $fiddler.Id -and $class.ToString() -match 'BUTTON' -and $text.ToString() -ceq $targetText) {
        $matches.Add($hwnd)
    }
    return $true
}
[void][FiddlerCaptureNative]::EnumChildWindows([IntPtr]$fiddler.MainWindowHandle, $callback, [IntPtr]::Zero)
if ($matches.Count -ne 1) { throw 'fiddler_reenable_control_missing_or_ambiguous' }
[void][FiddlerCaptureNative]::SendMessage($matches[0], 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)

$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
do {
    Start-Sleep -Milliseconds 250
    if (Test-FiddlerProxy) {
        [pscustomobject]@{ success=$true; changed=$true; listen_port=$ListenPort } | ConvertTo-Json -Compress
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)
throw 'fiddler_capture_reenable_timeout'
