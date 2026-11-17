param([int]$WaitSeconds=15)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SearchWindowNative {
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint dx,uint dy,uint data,UIntPtr extra);
 [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
}
"@
if($WaitSeconds -lt 1 -or $WaitSeconds -gt 60){throw 'invalid_wait_bound'}
function Get-SearchContexts {
 $desktop=[System.Windows.Automation.AutomationElement]::RootElement
 $wins=$desktop.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
 $rows=@()
 foreach($win in $wins){
  try{
   $current=$win.Current
   if($current.ClassName -ne 'Chrome_WidgetWin_0' -or $current.IsOffscreen){continue}
   $process=Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
   if(!$process -or $process.ProcessName -ne 'WeChatAppEx'){continue}
   $docs=$win.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document))
   foreach($doc in $docs){
    if($doc.Current.IsOffscreen){continue}
    $input=$doc.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'weixin-search-input'))
    if($input){$rows += [pscustomobject]@{window=$win;document=$doc;handle=$current.NativeWindowHandle}}
   }
  }catch [System.Windows.Automation.ElementNotAvailableException]{continue}
 }
 return @($rows)
}
function Get-MainWindows {
 $desktop=[System.Windows.Automation.AutomationElement]::RootElement
 $wins=$desktop.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
 $rows=@()
 foreach($win in $wins){
  try{
   $current=$win.Current
   $process=Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
   if($process -and $process.ProcessName -eq 'Weixin' -and $current.ClassName -eq 'Qt51514QWindowIcon' -and $current.ClassName -ne 'mmui::LoginWindow'){$rows += $win}
  }catch [System.Windows.Automation.ElementNotAvailableException]{continue}
 }
 return @($rows)
}
function Restore-MainFromTray {
 $desktop=[System.Windows.Automation.AutomationElement]::RootElement
 $nodes=$desktop.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
 $icons=@($nodes|Where-Object {$_.Current.Name -ceq '微信' -and $_.Current.AutomationId -ceq 'NotifyItemIcon' -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and !$_.Current.IsOffscreen})
 if($icons.Count -ne 1){throw 'wechat_main_window_required'}
 $invoke=$null
 if(!$icons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)){throw 'wechat_tray_not_invokable'}
 $invoke.Invoke()
}
$search=@(Get-SearchContexts)
if($search.Count -gt 1){throw 'ambiguous_search_window'}
if($search.Count -eq 1){[pscustomobject]@{success=$true;opened=$false;browser_hwnd=$search[0].handle;document='搜一搜'}|ConvertTo-Json -Compress;exit 0}
$main=@(Get-MainWindows)
if($main.Count -eq 0){Restore-MainFromTray;Start-Sleep -Seconds 2;$main=@(Get-MainWindows)}
if($main.Count -ne 1){throw 'wechat_main_window_required'}
$current=$main[0].Current
$handle=[IntPtr]$current.NativeWindowHandle
[void][SearchWindowNative]::ShowWindow($handle,9)
[void][SearchWindowNative]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 500
$rect=New-Object SearchWindowNative+RECT
if(-not [SearchWindowNative]::GetWindowRect($handle,[ref]$rect)){throw 'wechat_main_geometry_unverified'}
$dpi=[SearchWindowNative]::GetDpiForWindow($handle);if($dpi -lt 96 -or $dpi -gt 384){$dpi=96}
$scale=$dpi/96.0
$width=$rect.Right-$rect.Left;$height=$rect.Bottom-$rect.Top
if($width -lt [int](900*$scale) -or $height -lt [int](600*$scale)){throw 'wechat_main_geometry_unverified'}
# Qt does not expose sidebar children through UIA. These offsets are relative to
# the verified Qt main window and DPI-scaled; one click is followed by a hard
# search-input postcondition, with no coordinate fallback loop.
$x=$rect.Left+[int][Math]::Round(38*$scale)
$y=$rect.Top+[int][Math]::Round(353*$scale)
[void][SearchWindowNative]::SetCursorPos($x,$y)
Start-Sleep -Milliseconds 200
[SearchWindowNative]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
[SearchWindowNative]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
$deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
do{
 Start-Sleep -Milliseconds 250
 $search=@(Get-SearchContexts)
 if($search.Count -gt 1){throw 'ambiguous_search_window'}
 if($search.Count -eq 1){[pscustomobject]@{success=$true;opened=$true;browser_hwnd=$search[0].handle;document='搜一搜'}|ConvertTo-Json -Compress;exit 0}
}while([DateTime]::UtcNow -lt $deadline)
throw 'search_window_recovery_failed'
