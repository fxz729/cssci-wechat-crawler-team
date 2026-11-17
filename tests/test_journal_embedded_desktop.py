"""Execute production PowerShell logic against in-memory doubles, never live UIA."""
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'crawl' / 'open_journal_home.ps1'

# No UIAutomation assemblies or user32 imports: even a regression cannot touch Weixin.
HARNESS = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
Add-Type @"
using System;
namespace System.Windows.Automation {
 public enum TreeScope { Children, Descendants }
 public class Condition { public static object TrueCondition = new object(); }
 public class PropertyCondition {
  public string Property; public object Value;
  public PropertyCondition(object p,object v) { Property=(string)p; Value=v; }
 }
 public class AutomationElement {
  public static object RootElement;
  public static string AutomationIdProperty="AutomationId", ClassNameProperty="ClassName",
   ControlTypeProperty="ControlType", NameProperty="Name";
  public static bool Compare(object a,object b) { return Object.ReferenceEquals(a,b); }
 }
 public class ControlType {
  public static string Edit="Edit", Document="Document", Button="Button", Text="Text", TabItem="TabItem";
 }
 public class ValuePattern { public static string Pattern="Value"; }
 public class InvokePattern { public static string Pattern="Invoke"; }
 public class SelectionItemPattern { public static string Pattern="Selection"; }
}
public static class SessionWindowNative {
 public static IntPtr Foreground = (IntPtr)100;
 public static int Activations;
 public static uint Owner = 11;
 public static IntPtr GetForegroundWindow() { return Foreground; }
 public static bool IsProcessWindow(IntPtr h,uint p) { return h==(IntPtr)100 && p==Owner; }
 public static bool Activate(IntPtr h) { Activations++; Foreground=h; return true; }
}
"@
$script:Events=[Collections.Generic.List[string]]::new()
$script:Processes=@{11='Weixin';22='WeChatAppEx';33='Other'}
function Get-Process { param($Id,$ErrorAction) [pscustomobject]@{ProcessName=$script:Processes[[int]$Id]} }
function Start-Sleep { param($Milliseconds) Set-Variable -Name until -Value ([DateTime]::MinValue) -Scope 1 }
function New-Node($class='', $id='', $type='Pane', $process=22, $handle=0, $name='') {
 $node=[pscustomobject]@{
  Current=[pscustomobject]@{ClassName=$class;AutomationId=$id;ControlType=$type;ProcessId=$process;
   NativeWindowHandle=$handle;Name=$name;IsOffscreen=$false;IsEnabled=$true}
  Children=@();Patterns=@{}
 }
 $node | Add-Member ScriptMethod FindAll {
  param($scope,$condition)
  foreach($child in $this.Children) {
   if($condition -isnot [System.Windows.Automation.PropertyCondition] -or
      $child.Current.($condition.Property) -ceq $condition.Value) { $child }
   if($scope -eq [System.Windows.Automation.TreeScope]::Descendants) { $child.FindAll($scope,$condition) }
  }
 }
 $node | Add-Member ScriptMethod FindFirst {
  param($scope,$condition)
  @($this.FindAll($scope,$condition)) | Select-Object -First 1
 }
 $node | Add-Member ScriptMethod TryGetCurrentPattern {
  param($kind,$reference)
  $reference.Value=$this.Patterns[$kind]; return $null -ne $reference.Value
 }
 return $node
}
function New-Invoke($event) {
 $pattern=[pscustomobject]@{Event=$event}
 $pattern | Add-Member ScriptMethod Invoke {
  $script:Events.Add($this.Event)
  if($this.Event -eq 'search' -and $script:AfterSearch) { & $script:AfterSearch }
 }
 return $pattern
}
$ProfileUrl='https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=test-biz'
$hostWindow=New-Node 'Qt51514QWindowIcon' '' 'Window' 11 100
$document=New-Node '' 'RootWebArea' 'Document'
$edit=New-Node '' 'weixin-search-input' 'Edit'
$value=[pscustomobject]@{Current=[pscustomobject]@{IsReadOnly=$false;Value=''}}
$value | Add-Member ScriptMethod SetValue {
 param($text) $this.Current.Value=$text; $script:Events.Add('set')
 if($script:AfterSet) { & $script:AfterSet }
}
$edit.Patterns['Value']=$value
$button=New-Node '' '' 'Button' 22 0 '搜索'
$button.Patterns['Invoke']=New-Invoke 'search'
$card=New-Node 'website-wrapper result'
$link=New-Node '' '' 'Text' 22 0 ($ProfileUrl -replace '^https://','')
$label=New-Node '' '' 'Text' 22 0 '访问网页'
$card.Children=@($link,$label)
$card.Patterns['Invoke']=New-Invoke 'visit'
$document.Children=@($edit,$button,$card)
$hostWindow.Children=@($document)
$desktop=New-Node
$desktop.Children=@($hostWindow)
[System.Windows.Automation.AutomationElement]::RootElement=$desktop
__SETUP__
$tokens=$null;$errors=$null
$source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__SOURCE__'))
$ast=[System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw 'script_parse_failed' }
$outer=@($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] })
if($outer.Count -ne 1) { throw 'outer_try_not_unique' }
# Exclude production Add-Type (UIA/user32). Only the above inert doubles can be used.
$statements=@($outer[0].Body.Statements | Where-Object { $_.Extent.Text -notmatch '^Add-Type\b' })
$code=($statements | ForEach-Object {$_.Extent.Text}) -join "`n"
$errorCode=$null;$payload=$null
try { $raw=& ([scriptblock]::Create($code)); $payload=$raw | ConvertFrom-Json }
catch { $errorCode=$_.Exception.Message }
@{error=$errorCode;payload=$payload;events=@($script:Events.ToArray());activations=[SessionWindowNative]::Activations} | ConvertTo-Json -Depth 6 -Compress
'''


def run_offline(setup=''):
    powershell = shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('PowerShell required for offline UI doubles')
    source = base64.b64encode(SCRIPT.read_bytes()).decode('ascii')
    command = HARNESS.replace('__SOURCE__', source).replace('__SETUP__', setup)
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    # stdin avoids Windows command-line length limits; no production -File invocation.
    result = subprocess.run(
        [powershell, '-NoProfile', '-NonInteractive', '-Command',
         '$c=[Console]::In.ReadToEnd(); & ([scriptblock]::Create([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($c))))'],
        input=encoded.encode('ascii'), capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    assert result.stderr == b''
    return json.loads(result.stdout.decode('utf-8-sig'))


@pytest.mark.parametrize('setup', [
    '',
    "$hostWindow.Current.ClassName='Chrome_WidgetWin_0'; $hostWindow.Current.ProcessId=22; [SessionWindowNative]::Owner=22",
])
def test_visible_embedded_and_legacy_search_submit_exact_card(setup):
    result = run_offline(setup)
    assert result['error'] is None
    assert result['payload']['navigation_submitted'] is True
    assert result['payload']['browser_hwnd'] == 100
    assert result['events'] == ['set', 'search', 'visit']


@pytest.mark.parametrize('setup', [
    "$hostWindow.Current.ClassName='Other'",
    "$script:Processes[11]='Other'",
    "[SessionWindowNative]::Owner=33",
    '$hostWindow.Current.NativeWindowHandle=0',
    '$hostWindow.Current.IsOffscreen=$true',
    '$hostWindow.Current.IsEnabled=$false',
])
def test_untrusted_host_never_activates_or_writes(setup):
    result = run_offline(setup)
    assert result['error'] == 'search_window_missing_or_ambiguous'
    assert result['events'] == []
    assert result['activations'] == 0


@pytest.mark.parametrize('setup', [
    '$document.Children=@()',
    "$document.Current.AutomationId='Other'",
    "$document.Current.ControlType='Pane'",
    '$document.Current.IsOffscreen=$true',
    '$document.Current.ProcessId=33',
    '$edit.Current.IsOffscreen=$true',
    '$edit.Current.IsEnabled=$false',
    '$edit.Current.ProcessId=11',
    "$edit.Current.AutomationId='chat-input'",
    "$edit.Current.ControlType='Pane'",
    '$document.Children=@($button,$card); $hostWindow.Children+=@($edit)',
])
def test_absent_or_untrusted_embedded_search_fails_without_tab_or_sidebar_actions(setup):
    result = run_offline(setup)
    assert result['error'] == 'embedded_search_not_visible'
    assert result['events'] == []
    assert result['activations'] == 0


def test_hidden_duplicate_does_not_mask_the_visible_search_input():
    result = run_offline("$hidden=New-Node '' 'weixin-search-input' 'Edit'; $hidden.Current.IsOffscreen=$true; $document.Children=@($hidden)+$document.Children")
    assert result['error'] is None
    assert result['events'] == ['set', 'search', 'visit']


def test_multiple_visible_inputs_fail_closed():
    result = run_offline("$document.Children+=@(New-Node '' 'weixin-search-input' 'Edit')")
    assert result['error'] == 'search_window_missing_or_ambiguous'
    assert result['events'] == []


@pytest.mark.parametrize('setup', [
    "$chat=New-Node '' '' 'Button' 11 0 '搜索'; $chat.Patterns['Invoke']=New-Invoke 'chat'; $hostWindow.Children+=@($chat)",
    "$other=New-Node '' 'RootWebArea' 'Document'; $other.Children=@((New-Node '' '' 'Button' 22 0 '搜索')); $hostWindow.Children+=@($other)",
])
def test_search_is_scoped_to_the_document_containing_the_verified_input(setup):
    result = run_offline(setup)
    assert result['error'] is None
    assert result['events'] == ['set', 'search', 'visit']


@pytest.mark.parametrize('setup,code,events', [
    ('$button.Current.ProcessId=11', 'search_button_missing_or_ambiguous', ['set']),
    ('$button.Current.IsOffscreen=$true', 'search_button_missing_or_ambiguous', ['set']),
    ('$card.Current.ProcessId=11', 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ('$card.Current.IsEnabled=$false', 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ('$card.Current.IsOffscreen=$true', 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ("$link.Current.Name+='&extra=1'", 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ("$link.Current.Name=$link.Current.Name.Replace('test-biz','TEST-BIZ')", 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ('$link.Current.IsOffscreen=$true', 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ('$document.Children=@($edit,$button); $hostWindow.Children+=@($card)', 'profile_link_missing_or_ambiguous', ['set', 'search']),
    ("$script:AfterSet={ [SessionWindowNative]::Foreground=[IntPtr]999 }", 'foreground_changed', ['set']),
    ("$script:AfterSearch={ $edit.Current.IsOffscreen=$true }", 'search_context_changed', ['set', 'search']),
    ("$script:AfterSearch={ $script:Processes[22]='Other' }", 'search_context_changed', ['set', 'search']),
])
def test_guarded_actions_fail_closed(setup,code,events):
    result = run_offline(setup)
    assert result['error'] == code
    assert result['events'] == events
