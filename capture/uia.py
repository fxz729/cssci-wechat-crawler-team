"""Turn explicitly labelled client UI counts into attributable observations."""
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit, parse_qs

from capture.reader import normalize_event, public_url


def _title(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text)))


def build_uia_event(observation, article_event, *, confirmed_public_url):
    url=public_url(observation.get('url'))
    parsed=urlsplit(url); query=parse_qs(parsed.query)
    expected={'__biz':article_event['biz'],'biz':article_event['biz'],'mid':str(article_event['mid']),'appmsgid':str(article_event['mid']),'idx':str(article_event['idx']),'sn':article_event.get('sn','')}
    if parsed.scheme!='https' or parsed.hostname!='mp.weixin.qq.com' or parsed.port not in (None,443):
        raise ValueError('uia_unapproved_host')
    if any(any(v != expected[k] for v in values) for k,values in query.items() if k in expected):
        raise ValueError('uia_conflicting_identity')
    if not url or url!=public_url(confirmed_public_url):
        raise ValueError('copied_url_not_verified')
    if _title(observation.get('title',''))!=_title(article_event['article'].get('title','')):
        raise ValueError('uia_title_mismatch')
    endpoint=article_event.get('endpoint','')
    if endpoint.startswith('/s/') and urlsplit(url).path!=endpoint:
        raise ValueError('uia_article_mismatch')
    stats={};comments={}
    keys={'阅读':'read_num','赞':'old_like_num','在看':'like_num','分享':'share_num','收藏':'collect_num'}
    candidates=observation.get('metric_candidates')
    if not observation.get('identity_stable') or not isinstance(candidates,list):
        raise ValueError('unscoped_or_unstable_uia_observation')
    seen={}
    for candidate in candidates:
        label=candidate.get('label','')
        if candidate.get('scope') not in ('article_action_bar','article_read_count'): continue
        if candidate.get('control_type') not in ('Button','Group'):continue
        match=re.fullmatch(r'(阅读|赞|在看|分享|收藏|留言|评论)\s*(\d+(?:\.\d+)?[万亿]?\+?)',label.strip())
        if not match:continue
        word,value=match.groups()
        if word in seen and seen[word] != value:
            raise ValueError('ambiguous_uia_metric')
        seen[word]=value
        if word in keys:stats[keys[word]]=value
        else:comments['total_count']=value
    raw={'schema_version':1,'source':'wechat_uia','kind':'metrics','endpoint':'/client/uia/article',
         'event_id':'uia-'+hashlib.sha256(json.dumps(observation,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24],
         'captured_at':observation['captured_at'],'biz':article_event['biz'],'mid':article_event['mid'],
         'idx':article_event['idx'],'sn':article_event.get('sn',''),'stats':stats,'comments':comments,
         'uia_evidence':{'identity_stable':True,'scoped_candidates':candidates}}
    return {**raw,**normalize_event(raw,{article_event['biz']})}
