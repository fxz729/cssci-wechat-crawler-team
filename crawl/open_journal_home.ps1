param([Parameter(Mandatory=$true)][string]$ProfileUrl)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
try {
 $uri=[Uri]$ProfileUrl
 if($uri.Scheme -ne 'https' -or $uri.Authority -ne 'mp.weixin.qq.com' -or $uri.AbsolutePath -ne '/mp/profile_ext' -or $uri.Query -notmatch '(^|[?&])action=home(&|$)' -or $uri.Query -match '(?i)([?&])(key|uin|pass_ticket|token)='){throw 'unapproved_home_url'}
 Add-Type -AssemblyName UIAutomationClient
 Add-Type -AssemblyName UIAutomationTypes
 Add-Type @"
using System;using System.Runtime.InteropServices;
public static class SessionWindowNative {
 [DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")]public static extern bool ShowWindow(IntPtr h,int c);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a,uint b,bool attach);
 [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
 public static bool IsProcessWindow(IntPtr h,uint expected) { uint actual; return GetWindowThreadProcessId(h,out actual)!=0 && actual==expected; }
 public static bool Activate(IntPtr h) {
  uint pid; uint own=GetCurrentThreadId(),foreground=GetWindowThreadProcessId(GetForegroundWindow(),out pid),target=GetWindowThreadProcessId(h,out pid);
  bool a=false,b=false;
  try {
   ShowWindow(h,9);
   if(own!=foreground) a=AttachThreadInput(own,foreground,true);
   if(own!=target && target!=foreground) b=AttachThreadInput(own,target,true);
   BringWindowToTop(h); SetForegroundWindow(h);
   return GetForegroundWindow()==h;
  } finally {
   if(b) AttachThreadInput(own,target,false);
   if(a) AttachThreadInput(own,foreground,false);
  }
 }

}
"@
 function BrowserKind($window) {
  try {
   $current=$window.Current
   if($current.NativeWindowHandle -eq 0 -or $current.IsOffscreen -or !$current.IsEnabled -or ![SessionWindowNative]::IsProcessWindow([IntPtr]$current.NativeWindowHandle,[uint32]$current.ProcessId)){return}
   $process=(Get-Process -Id $current.ProcessId -ErrorAction Stop).ProcessName
   if($process -eq 'WeChatAppEx' -and $current.ClassName -eq 'Chrome_WidgetWin_0'){return 'standalone'}
   if($process -eq 'Weixin' -and $current.ClassName -eq 'Qt51514QWindowIcon'){return 'embedded'}
  } catch {return}
 }
 function BrowserWindows {
  $wins=[System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
  foreach($w in $wins){if(BrowserKind $w){$w}}
 }
 function Test-WebElement($element,$processId) {
  try {
   return $element -and !$element.Current.IsOffscreen -and $element.Current.IsEnabled -and $element.Current.ProcessId -eq $processId -and (Get-Process -Id $processId -ErrorAction Stop).ProcessName -eq 'WeChatAppEx'
  } catch {return $false}
 }
 function SearchContext($window) {
  $kind=BrowserKind $window
  if(!$kind){return}
  $roots=@($window)
  if($kind -eq 'embedded'){
   # RootWebArea is a UIA Document, not a native window (its HWND may be zero).
   $roots=@($window.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'RootWebArea')) | Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document})
  }
  $contexts=@(foreach($root in $roots){
   $webProcessId=$root.Current.ProcessId
   if(!(Test-WebElement $root $webProcessId)){continue}
   $edits=@($root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'weixin-search-input')) | Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and (Test-WebElement $_ $webProcessId)})
   foreach($edit in $edits){[pscustomobject]@{Root=$root;Input=$edit;ProcessId=$webProcessId}}
  })
  if($contexts.Count -gt 1){throw 'search_window_missing_or_ambiguous'}
  if($contexts.Count -eq 1){return $contexts[0]}
 }
 function Guard($window) {
  if(!(BrowserKind $window) -or [SessionWindowNative]::GetForegroundWindow() -ne [IntPtr]$window.Current.NativeWindowHandle){throw 'foreground_changed'}
 }
 function Guard-SearchContext($window,$context) {
  Guard $window
  try {$current=SearchContext $window} catch {throw 'search_context_changed'}
  if(!$current -or $current.ProcessId -ne $context.ProcessId -or ![System.Windows.Automation.AutomationElement]::Compare($current.Root,$context.Root) -or ![System.Windows.Automation.AutomationElement]::Compare($current.Input,$context.Input)){throw 'search_context_changed'}
 }
 $windows=@(BrowserWindows)
 $candidates=@(foreach($w in $windows){
  $found=SearchContext $w
  if($found){[pscustomobject]@{Window=$w;Context=$found}}
 })
 if($candidates.Count -eq 0 -and @($windows | Where-Object {(BrowserKind $_) -eq 'embedded'}).Count -gt 0){throw 'embedded_search_not_visible'}
 if($candidates.Count -ne 1){throw 'search_window_missing_or_ambiguous'}
 $window=$candidates[0].Window;$context=$candidates[0].Context
 # No tab/sidebar discovery: only an already visible, verified search may be used.
 if(!(BrowserKind $window)){throw 'foreground_changed'}
 [void][SessionWindowNative]::Activate([IntPtr]$window.Current.NativeWindowHandle)
 Guard-SearchContext $window $context
 $input=$context.Input;$value=$null
 if(!$input -or $input.Current.IsOffscreen -or !$input.Current.IsEnabled -or !$input.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value) -or $value.Current.IsReadOnly){throw 'search_input_not_editable'}
 Guard-SearchContext $window $context
 $value.SetValue($ProfileUrl)
 $until=[DateTime]::UtcNow.AddSeconds(3);$applied=$false
 do {
  Start-Sleep -Milliseconds 100
  Guard-SearchContext $window $context
  $value=$null
  if($input.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value) -and $value.Current.Value -ceq $ProfileUrl){$applied=$true;break}
 } while([DateTime]::UtcNow -lt $until)
 if(!$applied){throw 'search_value_not_applied'}
 $nodes=$context.Root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'搜索'))
 $buttons=@($nodes|Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and (Test-WebElement $_ $context.ProcessId)})
 if($buttons.Count -ne 1){throw 'search_button_missing_or_ambiguous'}
 $invoke=$null
 if(!$buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)){throw 'search_button_not_invokable'}
 if(!(Test-WebElement $buttons[0] $context.ProcessId)){throw 'search_button_missing_or_ambiguous'}
 Guard-SearchContext $window $context
 $invoke.Invoke()
 # The search result contains a dedicated website card; ordinary results are never opened.
 $expectedLink=$ProfileUrl -replace '^https://',''
 $until=[DateTime]::UtcNow.AddSeconds(6);$cards=@()
 do {
  Start-Sleep -Milliseconds 150
  Guard-SearchContext $window $context
  $nodes=$context.Root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
  $cards=@($nodes | Where-Object {
   if($_.Current.ClassName -notlike 'website-wrapper*' -or !(Test-WebElement $_ $context.ProcessId)){return $false}
   $text=@($_.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text)) | Where-Object {Test-WebElement $_ $context.ProcessId})
   return @($text | Where-Object {$_.Current.Name -ceq $expectedLink}).Count -eq 1 -and @($text | Where-Object {$_.Current.Name -ceq '访问网页'}).Count -eq 1
  })
  if($cards.Count -eq 1){break}
 } while([DateTime]::UtcNow -lt $until)
 if($cards.Count -ne 1){throw 'profile_link_missing_or_ambiguous'}
 $visit=$null
 if(!$cards[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$visit)){throw 'profile_link_not_invokable'}
 if(!(Test-WebElement $cards[0] $context.ProcessId)){throw 'profile_link_missing_or_ambiguous'}
 Guard-SearchContext $window $context
 $visit.Invoke()
 # Only a navigation attempt is reported; Fiddler/HTTP validates the actual biz.
 @{success=$true;navigation_submitted=$true;browser_hwnd=$window.Current.NativeWindowHandle}|ConvertTo-Json -Compress
} catch {
 # Never print full invocation/exception objects containing request material.
 $code='ui_navigation_failed'
 $allowed=@('unapproved_home_url','foreground_changed','search_tab_not_selectable',
  'search_window_missing_or_ambiguous','search_input_not_editable','search_value_not_applied',
  'embedded_search_not_visible','search_context_changed',
  'search_button_missing_or_ambiguous','search_button_not_invokable',
  'profile_link_missing_or_ambiguous','profile_link_not_invokable')
 if($allowed -ccontains $_.Exception.Message){$code=$_.Exception.Message}
 @{success=$false;error_code=$code}|ConvertTo-Json -Compress
 exit 1
}
