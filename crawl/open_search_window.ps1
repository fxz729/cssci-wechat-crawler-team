param([int]$WaitSeconds = 20)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SearchOpenNative {
 public delegate bool EnumProc(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
 [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint data, UIntPtr e);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string className, string windowName);
 [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object value);
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
 public static bool Activate(IntPtr h) {
  uint pid; uint own = GetCurrentThreadId();
  uint foreground = GetWindowThreadProcessId(GetForegroundWindow(), out pid);
  uint target = GetWindowThreadProcessId(h, out pid);
  bool a = false, b = false;
  try {
   ShowWindow(h, 9);
   if (own != foreground) a = AttachThreadInput(own, foreground, true);
   if (own != target && target != foreground) b = AttachThreadInput(own, target, true);
   BringWindowToTop(h); SetForegroundWindow(h);
   return GetForegroundWindow() == h;
  } finally {
   if (b) AttachThreadInput(own, target, false);
   if (a) AttachThreadInput(own, foreground, false);
  }
 }
 public static long FindByClass(string className) {
  long found = 0;
  EnumWindows((h, l) => {
   if (!IsWindowVisible(h)) return true;
   var sb = new StringBuilder(256); GetClassName(h, sb, 256);
   if (sb.ToString() != className) return true;
   found = h.ToInt64(); return false;
  }, IntPtr.Zero);
  return found;
 }
 public static long[] FindBrowsers() {
  var list = new System.Collections.Generic.List<long>();
  EnumWindows((h, l) => {
   if (!IsWindowVisible(h)) return true;
   var sb = new StringBuilder(256); GetClassName(h, sb, 256);
   if (sb.ToString() == "Chrome_WidgetWin_0") list.Add(h.ToInt64());
   return true;
  }, IntPtr.Zero);
  return list.ToArray();
 }
 public static uint ProcessId(long h) { uint p; GetWindowThreadProcessId(new IntPtr(h), out p); return p; }
 private static long _renderChild = 0;
 private static bool EnumChildProc(IntPtr h, IntPtr l) {
  var sb = new StringBuilder(256);
  GetClassName(h, sb, 256);
  if (sb.ToString() == "Chrome_RenderWidgetHostHWND") { _renderChild = h.ToInt64(); return false; }
  return true;
 }
 [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr h, EnumProc p, IntPtr l);
 public static long RenderChild(long parent) {
  _renderChild = 0;
  EnumChildWindows(new IntPtr(parent), EnumChildProc, IntPtr.Zero);
  return _renderChild;
 }
 public static object Accessible(IntPtr hwnd) {
  Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
  object value;
  int hr = AccessibleObjectFromWindow(hwnd, 0xFFFFFFFC, ref iid, out value);
  if (hr < 0) Marshal.ThrowExceptionForHR(hr);
  return value;
 }
}
"@

function Get-MsaaRoot([long]$Handle) {
    $render = [SearchOpenNative]::RenderChild($Handle)
    if ($render -eq 0) { return $null }
    try { return [SearchOpenNative]::Accessible([IntPtr]$render) } catch { return $null }
}

function Get-SearchWindow {
    foreach ($handle in [SearchOpenNative]::FindBrowsers()) {
        $processId = [SearchOpenNative]::ProcessId($handle)
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if (!$process -or $process.ProcessName -ne 'WeChatAppEx') { continue }
        $root = Get-MsaaRoot $handle
        if ($null -eq $root) { continue }
        try {
            if ([string]$root.accName(0) -match '搜一搜') { return $handle }
        } catch {}
    }
    return 0
}

$existing = Get-SearchWindow
if ($existing -ne 0) {
    [pscustomobject]@{ success = $true; search_hwnd = $existing; opened = $false } | ConvertTo-Json -Compress
    exit 0
}

$main = [SearchOpenNative]::FindByClass('WeChatMainWndForPC')
if ($main -eq 0) {
    [pscustomobject]@{ success = $false; error_code = 'wechat_main_window_required' } | ConvertTo-Json -Compress
    exit 1
}
$mainProcessId = [SearchOpenNative]::ProcessId($main)
$mainProcess = Get-Process -Id $mainProcessId -ErrorAction SilentlyContinue
if (!$mainProcess -or $mainProcess.ProcessName -ne 'WeChat') {
    [pscustomobject]@{ success = $false; error_code = 'wechat_main_window_required' } | ConvertTo-Json -Compress
    exit 1
}
[void][SearchOpenNative]::Activate([IntPtr]$main)
Start-Sleep -Milliseconds 400
$rect = New-Object SearchOpenNative+RECT
if (-not [SearchOpenNative]::GetWindowRect([IntPtr]$main, [ref]$rect)) {
    [pscustomobject]@{ success = $false; error_code = 'wechat_main_geometry_unverified' } | ConvertTo-Json -Compress
    exit 1
}
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
foreach ($offset in 393, 383, 403, 373, 413, 433, 453, 473, 493, 513, 533) {
    if ([DateTime]::UtcNow -ge $deadline) { break }
    [void][SearchOpenNative]::SetCursorPos($rect.L + 38, $rect.T + $offset)
    [SearchOpenNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    [SearchOpenNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 900
    $found = Get-SearchWindow
    if ($found -ne 0) {
        [pscustomobject]@{ success = $true; search_hwnd = $found; opened = $true; sidebar_offset = $offset } | ConvertTo-Json -Compress
        exit 0
    }
}
[pscustomobject]@{ success = $false; error_code = 'search_window_open_failed' } | ConvertTo-Json -Compress
exit 1
