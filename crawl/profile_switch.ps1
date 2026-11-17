param([string]$Account, [switch]$InspectOnly, [switch]$ForMsaa, [int]$WaitSeconds=30)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class MsaaProfileNative {
 public delegate bool EnumProc(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string className, string windowName);
 [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object value);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
 [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
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
 public static long FindH5() {
  long found=0;
  EnumWindows((h,l)=>{
   if(!IsWindowVisible(h))return true;
   var sb=new StringBuilder(256);
   GetClassName(h,sb,256);
   if(sb.ToString()!="H5SubscriptionProfileWnd")return true;
   found=h.ToInt64();return false;
  },IntPtr.Zero);
  return found;
 }
 public static long[] FindBrowsers() {
  var list=new System.Collections.Generic.List<long>();
  EnumWindows((h,l)=>{
   if(!IsWindowVisible(h))return true;
   var sb=new StringBuilder(256);
   GetClassName(h,sb,256);
   if(sb.ToString()=="Chrome_WidgetWin_0")list.Add(h.ToInt64());
   return true;
  },IntPtr.Zero);
  return list.ToArray();
 }
 public static uint ProcessId(long h) { uint p; GetWindowThreadProcessId(new IntPtr(h),out p); return p; }
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
function Get-H5ProfileHandle {
 $handle=[MsaaProfileNative]::FindH5()
 if($handle -eq 0){return 0}
 $processId=[MsaaProfileNative]::ProcessId($handle)
 $process=Get-Process -Id $processId -ErrorAction SilentlyContinue
 if(!$process -or $process.ProcessName -ne 'WeChat'){return 0}
 return $handle
}
function Get-H5ProfileName([long]$Handle) {
 $render=[MsaaProfileNative]::RenderChild($Handle)
 if($render -eq 0){return ''}
 try { $accessible=[MsaaProfileNative]::Accessible([IntPtr]$render) } catch { return '' }
 try { $value=[string]$accessible.accValue(0) } catch { return '' }
 $match=[regex]::Match($value,'showName=([^&]+)')
 if(!$match.Success){return ''}
 try { return [Uri]::UnescapeDataString($match.Groups[1].Value) } catch { return '' }
}

function Get-SearchWindowHandle {
 foreach($handle in [MsaaProfileNative]::FindBrowsers()){
  $processId=[MsaaProfileNative]::ProcessId($handle)
  $process=Get-Process -Id $processId -ErrorAction SilentlyContinue
  if(!$process -or $process.ProcessName -ne 'WeChatAppEx'){continue}
  $render=[MsaaProfileNative]::RenderChild($handle)
  if($render -eq 0){continue}
  try{$root=[MsaaProfileNative]::Accessible([IntPtr]$render)}catch{continue}
  try{if([string]$root.accName(0) -match '搜一搜'){return $handle}}catch{}
 }
 return 0
}
function Find-MsaaSearchInput($Root){
 $script:msaaFound=$null
 function WalkInput($o,[int]$depth){
  if($null -eq $o -or $depth -lt 0 -or $script:msaaFound){return}
  try{
   $name=[string]$o.accName(0);$role=[int]$o.accRole(0)
   $left=0;$top=0;$width=0;$height=0
   try{[void]$o.accLocation([ref]$left,[ref]$top,[ref]$width,[ref]$height,0)}catch{}
   if($role -eq 42 -and $name -eq '' -and $width -gt 300){$script:msaaFound=$o;return}
   $count=[int]$o.accChildCount
   for($i=1;$i -le [Math]::Min($count,40);$i++){WalkInput $o.accChild($i) ($depth-1)}
  }catch{}
 }
 WalkInput $Root 12
 return $script:msaaFound
}
function Find-MsaaAccountResult($Root,[string]$Account){
 $pattern='^'+[regex]::Escape($Account)+'\s+.*账号描述'
 $script:msaaFound=$null
 function WalkResult($o,[int]$depth){
  if($null -eq $o -or $depth -lt 0 -or $script:msaaFound){return}
  try{
   $name=[string]$o.accName(0);$role=[int]$o.accRole(0)
   if($role -eq 43 -and $name -match $pattern){$script:msaaFound=$o;return}
   $count=[int]$o.accChildCount
   for($i=1;$i -le [Math]::Min($count,40);$i++){WalkResult $o.accChild($i) ($depth-1)}
  }catch{}
 }
 WalkResult $Root 12
 return $script:msaaFound
}
function Invoke-MsaaSearchAndOpen([string]$Account,[int]$TimeoutSeconds){
 $searchHandle=Get-SearchWindowHandle
 if($searchHandle -eq 0){throw 'search_window_required'}
 [void][MsaaProfileNative]::Activate([IntPtr]$searchHandle)
 Start-Sleep -Milliseconds 300
 $render=[MsaaProfileNative]::RenderChild($searchHandle)
 if($render -eq 0){throw 'search_render_missing'}
 try{$root=[MsaaProfileNative]::Accessible([IntPtr]$render)}catch{throw 'search_root_unavailable'}
 $input=Find-MsaaSearchInput $root
 if($null -eq $input){throw 'search_input_not_found'}
 $input.accSelect(1,0)
 Start-Sleep -Milliseconds 300
 [System.Windows.Forms.SendKeys]::SendWait('^a')
 Start-Sleep -Milliseconds 100
 [System.Windows.Forms.SendKeys]::SendWait('{DEL}')
 [System.Windows.Forms.SendKeys]::SendWait($Account)
 Start-Sleep -Milliseconds 500
 [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
 $deadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
 $target=$null
 do{
  Start-Sleep -Milliseconds 300
  $render=[MsaaProfileNative]::RenderChild($searchHandle)
  if($render -eq 0){continue}
  try{$root=[MsaaProfileNative]::Accessible([IntPtr]$render)}catch{continue}
  $target=Find-MsaaAccountResult $root $Account
 }while($null -eq $target -and [DateTime]::UtcNow -lt $deadline)
 if($null -eq $target){throw 'account_result_missing'}
 try {
  $target.accDoDefaultAction(0)
 } catch {
  try {
   $target.accSelect(1,0)
   Start-Sleep -Milliseconds 200
   [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
  } catch {
   throw 'account_result_not_invokable'
  }
 }
 return $searchHandle
}
if(!$InspectOnly -and [string]::IsNullOrWhiteSpace($Account)){throw 'account_missing'}
if($WaitSeconds -lt 1 -or $WaitSeconds -gt 60){throw 'invalid_wait_bound'}

# Only web content in WeChatAppEx is actionable. Never type into the main chat UI.
function Get-BrowserContexts {
 $desktop=[System.Windows.Automation.AutomationElement]::RootElement
 $wins=$desktop.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
 foreach($win in $wins){
  try{
   $c=$win.Current
   if($c.ClassName -ne 'Chrome_WidgetWin_0' -or $c.IsOffscreen){continue}
   $proc=Get-Process -Id $c.ProcessId -ErrorAction SilentlyContinue
   if(!$proc -or $proc.ProcessName -ne 'WeChatAppEx'){continue}
   $docs=$win.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document))
   foreach($doc in $docs){if(!$doc.Current.IsOffscreen){[pscustomobject]@{window=$win;document=$doc;handle=$c.NativeWindowHandle}}}
  }catch [System.Windows.Automation.ElementNotAvailableException]{continue}
 }
}
function Get-SearchInput($context){
 return $context.document.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'weixin-search-input'))
}
function Get-Search {
 $matches=@(Get-BrowserContexts | Where-Object {$_.document.Current.Name -like '*搜一搜' -and (Get-SearchInput $_)})
 if($matches.Count -gt 1){throw 'ambiguous_search_window'}
 if($matches.Count -eq 1){return $matches[0]}
 return $null
}
function Invoke-Verified($element){
 if(!$element -or !$element.Current.IsEnabled -or $element.Current.IsOffscreen){throw 'navigation_control_unavailable'}
 $pattern=$null
 if(!$element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){throw 'navigation_control_not_invokable'}
 $pattern.Invoke()
}
function Get-Profile {
 $profiles=@(Get-BrowserContexts | Where-Object {$_.document.Current.Name -ceq $Account})
 if($profiles.Count -gt 1){throw 'ambiguous_profile_window'}
 if($profiles.Count -eq 1){return $profiles[0]}
 return $null
}
function Show-All($profile){
 $tabs=$profile.document.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'全部'))
 $tabs=@($tabs|Where-Object {$_.Current.ClassName -match 'profile_details__tabs-item'})
 if($tabs.Count -ne 1){throw 'profile_all_tab_missing'}
 if($tabs[0].Current.ClassName -notmatch '(^|\s)active(\s|$)'){Invoke-Verified $tabs[0]}
 $deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
 do{
  $current=Get-Profile
  if($current){
   $nodes=$current.document.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'全部'))
   $active=@($nodes|Where-Object {$_.Current.ClassName -match 'profile_details__tabs-item' -and $_.Current.ClassName -match '(^|\s)active(\s|$)'})
   if($active.Count -eq 1){return [pscustomobject]@{success=$true;browser_hwnd=$current.handle;document=$Account;active_tab='全部'}}
  }
  Start-Sleep -Milliseconds 250
 }while([DateTime]::UtcNow -lt $deadline)
 throw 'profile_all_tab_timeout'
}
function Select-SearchTab {
 # Activate an explicitly named search tab; never cycle or close arbitrary tabs.
 foreach($context in @(Get-BrowserContexts)){
  $tabs=$context.window.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::TabItem))
  $searchTabs=@($tabs|Where-Object {$_.Current.Name -like '*搜一搜*' -and !$_.Current.IsOffscreen})
  if($searchTabs.Count -ne 1){continue}
  $selection=$null
  if($searchTabs[0].TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$selection)){$selection.Select()}else{Invoke-Verified $searchTabs[0]}
  Start-Sleep -Milliseconds 300
  $search=Get-Search
  if($search){return $search}
 }
 return $null
}
if($InspectOnly){
 $contexts=@(Get-BrowserContexts)
 $rows=@($contexts|ForEach-Object{[pscustomobject]@{browser_hwnd=$_.handle;document=$_.document.Current.Name;search_input=($null -ne (Get-SearchInput $_))}})
 $h5=Get-H5ProfileHandle
 [pscustomobject]@{read_only=$true;profile_hwnd=$h5;windows=$rows}|ConvertTo-Json -Depth 4 -Compress
 exit 0
}
if($ForMsaa){
 # Always replace the previous H5 profile window. Reusing a stale H5 DOM
 # can leave the window alive but without any article cards.
 $oldH5=Get-H5ProfileHandle
 if($oldH5 -ne 0){
  [void][MsaaProfileNative]::PostMessage([IntPtr]$oldH5,0x0010,[IntPtr]::Zero,[IntPtr]::Zero)
  $closeDeadline=[DateTime]::UtcNow.AddSeconds(5)
  do{
   Start-Sleep -Milliseconds 200
   if((Get-H5ProfileHandle) -eq 0){break}
  }while([DateTime]::UtcNow -lt $closeDeadline)
 }
 $searchHandle=Invoke-MsaaSearchAndOpen $Account $WaitSeconds
 $deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
 $lastName=''
 $normalizedAccount=[regex]::Replace($Account.Normalize([Text.NormalizationForm]::FormKC),'\s+',' ').Trim()
 do{
  $h5=Get-H5ProfileHandle
  if($h5 -ne 0){
   $name=Get-H5ProfileName $h5
   if($name){$lastName=$name}
   $normalizedProfile=if($name){[regex]::Replace($name.Normalize([Text.NormalizationForm]::FormKC),'\s+',' ').Trim()}else{''}
   if($normalizedProfile -and $normalizedProfile -eq $normalizedAccount){
    [pscustomobject]@{success=$true;search_hwnd=$searchHandle;profile_hwnd=$h5;document=$Account;active_tab='全部';mode='msaa'}|ConvertTo-Json -Compress
    exit 0
   }
  }
  Start-Sleep -Milliseconds 250
 }while([DateTime]::UtcNow -lt $deadline)
 if($lastName){Write-Warning ('profile_name_timeout:'+$lastName)}
 throw 'profile_name_timeout'
}
$profile=Get-Profile
if($profile){Show-All $profile|ConvertTo-Json -Compress;exit 0}
$search=Get-Search
if(!$search){$search=Select-SearchTab}
if(!$search){
 $ensure=Join-Path $PSScriptRoot 'ensure_search_window.ps1'
 & powershell -NoProfile -File $ensure -WaitSeconds $WaitSeconds | Out-Null
 if($LASTEXITCODE -ne 0){throw 'search_window_required'}
 $search=Get-Search
}
if(!$search){throw 'search_window_required'}
$input=Get-SearchInput $search
$value=$null
if(!$input.Current.IsEnabled -or $input.Current.IsOffscreen -or !$input.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value)){throw 'search_input_not_editable'}
if($value.Current.IsReadOnly){throw 'search_input_not_editable'}
$value.SetValue($Account)
# ValuePattern.SetValue is asynchronous in current WeChatAppEx builds. Re-read
# the web input instead of trusting the stale pattern object immediately.
$search_value_deadline=[DateTime]::UtcNow.AddSeconds(3)
$value_applied=$false
do{
 Start-Sleep -Milliseconds 100
 $search=Get-Search
 if($search){
  $input=Get-SearchInput $search
  $value=$null
  if($input -and $input.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value) -and $value.Current.Value -ceq $Account){$value_applied=$true;break}
 }
}while([DateTime]::UtcNow -lt $search_value_deadline)
if(!$value_applied){throw 'search_value_not_applied'}
$buttons=$search.document.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'搜索'))
$buttons=@($buttons|Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and !$_.Current.IsOffscreen})
if($buttons.Count -ne 1){throw 'search_button_missing_or_ambiguous'}
Invoke-Verified $buttons[0]
$deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
$openedAccounts=$false
$target=$null
do{
 $search=Get-Search
 if($search){
  $input=Get-SearchInput $search
  $value=$null
  if($input.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value) -and $value.Current.Value -ceq $Account){
   $nodes=$search.document.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
   $candidates=@($nodes|Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and ($_.Current.ClassName -match 'header-detail' -or $_.Current.Name -cmatch '账号描述') -and $_.Current.Name -cmatch ('^'+[Regex]::Escape($Account)+'\s+.*账号描述') -and !$_.Current.IsOffscreen})
   if($candidates.Count -gt 1){throw 'ambiguous_account_result'}
   if($candidates.Count -eq 1){$target=$candidates[0];break}
   if(!$openedAccounts){
    # Current Search exposes an explicit official-account filter button. Select
    # that named filter only; never cycle or click an arbitrary result tab.
    $accountTabs=@($nodes|Where-Object {$_.Current.Name -ceq '公众号' -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and $_.Current.ClassName -match '(^|\s)item(\s|$)' -and !$_.Current.IsOffscreen})
    if($accountTabs.Count -gt 1){throw 'ambiguous_account_filter'}
    if($accountTabs.Count -eq 1){Invoke-Verified $accountTabs[0];$openedAccounts=$true;continue}
    # Compatibility fallback for the older result-block heading.
    $heads=@($nodes|Where-Object {$_.Current.Name -ceq ($Account+' - 账号') -and !$_.Current.IsOffscreen})
    if($heads.Count -eq 1){
     $invoke=$null
     if($heads[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)){$invoke.Invoke();$openedAccounts=$true}
    }
   }
  }
 }
 Start-Sleep -Milliseconds 300
}while([DateTime]::UtcNow -lt $deadline)
if(!$target){throw 'account_result_missing'}
# A matching name is only a navigation candidate. HTTP biz validation remains mandatory.
Invoke-Verified $target
$deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
do{
 $profile=Get-Profile
 if($profile){Show-All $profile|ConvertTo-Json -Compress;exit 0}
 Start-Sleep -Milliseconds 250
}while([DateTime]::UtcNow -lt $deadline)
throw 'profile_page_timeout'
