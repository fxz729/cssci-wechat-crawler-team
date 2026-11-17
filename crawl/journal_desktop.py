"""Bounded public-home navigation; never closes the user's search window."""
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import parse_qs,urlsplit


UI_ERROR_CODES=frozenset({
    'unapproved_home_url','foreground_changed','search_tab_not_selectable',
    'search_window_missing_or_ambiguous','search_input_not_editable',
    'search_value_not_applied','search_button_missing_or_ambiguous',
    'search_button_not_invokable','ui_navigation_failed',
    'profile_link_missing_or_ambiguous','profile_link_not_invokable',
    'embedded_search_not_visible','search_context_changed',
})


def open_profile(url,biz,*,sleep=time.sleep,should_stop=lambda:False):
    try:
        parsed=urlsplit(url);query=parse_qs(parsed.query)
        if (parsed.scheme!='https' or parsed.netloc!='mp.weixin.qq.com' or parsed.path!='/mp/profile_ext'
                or query.get('action')!=['home'] or query.get('__biz')!=[biz]
                or set(query)-{'action','__biz','scene'} or any(len(v)!=1 for v in query.values())):
            raise ValueError()
    except (ValueError,TypeError):
        raise ValueError('unapproved_home_navigation') from None
    script=Path(__file__).with_name('open_journal_home.ps1')
    error_code='ui_unavailable'
    for attempt in range(2):
        if should_stop():raise RuntimeError('paused_by_operator')
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(script),
                                   '-ProfileUrl',url],capture_output=True,timeout=25)
            # A failed process with no JSON keeps the existing unavailable contract.
            empty_failure=result.returncode!=0 and not result.stdout
            payload=None if empty_failure else json.loads(result.stdout.decode('utf-8-sig'))
            error_code='ui_unavailable' if empty_failure else 'ui_invalid_response'
            if isinstance(payload,dict):
                if result.returncode==0 and payload.get('success') is True:return payload
                if payload.get('success') is False:
                    code=payload.get('error_code')
                    error_code=code if isinstance(code,str) and code in UI_ERROR_CODES else 'ui_navigation_failed'
        except subprocess.TimeoutExpired:
            error_code='ui_navigation_timeout'
        except OSError:
            error_code='ui_unavailable'
        except ValueError:
            error_code='ui_invalid_response'
        if attempt==0:sleep(1)
    raise RuntimeError(error_code) from None
