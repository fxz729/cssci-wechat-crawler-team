param(
    [Parameter(Mandatory=$true)][ValidateRange(1,9223372036854775807)][long]$WindowHandle,
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$Account,
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$OutputDir,
    [ValidateRange(0,10000)][int]$MaxSteps = 2000,
    [string]$ExpectedFirstTitle = '',
    [string]$ResumeFramesPath = '',
    [string]$ResumeFramesSha256 = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Test-CatalogCardClass($ClassName) {
    return $ClassName -cmatch '(^|\s)(article-item|article_item|double-img-card-item|js_article_card)(\s|$)'
}

function Get-ActiveCatalogTabElement($Nodes) {
    $active = @($Nodes | Where-Object {
        $_.Current.ClassName -cmatch '(^|\s)profile_details__tabs-item(\s|$)' -and
        $_.Current.ClassName -cmatch '(^|\s)active(\s|$)'
    })
    if ($active.Count -ne 1 -or $active[0].Current.Name -cne '全部') {
        throw 'active_tab_not_all'
    }
    return $active[0]
}

function Get-ActiveCatalogTab($Nodes) {
    return (Get-ActiveCatalogTabElement $Nodes).Current.Name
}

function Select-CatalogTitle($Texts) {
    # Read counters are excluded metadata, never a prerequisite for finding a title.
    $titles = @($Texts | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_.title) -and
        $_.title -notmatch '^(阅读|赞)\s*[\d.,万+]+(?:\s|$)' -and
        $_.title -notmatch '^\d+个内容$|^\d{1,2}:\d{2}$|^(\d{4}年)?\d{1,2}月\d{1,2}日$|^(今天|昨天|星期[一二三四五六日天]|置顶)$'
    })
    if ($titles.Count -ne 1 -or [string]::IsNullOrWhiteSpace($titles[0].runtime)) {
        throw 'unsupported_card_title'
    }
    return $titles[0]
}

function Select-CatalogDate($Texts) {
    $dates = @($Texts | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_.title) -and
        $_.title -match '^(?:\d{4}年)?\d{1,2}月\d{1,2}日$|^(今天|昨天|星期[一二三四五六日天])$'
    } | ForEach-Object { $_.title.Trim() } | Select-Object -Unique)
    if ($dates.Count -gt 1) { throw 'ambiguous_card_date' }
    return $(if ($dates.Count -eq 1) { $dates[0] } else { '' })
}

function Normalize-CatalogText($Value) {
    return ([regex]::Replace(([string]$Value).Replace([char]0x00A0, ' '), '\s+', ' ')).Trim()
}

function Get-CatalogItems($Nodes) {
    $items = [Collections.Generic.List[object]]::new(); $seen = @{}
    foreach ($card in $Nodes) {
        if (-not (Test-CatalogCardClass $card.Current.ClassName)) { continue }
        # The observed article-item and double-img-card-item expose direct Text children.
        # Do not guess a title from a container name or arbitrary descendant text.
        $children = $card.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Text))
        $texts = @($children | ForEach-Object {
            [pscustomobject]@{title=$_.Current.Name; runtime=($_.GetRuntimeId() -join ',')}
        })
        $title = Select-CatalogTitle $texts
        $item = [pscustomobject]@{
            runtime = $title.runtime
            card_class = $card.Current.ClassName
            title = $title.title
            displayed_date = Select-CatalogDate $texts
        }
        Merge-CatalogItems $items $seen @($item)
    }
    if ($items.Count -eq 0) { throw 'unsupported_catalog_cards' }
    return $items.ToArray()
}

function Merge-CatalogItems($Ordered, $Seen, $Items) {
    foreach ($item in $Items) {
        if ([string]::IsNullOrWhiteSpace($item.runtime)) { throw 'missing_card_runtime' }
        if ($Seen.ContainsKey($item.runtime)) {
            $previous = $Seen[$item.runtime]
            if ($previous.title -cne $item.title -or $previous.card_class -cne $item.card_class -or
                $previous.displayed_date -cne $item.displayed_date) {
                throw 'card_runtime_identity_changed'
            }
        } else {
            $Seen[$item.runtime] = $item
            $Ordered.Add($item)
        }
    }
}

function Test-CatalogVisible($Node, $Viewport) {
    $r = $Node.Current.BoundingRectangle
    return (-not $Node.Current.IsOffscreen -and $r.Width -gt 0 -and $r.Height -gt 0 -and
        $Viewport.Width -gt 0 -and $Viewport.Height -gt 0 -and
        $r.Right -gt $Viewport.Left -and $r.Left -lt $Viewport.Right -and
        $r.Bottom -gt $Viewport.Top -and $r.Top -lt $Viewport.Bottom)
}

function Assert-CatalogIdentity($Root, $Document, $ActiveTab, $Handle, $ExpectedAccount, $ExpectedRuntime) {
    if ($null -eq $Document -or $null -eq $ActiveTab) { throw 'catalog_identity_missing' }
    if ((Get-Process -Id $Root.Current.ProcessId).ProcessName -cne 'WeChatAppEx' -or
        $Root.Current.ClassName -cne 'Chrome_WidgetWin_0') { throw 'unapproved_window' }
    if ([JournalCatalogNative]::GetForegroundWindow().ToInt64() -ne $Handle) {
        throw 'foreground_changed'
    }
    if ($Document.Current.IsOffscreen -or $Document.Current.Name -cne $ExpectedAccount) {
        throw 'document_identity_changed'
    }
    $runtime = $Document.GetRuntimeId() -join ','
    if ([string]::IsNullOrWhiteSpace($runtime)) { throw 'unsupported_document_runtime' }
    if ($ExpectedRuntime -and $ExpectedRuntime -cne $runtime) { throw 'document_runtime_changed' }
    $active = Get-ActiveCatalogTab @($ActiveTab)
    $r = $Root.Current.BoundingRectangle; $d = $Document.Current.BoundingRectangle
    $left = [Math]::Max($r.Left, $d.Left); $top = [Math]::Max($r.Top, $d.Top)
    $right = [Math]::Min($r.Right, $d.Right); $bottom = [Math]::Min($r.Bottom, $d.Bottom)
    if ($right -le $left -or $bottom -le $top) { throw 'document_not_visible' }
    return [pscustomobject]@{
        document = $Document; active_tab = $active; active_tab_element = $ActiveTab
        document_runtime = $runtime; scope_observed = $true
        viewport = [pscustomobject]@{
            Left=$left; Top=$top; Right=$right; Bottom=$bottom
            Width=($right-$left); Height=($bottom-$top)
        }
    }
}

function Assert-CatalogContext($Root, $Handle, $ExpectedAccount, $ExpectedRuntime) {
    if ((Get-Process -Id $Root.Current.ProcessId).ProcessName -cne 'WeChatAppEx' -or
        $Root.Current.ClassName -cne 'Chrome_WidgetWin_0') { throw 'unapproved_window' }
    if ([JournalCatalogNative]::GetForegroundWindow().ToInt64() -ne $Handle) {
        throw 'foreground_changed'
    }
    $documents = @($Root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Document)) | Where-Object {
                -not $_.Current.IsOffscreen
            })
    if ($documents.Count -ne 1 -or $documents[0].Current.Name -cne $ExpectedAccount) {
        throw 'document_identity_changed'
    }
    $doc = $documents[0]
    $runtime = $doc.GetRuntimeId() -join ','
    if ([string]::IsNullOrWhiteSpace($runtime)) { throw 'unsupported_document_runtime' }
    if ($ExpectedRuntime -and $ExpectedRuntime -cne $runtime) { throw 'document_runtime_changed' }
    $nodes = $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    $activeElement = Get-ActiveCatalogTabElement $nodes
    $identity = Assert-CatalogIdentity $Root $doc $activeElement $Handle $ExpectedAccount $ExpectedRuntime
    return [pscustomobject]@{
        document = $doc; nodes = $nodes; active_tab = $identity.active_tab
        active_tab_element = $activeElement; document_runtime = $runtime
        scope_observed = $true; viewport = $identity.viewport
    }
}

function Get-CatalogMarkers($Context) {
    $terminalVisible = $false; $terminalLabel = ''; $loading = $false; $loadError = $false
    foreach ($node in $Context.nodes) {
        if (Test-CatalogCardClass $node.Current.ClassName) { continue }
        $name = $node.Current.Name
        if ($name -notmatch '^(已无更多消息|正在加载.*|加载中.*|加载失败.*|加载出错.*|网络错误.*|网络异常.*|操作频繁.*|访问过于频繁.*)$') { continue }
        # An article title is not a terminal or access-error marker.
        $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($node)
        $insideCard = $false
        while ($parent -and ($parent.GetRuntimeId() -join ',') -cne $Context.document_runtime) {
            if (Test-CatalogCardClass $parent.Current.ClassName) { $insideCard = $true; break }
            $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
        }
        if ($insideCard) { continue }
        if ($name -ceq '已无更多消息') {
            $terminalLabel = $name
            if (Test-CatalogVisible $node $Context.viewport) { $terminalVisible = $true }
        } elseif ($name -match '^(正在加载|加载中)' -and (Test-CatalogVisible $node $Context.viewport)) { $loading = $true
        } elseif (Test-CatalogVisible $node $Context.viewport) { $loadError = $true }
    }
    return [pscustomobject]@{
        terminal_visible=$terminalVisible; terminal_label=$terminalLabel
        loading=$loading; load_error=$loadError
    }
}

function Set-CatalogFrameStatus($frame, $step, $MaxSteps) {
    $frame.reached_tail = $frame.terminal_visible -and -not $frame.loading -and -not $frame.load_error
    if ($frame.load_error) { $frame.status = 'catalog_access_or_load_error'
    } elseif ($frame.reached_tail) { $frame.status = 'reached_tail'
    } elseif ($MaxSteps -eq 0) { $frame.status = 'inspection_only'
    } elseif ($step -eq $MaxSteps) { $frame.status = 'catalog_step_limit' }
}

function Write-CatalogFrame($Frame, $Log, $Directory, $Ordered, $Finalize) {
    $json = ConvertTo-Json -InputObject $Frame -Depth 8 -Compress
    $Log.WriteLine($json)
    $Log.Flush()
    $Log.BaseStream.Flush($true)
    [Console]::WriteLine($json)
    [Console]::Out.Flush()
    # JSONL is the recoverable in-progress evidence. A final snapshot is
    # published exactly once and only after the verified terminal frame.
    if (-not $Finalize) { return }
    $snapshot = [ordered]@{}
    foreach ($property in $Frame.PSObject.Properties) { $snapshot[$property.Name] = $property.Value }
    $snapshot['items'] = $Ordered.ToArray()
    $path = Join-Path $Directory 'catalog_snapshot.json'
    $temp = Join-Path $Directory 'catalog_snapshot.json.tmp'
    [IO.File]::WriteAllText($temp, (ConvertTo-Json -InputObject $snapshot -Depth 8),
        [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temp, $path)
}

function Get-VisibleCatalogItems($Context) {
    $visibleCards = @($Context.nodes | Where-Object {
        (Test-CatalogCardClass $_.Current.ClassName) -and (Test-CatalogVisible $_ $Context.viewport)
    })
    if ($visibleCards.Count -eq 0) { throw 'visible_catalog_cards_missing' }
    return @(Get-CatalogItems $visibleCards)
}

function Scroll-Catalog($Root, $Handle, $ExpectedAccount, $ExpectedRuntime, $Context, [int]$Delta = -600) {
    # Never activate a window, select a tab, invoke an article, or send keyboard input.
    $identity = Assert-CatalogIdentity $Root $Context.document $Context.active_tab_element $Handle $ExpectedAccount $ExpectedRuntime
    $r = $identity.viewport
    if (-not [JournalCatalogNative]::SetCursorPos([int]($r.Left+$r.Width/2), [int]($r.Top+$r.Height/2))) {
        throw 'catalog_cursor_failed'
    }
    $null = Assert-CatalogIdentity $Root $Context.document $Context.active_tab_element $Handle $ExpectedAccount $ExpectedRuntime
    $wheel = if ($Delta -lt 0) { [uint32]([long]4294967296 + $Delta) } else { [uint32]$Delta }
    [JournalCatalogNative]::mouse_event(0x0800, 0, 0, $wheel, [UIntPtr]::Zero)
}

function Reset-CatalogTop($Root, $Handle, $ExpectedAccount, $ExpectedRuntime, $ExpectedFirstTitle) {
    $expected = Normalize-CatalogText $ExpectedFirstTitle
    $previous = ''; $stable = 0
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $context = Assert-CatalogContext $Root $Handle $ExpectedAccount $ExpectedRuntime
        $visible = @(Get-VisibleCatalogItems $context)
        $visibleTitles = @($visible | ForEach-Object { Normalize-CatalogText $_.title })
        if ($expected -and $visibleTitles -contains $expected) { return }
        $signature = $visibleTitles -join [char]0x001F
        if ($signature -ceq $previous) { $stable++ } else { $previous = $signature; $stable = 0 }
        if (-not $expected -and $stable -ge 2) { return }
        Scroll-Catalog $Root $Handle $ExpectedAccount $ExpectedRuntime $context 1200
        Start-Sleep -Milliseconds 250
    }
    throw 'catalog_top_unverified'
}

function Read-CatalogResumeFrames($Path, $Sha256, $ExpectedAccount, $ExpectedRuntime) {
    $hasPath = -not [string]::IsNullOrWhiteSpace($Path)
    $hasHash = -not [string]::IsNullOrWhiteSpace($Sha256)
    if ($hasPath -xor $hasHash) { throw 'resume_parameters_incomplete' }
    if (-not $hasPath) {
        return [pscustomobject]@{ lines=@(); frames=@(); next_step=0 }
    }
    if ($Sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'resume_source_changed' }
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { throw 'resume_source_changed' }
    $actualHash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -cne $Sha256.ToLowerInvariant()) { throw 'resume_source_changed' }
    $lines = @(Get-Content -LiteralPath $fullPath -Encoding UTF8)
    if ($lines.Count -eq 0) { throw 'resume_frame_invalid' }
    $frames = @()
    for ($index = 0; $index -lt $lines.Count; $index++) {
        try { $resumeFrame = $lines[$index] | ConvertFrom-Json } catch { throw 'resume_frame_invalid' }
        if ($resumeFrame.step -ne $index) { throw 'resume_steps_not_contiguous' }
        if ($resumeFrame.account -cne $ExpectedAccount -or
            $resumeFrame.document_runtime -cne $ExpectedRuntime) { throw 'resume_document_changed' }
        if ($resumeFrame.schema_version -ne 2 -or
            $resumeFrame.active_tab -cne '全部' -or
            $resumeFrame.scope_observed -ne $true -or
            $resumeFrame.reached_tail -eq $true -or
            $null -eq $resumeFrame.items) { throw 'resume_frame_invalid' }
        $frames += $resumeFrame
    }
    return [pscustomobject]@{ lines=$lines; frames=$frames; next_step=$frames.Count }
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class JournalCatalogNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
}
"@
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
$initial = Assert-CatalogContext $root $WindowHandle $Account ''
$documentRuntime = $initial.document_runtime
if ($ResumeFramesPath) {
    $resume = Read-CatalogResumeFrames $ResumeFramesPath $ResumeFramesSha256 $Account $documentRuntime
} else {
    if ($ResumeFramesSha256) { throw 'resume_parameters_incomplete' }
    Reset-CatalogTop $root $WindowHandle $Account $documentRuntime $ExpectedFirstTitle
    $resume = [pscustomobject]@{ lines=@(); frames=@(); next_step=0 }
}
$null = Assert-CatalogContext $root $WindowHandle $Account $documentRuntime
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
# Refuse reuse: old snapshots/logs are evidence, not scratch files.
foreach ($name in @('catalog_snapshot.json', 'catalog_frames.jsonl', 'catalog_snapshot.json.tmp')) {
    if (Test-Path -LiteralPath (Join-Path $OutputDir $name)) { throw 'catalog_output_exists_use_new_directory' }
}
$null = [IO.Directory]::CreateDirectory($OutputDir)
$stream = [IO.File]::Open((Join-Path $OutputDir 'catalog_frames.jsonl'),
    [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
$log = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
$ordered = [Collections.Generic.List[object]]::new(); $seen = @{}
foreach ($resumeFrame in $resume.frames) {
    Merge-CatalogItems $ordered $seen @($resumeFrame.items)
    $log.WriteLine($resume.lines[$resumeFrame.step])
}
$log.Flush(); $log.BaseStream.Flush($true)
$resumeNextStep = [int]$resume.next_step
try {
    for ($step = 0; $step -le $MaxSteps; $step++) {
        if ($step -lt $resumeNextStep) { continue }
        $context = Assert-CatalogContext $root $WindowHandle $Account $documentRuntime
        $items = @(Get-VisibleCatalogItems $context)
        Merge-CatalogItems $ordered $seen $items
        $markers = Get-CatalogMarkers $context
        # Verify again after enumeration, before publishing any claim about the frame.
        $null = Assert-CatalogIdentity $root $context.document $context.active_tab_element $WindowHandle $Account $documentRuntime
        $frame = [pscustomobject]@{
            schema_version = 2
            scope_observed = $context.scope_observed
            at = [DateTime]::UtcNow.ToString('o')
            step = $step
            account = $Account
            active_tab = $context.active_tab
            document_runtime = $context.document_runtime
            items = $items
            terminal_visible = $markers.terminal_visible
            terminal_label = $markers.terminal_label
            loading = $markers.loading
            load_error = $markers.load_error
            reached_tail = $false
            full_history_complete = $false
            status = 'scanning'
            source = 'wechat_uia_catalog_navigation_only'
        }
        Set-CatalogFrameStatus $frame $step $MaxSteps
        $finalize = $frame.reached_tail -or $MaxSteps -eq 0
        Write-CatalogFrame $frame $log $OutputDir $ordered $finalize
        if ($frame.load_error) { throw 'catalog_access_or_load_error' }
        if ($frame.reached_tail -or $MaxSteps -eq 0) { break }
        if ($step -eq $MaxSteps) { throw 'catalog_step_limit' }
        if (-not $frame.loading) { Scroll-Catalog $root $WindowHandle $Account $documentRuntime $context -600 }
        Start-Sleep -Milliseconds 900
    }
} finally {
    $log.Dispose()
}
