param([long]$WindowHandle, [string]$OutputPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Web
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class CssciObservationNative {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint dx,uint dy,uint data,UIntPtr extra);
}
"@
$root=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
$clientPid=$root.Current.ProcessId
if((Get-Process -Id $clientPid).ProcessName -ne 'WeChatAppEx' -or $root.Current.ClassName -ne 'Chrome_WidgetWin_0'){throw 'not_wechat_browser'}
function Assert-Focus {
 if([CssciObservationNative]::GetForegroundWindow().ToInt64() -ne $WindowHandle){throw 'focus_mismatch'}
}
function Get-ArticleDocument {
 return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document)))
}
function Copy-PublicArticleLink {
 Assert-Focus
 $menu=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty,'AppMenuButton')))
 if(-not $menu -or $menu.Current.BoundingRectangle.IsEmpty){throw 'menu_missing'}
 $rect=$menu.Current.BoundingRectangle
 [CssciObservationNative]::SetCursorPos([int]($rect.X+$rect.Width/2),[int]($rect.Y+$rect.Height/2)) | Out-Null
 [CssciObservationNative]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
 [CssciObservationNative]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 200
 $windows=[System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty,$clientPid)))
 $copy=$null
 foreach($w in $windows){$els=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition);foreach($e in $els){if($e.Current.ControlType.ProgrammaticName -eq 'ControlType.MenuItem' -and $e.Current.Name -eq '复制链接'){$copy=$e}}}
 if(-not $copy){throw 'copy_link_missing'}
 $copy.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
 Start-Sleep -Milliseconds 150
 $link=Get-Clipboard -Raw
 $uri=$null
 if(-not [Uri]::TryCreate($link,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -ne 'https' -or $uri.Host -ne 'mp.weixin.qq.com' -or ($uri.AbsolutePath -ne '/s' -and -not $uri.AbsolutePath.StartsWith('/s/'))){throw 'not_official_article_url'}
 $q=[System.Web.HttpUtility]::ParseQueryString($uri.Query);$parts=@()
 foreach($k in @('__biz','mid','idx','sn')){if($q[$k]){$parts+=[Uri]::EscapeDataString($k)+'='+[Uri]::EscapeDataString($q[$k])}}
 $public=$uri.GetLeftPart([UriPartial]::Path);if($parts.Count -gt 0){$public+='?'+($parts -join '&')}
 return $public
}
Assert-Focus
$deadline=[DateTime]::UtcNow.AddSeconds(15)
do {
 $doc=Get-ArticleDocument
 if($doc -and $doc.Current.Name -and $doc.Current.Name -notmatch '^(正在加载|微信公众平台)$'){break}
 Start-Sleep -Milliseconds 250
} while([DateTime]::UtcNow -lt $deadline)
if(-not $doc -or $doc.Current.Name -match '^(正在加载|微信公众平台|)$'){throw 'article_not_ready'}
$title=$doc.Current.Name
$runtime=($doc.GetRuntimeId() -join ',')
$urlBefore=Copy-PublicArticleLink
$elements=$doc.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
$buttons=@();$reads=@();$bottom=$root.Current.BoundingRectangle.Bottom
foreach($el in $elements){
 $c=$el.Current;$name=$c.Name;$rect=$c.BoundingRectangle
 if($c.ControlType.ProgrammaticName -eq 'ControlType.Group' -and $name -match '^阅读\s*\d+(\.\d+)?[万亿]?\+?$'){$reads+=[PSCustomObject]@{label=$name;control_type='Group';scope='article_read_count'}}
 if($c.ControlType.ProgrammaticName -eq 'ControlType.Button' -and $name -match '^(赞|分享|在看|收藏|留言|评论)\s*' -and -not $c.IsOffscreen -and -not $rect.IsEmpty -and $rect.Top -ge $bottom-180){
  $buttons+=[PSCustomObject]@{label=$name;control_type='Button';scope='article_action_bar';top=$rect.Top}
 }
}
# Require the actual bottom action bar, not a comment's like button or body text.
$rows=@()
foreach($button in $buttons){
 $same=@($buttons | Where-Object {[Math]::Abs($_.top-$button.top) -le 8})
 if(@($same | Where-Object {$_.label -match '^赞\s*\d'}).Count -gt 0 -and @($same | Where-Object {$_.label -match '^分享\s*\d'}).Count -gt 0 -and @($same | Where-Object {$_.label -match '^在看\s*\d'}).Count -gt 0){$rows+=,$same}
}
$candidates=@($reads)
if($rows.Count -gt 0){
 $barTop=$rows[0][0].top
 if(@($rows | Where-Object {[Math]::Abs($_[0].top-$barTop) -gt 8}).Count -gt 0){throw 'ambiguous_article_action_bar'}
 $candidates+=@($rows[0] | ForEach-Object {[PSCustomObject]@{label=$_.label;control_type='Button';scope='article_action_bar'}})
}
$urlAfter=Copy-PublicArticleLink
$after=Get-ArticleDocument
if(-not $after -or $urlBefore -ne $urlAfter -or $after.Current.Name -ne $title -or ($after.GetRuntimeId() -join ',') -ne $runtime){throw 'article_changed_during_observation'}
Assert-Focus
$result=[PSCustomObject]@{title=$title;url=$urlAfter;identity_stable=$true;metric_candidates=$candidates;captured_at=[DateTime]::UtcNow.ToString('o')}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output 'OBSERVATION_SAVED'

