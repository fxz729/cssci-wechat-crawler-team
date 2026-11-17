"""Offline desktop-boundary tests: never execute the navigation script/UI."""
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crawl import journal_desktop

URL='https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=test-biz'
SCRIPT=Path(journal_desktop.__file__).with_name('open_journal_home.ps1')
CODES=(
    'unapproved_home_url', 'foreground_changed', 'search_tab_not_selectable',
    'search_window_missing_or_ambiguous', 'search_input_not_editable',
    'search_value_not_applied', 'search_button_missing_or_ambiguous',
    'search_button_not_invokable', 'embedded_search_not_visible', 'search_context_changed',
    'profile_link_missing_or_ambiguous','profile_link_not_invokable', 'ui_navigation_failed',
)


def response(payload,returncode=1):
    return SimpleNamespace(returncode=returncode,
                           stdout=json.dumps(payload).encode('utf-8-sig'),
                           stderr=b'private exception https://example.invalid/?token=PRIVATE')


@pytest.mark.parametrize('code',CODES)
@pytest.mark.parametrize('returncode',[0,1])
def test_preserves_only_fixed_error_codes(monkeypatch,code,returncode):
    run=Mock(return_value=response({'success':False,'error_code':code},returncode))
    monkeypatch.setattr(journal_desktop.subprocess,'run',run)
    sleep=Mock()
    with pytest.raises(RuntimeError) as exc:
        journal_desktop.open_profile(URL,'test-biz',sleep=sleep)
    assert str(exc.value)==code
    assert run.call_count==2
    sleep.assert_called_once_with(1)
    assert all(call.kwargs['timeout']==25 for call in run.call_args_list)


@pytest.mark.parametrize('code',[
    'https://example.invalid/?token=PRIVATE', 'foreground_changed\nPRIVATE',
    'FOREGROUND_CHANGED', None, [], {},
])
def test_unknown_error_code_is_not_exposed(monkeypatch,code):
    monkeypatch.setattr(journal_desktop.subprocess,'run',
                        Mock(return_value=response({'success':False,'error_code':code})))
    with pytest.raises(RuntimeError,match='^ui_navigation_failed$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)


@pytest.mark.parametrize('raw',[
    b'private invalid JSON', b'\xff', b'[]', b'null', b'"private"', b'1',
    b'{}', b'{"success":"true"}',
])
def test_invalid_response_has_fixed_code(monkeypatch,raw):
    monkeypatch.setattr(journal_desktop.subprocess,'run',Mock(return_value=
        SimpleNamespace(returncode=0,stdout=raw,stderr=b'PRIVATE')))
    with pytest.raises(RuntimeError,match='^ui_invalid_response$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)


@pytest.mark.parametrize(('error','code'),[
    (subprocess.TimeoutExpired(['PRIVATE'],25,output=b'PRIVATE'),'ui_navigation_timeout'),
    (OSError('PRIVATE URL exception'),'ui_unavailable'),
])
def test_transport_errors_are_sanitized_and_bounded(monkeypatch,error,code):
    run=Mock(side_effect=error)
    monkeypatch.setattr(journal_desktop.subprocess,'run',run)
    with pytest.raises(RuntimeError) as exc:
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)
    assert str(exc.value)==code
    assert exc.value.__context__ is None
    assert run.call_count==2


def test_success_after_one_failure(monkeypatch):
    success={'success':True,'navigation_submitted':True,'browser_hwnd':123}
    run=Mock(side_effect=[response({'success':False,'error_code':'foreground_changed'}),
                          response(success,0)])
    monkeypatch.setattr(journal_desktop.subprocess,'run',run)
    assert journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)==success
    assert run.call_count==2


def test_nonzero_exit_cannot_claim_success(monkeypatch):
    monkeypatch.setattr(journal_desktop.subprocess,'run',
                        Mock(return_value=response({'success':True})))
    with pytest.raises(RuntimeError,match='^ui_invalid_response$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)


@pytest.mark.parametrize('stop_values',[[True],[False,True]])
def test_pause_prevents_remaining_attempts(monkeypatch,stop_values):
    run=Mock(return_value=response({'success':False,'error_code':'foreground_changed'}))
    monkeypatch.setattr(journal_desktop.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='^paused_by_operator$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None,
                                     should_stop=Mock(side_effect=stop_values))
    assert run.call_count==len(stop_values)-1


def test_unapproved_url_never_starts_subprocess(monkeypatch):
    run=Mock()
    monkeypatch.setattr(journal_desktop.subprocess,'run',run)
    with pytest.raises(ValueError,match='^unapproved_home_navigation$'):
        journal_desktop.open_profile(URL+'&token=PRIVATE','test-biz')
    run.assert_not_called()


@pytest.mark.parametrize('message',list(CODES)+[
    'https://example.invalid/?token=PRIVATE','FOREGROUND_CHANGED',
    'foreground_changed\nPRIVATE',
])
def test_powershell_catch_whitelists_without_running_ui(message):
    powershell=shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('PowerShell unavailable; isolated catch test requires Windows')
    # Parse the production file, but execute ONLY its outer catch block.
    source=SCRIPT.read_text(encoding='utf-8-sig').replace("'","''")
    escaped=message.replace("'","''")
    command=f"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseInput('{source}',[ref]$tokens,[ref]$errors)
if($errors.Count){{throw 'script_parse_failed'}}
$outer=@($ast.EndBlock.Statements | Where-Object {{$_ -is [System.Management.Automation.Language.TryStatementAst]}})
if($outer.Count -ne 1){{throw 'outer_try_not_unique'}}
$body=$outer[0].CatchClauses[0].Body.Extent.Text
$message='{escaped}'
& ([scriptblock]::Create('try {{throw $message}} catch '+$body))
"""
    encoded=base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    result=subprocess.run([powershell,'-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                          capture_output=True,timeout=15)
    assert result.returncode==1
    assert result.stderr==b''
    payload=json.loads(result.stdout.decode('utf-8-sig'))
    expected=message if message in CODES else 'ui_navigation_failed'
    assert payload=={'success':False,'error_code':expected}


def test_script_focus_and_navigation_boundaries():
    source=SCRIPT.read_text(encoding='utf-8-sig')
    thrown=set(re.findall(r"throw '([^']+)'",source))
    assert thrown<=set(CODES)
    assert "if(!$input -or $input.Current.IsOffscreen -or !$input.Current.IsEnabled" in source
    assert re.search(r'Guard-SearchContext \$window \$context\s+\$value.SetValue\(\$ProfileUrl\)',source)
    assert re.search(r'Guard-SearchContext \$window \$context\s+\$invoke.Invoke\(\)',source)
    assert source.count('.SetValue(')==1 and source.count('.Invoke()')==2
    for forbidden in ('SendKeys','SendWait','WaitForInputIdle','GetLastInputInfo',
                      'CloseWindow','WM_CLOSE','Stop-Process','Start-Process','Navigate('):
        assert forbidden not in source


def test_empty_failed_process_preserves_unavailable_compatibility(monkeypatch):
    monkeypatch.setattr(journal_desktop.subprocess,'run',Mock(return_value=
        SimpleNamespace(returncode=1,stdout=b'',stderr=b'PRIVATE')))
    with pytest.raises(RuntimeError,match='^ui_unavailable$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)


def test_second_attempt_reports_its_own_failure(monkeypatch):
    monkeypatch.setattr(journal_desktop.subprocess,'run',Mock(side_effect=[
        response({'success':False,'error_code':'foreground_changed'}),
        response({'success':False,'error_code':'search_value_not_applied'}),
    ]))
    with pytest.raises(RuntimeError,match='^search_value_not_applied$'):
        journal_desktop.open_profile(URL,'test-biz',sleep=lambda _:None)


def test_missing_search_never_switches_tabs_or_uses_coordinates():
    source=SCRIPT.read_text(encoding='utf-8-sig')
    for forbidden in ('Select-NativeSearchTab', '.Select()', 'ScreenToClient',
                      'SendMessageTimeout', 'BoundingRectangle', 'SetCursorPos'):
        assert forbidden not in source
    assert "throw 'embedded_search_not_visible'" in source


def test_activation_attaches_both_threads_and_always_detaches():
    source=SCRIPT.read_text(encoding='utf-8-sig')
    assert 'AttachThreadInput(own,foreground,true)' in source
    assert 'AttachThreadInput(own,target,true)' in source
    assert 'finally' in source
    assert 'AttachThreadInput(own,target,false)' in source
    assert 'AttachThreadInput(own,foreground,false)' in source


def test_visits_only_exact_target_website_card_after_search():
    source=SCRIPT.read_text(encoding='utf-8-sig')
    assert "'website-wrapper*'" in source
    assert '$_.Current.Name -ceq $expectedLink' in source
    assert "'访问网页'" in source
    assert 'Guard-SearchContext $window $context\n $visit.Invoke()' in source
    assert 'profile_link_missing_or_ambiguous' in source


def test_embedded_document_has_no_native_handle_requirement():
    source=SCRIPT.read_text(encoding='utf-8-sig')
    assert "'RootWebArea'" in source
    assert "'Qt51514QWindowIcon'" in source
    assert "'Weixin'" in source
    assert "'Chrome_WidgetWin_0'" in source
    assert 'IsProcessWindow' in source
