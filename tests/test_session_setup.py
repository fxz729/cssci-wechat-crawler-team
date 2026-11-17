from pathlib import Path
import pytest
from capture.session_setup import prepare_installation,activate_installation


def test_prepare_does_not_modify_active_and_hooks_existing_ondone(tmp_path):
    active=tmp_path/'CustomRules.js'
    original='import Fiddler;\nclass Handlers { static function OnDone(s: Session) { Existing.Record(s); } }'
    active.write_text(original)
    report=prepare_installation(active,tmp_path/'private',tmp_path/'out')
    assert active.read_text()==original
    candidate=Path(report['candidate']).read_text(encoding='utf-8-sig')
    assert 'CssciSessionCapture.Record(s);' in candidate and 'Existing.Record(s);' in candidate
    assert not (tmp_path/'private').exists()


def test_activation_requires_unchanged_active_and_explicit_confirmation(tmp_path):
    active=tmp_path/'CustomRules.js';active.write_text('class Handlers {}')
    report=prepare_installation(active,tmp_path/'private',tmp_path/'out')
    with pytest.raises(ValueError,match='confirmation_required'):
        activate_installation(report['manifest'])
    active.write_text('USER EDIT')
    with pytest.raises(ValueError,match='active_rules_changed'):
        activate_installation(report['manifest'],confirmed=True)
    assert active.read_text()=='USER EDIT'


def test_activation_backs_up_exact_original(tmp_path):
    active=tmp_path/'CustomRules.js';active.write_text('class Handlers {}')
    before=active.read_bytes();report=prepare_installation(active,tmp_path/'private',tmp_path/'out')
    result=activate_installation(report['manifest'],confirmed=True)
    assert Path(result['backup']).read_bytes()==before
    assert b'CssciSessionCapture.Record' in active.read_bytes()

def test_hook_ignores_commented_and_quoted_ondone_examples(tmp_path):
    active=tmp_path/'CustomRules.js'
    active.write_text('class Handlers { static function OnDone(s: Session) { Existing.Record(s); } /* static function OnDone(s: Session) {} */ var example="static function OnDone(s: Session) {}"; }')
    result=prepare_installation(active,tmp_path/'private',tmp_path/'out')
    candidate=Path(result['candidate']).read_text(encoding='utf-8-sig')
    assert candidate.count('CssciSessionCapture.Record(s);')==1


@pytest.mark.parametrize('handler', [
    '',
    'static function OnBeforeRequest(oSession: Session) { Existing.Before(oSession); }',
    'static function OnBeforeRequest(req: Session): void { Existing.Before(req); }',
])
def test_health_hook_initial_install_preserves_handlers(tmp_path, handler):
    active=tmp_path/'CustomRules.js'
    original='import Fiddler;\nclass Handlers { '+handler+' static function OnDone(s: Session) { Existing.Done(s); } }'
    active.write_text(original)
    report=prepare_installation(active,tmp_path/'unused-private',tmp_path/'out')
    candidate=Path(report['candidate']).read_text(encoding='utf-8-sig')
    arg='req' if 'req: Session' in handler else 'oSession'
    assert f'if(CssciSessionCapture.HandleHealth({arg}))return;' in candidate
    assert candidate.count('static function OnBeforeRequest(')==1
    assert 'Existing.Done(s);' in candidate
    if handler:
        assert candidate.index(f'HandleHealth({arg})') < candidate.index(f'Existing.Before({arg})')
    assert active.read_text()==original
    assert not (tmp_path/'unused-private').exists()


def test_health_hook_ignores_comments_and_strings(tmp_path):
    active=tmp_path/'CustomRules.js'
    original='''import Fiddler;
// CssciSessionCapture is an example, not an installed hook.
class Handlers {
/* static function OnBeforeRequest(fake: Session) {} */
var example="static function OnBeforeRequest(quoted: Session) {}";
static function OnBeforeRequest(real: Session) { Existing.Before(real); }
}'''
    active.write_text(original)
    result=prepare_installation(active,tmp_path/'unused-private',tmp_path/'out')
    candidate=Path(result['candidate']).read_text(encoding='utf-8-sig')
    assert 'if(CssciSessionCapture.HandleHealth(real))return;' in candidate
    assert 'HandleHealth(fake)' not in candidate and 'HandleHealth(quoted)' not in candidate
    assert 'Existing.Before(real);' in candidate
    assert active.read_text()==original


def test_health_hook_rejects_duplicate_real_handlers_without_writes(tmp_path):
    active=tmp_path/'CustomRules.js'
    active.write_text('class Handlers { static function OnBeforeRequest(a: Session) {} static function OnBeforeRequest(b: Session) {} }')
    with pytest.raises(ValueError,match='^ambiguous_onbeforerequest_handler$'):
        prepare_installation(active,tmp_path/'unused-private',tmp_path/'out')
    assert not (tmp_path/'out').exists()
