"""Offline contracts: parse/load functions only; never run the UIA entry point."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "crawl" / "client_catalog.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def source():
    assert SCRIPT.exists(), "formal catalog scanner is missing"
    return SCRIPT.read_text(encoding="utf-8-sig")


def run_functions(body):
    source()
    if not POWERSHELL:
        pytest.skip("PowerShell is not installed")
    harness = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:CATALOG_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Only function definitions are evaluated. No Add-Type, UIA, file writes or main loop.
foreach ($definition in $ast.EndBlock.Statements) {
    if ($definition -is [Management.Automation.Language.FunctionDefinitionAst]) {
        Invoke-Expression $definition.Extent.Text
    }
}
""" + body
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(harness.encode("utf-16-le")).decode("ascii")],
        env={**os.environ, "CATALOG_SCRIPT": str(SCRIPT)},
        capture_output=True, encoding="utf-8", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_parameters_and_powershell_parse_without_executing_scanner():
    text = source()
    for name in ("WindowHandle", "Account", "OutputDir", "MaxSteps",
                 "ResumeFramesPath", "ResumeFramesSha256"):
        assert f"${name}" in text
    assert "ValidateRange(0," in text
    assert run_functions("@{parsed=$true} | ConvertTo-Json -Compress") == {"parsed": True}


def test_resume_contract_requires_hash_contiguous_steps_and_same_document():
    text = source()
    for token in (
        "resume_parameters_incomplete",
        "resume_source_changed",
        "resume_steps_not_contiguous",
        "resume_document_changed",
        "resume_frame_invalid",
    ):
        assert token in text
    assert "Get-FileHash" in text
    assert "$resumeNextStep" in text
    assert "Merge-CatalogItems $ordered $seen @($resumeFrame.items)" in text
    resume_branch = text[text.index("if ($ResumeFramesPath)"):]
    assert resume_branch.index("Read-CatalogResumeFrames") < resume_branch.index("for ($step = 0;")
    assert "if ($step -lt $resumeNextStep) { continue }" in resume_branch


def test_source_requires_observed_all_tab_and_window_guards():
    text = source()
    assert not re.search(r"active_tab\s*=\s*['\"]全部['\"]", text, re.I)
    for required in ("WeChatAppEx", "Chrome_WidgetWin_0", "GetForegroundWindow",
                     "document_identity_changed", "document_runtime_changed",
                     "active_tab_not_all", "Get-ActiveCatalogTab"):
        assert required in text
    loop = text[text.index("for ($step = 0;"):]
    assert "Assert-CatalogContext" in loop
    assert loop.index("Assert-CatalogContext") < loop.index("Get-VisibleCatalogItems")
    assert loop.index("Write-CatalogFrame") < loop.index("Scroll-Catalog")


def test_main_scan_parses_only_visible_cards_before_accumulating_catalog():
    loop = source()[source().index("for ($step = 0;"):]
    assert "$items = @(Get-VisibleCatalogItems $context)" in loop
    assert "$items = @(Get-CatalogItems $context.nodes)" not in loop


def test_rechecks_use_cached_document_and_active_tab_elements():
    text = source()
    assert "function Get-ActiveCatalogTabElement(" in text

    context = text[text.index("function Assert-CatalogContext("):text.index("function Get-CatalogMarkers(")]
    assert "active_tab_element" in context

    identity = text[text.index("function Assert-CatalogIdentity("):text.index("function Assert-CatalogContext(")]
    assert ".FindAll(" not in identity
    assert "Get-ActiveCatalogTab @($ActiveTab)" in identity

    loop = text[text.index("for ($step = 0;"):text.index("} finally {")]
    assert loop.count("Assert-CatalogContext") == 1
    assert "$null = Assert-CatalogIdentity $root $context.document $context.active_tab_element" in loop

    scroll = text[text.index("function Scroll-Catalog("):text.index("function Reset-CatalogTop(")]
    assert "Assert-CatalogContext" not in scroll
    assert "Assert-CatalogIdentity $Root $Context.document $Context.active_tab_element" in scroll


@pytest.mark.parametrize("classes,expected", [
    (["profile_details__tabs-item active", "profile_details__tabs-item"], "全部"),
    (["profile_details__tabs-item", "profile_details__tabs-item active"], "error"),
    (["profile_details__tabs-item", "profile_details__tabs-item"], "error"),
    (["profile_details__tabs-item active", "profile_details__tabs-item active"], "error"),
    (["profile_details__tabs-item inactive", "profile_details__tabs-item"], "error"),
])
def test_active_tab_is_verified_not_assumed(classes, expected):
    encoded = json.dumps(classes)
    body = f"$classes = '{encoded}' | ConvertFrom-Json\n" + r"""
$nodes = @(
    [pscustomobject]@{Current=[pscustomobject]@{Name='全部';ClassName=$classes[0]}},
    [pscustomobject]@{Current=[pscustomobject]@{Name='文章';ClassName=$classes[1]}}
)
try { $value = Get-ActiveCatalogTab $nodes } catch { $value = 'error' }
@{value=$value} | ConvertTo-Json -Compress
"""
    assert run_functions(body)["value"] == expected


def test_article_classes_include_double_image_cards_without_read_counter():
    result = run_functions(r"""
$texts = @([pscustomobject]@{title='图片卡片标题';runtime='42,10'})
@{
    ordinary=(Test-CatalogCardClass 'article-item article-item__image-content')
    multi_first=(Test-CatalogCardClass 'first_item article_item multi-first__item article-item__header__with-mask inner-info-line1')
    double=(Test-CatalogCardClass 'double-img-card-item js_article_card')
    false_match=(Test-CatalogCardClass 'article-item__image-content')
    title=(Select-CatalogTitle $texts).title
} | ConvertTo-Json -Compress
""")
    assert result == {"ordinary": True, "multi_first": True, "double": True,
                      "false_match": False, "title": "图片卡片标题"}
    text = source()
    assert "Test-CatalogCardClass" in text and "TreeScope]::Children" in text
    assert "$texts[$i-1]" not in text and "$texts[$i - 1]" not in text


def test_card_title_ambiguity_fails_closed_and_metrics_are_not_titles():
    result = run_functions(r"""
$valid = @([pscustomobject]@{title='标题';runtime='1'},
           [pscustomobject]@{title='阅读 12 赞 0';runtime='2'})
$ambiguous = @([pscustomobject]@{title='标题一';runtime='1'},
               [pscustomobject]@{title='标题二';runtime='2'})
try { $null = Select-CatalogTitle $ambiguous; $bad = $false } catch { $bad = $true }
try { $null = Select-CatalogTitle @(); $empty = $false } catch { $empty = $true }
@{title=(Select-CatalogTitle $valid).title;ambiguous=$bad;empty=$empty} | ConvertTo-Json -Compress
""")
    assert result == {"title": "标题", "ambiguous": True, "empty": True}


def test_runtime_dedup_preserves_order_and_keeps_equal_titles_distinct():
    result = run_functions(r"""
$ordered = [Collections.Generic.List[object]]::new(); $seen = @{}
$first = [pscustomobject]@{runtime='42,20';title='同名';card_class='article-item'}
$second = [pscustomobject]@{runtime='42,1';title='同名';card_class='double-img-card-item js_article_card'}
Merge-CatalogItems $ordered $seen @($first,$second,$first)
$changed = [pscustomobject]@{runtime='42,20';title='另一个标题';card_class='article-item'}
try { Merge-CatalogItems $ordered $seen @($changed); $conflict=$false } catch { $conflict=$true }
@{items=$ordered.ToArray();conflict=$conflict} | ConvertTo-Json -Depth 5 -Compress
""")
    assert [x["runtime"] for x in result["items"]] == ["42,20", "42,1"]
    assert result["conflict"] is True
    assert not re.search(r"\$\w+\.Values\b", source(), re.I)


@pytest.mark.parametrize("offscreen,left,width,expected", [
    (False, 10, 20, True), (True, 10, 20, False),
    (False, 200, 20, False), (False, 10, 0, False),
])
def test_terminal_visibility_requires_viewport_intersection(offscreen, left, width, expected):
    result = run_functions(f"$offscreen = ${str(offscreen).lower()}\n" + f"$left={left};$width={width}\n" + r"""
$node=[pscustomobject]@{Current=[pscustomobject]@{
    IsOffscreen=$offscreen
    BoundingRectangle=[pscustomobject]@{Left=$left;Top=10;Right=($left+$width);Bottom=30;Width=$width;Height=20}
}}
$viewport=[pscustomobject]@{Left=0;Top=0;Right=100;Bottom=100;Width=100;Height=100}
@{visible=(Test-CatalogVisible $node $viewport)} | ConvertTo-Json -Compress
""")
    assert result["visible"] is expected


def test_persistence_and_completion_are_navigation_only_and_bounded():
    text = source()
    for field in ("at", "step", "account", "active_tab", "document_runtime", "items",
                  "terminal_visible", "terminal_label", "runtime", "card_class", "title"):
        assert re.search(rf"\b{field}\s*=", text)
    for token in ("catalog_snapshot.json", "catalog_frames.jsonl", "CreateNew", "Flush",
                  "catalog_access_or_load_error", "unsupported_card_title",
                  "catalog_step_limit", "full_history_complete = $false"):
        assert token in text
    assert "$step -le $MaxSteps" in text
    assert "$MaxSteps -eq 0" in text
    assert "$frame.terminal_visible -and -not $frame.loading -and -not $frame.load_error" in text
    for forbidden in ("Invoke-WebRequest", "HttpClient", "SendKeys", "keybd_event", "0x57",
                      "SetForegroundWindow", "Stop-Process", "read_count", "like_count"):
        assert forbidden not in text


@pytest.mark.skipif(os.name != "nt", reason="Offline UIA enum types require Windows")
def test_enumerator_uses_real_card_classes_and_deduplicates_each_frame():
    result = run_functions(r"""
# Load enums only. FindAll/GetRuntimeId below are synthetic ScriptMethods, not UIA calls.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Mock-Text($title, $id) {
    $text=[pscustomobject]@{Current=[pscustomobject]@{Name=$title};Id=$id}
    $text | Add-Member ScriptMethod GetRuntimeId { return @(42,$this.Id) }
    return $text
}
function Mock-Card($class, $texts) {
    $card=[pscustomobject]@{Current=[pscustomobject]@{ClassName=$class};Texts=$texts}
    $card | Add-Member ScriptMethod FindAll { param($scope,$condition) return $this.Texts }
    return $card
}
$first=Mock-Card 'article-item article-item__image-content' @((Mock-Text '同名' 20),(Mock-Text '阅读 10 赞 2' 21))
$second=Mock-Card 'double-img-card-item js_article_card' @((Mock-Text '同名' 1))
$other=Mock-Card 'profile_details__description' @((Mock-Text '不应进入目录' 9))
$items=@(Get-CatalogItems @($first,$second,$first,$other))
try { $null=Get-CatalogItems @($other); $unknown=$false } catch { $unknown=$true }
@{items=$items;unknown=$unknown} | ConvertTo-Json -Depth 5 -Compress
""")
    assert [x["runtime"] for x in result["items"]] == ["42,20", "42,1"]
    assert [x["title"] for x in result["items"]] == ["同名", "同名"]
    assert result["items"][1]["card_class"] == "double-img-card-item js_article_card"
    assert result["unknown"] is True


@pytest.mark.parametrize("visible,loading,error,step,limit,status,tail", [
    (True, False, False, 5, 10, "reached_tail", True),
    (True, True, False, 5, 10, "scanning", False),
    (True, False, True, 5, 10, "catalog_access_or_load_error", False),
    (False, False, False, 10, 10, "catalog_step_limit", False),
    (False, False, False, 0, 0, "inspection_only", False),
    (True, True, True, 0, 0, "catalog_access_or_load_error", False),
])
def test_frame_completion_never_upgrades_to_full_history(visible, loading, error, step, limit, status, tail):
    data = json.dumps({"terminal_visible": visible, "loading": loading, "load_error": error,
                       "reached_tail": False, "full_history_complete": False, "status": "scanning"})
    result = run_functions(
        f"$frame = '{data}' | ConvertFrom-Json\n"
        f"Set-CatalogFrameStatus $frame {step} {limit}\n"
        "$frame | ConvertTo-Json -Compress"
    )
    assert result["status"] == status
    assert result["reached_tail"] is tail
    assert result["full_history_complete"] is False



def test_zero_step_parameter_binding_without_running_entry_point():
    result = run_functions(r"""
Invoke-Expression ('function Test-CatalogParameters {' + $ast.ParamBlock.Extent.Text + '; return $MaxSteps }')
$zero = Test-CatalogParameters -WindowHandle 1 -Account '测试刊物' -OutputDir 'unused' -MaxSteps 0
try {
    $null = Test-CatalogParameters -WindowHandle 1 -Account '测试刊物' -OutputDir 'unused' -MaxSteps -1
    $negative = $false
} catch { $negative = $true }
@{zero=$zero;negative_rejected=$negative} | ConvertTo-Json -Compress
""")
    assert result == {"zero": 0, "negative_rejected": True}



def test_schema2_scope_flag_is_derived_only_from_validated_context():
    text = source()
    context = text[text.index("function Assert-CatalogIdentity("):text.index("function Get-CatalogMarkers(")]
    assert "scope_observed = $true" in context
    for guard in ("unapproved_window", "foreground_changed", "document_identity_changed",
                  "Get-ActiveCatalogTab", "document_not_visible"):
        assert context.index(guard) < context.index("scope_observed = $true")
    loop = text[text.index("for ($step = 0;"):]
    assert "schema_version = 2" in loop
    assert "scope_observed = $context.scope_observed" in loop
    assert loop.index("Assert-CatalogContext") < loop.index("schema_version = 2")


@pytest.mark.parametrize("failure", ["", "account", "tab", "second_guard"])
def test_schema2_real_loop_offline_success_and_context_failure(failure):
    # Execute ONLY the loop AST with synthetic observation and in-memory publication.
    # No client entry point, native methods, UIA calls, or filesystem writes are executed.
    result = run_functions(f"$failure = '{failure}'\n" + r"""
$script:guardCalls=0; $script:savedFrame=$null
$root=$null; $WindowHandle=1; $Account='集成测试刊物'; $documentRuntime='42,1'
$MaxSteps=0; $OutputDir='unused'; $log=$null; $published=$false
$ordered=[Collections.Generic.List[object]]::new(); $seen=@{}
function Assert-CatalogContext {
    $script:guardCalls++
    if ($failure -eq 'account') { throw 'document_identity_changed' }
    if ($failure -eq 'tab') { throw 'active_tab_not_all' }
    return [pscustomobject]@{
        nodes=@();active_tab='全部';document_runtime='42,1';scope_observed=$true
        document='doc';active_tab_element='tab'
    }
}
function Assert-CatalogIdentity {
    $script:guardCalls++
    if ($failure -eq 'second_guard' -and $script:guardCalls -eq 2) { throw 'foreground_changed' }
    return [pscustomobject]@{active_tab='全部';document_runtime='42,1';scope_observed=$true}
}
function Get-VisibleCatalogItems { return [pscustomobject]@{runtime='42,20';title='合成文章';card_class='article-item'} }
function Get-CatalogMarkers {
    return [pscustomobject]@{terminal_visible=$true;terminal_label='已无更多消息';loading=$false;load_error=$false}
}
function Write-CatalogFrame($Frame) { $script:savedFrame=$Frame }
function Scroll-Catalog { throw 'offline_test_must_not_scroll' }
$loops=@($ast.FindAll({param($node) $node -is [Management.Automation.Language.ForStatementAst]}, $true) |
    Where-Object { $_.Extent.Text -match '\$step\s*=\s*0' })
if ($loops.Count -ne 1) { throw 'unexpected_scanner_loop' }
$errorName=''
try { Invoke-Expression $loops[0].Extent.Text } catch { $errorName=$_.Exception.Message }
@{frame=$script:savedFrame;error=$errorName;guards=$script:guardCalls} | ConvertTo-Json -Depth 8 -Compress
""")
    if failure:
        assert result["frame"] is None
        assert result["error"] == {"account": "document_identity_changed", "tab": "active_tab_not_all",
                                   "second_guard": "foreground_changed"}[failure]
    else:
        assert result["error"] == ""
        assert result["guards"] == 2
        frame = result["frame"]
        assert frame["schema_version"] == 2
        assert frame["scope_observed"] is True
        assert frame["account"] == "集成测试刊物"
        assert frame["active_tab"] == "全部"
        assert frame["terminal_visible"] is True
        assert frame["reached_tail"] is True
        assert frame["status"] == "reached_tail"
        assert frame["full_history_complete"] is False


def test_catalog_card_displayed_date_is_preserved_as_identity_evidence():
    result = run_functions(r'''
$none = @([pscustomobject]@{title='文章标题';runtime='1'})
$one = @([pscustomobject]@{title='文章标题';runtime='1'},
         [pscustomobject]@{title='2026年9月7日';runtime='2'})
$many = @([pscustomobject]@{title='9月7日';runtime='2'},
          [pscustomobject]@{title='9月8日';runtime='3'})
try { $null = Select-CatalogDate $many; $ambiguous=$false } catch { $ambiguous=$true }
@{none=(Select-CatalogDate $none);one=(Select-CatalogDate $one);ambiguous=$ambiguous} | ConvertTo-Json -Compress
''')
    assert result == {"none": "", "one": "2026年9月7日", "ambiguous": True}
    text = source()
    assert "displayed_date = Select-CatalogDate $texts" in text


def test_scanner_resets_to_top_before_each_independent_pass():
    text = source()
    assert "$ExpectedFirstTitle" in text
    assert "function Reset-CatalogTop" in text
    main = text[text.index("$root ="):]
    assert main.index("Reset-CatalogTop") < main.index("for ($step = 0;")
    reset = text[text.index("function Reset-CatalogTop"):text.index("Add-Type -AssemblyName")]
    assert "catalog_top_unverified" in reset
    assert "1200" in reset
    assert "ExpectedFirstTitle" in reset


def test_catalog_snapshot_is_published_once_only_for_a_final_frame():
    text = source()
    assert "function Write-CatalogFrame($Frame, $Log, $Directory, $Ordered, $Finalize)" in text
    assert "if (-not $Finalize) { return }" in text
    assert "$finalize = $frame.reached_tail -or $MaxSteps -eq 0" in text
    assert "[IO.File]::Move($temp, $path)" in text
    assert "[IO.File]::Replace" not in text


def test_loading_and_error_markers_must_be_visible_in_catalog_viewport():
    text = source()
    markers = text[text.index("function Get-CatalogMarkers("):text.index("function Set-CatalogFrameStatus(")]
    assert "elseif ($name -match '^(正在加载|加载中)' -and (Test-CatalogVisible $node $Context.viewport))" in markers
    assert "elseif (Test-CatalogVisible $node $Context.viewport) { $loadError = $true }" in markers


def test_video_duration_is_metadata_not_a_catalog_title():
    result = run_functions(r'''
$texts=@(
 [pscustomobject]@{title='01:57';runtime='duration'},
 [pscustomobject]@{title='视频 | 陈云故事';runtime='title'},
 [pscustomobject]@{title='阅读 70 赞 3';runtime='stats'}
)
$result=Select-CatalogTitle $texts
@{title=$result.title;runtime=$result.runtime}|ConvertTo-Json -Compress
''')
    assert result == {"title": "视频 | 陈云故事", "runtime": "title"}
