"""Journal-scoped background field collection, with stdin-only credentials.

This does not start or alter the desktop/126-journal production workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import requests

from crawl.profile_session_probe import (
    HomeRequest, ProbeError, _article_identity, _atomic_json, article_snapshot, read_home_request,
)
from crawl.request_governor import GovernedSession, TrafficStopped
from utils.runtime_lock import runtime_lock
from utils.public_content import public_snapshot


def _key(item):
    return f"{item['mid']}_{item['idx']}"


def merge_catalog(catalog, baseline_dir=None):
    biz = catalog['target_biz']
    items = {}
    for item in catalog['articles']:
        parts = _article_identity(item['url'])
        if parts.biz != biz:
            raise ProbeError('catalog_biz_mismatch')
        items[(parts.biz, parts.mid, parts.idx)] = {**item, 'source': 'current_profile_ext'}
    current = set(items)
    previous = set()
    if baseline_dir:
        for path in sorted((Path(baseline_dir)/'capture_cache/articles').glob('*/capture_evidence.json')):
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            event = raw.get('article_event') or {}
            article = event.get('article') or {}
            parts = _article_identity(article.get('url'))
            if parts.biz != biz or any(str(event.get(k)) != str(getattr(parts, k)) for k in ('biz','mid','idx','sn')):
                raise ProbeError('baseline_identity_mismatch')
            identity = (parts.biz, parts.mid, parts.idx)
            previous.add(identity)
            if identity not in items:
                items[identity] = {'biz':parts.biz,'mid':parts.mid,'idx':parts.idx,'sn':parts.sn,
                                  'url':parts.canonical_url,'title':article.get('title',''),
                                  'source':'prior_local_catalog'}
    return list(items.values()), {'api_count':len(current),'baseline_count':len(previous),
        'overlap_count':len(current & previous),'baseline_only_count':len(previous-current),
        'api_only_count':len(current-previous),'merged_count':len(items),'catalog_complete':False}



def install_session_cookies(session, template):
    headers = {}
    for name, value in template.headers.items():
        if name.lower() != "cookie":
            headers[name] = value
            continue
        for part in value.split(";"):
            if "=" not in part:
                continue
            key, item = part.strip().split("=", 1)
            session.cookies.set(key, item, domain="mp.weixin.qq.com", path="/", secure=True)
    return HomeRequest(template.biz, dict(template.params), headers)





def run_articles(jobs, template, output, session, exporter, *, sleep=time.sleep,
                 should_stop=lambda: False, on_article=None, on_progress=None, journal_meta=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    snapshots = root/'article_snapshots'
    snapshots.mkdir(exist_ok=True)
    for job in jobs:
        parts = _article_identity(job['url'])
        if parts.biz != template.biz or any(str(job.get(k)) != str(getattr(parts,k)) for k in ('biz','mid','idx','sn')):
            raise ProbeError('job_identity_mismatch')
    control = root/'run_control.json'
    if not control.exists():
        _atomic_json(control, {'stop_requested':False})
    state_path = root/'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'jobs':{}}
    if state.get('target_biz',template.biz) != template.biz:
        raise ProbeError('state_biz_mismatch')
    state.update(phase='collecting_fields',target_biz=template.biz,total=len(jobs),client_ui_actions=0,
                 production_resumed=False,credentials_exported=False,media_policy='metadata_only')
    state.setdefault('started_at', datetime.now(timezone.utc).isoformat())
    failures = 0

    def checkpoint():
        current = [state['jobs'].get(_key(job),{}) for job in jobs]
        state.update(complete_count=sum(j.get('status')=='complete' for j in current),
                     partial_count=sum(j.get('status')=='partial' for j in current),
                     failed_count=sum(j.get('status')=='failed' for j in current),
                     updated_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(state_path,state)

    checkpoint()
    for number, job in enumerate(jobs):
        if should_stop() or json.loads(control.read_text(encoding='utf-8')).get('stop_requested'):
            state['phase']='paused_by_operator'
            break
        key = _key(job)
        saved = snapshots/(key+'.json')
        try:
            cached = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else None
            if (cached and cached.get('six_metrics_present') is True and cached.get('identity_matches') is True
                    and (cached.get('content_html') or cached.get('article_type') == '9')
                    and all(str(cached.get(k))==str(job.get(k)) for k in ('biz','mid','idx','sn'))):
                cached=public_snapshot({**cached,**(journal_meta or {})})
                _atomic_json(saved,cached)
                row=exporter.save(cached)
                if on_article:
                    on_article(cached,row)
                if on_progress:
                    on_progress('article_cached',number=number+1,total=len(jobs),mid=job['mid'],idx=job['idx'])
                state['jobs'][key]={'status':'complete','title':job.get('title',''),'source':'saved_background_response'}
                checkpoint()
                continue
            if number and not isinstance(session,GovernedSession):
                sleep(1.6)
            response = session.get(job['url'],headers=template.headers,timeout=(10,30),allow_redirects=False)
            if response.status_code in (401,403,429):
                state['jobs'][key]={'status':'failed','http_status':response.status_code,'error_code':'http_session_rejected'}
                state['phase']='rate_limited' if response.status_code==429 else 'needs_session'
                checkpoint()
                break
            if response.status_code != 200:
                raise ProbeError('article_http_rejected')
            article = public_snapshot({**article_snapshot(response.text,job['url']),**(journal_meta or {})})
            _atomic_json(saved,article)
            row=exporter.save(article)
            if on_article:
                on_article(article,row)
            if on_progress:
                on_progress('article_complete',number=number+1,total=len(jobs),mid=job['mid'],idx=job['idx'],six_metrics_present=article['six_metrics_present'])
            state['jobs'][key]={'status':'complete' if article['six_metrics_present'] and (bool(article.get('content_html','').strip()) or article.get('article_type')=='9') else 'partial',
                               'title':article.get('title',''),'source':'new_background_response',
                               'http_status':200,'business_ret':article['business_ret']}
            failures=0
        except TrafficStopped as exc:
            state['phase']=str(exc) if str(exc) in {'paused_by_operator','rate_limited','needs_verification','needs_attention'} else 'needs_attention'
            checkpoint()
            break
        except (requests.RequestException,ValueError,TypeError,KeyError) as exc:
            known_errors={'article_identity_mismatch','article_business_rejected','article_http_rejected',
                          'missing_server_data','invalid_server_data','invalid_article_identity','invalid_article_url',
                          'ambiguous_server_data','nonliteral_server_data','invalid_server_literal','unclosed_server_literal'}
            code=str(exc) if isinstance(exc,ProbeError) and str(exc) in known_errors else type(exc).__name__
            state['jobs'][key]={'status':'failed','title':job.get('title',''),'error_code':code}
            if on_progress:
                on_progress('article_failed',number=number+1,total=len(jobs),mid=job['mid'],idx=job['idx'],error_code=code)
            failures+=1
            if isinstance(exc,ProbeError) and str(exc)=='article_business_rejected':
                if isinstance(session,GovernedSession):session.governor.defer('unknown_business',session.governor.restriction_rest)
                state['phase']='needs_attention'
            elif failures>=3:
                state['phase']='needs_attention'
        checkpoint()
        if state['phase'] in ('needs_session','needs_attention','rate_limited'):
            break
    else:
        state['phase']='completed_fields' if state['complete_count']==len(jobs) else 'completed_with_gaps'
    checkpoint()
    return state




def main():
    from export.session_article_exporter import SessionArticleExporter
    from export.csv_exporter import export_articles_csv
    from export.realtime_exporter import default_export_headers
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--catalog',type=Path)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--stop',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.stop:
        _atomic_json(args.output/'run_control.json',{'stop_requested':True})
        return
    if not args.catalog:
        parser.error('catalog is required for a run')
    # Request/cookies only enter through stdin, never process arguments or files.
    raw=json.load(sys.stdin)
    template=read_home_request(raw['request'])
    with runtime_lock(args.output/'run.lock'):
        catalog=json.loads(args.catalog.read_text(encoding='utf-8'))
        jobs,summary=merge_catalog(catalog,args.baseline)
        _atomic_json(args.output/'catalog_comparison.json',summary)
        export_articles_csv(jobs,str(args.output/'历史目录_合并.csv'),default_export_headers(jobs))
        exporter=SessionArticleExporter(args.output/'交付结果')
        with requests.Session() as session:
            session.trust_env=False
            active_template=install_session_cookies(session,template)
            state=run_articles(jobs,active_template,args.output,session,exporter)
        print(json.dumps({k:state.get(k) for k in ('phase','total','complete_count','partial_count','failed_count')},ensure_ascii=False))


if __name__=='__main__':
    main()