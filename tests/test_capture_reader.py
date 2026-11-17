import json
from pathlib import Path

import pytest

from capture.reader import CaptureValidationError, load_events, normalize_event, public_url

BIZ = 'Mzk3NTE3NTU0Ng=='
URL = f'https://mp.weixin.qq.com/s?__biz={BIZ}&mid=123&idx=1&sn=abc'


def event(kind='metrics'):
    return {'schema_version': 1, 'event_id': 'test-1', 'captured_at': '2026-09-06T10:00:00Z',
            'source': 'fiddler_classic', 'endpoint': '/mp/getappmsgext', 'kind': kind,
            'biz': BIZ, 'mid': '123', 'idx': 1, 'sn': 'abc',
            'stats': {'read_num': 0, 'old_like_num': 2}, 'comments': {}}


def test_zero_is_observed_and_missing_fields_are_not_zero():
    parsed = normalize_event(event(), {BIZ})
    assert parsed['metrics']['read_count'] == 0
    assert parsed['metrics']['favorite_count'] is None
    assert parsed['field_meta']['read_count']['status'] == 'observed'
    assert parsed['field_meta']['favorite_count']['status'] == 'not_returned'


def test_off_target_or_unidentified_metrics_are_rejected():
    for change in [{'biz': 'other'}, {'mid': ''}, {'idx': 0}]:
        raw = event(); raw.update(change)
        with pytest.raises(CaptureValidationError):
            normalize_event(raw, {BIZ})


def test_query_and_unknown_headers_are_never_retained():
    raw = event(); raw['Cookie'] = 'secret-cookie'
    raw['stats']['appmsg_token'] = 'secret-token'
    raw['endpoint'] += '?token=secret-url'
    parsed = normalize_event(raw, {BIZ})
    assert 'secret' not in json.dumps(parsed)
    assert public_url(URL + '&key=secret&pass_ticket=secret') == URL.replace('==','%3D%3D')


def test_article_is_sanitized_and_identity_checked():
    raw = event('article'); raw['endpoint'] = '/s'
    raw['article'] = {'url': URL, 'title': 'Sample', 'author': 'Writer',
       'content_html': '<div id="js_content"><p onclick="bad()">Body</p><script>token="secret"</script><a href="'+URL+'&key=secret">ref</a></div>',
       'Cookie': 'secret'}
    parsed = normalize_event(raw, {BIZ})
    assert 'secret' not in json.dumps(parsed)
    assert 'onclick' not in parsed['article']['content_html']
    raw['article']['url'] = URL.replace('mid=123','mid=999')
    with pytest.raises(CaptureValidationError): normalize_event(raw, {BIZ})


def test_comments_only_event_preserves_other_missing_metrics():
    raw = event(); raw['stats'] = {}; raw['comments'] = {'total_count': 0}; raw['endpoint'] = '/mp/appmsg_comment'
    parsed = normalize_event(raw, {BIZ})
    assert parsed['metrics']['comment_count'] == 0
    assert parsed['metrics']['read_count'] is None
    assert parsed['field_meta']['comment_count']['raw_field'] == 'total_count'


def test_loader_deduplicates_and_reports_malformed_lines_without_echoing_them(tmp_path):
    p = tmp_path/'inbox.jsonl'
    line=json.dumps(event()); p.write_text(line+'\n'+line+'\ninvalid-secret\n', encoding='utf-8')
    events, issues=load_events(p, {BIZ})
    assert len(events)==1 and len(issues)==1
    assert 'invalid-secret' not in json.dumps(issues)


def test_abbreviated_counts_are_not_labeled_exact():
    raw=event(); raw['stats']['read_num']='10万+'
    parsed=normalize_event(raw,{BIZ})
    assert parsed['metrics']['read_count']==100000
    assert parsed['field_meta']['read_count']['precision']=='lower_bound'


def test_script_is_passive_and_has_no_credential_exports():
    script=Path('capture/fiddler_classic.js').read_text(encoding='utf-8')
    assert 'GetResponseBodyAsString' in script
    assert 'utilSetResponseBody' not in script
    assert 'actAttachProxy' not in script and 'actDetachProxy' not in script
    assert 'SaveSession' not in script and 'SaveResponseBody' not in script

def test_wire_roundtrip_preserves_observed_metrics_without_unknown_payload():
    from capture.reader import to_wire_event
    raw=event();raw['Cookie']='secret'; raw['stats']['appmsg_token']='secret'
    normalized=normalize_event(raw,{BIZ})
    assert normalize_event(to_wire_event(normalized),{BIZ})==normalized
    assert 'secret' not in json.dumps(to_wire_event(normalized))

def test_html_event_strips_srcdoc_and_unquoted_handlers():
    raw=event('article');raw['endpoint']='/s';raw['article']={'url':URL,'content_html':'<div id="js_content"><iframe srcdoc="&lt;script&gt;bad()&lt;/script&gt;"></iframe><img onerror=bad() src="https://mmbiz.qpic.cn/a.png"></div>'}
    result=normalize_event(raw,{BIZ})['article']['content_html']
    assert 'srcdoc' not in result and 'onerror' not in result

def test_html_event_drops_sensitive_data_attributes_and_srcset():
    raw=event('article');raw['endpoint']='/s';raw['article']={'url':URL,'content_html':'<div id="js_content" data-key="secret-key" data-uin="secret-user"><img srcset="https://mmbiz.qpic.cn/a?pass_ticket=secret-ticket 2x" src="https://mmbiz.qpic.cn/a"></div>'}
    result=normalize_event(raw,{BIZ})['article']['content_html']
    assert 'secret' not in result and 'srcset' not in result
