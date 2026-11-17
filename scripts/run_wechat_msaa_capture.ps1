param(
    [Parameter(Mandatory=$true)][long]$ProfileWindowHandle,
    [Parameter(Mandatory=$true)][string]$Biz,
    [Parameter(Mandatory=$true)][string]$CaptureInbox,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [int]$MaxArticles = 0,
    [int]$DelaySeconds = 15,
    [int]$WaitSeconds = 45,
    [int]$ScrollDelta = -120,
    [int]$MaxIdleScrolls = 4,
    [int]$MinCardHeight = 40,
    [int]$CaptureRetries = 4,
    [int]$MetricsGraceSeconds = 5,
    [string]$Account = '',
    [switch]$RefreshArticle,
    [switch]$StartFromTop,
    [int]$ResumeAnchorStride = 3,
    [int]$ResumeAnchorMaxSteps = 120,
    [int]$MaxNoArticleMinutes = 25
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class CssciMsaaNative {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object ppv);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint data, UIntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
    [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);

    public static object Accessible(IntPtr hwnd) {
        Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
        object value;
        int hr = AccessibleObjectFromWindow(hwnd, 0xFFFFFFFC, ref iid, out value);
        if (hr < 0) Marshal.ThrowExceptionForHR(hr);
        return value;
    }

    public static bool Activate(IntPtr h) {
        uint pid;
        uint own = GetCurrentThreadId();
        uint foreground = GetWindowThreadProcessId(GetForegroundWindow(), out pid);
        uint target = GetWindowThreadProcessId(h, out pid);
        bool a = false, b = false;
        try {
            ShowWindow(h, 9);
            if (own != foreground) a = AttachThreadInput(own, foreground, true);
            if (own != target && target != foreground) b = AttachThreadInput(own, target, true);
            BringWindowToTop(h);
            SetForegroundWindow(h);
            return GetForegroundWindow() == h;
        } finally {
            if (b) AttachThreadInput(own, target, false);
            if (a) AttachThreadInput(own, foreground, false);
        }
    }

    public static long FindBrowserWindow(uint processId) {
        long found = 0;
        EnumWindows((h, l) => {
            if (!IsWindowVisible(h)) return true;
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (pid != processId) return true;
            var cls = new StringBuilder(128);
            GetClassName(h, cls, cls.Capacity);
            if (cls.ToString() != "Chrome_WidgetWin_0") return true;
            found = h.ToInt64();
            return false;
        }, IntPtr.Zero);
        return found;
    }

    private static long _foundChild = 0;
    private static bool EnumChildProc(IntPtr h, IntPtr l) {
        var cls = new StringBuilder(128);
        GetClassName(h, cls, cls.Capacity);
        if (cls.ToString() == "Chrome_RenderWidgetHostHWND") {
            _foundChild = h.ToInt64();
            return false;
        }
        return true;
    }

    public static long FindRenderWindow(long parent) {
        _foundChild = 0;
        EnumChildWindows(new IntPtr(parent), EnumChildProc, IntPtr.Zero);
        return _foundChild;
    }

    public static long[] ListBrowserWindows() {
        var list = new System.Collections.Generic.List<long>();
        EnumWindows((h, l) => {
            if (!IsWindowVisible(h)) return true;
            var cls = new StringBuilder(128);
            GetClassName(h, cls, cls.Capacity);
            if (cls.ToString() != "Chrome_WidgetWin_0") return true;
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            try {
                if (System.Diagnostics.Process.GetProcessById((int)pid).ProcessName != "WeChatAppEx") return true;
            } catch { return true; }
            list.Add(h.ToInt64());
            return true;
        }, IntPtr.Zero);
        return list.ToArray();
    }

    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr h, EnumProc p, IntPtr l);
}
'@

function Invoke-Key([byte]$Key, [bool]$Up) {
    [CssciMsaaNative]::keybd_event($Key, 0, $(if ($Up) { 2 } else { 0 }), [UIntPtr]::Zero)
}

function Normalize-Title([string]$Value) {
    return [regex]::Replace(
        $Value.Normalize([Text.NormalizationForm]::FormKC),
        '\s+',
        ' '
    ).Trim()
}

function Get-ObjectPath($Root, [int[]]$Path) {
    $current = $Root
    foreach ($index in $Path) {
        $current = $current.accChild($index)
        if ($null -eq $current) { return $null }
    }
    return $current
}

function Get-ObjectLocation($Object) {
    $left = 0; $top = 0; $width = 0; $height = 0
    try { [void]$Object.accLocation([ref]$left, [ref]$top, [ref]$width, [ref]$height, 0) } catch {}
    return [pscustomobject]@{
        Left = $left; Top = $top; Width = $width; Height = $height
        Right = $left + $width; Bottom = $top + $height
        CenterX = [int]($left + $width / 2); CenterY = [int]($top + $height / 2)
    }
}

function Get-TitleInSubtree($Object, [int]$Depth = 5) {
    if ($Depth -lt 0 -or $null -eq $Object) { return $null }
    try {
        $name = [string]$Object.accName(0)
        $role = [int]$Object.accRole(0)
        if ($role -eq 41 -and $name.Length -ge 6 -and
            $name -notmatch '^(阅读|赞|个内容|正在加载|已无更多消息|置顶|余下\s*\d+\s*篇|查看全部|展开全部|收起|查看更多|今天|昨天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日)') {
            return $name
        }
        $count = [int]$Object.accChildCount
        for ($index = 1; $index -le [Math]::Min($count, 24); $index++) {
            $child = $Object.accChild($index)
            $title = Get-TitleInSubtree $child ($Depth - 1)
            if ($title) { return $title }
        }
    } catch {}
    return $null
}

function Get-CardNodes($Root, $ProfileRect, [int]$Depth = 12) {
    $cards = [Collections.Generic.List[object]]::new()
    $state = [pscustomobject]@{ Terminal = $false; Restriction = $false; Loading = $false }

    function Walk($Object, [int]$Remaining) {
        if ($null -eq $Object -or $Remaining -lt 0) { return }
        try {
            $name = [string]$Object.accName(0)
            if ($name) {
                if ($name -match '已无更多消息') { $state.Terminal = $true }
                elseif ($name -match '操作频繁|操作繁忙|访问过于频繁|请稍后再试') { $state.Restriction = $true }
                elseif ($name -match '正在加载|加载中') { $state.Loading = $true }
            }

            $role = [int]$Object.accRole(0)
            if ($role -eq 20) {
                $location = Get-ObjectLocation $Object
                $candidate = ($location.Width -gt 0 -and $location.Height -ge $MinCardHeight)
                $visible = ($candidate -and
                            $location.Top -ge $ProfileRect.Top -and
                            $location.Bottom -le $ProfileRect.Bottom)
                if ($candidate) {
                    $action = [string]$Object.accDefaultAction(0)
                    if ($action -eq '点击') {
                        $title = Get-TitleInSubtree $Object 5
                        if ($title) {
                            # 已确认是卡片：其子树无需再深挖，显著降低单次扫描成本
                            if ($Remaining -gt 4) { $Remaining = 4 }
                            $cards.Add([pscustomobject]@{
                                Title = $title; Path = $Remaining
                                Left = $location.Left; Top = $location.Top
                                Width = $location.Width; Height = $location.Height
                                Right = $location.Right; Bottom = $location.Bottom
                                CenterX = $location.CenterX; CenterY = $location.CenterY
                                Visible = $visible
                            })
                            return
                        }
                    }
                }
            }

            $count = [int]$Object.accChildCount
            for ($index = 1; $index -le [Math]::Min($count, 4000); $index++) {
                Walk $Object.accChild($index) ($Remaining - 1)
            }
        } catch {}
    }

    Walk $Root $Depth
    return [pscustomobject]@{
        Cards = @($cards | Sort-Object Top, Left -Unique)
        Terminal = $state.Terminal
        Restriction = $state.Restriction
        Loading = $state.Loading
        ProfileTop = $ProfileRect.Top
        ProfileBottom = $ProfileRect.Bottom
    }
}

function Set-SharedUtf8Text([string]$Path, [string]$Text) {
    $bytes = ([System.Text.UTF8Encoding]::new($false)).GetBytes($Text)
    $lastError = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Create,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
            return
        } catch [System.IO.IOException] {
            $lastError = $_
            Start-Sleep -Milliseconds 100
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    throw "state_log_write_failed:$($lastError.Exception.Message)"
}

function Add-SharedUtf8Line([string]$Path, [string]$Text) {
    $payload = $Text + [Environment]::NewLine
    $bytes = ([System.Text.UTF8Encoding]::new($false)).GetBytes($payload)
    $lastError = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Append,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
            return
        } catch [System.IO.IOException] {
            $lastError = $_
            Start-Sleep -Milliseconds 100
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    throw "article_log_write_failed:$($lastError.Exception.Message)"
}

function Read-NewCapture($Path, [long]$Offset, [string]$ExpectedTitle, [string]$ExpectedBiz, [int]$TimeoutSeconds, [int]$MetricsGraceSeconds = 5) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $metricsDeadline = $null
    $article = $null
    $metrics = $null
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false), $true)
        try {
            $stream.Seek($Offset, [IO.SeekOrigin]::Begin) | Out-Null
            while ([DateTime]::UtcNow -lt $deadline) {
                $line = $reader.ReadLine()
                if ($null -eq $line) {
                    if ($null -ne $metricsDeadline -and [DateTime]::UtcNow -ge $metricsDeadline) { break }
                    Start-Sleep -Milliseconds 250
                    continue
                }
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                try { $record = $line | ConvertFrom-Json } catch { continue }
                if ($record.biz -ne $ExpectedBiz) { continue }
                if ($record.kind -eq 'article' -and (Normalize-Title $record.article.title) -eq (Normalize-Title $ExpectedTitle)) {
                    $article = $record
                    if ($null -eq $metricsDeadline) {
                        $metricsDeadline = [DateTime]::UtcNow.AddSeconds($MetricsGraceSeconds)
                    }
                } elseif ($article -and $record.kind -eq 'metrics' -and
                    $record.mid -eq $article.mid -and $record.idx -eq $article.idx -and $record.sn -eq $article.sn) {
                    $metrics = $record
                    break
                }
                if ($null -ne $metricsDeadline -and [DateTime]::UtcNow -ge $metricsDeadline) { break }
            }
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
    return [pscustomobject]@{ Article = $article; Metrics = $metrics }
}

function Read-MetricsFromOffset($Path, [long]$Offset, $Article, [int]$TimeoutSeconds = 3) {
    if ($null -eq $Article) { return $null }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false), $true)
        try {
            $stream.Seek($Offset, [IO.SeekOrigin]::Begin) | Out-Null
            while ([DateTime]::UtcNow -lt $deadline) {
                $line = $reader.ReadLine()
                if ($null -eq $line) { Start-Sleep -Milliseconds 200; continue }
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                try { $record = $line | ConvertFrom-Json } catch { continue }
                if ($record.kind -eq 'metrics' -and
                    $record.biz -eq $Article.biz -and
                    $record.mid -eq $Article.mid -and
                    $record.idx -eq $Article.idx -and
                    $record.sn -eq $Article.sn) {
                    return $record
                }
            }
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
    return $null
}

function Get-BootstrapMetric($Metrics, [string]$FieldName) {
    if ($null -eq $Metrics -or $null -eq $Metrics.bootstrap_fields) { return $null }
    foreach ($prefix in @(
        '$html.cgiDataNew.user_info.appmsg_bar_data.',
        '$html.appmsg_bar_data.',
        '$html.cgiData.'
    )) {
        $property = $Metrics.bootstrap_fields.PSObject.Properties[($prefix + $FieldName)]
        if ($null -eq $property) { continue }
        $values = @($property.Value)
        foreach ($value in $values) {
            if ($null -ne $value -and $null -ne $value.value) { return $value.value }
        }
    }
    return $null
}

function Activate-Profile($Handle) {
    if (-not [CssciMsaaNative]::Activate([IntPtr]$Handle)) { throw 'profile_activate_failed' }
}

function Get-ProfileDocumentName([long]$Handle) {
    # 与 profile_switch.ps1 一致：取 UI Automation 的 Document 元素名（即公众号名）
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Handle)
        if ($null -eq $root) { return '' }
        $cond = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Document)
        $docs = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond) |
                  Where-Object { -not $_.Current.IsOffscreen })
        if ($docs.Count -ge 1) { return [string]$docs[0].Current.Name }
    } catch {}
    return ''
}

function Wait-ArticleWindow([long]$ProfileHandle, $Existing, [int]$TimeoutMs = 8000) {
    # 只接受本次点击后新出现的浏览器窗口，避免命中残留的旧文章页。
    $existingSet = [Collections.Generic.HashSet[long]]::new()
    foreach ($item in @($Existing)) { [void]$existingSet.Add([long]$item) }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    do {
        foreach ($process in @(Get-Process WeChatAppEx -ErrorAction SilentlyContinue)) {
            $h = [CssciMsaaNative]::FindBrowserWindow([uint32]$process.Id)
            if ($h -ne 0 -and [long]$h -ne [long]$ProfileHandle -and -not $existingSet.Contains([long]$h)) {
                return [long]$h
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    foreach ($h in @([CssciMsaaNative]::ListBrowserWindows())) {
        if ([long]$h -ne [long]$ProfileHandle -and -not $existingSet.Contains([long]$h)) {
            return [long]$h
        }
    }
    return [long]0
}

function Activate-Browser($Handle) {
    if (-not [CssciMsaaNative]::Activate([IntPtr]$Handle)) { throw 'browser_activate_failed' }
}

function Close-Browser($Handle) {
    Activate-Browser $Handle
    Invoke-Key 0x11 $false
    Invoke-Key 0x57 $false
    Invoke-Key 0x57 $true
    Invoke-Key 0x11 $true
}

function Close-ExistingArticleWindows([long]$ProfileHandle) {
    $closed = 0
    foreach ($handle in @([CssciMsaaNative]::ListBrowserWindows())) {
        if ([long]$handle -eq [long]$ProfileHandle) { continue }
        [void][CssciMsaaNative]::PostMessage([IntPtr]$handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
        $closed++
    }
    if ($closed -gt 0) { Start-Sleep -Milliseconds 400 }
    return $closed
}

function Invoke-ArticleReload([long]$Handle) {
    try {
        $root = [CssciMsaaNative]::Accessible([IntPtr]$Handle)
        if ($null -ne $root) {
            $script:reloadTarget = $null
            function Find-ReloadButton($Object, [int]$Remaining) {
                if ($null -eq $Object -or $Remaining -lt 0 -or $null -ne $script:reloadTarget) { return }
                try {
                    $name = [string]$Object.accName(0)
                    $role = [int]$Object.accRole(0)
                    if ($role -eq 43 -and ($name -eq '重新加载' -or $name -match '^(Reload|Refresh)$')) {
                        $location = Get-ObjectLocation $Object
                        if ($location.Width -gt 0 -and $location.Height -gt 0) {
                            $script:reloadTarget = $Object
                            return
                        }
                    }
                    $count = [int]$Object.accChildCount
                    for ($index = 1; $index -le [Math]::Min($count, 200); $index++) {
                        Find-ReloadButton $Object.accChild($index) ($Remaining - 1)
                        if ($null -ne $script:reloadTarget) { return }
                    }
                } catch {}
            }
            Find-ReloadButton $root 7
            if ($null -ne $script:reloadTarget) {
                $location = Get-ObjectLocation $script:reloadTarget
                Activate-Browser $Handle
                [void][CssciMsaaNative]::SetCursorPos($location.CenterX, $location.CenterY)
                Start-Sleep -Milliseconds 150
                [CssciMsaaNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
                [CssciMsaaNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
                return $true
            }
        }
    } catch {}
    try {
        Activate-Browser $Handle
        Invoke-Key 0x11 $false
        Invoke-Key 0x52 $false
        Invoke-Key 0x52 $true
        Invoke-Key 0x11 $true
    } catch {}
    return $false
}

function Ensure-FiddlerCapture {
    try {
        $fiddler = Get-Process -Name 'Fiddler' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $fiddler -or $fiddler.MainWindowHandle -eq 0) { return $false }
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$fiddler.MainWindowHandle)
        if ($null -eq $root) { return $false }
        $all = @($root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        ))
        foreach ($element in $all) {
            try {
                $name = [string]$element.Current.Name
                if ($name -notmatch '系统代理已更改|重新启用捕获') { continue }
                $rect = $element.Current.BoundingRectangle
                if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
                Activate-Profile $fiddler.MainWindowHandle
                [void][CssciMsaaNative]::SetCursorPos(
                    [int]($rect.X + $rect.Width / 2),
                    [int]($rect.Y + $rect.Height / 2)
                )
                Start-Sleep -Milliseconds 150
                [CssciMsaaNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
                [CssciMsaaNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds 800
                return $true
            } catch {}
        }
    } catch {}
    return $false
}

function Get-ProfileState($ProfileHandle, $RenderHandle) {
    $rect = New-Object CssciMsaaNative+RECT
    if (-not [CssciMsaaNative]::GetWindowRect([IntPtr]$ProfileHandle, [ref]$rect)) { throw 'profile_rect_failed' }
    return Get-CardNodes ([CssciMsaaNative]::Accessible([IntPtr]$RenderHandle)) $rect
}

function Get-ProfileWheelPoint($ProfileHandle) {
    $rect = New-Object CssciMsaaNative+RECT
    if (-not [CssciMsaaNative]::GetWindowRect([IntPtr]$ProfileHandle, [ref]$rect)) { throw 'profile_rect_failed' }
    return [pscustomobject]@{
        X = [int]($rect.Left + ($rect.Right - $rect.Left) / 2)
        Y = [int]($rect.Top + ($rect.Bottom - $rect.Top) * 0.55)
    }
}

function Reset-ProfileTop($ProfileHandle) {
    Activate-Profile $ProfileHandle
    $point = Get-ProfileWheelPoint $ProfileHandle
    [void][CssciMsaaNative]::SetCursorPos($point.X, $point.Y)
    for ($index = 0; $index -lt 160; $index++) {
        [CssciMsaaNative]::mouse_event(0x0800, 0, 0, [uint32]1200, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 70
    }
    Start-Sleep -Milliseconds 700
}

function Select-ArticleHistoryTab($ProfileHandle, $RenderHandle) {
    # 主页默认“全部”混合流只覆盖近期内容；点击“文章”筛选后历史目录可继续加载到更早文章。
    try {
        $root = [CssciMsaaNative]::Accessible([IntPtr]$RenderHandle)
        if ($null -eq $root) { return $false }
        $script:articleTabTarget = $null
        function Find-ArticleTab($Object, [int]$Remaining) {
            if ($null -eq $Object -or $Remaining -lt 0 -or $null -ne $script:articleTabTarget) { return }
            try {
                $name = [string]$Object.accName(0)
                $role = [int]$Object.accRole(0)
                if ($name -eq '文章' -and $role -eq 41) {
                    $location = Get-ObjectLocation $Object
                    if ($location.Width -gt 0 -and $location.Height -gt 0) { $script:articleTabTarget = $Object; return }
                }
                $count = [int]$Object.accChildCount
                for ($index = 1; $index -le [Math]::Min($count, 300); $index++) {
                    Find-ArticleTab $Object.accChild($index) ($Remaining - 1)
                    if ($null -ne $script:articleTabTarget) { return }
                }
            } catch {}
        }
        Find-ArticleTab $root 9
        if ($null -eq $script:articleTabTarget) { return $false }
        $location = Get-ObjectLocation $script:articleTabTarget
        Activate-Profile $ProfileHandle
        [void][CssciMsaaNative]::SetCursorPos($location.CenterX, $location.CenterY)
        Start-Sleep -Milliseconds 200
        [CssciMsaaNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [CssciMsaaNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 900
        return $true
    } catch {
        return $false
    }
}

function Send-ProfileWheel([int]$WheelDelta) {
    $point = Get-ProfileWheelPoint $ProfileWindowHandle
    [void][CssciMsaaNative]::SetCursorPos($point.X, $point.Y)
    [CssciMsaaNative]::mouse_event(0x0800, 0, 0, [uint32]((([long]$WheelDelta + 4294967296) % 4294967296)), [UIntPtr]::Zero)
}

function Restore-ProfileAnchor($ProfileHandle, $RenderHandle, [string]$AnchorTitle, [int]$Stride, [int]$MaxSteps) {
    $anchor = Normalize-Title $AnchorTitle
    if (-not $anchor) { return $false }
    $coarseDelta = [Math]::Abs([int]$ScrollDelta) * [Math]::Max(2, [int]$Stride)
    $deadline = [DateTime]::UtcNow.AddSeconds(75)

    for ($step = 0; $step -lt $MaxSteps; $step++) {
        if ([DateTime]::UtcNow -ge $deadline) { break }
        $state = Get-ProfileState $ProfileHandle $RenderHandle
        if ($state.Restriction) { throw 'platform_restriction_visible' }
        $matches = @($state.Cards | Where-Object {
            (Normalize-Title $_.Title) -eq $anchor
        } | Select-Object -First 1)
        if ($matches.Count -gt 0) { return $true }
        if ($state.Terminal) { break }

        Activate-Profile $ProfileHandle
        Send-ProfileWheel (-$coarseDelta)
        if ($state.Loading) { Start-Sleep -Milliseconds 1200 } else { Start-Sleep -Milliseconds 300 }
    }
    return $false
}

function Extend-CaptureWindow($InboxPath) {
    $pilotDir = Split-Path -Parent $InboxPath
    $python = 'D:\miniforge3\python.exe'
    $code = "from pathlib import Path; from capture.setup import set_capture_enabled; set_capture_enabled(Path(r'$($pilotDir.Replace("'", "''"))'), True, minutes=60)"
    & $python -X utf8 -c $code | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'capture_window_extend_failed' }
}

$CaptureInbox = [IO.Path]::GetFullPath($CaptureInbox)
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
[IO.Directory]::CreateDirectory($OutputDir) | Out-Null
$statePath = Join-Path $OutputDir 'state.json'
$outputPath = Join-Path $OutputDir 'articles.jsonl'
$metricsRetryQueuePath = [IO.Path]::GetFullPath((Join-Path $OutputDir '..\..\..\metrics_retry_queue.jsonl'))
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($metricsRetryQueuePath)) | Out-Null
$state = if (Test-Path -LiteralPath $statePath) {
    Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    [pscustomobject]@{ processed_titles = @(); processed_identities = @(); completed = $false; stop_reason = ''; last_processed_title = ''; processed_count = 0 }
}
foreach ($fieldName in @('last_processed_title', 'processed_count')) {
    if ($null -eq $state.PSObject.Properties[$fieldName]) {
        $state | Add-Member -NotePropertyName $fieldName -NotePropertyValue $(if ($fieldName -eq 'processed_count') { 0 } else { '' }) -Force
    }
}
$processed = [Collections.Generic.HashSet[string]]::new()
foreach ($title in @($state.processed_titles)) { [void]$processed.Add((Normalize-Title ([string]$title))) }
if ([string]::IsNullOrWhiteSpace([string]$state.last_processed_title) -and $processed.Count -gt 0 -and (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    try {
        $lastLine = Get-Content -LiteralPath $outputPath -Tail 1 -Encoding UTF8
        if (-not [string]::IsNullOrWhiteSpace([string]$lastLine)) {
            $lastRow = $lastLine | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$lastRow.title)) {
                $state.last_processed_title = [string]$lastRow.title
            }
        }
    } catch {}
}
$state.processed_count = $processed.Count
$skippedTitles = @($state.skipped_titles | Where-Object { $_ })
$state | Add-Member -NotePropertyName skipped_titles -NotePropertyValue $skippedTitles -Force
$skipped = [Collections.Generic.HashSet[string]]::new()
foreach ($title in $skippedTitles) { [void]$skipped.Add((Normalize-Title ([string]$title))) }
$renderHandle = [CssciMsaaNative]::FindRenderWindow($ProfileWindowHandle)
if ($renderHandle -eq 0) { throw 'render_window_missing' }
if ($Account) {
    $docName = Get-ProfileDocumentName ([long]$ProfileWindowHandle)
    if (-not $docName) {
        Write-Output '{"warning":"profile_document_unreadable"}'
    } elseif ((Normalize-Title $docName) -ne (Normalize-Title $Account)) {
        throw ("profile_identity_mismatch:" + $docName)
    }
}
$count = 0
$idleScrolls = 0
$loadingWaits = 0
$loopCount = 0
$cacheHits = 0
$approachScrolls = 0
$skipScanScrolls = 0
$pendingTitle = $null
$pendingAttempts = 0
$pendingSearchScrolls = 0
$lastOffset = (Get-Item -LiteralPath $CaptureInbox).Length
$lastCaptureExtend = [DateTime]::MinValue
$lastProgressAt = [DateTime]::UtcNow
$lastArticleAt = [DateTime]::UtcNow
$lastCardCount = -1
$lastFiddlerCheck = [DateTime]::MinValue
Extend-CaptureWindow $CaptureInbox
$lastCaptureExtend = [DateTime]::UtcNow
if (Ensure-FiddlerCapture) { Write-Output '{"status":"fiddler_capture_reenabled"}' }
Reset-ProfileTop $ProfileWindowHandle
if (Select-ArticleHistoryTab $ProfileWindowHandle $renderHandle) {
    Write-Output '{"status":"article_tab_selected"}'
}
$profileState = $null
if (-not $StartFromTop -and $state.completed -ne $true -and $processed.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace([string]$state.last_processed_title)) {
    Write-Output ([pscustomobject]@{
        status = 'resume_anchor_searching'
        anchor = [string]$state.last_processed_title
        processed_count = $processed.Count
    } | ConvertTo-Json -Compress)
    $anchorRestored = $false
    try {
        $anchorRestored = Restore-ProfileAnchor $ProfileWindowHandle $renderHandle ([string]$state.last_processed_title) $ResumeAnchorStride $ResumeAnchorMaxSteps
    } catch {
        Write-Output ([pscustomobject]@{ status = 'resume_anchor_error'; reason = $_.Exception.Message } | ConvertTo-Json -Compress)
        $anchorRestored = $false
    }
    if ($anchorRestored) {
        Write-Output ([pscustomobject]@{ status = 'resume_anchor_found'; anchor = [string]$state.last_processed_title } | ConvertTo-Json -Compress)
    } else {
        Write-Output ([pscustomobject]@{ status = 'resume_anchor_fallback_top'; anchor = [string]$state.last_processed_title } | ConvertTo-Json -Compress)
        Reset-ProfileTop $ProfileWindowHandle
    }
}
$profileStateRect = $null
$journalStartedAt = [DateTime]::UtcNow

try {
    while ($MaxArticles -eq 0 -or $count -lt $MaxArticles) {
        $loopCount++
        if (([DateTime]::UtcNow - $lastFiddlerCheck).TotalSeconds -ge 300) {
            if (Ensure-FiddlerCapture) { Write-Output '{"status":"fiddler_capture_reenabled"}' }
            $lastFiddlerCheck = [DateTime]::UtcNow
        }
        if (($loopCount % 6) -eq 0) {
            $hbCards = if ($null -ne $profileState) { $profileState.Cards.Count } else { -1 }
            $hbTarget = ''
            $hbTop = 0
            if ($pendingTitle) { $hbTarget = $pendingTitle }
            elseif ($null -ne $card) { $hbTarget = $card.Title; $hbTop = [int]$card.Top }
            Write-Output ([pscustomobject]@{
                heartbeat = $loopCount; done = $count; cards = $hbCards
                idle = $idleScrolls; approach = $approachScrolls
                terminal = $(if ($null -ne $profileState) { [bool]$profileState.Terminal } else { $false })
                target = $hbTarget; targetTop = $hbTop
            } | ConvertTo-Json -Compress)
        }
        if (([DateTime]::UtcNow - $lastCaptureExtend).TotalMinutes -ge 30) {
            Extend-CaptureWindow $CaptureInbox
            $lastCaptureExtend = [DateTime]::UtcNow
        }
        if ($count -eq 0 -and ([DateTime]::UtcNow - $journalStartedAt).TotalMinutes -ge $MaxNoArticleMinutes) {
            throw "capture_stalled_no_articles:minutes=$MaxNoArticleMinutes"
        }
        if ($count -gt 0 -and ([DateTime]::UtcNow - $lastArticleAt).TotalMinutes -ge 15) {
            throw "capture_stalled_no_progress:count=$count"
        }
        $currentRect = New-Object CssciMsaaNative+RECT
        if (-not [CssciMsaaNative]::GetWindowRect([IntPtr]$ProfileWindowHandle, [ref]$currentRect)) { throw 'profile_rect_failed' }
        $rectChanged = ($null -eq $profileStateRect -or
                        $currentRect.Left -ne $profileStateRect.Left -or
                        $currentRect.Top -ne $profileStateRect.Top -or
                        $currentRect.Right -ne $profileStateRect.Right -or
                        $currentRect.Bottom -ne $profileStateRect.Bottom)
        if ($null -eq $profileState -or $rectChanged) {
            Activate-Profile $ProfileWindowHandle
            $renderHandle = [CssciMsaaNative]::FindRenderWindow($ProfileWindowHandle)
            if ($renderHandle -eq 0) { throw 'render_window_missing' }
            $profileState = Get-ProfileState $ProfileWindowHandle $renderHandle
            if ($profileState.Cards.Count -ne $lastCardCount) {
                # 目录卡片数变化说明微信仍在加载历史文章，不能按“无新增文章”误判卡死。
                $lastCardCount = $profileState.Cards.Count
                $lastProgressAt = [DateTime]::UtcNow
            }
            $approachScrolls = 0
            $skipScanScrolls = 0
            $profileStateRect = $currentRect
        }
        if ($profileState.Restriction) { throw 'platform_restriction_visible' }

        $card = $null
        if ($pendingTitle) {
            $pendingCard = @($profileState.Cards | Where-Object {
                (Normalize-Title $_.Title) -eq $pendingTitle
            } | Select-Object -First 1)
            if ($pendingCard.Count -gt 0) {
                $card = $pendingCard[0]
                $pendingSearchScrolls = 0
            } else {
                $pendingSearchScrolls++
                if ($pendingSearchScrolls -gt 6) {
                    $skipTitle = $pendingTitle
                    [void]$skipped.Add($skipTitle)
                    $state.skipped_titles = @($skipped)
                    $state.stop_reason = ''
                    Set-SharedUtf8Text $statePath ($state | ConvertTo-Json -Depth 8)
                    Write-Output ([pscustomobject]@{
                        index = $count
                        status = 'skipped'
                        reason = 'capture_missing_retry_target_lost_skip'
                        title = $skipTitle
                    } | ConvertTo-Json -Compress)
                    $lastOffset = (Get-Item -LiteralPath $CaptureInbox).Length
                    $pendingTitle = $null
                    $pendingAttempts = 0
                    $pendingSearchScrolls = 0
                    $profileState = $null
                    Start-Sleep -Seconds $DelaySeconds
                    continue
                }
                Activate-Profile $ProfileWindowHandle
                $point = Get-ProfileWheelPoint $ProfileWindowHandle
                [void][CssciMsaaNative]::SetCursorPos($point.X, $point.Y)
                Start-Sleep -Milliseconds 150
                $wheelDelta = [Math]::Abs($ScrollDelta)
                Send-ProfileWheel $wheelDelta
                Start-Sleep -Milliseconds 1500
                $profileState = $null
                continue
            }
        } else {
            $unprocessed = @($profileState.Cards | Where-Object {
                $titleKey = Normalize-Title $_.Title
                -not $processed.Contains($titleKey) -and -not $skipped.Contains($titleKey)
            })
            if ($profileState.Terminal -and $unprocessed.Count -eq 0) {
                $state.completed = $true; $state.stop_reason = 'reached_tail'; break
            }

            $card = @($unprocessed | Select-Object -First 1)
            if ($card.Count -eq 0) {
                # 页面显示“正在加载...”时耐心等待，不计入判停次数（上限 5 分钟），避免把加载中误判为已到底
                if ($profileState.Loading -and $loadingWaits -lt 150) {
                    $loadingWaits++
                    Activate-Profile $ProfileWindowHandle
                    $point0 = Get-ProfileWheelPoint $ProfileWindowHandle
                    [void][CssciMsaaNative]::SetCursorPos($point0.X, $point0.Y)
                    Send-ProfileWheel $ScrollDelta
                    Start-Sleep -Seconds 2
                    $profileState = $null
                    continue
                }
                $loadingWaits = 0
                $idleScrolls++
                if ($idleScrolls -gt $MaxIdleScrolls) { throw "scroll_stalled_no_new_cards:cards=$($profileState.Cards.Count)" }
                Activate-Profile $ProfileWindowHandle
                $point = Get-ProfileWheelPoint $ProfileWindowHandle
                [void][CssciMsaaNative]::SetCursorPos($point.X, $point.Y)
                Start-Sleep -Milliseconds 150
                $skipWheelDelta = [int]$ScrollDelta * 3
                Send-ProfileWheel $skipWheelDelta
                Start-Sleep -Milliseconds 650
                # 跳过已处理卡片时复用缓存坐标，每 20 步才重建一次 MSAA 树
                $skipShiftPx = [int]([Math]::Abs([int]$skipWheelDelta) * 1.6667)
                if ($skipWheelDelta -lt 0) { $skipShiftPx = -$skipShiftPx }
                foreach ($cachedCard in $profileState.Cards) {
                    $cachedCard.Top = [int]$cachedCard.Top + $skipShiftPx
                    $cachedCard.Bottom = [int]$cachedCard.Bottom + $skipShiftPx
                    $cachedCard.CenterY = [int]$cachedCard.CenterY + $skipShiftPx
                }
                $skipScanScrolls++
                if ($skipScanScrolls -ge 20) { $profileState = $null; $skipScanScrolls = 0 }
                continue
            }
            $card = $card[0]
        }

        $cardTitle = Normalize-Title $card.Title
        if ($pendingTitle -eq $cardTitle) {
            $pendingAttempts++
        } else {
            $pendingTitle = $cardTitle
            $pendingAttempts = 1
            $pendingSearchScrolls = 0
        }
        $cardHeight = [int]$card.Bottom - [int]$card.Top
        $viewHeight = [int]$profileState.ProfileBottom - [int]$profileState.ProfileTop
        $fullyVisible = ([int]$card.Top -ge [int]$profileState.ProfileTop) -and ([int]$card.Bottom -le [int]$profileState.ProfileBottom)
        $tallerThanView = $cardHeight -gt $viewHeight
        $clickTop = [Math]::Max([int]$card.Top, [int]$profileState.ProfileTop)
        $clickBottom = [Math]::Min([int]$card.Bottom, [int]$profileState.ProfileBottom)
        if ((-not $fullyVisible) -and (-not $tallerThanView)) {
            # 卡片完全不在可视区：朝它的方向滚动，直到露出来
            $idleScrolls++
            if ($idleScrolls -gt $MaxIdleScrolls) { throw "scroll_stalled_target_not_reached:cards=$($profileState.Cards.Count)" }
            Activate-Profile $ProfileWindowHandle
            $point = Get-ProfileWheelPoint $ProfileWindowHandle
            [void][CssciMsaaNative]::SetCursorPos($point.X, $point.Y)
            Start-Sleep -Milliseconds 150
            # 按实际距离精确滚动，避免大步长在视口上下反复跳导致永远不完整可见
            if ($card.Top -lt $profileState.ProfileTop) {
                $needPx = ([int]$profileState.ProfileTop - [int]$card.Top) + 24
                $wheelDelta = [int][Math]::Min(900, [Math]::Max(120, $needPx / 1.6667))
            } else {
                $needPx = ([int]$card.Bottom - [int]$profileState.ProfileBottom) + 24
                $wheelDelta = -[int][Math]::Min(900, [Math]::Max(120, $needPx / 1.6667))
            }
            Send-ProfileWheel $wheelDelta
            Start-Sleep -Milliseconds 700
            # 不整树重扫：按滚动量平移缓存坐标（实测 1 单位 ~ 1.667px），每 8 次再真实重扫一次纠偏
            $shiftPx = [int]([Math]::Abs([int]$wheelDelta) * 1.6667)
            if ($wheelDelta -lt 0) { $shiftPx = -$shiftPx }
            foreach ($cachedCard in $profileState.Cards) {
                $cachedCard.Top = [int]$cachedCard.Top + $shiftPx
                $cachedCard.Bottom = [int]$cachedCard.Bottom + $shiftPx
                $cachedCard.CenterY = [int]$cachedCard.CenterY + $shiftPx
            }
            $approachScrolls++
            if ($approachScrolls -ge 8) { $profileState = $null; $approachScrolls = 0 }
            continue
        }

        $idleScrolls = 0
        $loadingWaits = 0
        $clickY = if ($fullyVisible) { [int]$card.CenterY } else { [int](($clickTop + $clickBottom) / 2) }
        Activate-Profile $ProfileWindowHandle
        [void](Close-ExistingArticleWindows $ProfileWindowHandle)
        $windowsBefore = @([CssciMsaaNative]::ListBrowserWindows())
        Activate-Profile $ProfileWindowHandle
        [void][CssciMsaaNative]::SetCursorPos($card.CenterX, $clickY)
        [CssciMsaaNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [CssciMsaaNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)

        $browser = Wait-ArticleWindow $ProfileWindowHandle $windowsBefore 9000
        if ($browser -eq 0) {
            # 偶发点击未命中时，先等待窗口延迟出现；仍无窗口则重新激活主页并点击一次。
            Start-Sleep -Milliseconds 1200
            $browser = Wait-ArticleWindow $ProfileWindowHandle $windowsBefore 4000
        }
        if ($browser -eq 0) {
            # 卡片缓存坐标可能因列表高度变化而偏移。不要按旧坐标重复点击，
            # 强制重扫公众号列表，再由 pendingTitle 重新定位并点击。
            if ($pendingAttempts -gt $CaptureRetries) {
                $skipTitle = $pendingTitle
                [void]$skipped.Add($skipTitle)
                $state.skipped_titles = @($skipped)
                $state.stop_reason = ''
                Set-SharedUtf8Text $statePath ($state | ConvertTo-Json -Depth 8)
                Write-Output ([pscustomobject]@{
                    index = $count
                    status = 'skipped'
                    reason = 'browser_window_missing_skip'
                    title = $skipTitle
                } | ConvertTo-Json -Compress)
                $lastOffset = (Get-Item -LiteralPath $CaptureInbox).Length
                $pendingTitle = $null
                $pendingAttempts = 0
                $pendingSearchScrolls = 0
                $profileState = $null
                Start-Sleep -Seconds $DelaySeconds
                continue
            }
            $profileState = $null
            $pendingSearchScrolls = 0
            Start-Sleep -Milliseconds 800
            continue
        }
        Start-Sleep -Milliseconds 800

        $captureOffset = $lastOffset
        $initialWait = [Math]::Min(10, [Math]::Max(3, $WaitSeconds))
        $capture = Read-NewCapture $CaptureInbox $lastOffset $card.Title $Biz $initialWait $MetricsGraceSeconds
        if ($RefreshArticle -and $null -eq $capture.Article) {
            [void](Invoke-ArticleReload $browser)
            $capture = Read-NewCapture $CaptureInbox $lastOffset $card.Title $Biz $WaitSeconds $MetricsGraceSeconds
        }
        if ($null -eq $capture.Article) {
            try { Close-Browser $browser } catch {}
            if ($pendingAttempts -gt $CaptureRetries) {
                $skipTitle = $pendingTitle
                [void]$skipped.Add($skipTitle)
                $state.skipped_titles = @($skipped)
                $state.stop_reason = ''
                Set-SharedUtf8Text $statePath ($state | ConvertTo-Json -Depth 8)
                Write-Output ([pscustomobject]@{
                    index = $count
                    status = 'skipped'
                    reason = 'capture_missing_skip'
                    title = $skipTitle
                } | ConvertTo-Json -Compress)
                $pendingTitle = $null
                $pendingAttempts = 0
                $pendingSearchScrolls = 0
                $profileState = $null
                Start-Sleep -Seconds $DelaySeconds
                continue
            }
            $profileState = $null
            $pendingSearchScrolls = 0
            Start-Sleep -Milliseconds 800
            continue
        }
        if ($null -eq $capture.Metrics) {
            $capture.Metrics = Read-MetricsFromOffset $CaptureInbox $captureOffset $capture.Article
        }
        $lastOffset = (Get-Item -LiteralPath $CaptureInbox).Length

        $article = $capture.Article.article
        $metric = $capture.Metrics.metrics
        if ($null -eq $metric) {
            $metric = [pscustomobject]@{
                read_count = Get-BootstrapMetric $capture.Metrics 'read_num'
                like_count = Get-BootstrapMetric $capture.Metrics 'old_like_count'
                watching_count = Get-BootstrapMetric $capture.Metrics 'like_count'
                share_count = Get-BootstrapMetric $capture.Metrics 'share_count'
                favorite_count = Get-BootstrapMetric $capture.Metrics 'collect_count'
                comment_count = Get-BootstrapMetric $capture.Metrics 'comment_count'
            }
        }
        $missingMetricFields = @()
        foreach ($fieldName in @('read_count', 'like_count', 'watching_count', 'share_count', 'favorite_count', 'comment_count')) {
            if ($null -eq $metric.$fieldName) { $missingMetricFields += $fieldName }
        }
        if ($missingMetricFields.Count -gt 0) {
            Write-Output ([pscustomobject]@{
                index = $count + 1
                status = 'metrics_incomplete'
                title = $card.Title
                missing = $missingMetricFields
                attempt = $pendingAttempts
            } | ConvertTo-Json -Compress)
            if ($pendingAttempts -le $CaptureRetries) {
                Write-Output ([pscustomobject]@{
                    index = $count + 1
                    status = 'metrics_incomplete_retry'
                    title = $card.Title
                    attempt = $pendingAttempts
                    max_attempts = $CaptureRetries + 1
                } | ConvertTo-Json -Compress)
                Close-Browser $browser
                $profileState = $null
                Start-Sleep -Seconds 1
                continue
            }
            $metricsRetryRecord = [pscustomobject]@{
                queued_at = [DateTime]::UtcNow.ToString('o')
                ordinal = $null
                journal_name = $Account
                biz = $article.biz
                mid = $article.mid
                idx = $article.idx
                sn = $article.sn
                title = $article.title
                url = $article.url
                missing = $missingMetricFields
                reason = 'metrics_incomplete_after_retries'
            }
            Add-SharedUtf8Line $metricsRetryQueuePath ($metricsRetryRecord | ConvertTo-Json -Depth 8 -Compress)
        }
        $row = [pscustomobject]@{
            captured_at = $capture.Article.captured_at
            event_id = $capture.Article.event_id
            biz = $capture.Article.biz
            mid = $capture.Article.mid
            idx = $capture.Article.idx
            sn = $capture.Article.sn
            title = $article.title
            author = $article.author
            account_name = $article.account_name
            digest = $article.digest
            publish_time = $article.publish_time
            url = $article.url
            cover_url = $article.cover_url
            content_html = $article.content_html
            metrics = $metric
            metric_event_id = $capture.Metrics.event_id
        }
        Add-SharedUtf8Line $outputPath ($row | ConvertTo-Json -Depth 12 -Compress)
        [void]$processed.Add((Normalize-Title $card.Title))
        $state.last_processed_title = $card.Title
        $state.processed_count = $processed.Count
        $pendingTitle = $null
        $pendingAttempts = 0
        $pendingSearchScrolls = 0
        $count++
        $lastArticleAt = [DateTime]::UtcNow
        Write-Output ([pscustomobject]@{
            index = $count
            title = $row.title
            mid = $row.mid
            metrics = $row.metrics
        } | ConvertTo-Json -Compress)

        Close-Browser $browser
        # 页面未滚动时卡片坐标仍然有效，复用缓存可省掉一次整树扫描；每 5 篇强制刷新一次纠偏
        $cacheHits++
        if ($cacheHits -ge 5) { $profileState = $null; $cacheHits = 0 }

        $state.processed_titles = @($processed)
        $state.processed_identities = @($state.processed_identities) + @([pscustomobject]@{
            biz = $row.biz; mid = $row.mid; idx = $row.idx; sn = $row.sn
        })
        $state.stop_reason = ''
        Set-SharedUtf8Text $statePath ($state | ConvertTo-Json -Depth 8)
        $lastProgressAt = [DateTime]::UtcNow
        Start-Sleep -Seconds $DelaySeconds
    }
} catch {
    $state.stop_reason = $_.Exception.Message
    throw
} finally {
    Set-SharedUtf8Text $statePath ($state | ConvertTo-Json -Depth 8)
}
