"""Review-first, hash-guarded installation of the session capture hook.

Does not configure proxy/certificates, create private directories, or reload Fiddler.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

from crawl.journal_session_capture import prepare_candidate
from export.session_article_exporter import _atomic_stream


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe(path):
    path=Path(path).absolute()
    if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents)):
        raise ValueError('unsafe_setup_path')
    return path.resolve()


def _code_mask(source):
    # Preserve offsets while hiding strings/comments in the trusted local script.
    token=re.compile(r"/\*[\s\S]*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
    return token.sub(lambda m: ''.join('\n' if c=='\n' else ' ' for c in m.group()),source)


def prepare_installation(active_path,private_root,output_dir):
    active=_safe(active_path);out=_safe(output_dir)
    original=active.read_text(encoding='utf-8-sig')
    if 'CssciSessionCapture' in _code_mask(original):
        raise ValueError('session_hook_already_present')
    helper=prepare_candidate(Path(private_root).absolute())
    imports=re.findall(r'^import\s+[^;\r\n]+;',helper,re.M)
    helper=re.sub(r'^import\s+[^;\r\n]+;\s*','',helper,flags=re.M)
    masked=_code_mask(original)
    matches=list(re.finditer(r'\bstatic\s+function\s+OnDone\s*\(\s*(\w+)\s*:\s*Session\s*\)\s*(?::\s*void\s*)?\{',masked))
    if len(matches)>1:raise ValueError('ambiguous_ondone_handler')
    if matches:
        match=matches[0]
        candidate=original[:match.end()]+'\n CssciSessionCapture.Record('+match[1]+');\n'+original[match.end():]
    else:
        matches=list(re.finditer(r'\bclass\s+Handlers\s*\{',masked))
        if len(matches)!=1:raise ValueError('unsupported_handlers')
        match=matches[0]
        candidate=original[:match.end()]+'\n static function OnDone(s: Session) { CssciSessionCapture.Record(s); }\n'+original[match.end():]
    masked=_code_mask(candidate)
    matches=list(re.finditer(r'\bstatic\s+function\s+OnBeforeRequest\s*\(\s*(\w+)\s*:\s*Session\s*\)\s*(?::\s*void\s*)?\{',masked))
    if len(matches)>1:raise ValueError('ambiguous_onbeforerequest_handler')
    if matches:
        match=matches[0]
        candidate=candidate[:match.end()]+'\n if(CssciSessionCapture.HandleHealth('+match[1]+'))return;\n'+candidate[match.end():]
    else:
        matches=list(re.finditer(r'\bclass\s+Handlers\s*\{',masked))
        if len(matches)!=1:raise ValueError('unsupported_handlers')
        match=matches[0]
        candidate=candidate[:match.end()]+'\n static function OnBeforeRequest(oSession: Session) { if(CssciSessionCapture.HandleHealth(oSession))return; }\n'+candidate[match.end():]
    candidate='\n'.join(i for i in imports if i not in candidate)+'\n'+candidate+'\n'+helper
    out.mkdir(parents=True,exist_ok=True)
    path=out/'CustomRules.session-candidate.js'
    path.write_text(candidate,encoding='utf-8-sig')
    manifest=out/'session_installation.json'
    report={'active_path':str(active),'original_sha256':_hash(active),'candidate':str(path),
            'candidate_sha256':_hash(path),'private_root':str(Path(private_root).absolute()),
            'prepared_at':datetime.now(timezone.utc).isoformat(),'installed':False}
    manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return {**report,'manifest':str(manifest)}


def activate_installation(manifest_path,*,confirmed=False):
    if confirmed is not True:raise ValueError('confirmation_required')
    manifest=_safe(manifest_path)
    report=json.loads(manifest.read_text(encoding='utf-8'))
    active=_safe(report['active_path']);candidate=_safe(report['candidate'])
    if _hash(active)!=report['original_sha256']:raise ValueError('active_rules_changed')
    if _hash(candidate)!=report['candidate_sha256']:raise ValueError('candidate_changed')
    backup=manifest.parent/('CustomRules.before-session-'+report['original_sha256'][:16]+'.js')
    if backup.exists():
        if _hash(backup)!=report['original_sha256']:raise ValueError('backup_conflict')
    else:
        with backup.open('xb') as stream:stream.write(active.read_bytes())
    with _atomic_stream(active,'utf-8-sig') as stream:
        stream.write(candidate.read_text(encoding='utf-8-sig'))
    report.update(installed=True,backup=str(backup),installed_sha256=_hash(active))
    with _atomic_stream(manifest) as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    return report
