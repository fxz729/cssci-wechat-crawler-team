"""Public article projection; authenticated page data must never enter exports."""
from copy import deepcopy
from html import unescape
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

PRIVATE = re.compile(r'^(?:cookie|authorization|token|appmsg_token|pass_ticket|key|uin|wxuin|wap_sid2|slave_sid|data_ticket|fingerprint|wxtoken|exportkey|poc_token|ckey|openid|user_uin|auth_key|wxtokenkey|signature|sig|auth_token|access_token)$|_token$', re.I)
FIELDS = {'article_type','biz','mid','idx','sn','url','title','author','account_name','digest',
          'content_html','cover_url','publish_timestamp','comment_id','metrics','metric_fields',
          'identity_matches','business_ret','six_metrics_present','body_sha256','captured_at',
          'journal_id','journal_name','media_assets','metrics_provider'}
METRICS = {'read_count','like_count','watching_count','share_count','favorite_count','comment_count'}
RAW = {'read_num','old_like_count','like_count','share_count','collect_count','comment_count'}


def public_url(value):
    if not isinstance(value,str):
        return ''
    try:
        value=unescape(value.strip())
        if value.startswith('//'):
            value='https:'+value
        parts=urlsplit(value)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
            return ''
        kept=[]
        for part in parts.query.split('&'):
            if part and not PRIVATE.search(unquote(part.split('=',1)[0])):
                kept.append(part)
        return urlunsplit((parts.scheme,parts.netloc,parts.path,'&'.join(kept),''))
    except ValueError:
        return ''


def public_html(value):
    if not isinstance(value,str) or not value:
        return ''
    soup=BeautifulSoup(value,'html.parser')
    for tag in soup.find_all(['script','base','meta','link','form','input','button','object','embed','style']):
        tag.decompose()
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            lower=name.lower()
            if lower.startswith('on') or lower in ('srcdoc','nonce','integrity') or PRIVATE.search(lower.removeprefix('data-').replace('-','_')):
                del tag.attrs[name]
            elif lower in ('src','href','data-src','poster','data-cover','data-url','data-mpvid-url','data-display-src') or (isinstance(tag.attrs[name],str) and tag.attrs[name].strip().startswith(('http://','https://','//'))):
                tag.attrs[name]=public_url(tag.attrs[name])
            elif lower in ('srcset','data-srcset'):
                # Source sets are not needed to preserve the primary body image.
                del tag.attrs[name]
            elif lower=='style':
                tag.attrs[name]=re.sub(r'url\([^)]*\)', '', str(tag.attrs[name]), flags=re.I)
    return str(soup)


def public_snapshot(article):
    if not isinstance(article,dict):
        raise ValueError('invalid_public_snapshot')
    result={k:deepcopy(v) for k,v in article.items() if k in FIELDS}
    if result.get('metrics_provider') not in ('background_client_session_html','fiddler_classic:/s'):
        result.pop('metrics_provider',None)
    for key in ('url','cover_url'):
        if key in result:
            result[key]=public_url(result[key])
    if 'content_html' in result:
        result['content_html']=public_html(result['content_html'])
    if 'metrics' in result:
        result['metrics']={k:v for k,v in (result['metrics'] or {}).items() if k in METRICS and (v is None or type(v) is int)}
    if 'metric_fields' in result:
        result['metric_fields']={k:{'present':v.get('present') is True,'raw_value':v.get('raw_value') if type(v.get('raw_value')) is int or (isinstance(v.get('raw_value'),str) and re.fullmatch(r'\d{1,15}',v['raw_value'])) else None}
                                 for k,v in (result['metric_fields'] or {}).items()
                                 if k.startswith('$html.cgiDataNew.user_info.appmsg_bar_data.') and k.rsplit('.',1)[-1] in RAW and isinstance(v,dict)}
    if 'media_assets' in result:
        assets=[]
        for raw in result['media_assets'] or []:
            if not isinstance(raw,dict) or raw.get('kind') not in ('image','audio','video'):
                continue
            item={k:deepcopy(raw[k]) for k in ('kind','id','role','title','description','resource_index','asset_index') if k in raw and isinstance(raw[k],(str,int))}
            if raw.get('url'):
                item['url']=public_url(raw['url'])
            assets.append(item)
        result['media_assets']=assets
    return result
