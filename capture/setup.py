"""Prepare a reversible Classic observer; never changes proxy/certificate settings."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re


def _sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_capture(fiddler_dir, output_dir, target_biz, *, active_path=None):
    if not re.fullmatch(r'[A-Za-z0-9_=-]{1,200}',target_biz):raise ValueError('invalid_target_biz')
    out=Path(output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    metadata_path=out/'capture_activation.json'
    metadata=json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {}
    active=Path(active_path).resolve() if active_path else None
    if active and active.exists() and (str(active)!=metadata.get('active_path') or _sha(active)!=metadata.get('installed_sha256')):
        raise ValueError('unowned_active_rules_not_overwritten')
    sample=(Path(fiddler_dir)/'Scripts'/'SampleRules.js').read_text(encoding='utf-8-sig')
    helper=Path(__file__).with_name('fiddler_classic.js').read_text(encoding='utf-8-sig')
    helper+='\n'+Path(__file__).with_name('fiddler_diagnostics.js').read_text(encoding='utf-8-sig')
    control=out/'capture_control.json';inbox=out/'capture_inbox.jsonl'
    for marker,path in [
        ('__CSSCI_CONTROL_PATH__',control),
        ('__CSSCI_INBOX_PATH__',inbox),
        ('__CSSCI_DIAGNOSTICS_PATH__',out/'capture_diagnostics.json'),
        ('__CSSCI_DIAGNOSTICS_LIVE_PATH__',out/'capture_diagnostics.jsonl'),
    ]:
        helper=helper.replace(marker,json.dumps(str(path),ensure_ascii=True)[1:-1])
    helper=re.sub(r'^import .*?;\s*$','',helper,flags=re.M)
    sample='import System.IO;\nimport System.Collections;\nimport System.Text.RegularExpressions;\n'+sample
    sample,n=re.subn(
        r'(class Handlers\s*\{)',
        r'\1\n    static function OnDone(oSession: Session) { CssciCapture.Record(oSession); CssciDiagnostics.Record(oSession); }',
        sample,
        count=1,
    )
    if n!=1:raise ValueError('unsupported_sample_rules')
    sample,n=re.subn(r'(static function OnExecAction\(sParams: String\[\]\): Boolean\s*\{)',r'''\1
        if (sParams.Length > 0 && sParams[0] == "cssci-diagnose") { CssciDiagnostics.Export(); return true; }
        if (sParams.Length > 0 && sParams[0] == "cssci-status") { CssciCapture.Status(); return true; }
        if (sParams.Length > 0 && sParams[0] == "cssci-export") { CssciCapture.ExportExisting(); return true; }
        if (sParams.Length > 0 && sParams[0] == "cssci-reload") { FiddlerObject.ReloadScript(); return true; }
''',sample,count=1)
    if n!=1:raise ValueError('unsupported_execaction_signature')
    candidate=out/'CustomRules.cssci-candidate.js';candidate.write_text(sample+'\n'+helper,encoding='utf-8-sig')
    control.write_text(json.dumps({
        'enabled': False,
        'target_biz': target_biz,
        'expires_at': datetime.now(timezone.utc).isoformat(),
        'diagnostic_enabled': False,
        'diagnostic_expires_at': None,
        'diagnostic_after_id': None,
        'diagnostic_all_articles': False,
        'diagnostic_mid': '',
        'diagnostic_idx': '',
    }, indent=2), encoding='utf-8')
    if not inbox.exists():inbox.write_text('',encoding='utf-8')
    if active:
        active.parent.mkdir(parents=True,exist_ok=True)
        active.write_bytes(candidate.read_bytes())
        metadata={'active_path':str(active),'installed_sha256':_sha(active),'candidate':str(candidate)}
        metadata_path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'candidate':str(candidate),'control':str(control),'inbox':str(inbox),'activated':active is not None}


def set_capture_target(output_dir, target_biz):
    if not re.fullmatch(r'[A-Za-z0-9_=-]{1,200}', target_biz):
        raise ValueError('invalid_target_biz')
    p = Path(output_dir).resolve() / 'capture_control.json'
    control = json.loads(p.read_text(encoding='utf-8'))
    control['target_biz'] = target_biz
    control['enabled'] = False
    control['expires_at'] = datetime.now(timezone.utc).isoformat()
    temporary = p.with_suffix(p.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(control, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
    temporary.replace(p)
    return control


def set_capture_enabled(output_dir, enabled, *, minutes=15):
    if not 1<=minutes<=60:raise ValueError('capture_window_must_be_1_to_60_minutes')
    p=Path(output_dir)/'capture_control.json';control=json.loads(p.read_text(encoding='utf-8'))
    control['enabled']=bool(enabled)
    control['expires_at']=(datetime.now(timezone.utc)+timedelta(minutes=minutes if enabled else 0)).isoformat()
    p.write_text(json.dumps(control,ensure_ascii=False,indent=2),encoding='utf-8')
    return control


def renew_capture_window(output_dir, *, minutes=60, renew_before_minutes=45):
    if not 1 <= minutes <= 60:
        raise ValueError('capture_window_must_be_1_to_60_minutes')
    if not 0 <= renew_before_minutes < minutes:
        raise ValueError('invalid_capture_renew_threshold')
    p = Path(output_dir).resolve() / 'capture_control.json'
    control = json.loads(p.read_text(encoding='utf-8'))
    if control.get('enabled') is not True:
        return False
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(str(control.get('expires_at')).replace('Z', '+00:00'))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expires = now
    if expires > now + timedelta(minutes=renew_before_minutes):
        return False
    control['expires_at'] = (now + timedelta(minutes=minutes)).isoformat()
    temporary = p.with_suffix(p.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(control, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
    temporary.replace(p)
    return True


def set_diagnostic_enabled(output_dir, enabled, *, minutes=15, after_session_id=0):
    if not 1 <= minutes <= 60:
        raise ValueError('diagnostic_window_must_be_1_to_60_minutes')
    if type(after_session_id) is not int or after_session_id < 0:
        raise ValueError('invalid_diagnostic_after_session_id')
    p = Path(output_dir).resolve() / 'capture_control.json'
    control = json.loads(p.read_text(encoding='utf-8'))
    control['diagnostic_enabled'] = bool(enabled)
    control['diagnostic_expires_at'] = (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat() if enabled else None
    control['diagnostic_after_id'] = after_session_id if enabled else None
    control['diagnostic_all_articles'] = bool(enabled)
    control['diagnostic_mid'] = ''
    control['diagnostic_idx'] = ''
    temporary = p.with_suffix(p.suffix + '.tmp')
    temporary.write_text(json.dumps(control, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(p)
    return control


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--fiddler-dir')
    parser.add_argument('--target-biz')
    parser.add_argument('--activate-path',help='Explicitly install; unknown existing rules are never overwritten')
    parser.add_argument('--enable',action='store_true')
    parser.add_argument('--disable',action='store_true')
    parser.add_argument('--minutes',type=int,default=15)
    a=parser.parse_args()
    if a.enable and a.disable:parser.error('choose enable or disable')
    if a.enable or a.disable:
        set_capture_enabled(a.output_dir,a.enable,minutes=a.minutes)
        print('capture_enabled='+str(a.enable))
    else:
        if not a.fiddler_dir or not a.target_biz:parser.error('prepare requires fiddler-dir and target-biz')
        print(json.dumps(prepare_capture(a.fiddler_dir,a.output_dir,a.target_biz,active_path=a.activate_path),ensure_ascii=False))


if __name__=='__main__':main()
