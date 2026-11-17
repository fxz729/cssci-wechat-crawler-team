import json
from pathlib import Path
import pytest
from capture.setup import prepare_capture, renew_capture_window, set_capture_enabled, set_diagnostic_enabled

def test_prepare_is_data_only_and_does_not_overwrite_unknown_active_rules(tmp_path):
    fiddler=tmp_path/'Fiddler';(fiddler/'Scripts').mkdir(parents=True)
    (fiddler/'Scripts'/'SampleRules.js').write_text('import Fiddler;\nclass Handlers {\n static function OnExecAction(sParams: String[]): Boolean { return true; }\n}',encoding='utf-8')
    active=tmp_path/'CustomRules.js';active.write_text('USER RULES',encoding='utf-8')
    out=tmp_path/'out'
    result=prepare_capture(fiddler,out,'Mzk3NTE3NTU0Ng==')
    assert Path(result['candidate']).exists()
    assert not json.loads((out/'capture_control.json').read_text())['enabled']
    with pytest.raises(ValueError,match='unowned_active_rules'):
        prepare_capture(fiddler,out,'Mzk3NTE3NTU0Ng==',active_path=active)
    assert active.read_text()=='USER RULES'

def test_owned_activation_and_bounded_enable(tmp_path):
    fiddler=tmp_path/'Fiddler';(fiddler/'Scripts').mkdir(parents=True)
    (fiddler/'Scripts'/'SampleRules.js').write_text('import Fiddler;\nclass Handlers {\n static function OnExecAction(sParams: String[]): Boolean { return true; }\n}',encoding='utf-8')
    out=tmp_path/'out';active=tmp_path/'CustomRules.js'
    result=prepare_capture(fiddler,out,'Mzk3NTE3NTU0Ng==',active_path=active)
    assert result['activated'] and active.exists()
    state=set_capture_enabled(out,True,minutes=5)
    assert state['enabled'] and state['expires_at']
    with pytest.raises(ValueError):set_capture_enabled(out,True,minutes=9999)
    assert not set_capture_enabled(out,False)['enabled']

def test_renew_capture_window_only_when_close_to_expiry(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'capture_control.json').write_text(
        json.dumps({'enabled': True, 'target_biz': 'biz-a', 'expires_at': '2030-01-01T00:00:00+00:00'}),
        encoding='utf-8',
    )

    assert renew_capture_window(out, minutes=60, renew_before_minutes=45) is False

    control = json.loads((out / 'capture_control.json').read_text(encoding='utf-8'))
    control['expires_at'] = '2020-01-01T00:00:00+00:00'
    (out / 'capture_control.json').write_text(json.dumps(control), encoding='utf-8')

    assert renew_capture_window(out, minutes=60, renew_before_minutes=45) is True
    renewed = json.loads((out / 'capture_control.json').read_text(encoding='utf-8'))
    assert renewed['enabled'] is True
    assert renewed['target_biz'] == 'biz-a'
    assert renewed['expires_at'] != control['expires_at']


def test_diagnostic_command_is_explicit_and_observer_stays_disabled(tmp_path):
    fiddler=tmp_path/'Fiddler';(fiddler/'Scripts').mkdir(parents=True)
    (fiddler/'Scripts'/'SampleRules.js').write_text('import Fiddler;\nclass Handlers {\n static function OnExecAction(sParams: String[]): Boolean { return true; }\n}',encoding='utf-8')
    out=tmp_path/'out'
    result=prepare_capture(fiddler,out,'Mzk3NTE3NTU0Ng==')
    candidate=Path(result['candidate']).read_text(encoding='utf-8-sig')
    assert 'cssci-diagnose' in candidate
    assert 'cssci-reload' in candidate
    assert 'class CssciDiagnostics' in candidate
    assert 'CssciDiagnostics.Record(oSession)' in candidate
    assert 'capture_diagnostics.jsonl' in candidate
    assert '__CSSCI_DIAGNOSTICS_PATH__' not in candidate
    assert '__CSSCI_DIAGNOSTICS_LIVE_PATH__' not in candidate
    control=json.loads((out/'capture_control.json').read_text())
    assert control['diagnostic_enabled'] is False
    assert control['diagnostic_expires_at'] is None
    assert not control['enabled']


def test_diagnostic_enable_is_bounded_and_preserves_primary_capture_state(tmp_path):
    control = tmp_path / "capture_control.json"
    control.write_text(
        json.dumps({"enabled": True, "target_biz": "biz-a", "expires_at": "primary"}),
        encoding="utf-8",
    )

    enabled = set_diagnostic_enabled(tmp_path, True, minutes=5, after_session_id=7)

    assert enabled["enabled"] is True
    assert enabled["target_biz"] == "biz-a"
    assert enabled["expires_at"] == "primary"
    assert enabled["diagnostic_enabled"] is True
    assert enabled["diagnostic_after_id"] == 7
    assert enabled["diagnostic_all_articles"] is True
    assert enabled["diagnostic_expires_at"]

    disabled = set_diagnostic_enabled(tmp_path, False)
    assert disabled["enabled"] is True
    assert disabled["target_biz"] == "biz-a"
    assert disabled["diagnostic_enabled"] is False
    assert disabled["diagnostic_expires_at"] is None
    assert disabled["diagnostic_after_id"] is None
    assert disabled["diagnostic_all_articles"] is False

    with pytest.raises(ValueError, match="diagnostic_window_must_be_1_to_60_minutes"):
        set_diagnostic_enabled(tmp_path, True, minutes=61)
    with pytest.raises(ValueError, match="invalid_diagnostic_after_session_id"):
        set_diagnostic_enabled(tmp_path, True, after_session_id=-1)


def test_set_capture_target_switches_public_biz_without_enabling_capture(tmp_path):
    import json
    from capture.setup import set_capture_target

    control = tmp_path / "capture_control.json"
    control.write_text(json.dumps({"enabled": True, "target_biz": "old-biz"}), encoding="utf-8")

    result = set_capture_target(tmp_path, "new-biz")

    assert result["target_biz"] == "new-biz"
    assert result["enabled"] is False
    assert json.loads(control.read_text(encoding="utf-8"))["target_biz"] == "new-biz"
