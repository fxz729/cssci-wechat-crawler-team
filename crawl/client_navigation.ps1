param([ValidateSet('snapshot','open','return','reload','profile','profile-url')][string]$Action,[long]$WindowHandle,[string]$Account,[string]$Title,[string]$RuntimeId='', [string]$ProfileUrl='')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;using System.Runtime.InteropServices;
public static class JournalNavigationNative {
 [DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y);
 [DllImport("user32.dll")]public static extern void mouse_event(uint f,uint x,uint y,uint data,UIntPtr e);
 [DllImport("user32.dll")]public static extern void keybd_event(byte k,byte s,uint f,UIntPtr e);
}
"@
$root=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
if((Get-Process -Id $root.Current.ProcessId).ProcessName -ne 'WeChatAppEx' -or $root.Current.ClassName -ne 'Chrome_WidgetWin_0'){throw 'unapproved_window'}
function Key($s){return (($s.Normalize([Text.NormalizationForm]::FormKC)) -replace '\s+',' ').Trim()}
function Doc {return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document))}
function Guard($expected){
 if([JournalNavigationNative]::GetForegroundWindow().ToInt64() -ne $WindowHandle){throw 'foreground_changed'}
 $d=Doc;if(!$d -or (Key $d.Current.Name) -ne (Key $expected)){throw 'document_identity_changed'};return $d
}
function Invoke-Article($e){
 $scroll=$null;if($e.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern,[ref]$scroll)){$scroll.ScrollIntoView();Start-Sleep -Milliseconds 100}
 $node=$e
 for($j=0;$j -lt 5 -and $node;$j++){
  $p=$null
  if($node.Current.ClassName -match 'article|multi' -and $node.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$p)){
   $null=Guard $Account;$p.Invoke();return
  }
  $node=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($node)
 }
 throw 'article_card_not_invokable'
}
function Wait-Doc($expected){
 $until=[DateTime]::UtcNow.AddSeconds(15)
 do{$d=Doc;if($d -and (Key $d.Current.Name) -eq (Key $expected)){return $d};Start-Sleep -Milliseconds 150}while([DateTime]::UtcNow -lt $until)
 throw 'document_navigation_timeout'
}
function Open-ProfileUrl {
 if([string]::IsNullOrWhiteSpace($ProfileUrl)){throw 'profile_url_missing'}
 $address=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ClassNameProperty,'OmniboxViewViews'))
 if(!$address){throw 'address_bar_missing'}
 $value=$null
 if(!$address.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value)){throw 'address_bar_not_editable'}
 $value.SetValue($ProfileUrl)
 $rect=$address.Current.BoundingRectangle
 [JournalNavigationNative]::SetCursorPos([int]($rect.Left+$rect.Width/2),[int]($rect.Top+$rect.Height/2))
 [JournalNavigationNative]::mouse_event(2,0,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 100
 [JournalNavigationNative]::keybd_event(0x0D,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x0D,0,2,[UIntPtr]::Zero)
 $null=Wait-Doc $Account
 return (Select-Profile)
}

function Select-Profile {
 for($attempt=0;$attempt -lt 30;$attempt++){
  if([JournalNavigationNative]::GetForegroundWindow().ToInt64() -ne $WindowHandle){throw 'foreground_changed'}
  $d=Doc
  if($d -and (Key $d.Current.Name) -eq (Key $Account)){
   $tabs=$d.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'全部'))
   if($tabs -and $tabs.Current.ClassName -match 'profile_details__tabs-item'){return $d}
  }
  [JournalNavigationNative]::keybd_event(0x11,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x09,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x09,0,2,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x11,0,2,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 400
 }
 throw 'profile_tab_not_found'
}
if($Action -eq 'profile'){$d=Select-Profile;[pscustomobject]@{success=$true;document=$d.Current.Name}|ConvertTo-Json -Compress
}elseif($Action -eq 'profile-url'){$d=Open-ProfileUrl;[pscustomobject]@{success=$true;document=$d.Current.Name}|ConvertTo-Json -Compress
}elseif($Action -eq 'snapshot'){
 $doc=Guard $Account;$nodes=$doc.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
 $texts=@($nodes|Where-Object{$_.Current.ControlType.ProgrammaticName -eq 'ControlType.Text' -and $_.Current.Name})
 $items=@();$date='';$markers=@();$active=''
 foreach($n in $nodes){if($n.Current.Name -eq '已无更多消息'){$markers+=$n};if($n.Current.ClassName -match '^profile_details__tabs-item.*active'){$active=$n.Current.Name}}
 for($i=0;$i -lt $texts.Count;$i++){
  $c=$texts[$i].Current
  if($c.Name -match '^(\d{4}年)?\d{1,2}月\d{1,2}日$|^(今天|昨天|星期[一二三四五六日天])$'){$date=$c.Name}
  if($i -eq 0 -or $c.Name -notmatch '^阅读\s*\d'){continue}
  $e=$texts[$i-1];$name=$e.Current.Name
  if($name.Length -lt 4 -or $name -match '^(阅读|\d+个内容|\d{1,2}月\d{1,2}日)'){continue}
  $parent=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($e)
  $items+=[pscustomobject]@{title=$name;title_key=(Key $name);displayed_date=$date;runtime=($e.GetRuntimeId()-join ',');card_class=$parent.Current.ClassName}
 }
 $terminalVisible=$false
 foreach($m in $markers){$p=$null;if($m.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern,[ref]$p)){$p.ScrollIntoView();Start-Sleep -Milliseconds 100};if(!$m.Current.IsOffscreen){$terminalVisible=$true}}
 [pscustomobject]@{captured_at=[DateTime]::UtcNow.ToString('o');account=$Account;active_tab=$active;terminal_label='已无更多消息';terminal_visible=$terminalVisible;items=$items;source='wechat_uia_catalog_navigation_only'}|ConvertTo-Json -Depth 6 -Compress
}elseif($Action -eq 'open'){
 $doc=Guard $Account;$nodes=$doc.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text))
 $matches=@($nodes|Where-Object{(Key $_.Current.Name) -eq (Key $Title)})
 $selected=@($matches|Where-Object{($_.GetRuntimeId()-join ',') -eq $RuntimeId})
 if($selected.Count -eq 1){$item=$selected[0]}elseif($matches.Count -eq 1){$item=$matches[0]}else{throw 'article_title_missing_or_ambiguous'}
 Invoke-Article $item;$newDoc=Wait-Doc $Title
 [pscustomobject]@{action='open';document=$newDoc.Current.Name;document_runtime=($newDoc.GetRuntimeId()-join ',');success=$true}|ConvertTo-Json -Compress
}elseif($Action -eq 'reload'){
 $null=Guard $Title;$reload=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1003'))
 if(!$reload -or $reload.Current.Name -ne '重新加载'){throw 'reload_control_not_verified'}
 # Chromium hard reload requests a fresh response instead of a body-less HTTP 304.
 [JournalNavigationNative]::keybd_event(0x11,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x10,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x52,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x52,0,2,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x10,0,2,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x11,0,2,[UIntPtr]::Zero)
 [pscustomobject]@{action='reload';success=$true}|ConvertTo-Json -Compress
}else{
 $null=Wait-Doc $Title
 Start-Sleep -Milliseconds 200
 $null=Guard $Title
 [JournalNavigationNative]::keybd_event(0x11,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x57,0,0,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x57,0,2,[UIntPtr]::Zero);[JournalNavigationNative]::keybd_event(0x11,0,2,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 300
 $doc=Select-Profile
 [pscustomobject]@{action='return';document=$doc.Current.Name;success=$true}|ConvertTo-Json -Compress
}
