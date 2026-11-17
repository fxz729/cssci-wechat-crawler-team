from pathlib import Path


def test_one_click_launcher_keeps_capture_and_audit_steps_explicit():
    path = Path(__file__).parents[1] / "scripts" / "run_wechat_history.ps1"
    assert path.exists()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    for phrase in (
        "check-wechat-capture",
        "crawl-client-history",
        "download_bundle.py",
        "audit-client-bundle",
        "capture_inbox",
    ):
        assert phrase in text
    assert "Cookie" not in text
    assert "pass_ticket" not in text
    assert "Join-Path $Base 'download_bundle.py'" not in text


def test_client_navigation_supports_public_profile_url_switch():
    path = Path(__file__).parents[1] / "crawl" / "client_navigation.ps1"
    text = path.read_text(encoding="utf-8")
    assert "ProfileUrl" in text
    assert "profile-url" in text
    assert "OmniboxViewViews" in text
    assert "0x0D" in text or "Enter" in text


def test_profile_switch_script_uses_wechat_search_and_official_account_card():
    path = Path(__file__).parents[1] / "crawl" / "profile_switch.ps1"
    text = path.read_text(encoding="utf-8")
    assert "搜一搜" in text or "0x46" in text
    assert "搜一搜" in text
    assert "账号描述" in text
    assert "InvokePattern" in text


def test_profile_switch_never_types_into_main_wechat_or_closes_windows():
    path = Path(__file__).parents[1] / "crawl" / "profile_switch.ps1"
    text = path.read_text(encoding="utf-8-sig")
    for unsafe in ("Set-Clipboard", "Get-Clipboard", "keybd_event", "mouse_event",
                   "MainWindowHandle", "SetCursorPos", "NameProperty,'关闭'", "mainRect"):
        assert unsafe not in text, f"unsafe main-window fallback remains: {unsafe}"
    assert "search_window_required" in text
    assert "weixin-search-input" in text
    assert "InspectOnly" in text



def test_catalog_scan_accepts_dynamic_account_name():
    path = Path(__file__).parents[1] / "output" / "journal_validation_20260906" / "scan_profile.ps1"
    text = path.read_text(encoding="utf-8-sig")
    assert "$Account" in text
    assert "[string]$Account" in text


def test_two_stage_operator_scripts_autostart_fiddler_and_chain_phases():
    root = Path(__file__).parents[1]
    check = (root / "scripts" / "check_full_run.ps1").read_text(encoding="utf-8-sig")
    start = (root / "scripts" / "start_full_run.ps1").read_text(encoding="utf-8-sig")
    capture = root / "scripts" / "start_capture_first.ps1"
    background = root / "scripts" / "start_background_only.ps1"

    assert "[ValidateSet('capture-first','background-only')]" in check
    assert "Start-Process" in check
    assert "-WindowStyle Hidden" in check
    assert "Fiddler.exe" in check
    ensure_capture = root / "crawl" / "ensure_fiddler_capture.ps1"
    assert ensure_capture.exists()
    ensure_text = ensure_capture.read_text(encoding="utf-8-sig")
    assert "系统代理已更改。单击以重新启用捕获。" in ensure_text
    assert "SendMessage" in ensure_text
    assert "ProxyServer" in ensure_text
    assert "Set-ItemProperty" not in ensure_text
    assert "ensure_fiddler_capture.ps1" in check
    assert check.index("ensure_fiddler_capture.ps1") < check.index("check-wechat-capture")
    assert "capture_activation.json" in check
    assert "capture_control.json" in check
    assert "capture/setup.py" in check
    assert "--activate-path" in check
    assert "capture_rules_refresh_failed" in check
    assert check.index("capture/setup.py") < check.index("Start-Process")
    assert "if ($Phase -eq 'background-only')" in check
    assert capture.exists() and background.exists()
    assert "--phase capture-first" in capture.read_text(encoding="utf-8-sig")
    assert "--phase background-only" in background.read_text(encoding="utf-8-sig")
    assert "start_capture_first.ps1" in start
    assert "start_background_only.ps1" in start
    assert start.index("start_capture_first.ps1") < start.index("start_background_only.ps1")
    assert "$captureCode -notin @(0, 2)" in start
    assert "capture_first_partial" in start
    assert "$backgroundCode = $LASTEXITCODE" in start
    assert "$captureCode -eq 2 -or $backgroundCode -eq 2" in start


def test_profile_switch_waits_for_async_value_pattern_update():
    path = Path(__file__).parents[1] / "crawl" / "profile_switch.ps1"
    text = path.read_text(encoding="utf-8-sig")
    assert "$value.SetValue($Account)" in text
    assert "search_value_deadline" in text
    assert "Get-SearchInput $search" in text
    assert "if($value.Current.Value -cne $Account){throw 'search_value_not_applied'}" not in text


def test_profile_switch_supports_current_official_account_filter_button():
    path = Path(__file__).parents[1] / "crawl" / "profile_switch.ps1"
    text = path.read_text(encoding="utf-8-sig")
    assert "Name -ceq '公众号'" in text
    assert "ControlType]::Button" in text
    assert "header-detail" in text


def test_search_window_recovery_is_single_postcondition_checked_sidebar_click():
    path = Path(__file__).parents[1] / "crawl" / "ensure_search_window.ps1"
    assert path.exists()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    assert "Qt51514QWindowIcon" in text
    assert "mmui::LoginWindow" in text
    assert "NotifyItemIcon" in text
    assert "mouse_event" in text
    assert "weixin-search-input" in text
    assert "search_window_recovery_failed" in text
    assert "SetValue" not in text
    assert "关闭" not in text


def test_profile_switch_calls_search_recovery_when_profile_replaced_search_page():
    root = Path(__file__).parents[1]
    profile = (root / "crawl" / "profile_switch.ps1").read_text(encoding="utf-8-sig")
    check = (root / "scripts" / "check_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "ensure_search_window.ps1" in profile
    assert "ensure_search_window.ps1" in check


def test_unlock_aware_full_run_launcher_is_safe_and_parseable():
    import base64
    import os
    import shutil
    import subprocess

    path = Path(__file__).parents[1] / "scripts" / "start_unlock_aware_full_run.ps1"
    assert path.exists()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    for token in (
        "LogonUI",
        "GetForegroundWindow",
        "start_capture_first.ps1",
        "stop_full_run.ps1",
        "start_background_only.ps1",
        "Start-Process",
        "WaitForExit",
        "waiting_for_unlock",
        "MaxUnlockedFailures",
    ):
        assert token in text
    for forbidden in ("password", "pass_ticket", "Cookie", "Authorization", "shutdown", "reboot"):
        assert forbidden not in text
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        return
    command = f"$t=$null;$e=$null;[Management.Automation.Language.Parser]::ParseFile('{str(path).replace(chr(39), chr(39)*2)}',[ref]$t,[ref]$e)|Out-Null;if($e.Count){{throw($e|Out-String)}}"
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
        capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
