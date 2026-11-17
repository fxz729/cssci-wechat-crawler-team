"""Read data-only capture events. Credentials are never part of this contract."""
from datetime import datetime
import json
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

METRIC_FIELDS = ('read_count', 'like_count', 'watching_count', 'share_count', 'favorite_count', 'comment_count')
_STAT_KEYS = {'read_count': ('read_num',), 'like_count': ('old_like_num',),
              'watching_count': ('like_num',), 'share_count': ('share_num',),
              'favorite_count': ('collect_num', 'star_num')}
_SECRET_KEYS = {'cookie', 'authorization', 'token', 'appmsg_token', 'pass_ticket', 'key', 'uin', 'wxuin',
                'wap_sid2', 'slave_sid', 'data_ticket', 'fingerprint', 'wxtoken', 'exportkey', 'poc_token'}
_ID = re.compile(r'[A-Za-z0-9_=-]{1,200}\Z')
MAX_EVENT_BYTES = 8 * 1024 * 1024


class CaptureValidationError(ValueError):
    pass


def public_url(value):
    """Preserve public resource parameters, but never session parameters."""
    if not isinstance(value, str): return ''
    try:
        u = urlsplit(value)
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password: return ''
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if k.lower() not in _SECRET_KEYS and not k.lower().endswith('_token')]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ''))
    except ValueError:
        return ''


def clean_content_html(value):
    soup = BeautifulSoup(str(value or ''), 'lxml')
    for tag in soup.select('script, form, input, button, object, embed, base, meta, link'):
        tag.decompose()
    for tag in soup.find_all(True):
        for key, val in list(tag.attrs.items()):
            name = key.lower()
            if name in ('srcdoc', 'srcset') or name.removeprefix('data-') in _SECRET_KEYS or name.startswith('on') or any(secret in name for secret in ('token', 'ticket', 'cookie', 'authorization')):
                del tag.attrs[key]
            elif name in ('src', 'href', 'data-src', 'poster'):
                cleaned = public_url(val)
                if cleaned: tag.attrs[key] = cleaned
                else: del tag.attrs[key]
        if tag.name == 'iframe':
            tag.attrs['sandbox'] = ''
    content = soup.select_one('#js_content') or soup.body
    return str(content) if content else ''


def _count(value):
    if isinstance(value, bool): return None, 'invalid'
    if isinstance(value, int) and value >= 0: return value, 'exact'
    text = str(value).strip()
    if re.fullmatch(r'\d+', text): return int(text), 'exact'
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(万|亿)?(\+)?', text)
    if not m: return None, 'invalid'
    scale = {'万': 10000, '亿': 100000000, None: 1}[m[2]]
    return int(float(m[1]) * scale), 'lower_bound' if m[3] else 'rounded'


def _identity(raw, allowed):
    biz, mid = str(raw.get('biz') or ''), str(raw.get('mid') or '')
    idx = raw.get('idx')
    if biz not in allowed or not _ID.fullmatch(biz) or not mid.isdigit():
        raise CaptureValidationError('unapproved_or_unresolved_identity')
    if isinstance(idx, bool) or not str(idx).isdigit() or not 1 <= int(idx) <= 100:
        raise CaptureValidationError('invalid_article_index')
    return {'biz': biz, 'mid': mid, 'idx': int(idx), 'sn': str(raw.get('sn') or '')[:200]}


def normalize_event(raw, allowed_biz):
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise CaptureValidationError('unsupported_event')
    kind = raw.get('kind')
    if kind not in ('article', 'metrics', 'catalog', 'capability'):
        raise CaptureValidationError('unsupported_kind')
    stamp = str(raw.get('captured_at') or '')
    try:
        dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if dt.tzinfo is None: raise ValueError()
    except ValueError:
        raise CaptureValidationError('invalid_observation_time') from None
    endpoint = str(raw.get('endpoint') or '').split('?', 1)[0]
    if not endpoint.startswith('/') or len(endpoint) > 300:
        raise CaptureValidationError('invalid_endpoint')
    source = raw.get('source', 'fiddler_classic')
    if source not in ('fiddler_classic', 'wechat_uia', 'public_http', 'browser_dom'):
        raise CaptureValidationError('unknown_source')
    out = {'schema_version': 1, 'event_id': str(raw.get('event_id') or '')[:200],
           'captured_at': stamp, 'source': source, 'endpoint': endpoint, 'kind': kind}
    if kind in ('catalog', 'capability'):
        biz = str(raw.get('biz') or '')
        if biz not in allowed_biz: raise CaptureValidationError('off_target_catalog')
        out.update(biz=biz, ret=raw.get('ret') if isinstance(raw.get('ret'), int) else None,
                   has_more=raw.get('has_more') if isinstance(raw.get('has_more'), bool) else None,
                   next_cursor=str(raw.get('next_cursor') or '')[:200], articles=[])
        if kind == 'capability' and raw.get('mid'):
            out.update(_identity(raw, allowed_biz))
        for item in raw.get('articles') or []:
            identity = _identity(item, {biz})
            url = public_url(item.get('url'))
            if not _article_url(url): continue
            out['articles'].append({**identity, 'url': url, 'title': str(item.get('title') or '')[:1000],
                                    'publish_time': str(item.get('publish_time') or '')[:80]})
        return out
    out.update(_identity(raw, allowed_biz))
    if kind == 'article':
        a = raw.get('article') or {}
        url = public_url(a.get('url'))
        if not _article_url(url): raise CaptureValidationError('invalid_article_url')
        q = parse_qs(urlsplit(url).query)
        for key, expected in [('__biz', out['biz']), ('mid', out['mid']), ('idx', str(out['idx']))]:
            if q.get(key) and q[key][0] != expected:
                raise CaptureValidationError('article_identity_mismatch')
        text_keys = ('title', 'author', 'account_name', 'digest', 'publish_time')
        out['article'] = {key: str(a.get(key) or '')[:5000] for key in text_keys}
        out['article'].update(url=url, cover_url=public_url(a.get('cover_url')),
                              content_html=clean_content_html(a.get('content_html')))
        if a.get('content_origin') in ('html_fragment','cgiDataNew.picture_page_info_list'):
            out['article']['content_origin']=a['content_origin']
        return out
    if raw.get('metric_format') == 'client_html_bootstrap_v1':
        from capture.bootstrap import normalize_bootstrap
        return normalize_bootstrap(raw, out)
    stats, comments = raw.get('stats') or {}, raw.get('comments') or {}
    out.update(metrics={}, field_meta={})
    for field in METRIC_FIELDS:
        data = comments if field == 'comment_count' else stats
        keys = ('total_count', 'elected_comment_total_cnt', 'comment_count') if field == 'comment_count' else _STAT_KEYS[field]
        key = next((key for key in keys if key in data and data[key] is not None), None)
        value, precision = _count(data[key]) if key else (None, 'unknown')
        meta = {'status': 'observed' if value is not None else 'not_returned', 'precision': precision,
                'raw_field': key, 'captured_at': stamp, 'mapping': 'uia-labels-v1' if source == 'wechat_uia' else 'legacy-appmsgext-v1', 'source': source}
        if key:
            meta['raw_value'] = str(data[key])[:80]
            if source == 'wechat_uia':
                labels={'read_count':'阅读','like_count':'赞','watching_count':'在看','share_count':'分享','favorite_count':'收藏','comment_count':'留言'}
                meta['raw_label']=labels[field]
        if field == 'comment_count': meta['scope'] = 'endpoint_reported_count'
        out['metrics'][field], out['field_meta'][field] = value, meta
    return out


def _article_url(url):
    u = urlsplit(url)
    return u.scheme == 'https' and u.hostname == 'mp.weixin.qq.com' and (u.path == '/s' or u.path.startswith('/s/'))


def load_events(path, allowed_biz):
    events, issues, seen = [], [], set()
    with open(path, 'rb') as file:
        number = 0
        while True:
            line = file.readline(MAX_EVENT_BYTES + 1)
            if not line: break
            number += 1
            if len(line) > MAX_EVENT_BYTES:
                raise CaptureValidationError('event_too_large')
            if not line.strip(): continue
            try:
                raw = json.loads(line.decode('utf-8-sig'))
                event = normalize_event(raw, allowed_biz)
                key = event['event_id'] or json.dumps(event, sort_keys=True)
                if key in seen: continue
                seen.add(key); events.append(event)
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                issues.append({'line': number, 'reason': 'invalid_or_off_target_event'})
    return events, issues


def to_wire_event(normalized):
    """Serialize only the validated schema; never copy the original raw object."""
    out = dict(normalized)
    if out['kind'] == 'metrics' and out.get('metric_format') == 'client_html_bootstrap_v1':
        out.pop('metrics', None); out.pop('field_meta', None)
        return out
    if out['kind'] == 'metrics':
        out['stats'], out['comments'] = {}, {}
        for field in METRIC_FIELDS:
            meta=out['field_meta'][field]
            if out['metrics'][field] is None: continue
            key=meta['raw_field']
            target=out['comments'] if field=='comment_count' else out['stats']
            target[key]=meta.get('raw_value', out['metrics'][field])
        out.pop('metrics',None); out.pop('field_meta',None)
    return out

