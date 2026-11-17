"""Resumable journal queue. Desktop access is confined to session acquisition."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from crawl.request_governor import RequestGovernor, GovernedSession, TrafficStopped
from crawl.profile_session_probe import ProbeError, _article_identity, _atomic_json, collect_catalog


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(message, **fields):
    print(json.dumps({'time':now(),'event':message,**fields},ensure_ascii=False),flush=True)


def collect_journal_catalog(session, template, root, *, should_stop=lambda:False,
                            sleep=time.sleep, on_progress=emit, max_pages=10000):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/'catalog.json'
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'target_biz':template.biz,'articles':[],'pages':[],'next_offset':0,
        'pagination_complete':False,'catalog_complete':False}
    if state['target_biz']!=template.biz:
        raise ProbeError('catalog_biz_mismatch')
    known={}
    for item in state['articles']:
        identity=_article_identity(item['url'])
        if identity.biz!=template.biz:
            raise ProbeError('catalog_biz_mismatch')
        known[(identity.biz,identity.mid,identity.idx)]=identity.sn
    if state.get('pagination_complete'):
        return state
    for _ in range(max_pages):
        if should_stop():
            state['stop_reason']='paused_by_operator'
            break
        offset=state['next_offset']
        result=collect_catalog(session,template,max_pages=1,start_offset=offset,sleep=sleep)
        pages=result.get('pages') or []
        page=pages[-1] if pages else {}
        state['last_attempt']=page
        reason=result['stop_reason']
        if page.get('http_status')!=200 or page.get('business_ret')!=0:
            state['stop_reason']=reason
            break
        for article in result['articles']:
            identity=_article_identity(article['url'])
            key=(identity.biz,identity.mid,identity.idx)
            if identity.biz!=template.biz or (key in known and known[key]!=identity.sn):
                raise ProbeError('catalog_identity_conflict')
            if key not in known:
                state['articles'].append(article);known[key]=identity.sn
        state['pages'].extend(pages)
        state['stop_reason']=reason
        state['pagination_complete']=bool(result.get('pagination_complete'))
        if state['pagination_complete']:
            _atomic_json(path,state)
            break
        next_offset=page.get('next_offset')
        if reason!='page_limit' or type(next_offset) is not int or next_offset<=offset:
            # Preserve the failed cursor. Never advance an unresolved page silently.
            _atomic_json(path,state)
            break
        state['next_offset']=next_offset
        state['updated_at']=now()
        _atomic_json(path,state)
        on_progress('catalog_page',biz=template.biz,offset=offset,total=len(state['articles']))
        if not should_stop() and not isinstance(session,GovernedSession):
            sleep(1.6)
    else:
        state['stop_reason']='page_budget_reached'
    state['updated_at']=now()
    _atomic_json(path,state)
    return state


def run_queue(targets, root, run_one, *, should_stop=lambda:False, on_progress=emit):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/'queue.json'
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'journals':{}}
    state.update(phase='running',total=len(targets))
    for number,target in enumerate(targets,1):
        if should_stop():
            state['phase']='paused_by_operator';break
        if state['journals'].get(target.biz,{}).get('phase')=='completed_fields':
            continue
        state.update(current_biz=target.biz,current_number=number,updated_at=now())
        _atomic_json(path,state)
        on_progress('journal_start',number=number,total=len(targets),journal_name=target.journal_name)
        result=run_one(target,number)
        # Only operational metadata enters the queue, never session objects.
        summary={k:result[k] for k in ('phase','total','complete_count','partial_count','failed_count','error_code','catalog_stop_reason') if k in result}
        state['journals'][target.biz]=summary
        state['updated_at']=now()
        _atomic_json(path,state)
        if result['phase'] in ('rate_limited','needs_session','needs_attention','paused_by_operator','capture_not_ready','needs_verification','ui_unavailable'):
            state['phase']=result['phase'];break
    else:
        state['phase']='completed' if all(state['journals'].get(t.biz,{}).get('phase')=='completed_fields' for t in targets) else 'completed_with_gaps'
    state['updated_at']=now();_atomic_json(path,state)
    return state


def establish_session(session, template):
    from crawl.profile_session_probe import ENDPOINT
    from crawl.session_journal_run import install_session_cookies
    configured=install_session_cookies(session,template)
    response=session.get(ENDPOINT,params=configured.params,headers=configured.headers,
                         timeout=(10,30),allow_redirects=False)
    if response.status_code==429:
        raise ProbeError('rate_limited')
    if response.status_code!=200:
        raise ProbeError('home_session_rejected')
    return configured


class MediaQueue:
    """Durable, independent body-image tasks; no network during enqueue."""
    def __init__(self,root):
        self.root=Path(root)/'media_tasks';self.root.mkdir(parents=True,exist_ok=True)

    def _path(self,row):
        import hashlib
        key='|'.join(str(row[k]) for k in ('biz','mid','idx'))
        return self.root/(hashlib.sha256(key.encode()).hexdigest()+'.json')

    def enqueue(self,row):
        path=self._path(row)
        public={k:row[k] for k in ('biz','mid','idx','title','media_assets','cover_url','cover_image_url') if k in row}
        if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('row')==public:
            return
        has_images=any(asset.get('kind')=='image' for asset in public.get('media_assets',[]) if isinstance(asset,dict))
        _atomic_json(path,{'status':'pending' if has_images else 'complete','row':public})

    @property
    def pending_count(self):
        pending=0
        for path in self.root.glob('*.json'):
            task=json.loads(path.read_text(encoding='utf-8'))
            if task.get('status')!='complete' and any(a.get('kind')=='image' for a in task['row'].get('media_assets',[]) if isinstance(a,dict)):
                pending+=1
        return pending

    def drain(self,exporter,*,should_stop=lambda:False,download=None,on_progress=emit,requester=None):
        if download is None:
            from crawl.session_media import download_article_media
            download=download_article_media
        for path in sorted(self.root.glob('*.json')):
            if should_stop():break
            task=json.loads(path.read_text(encoding='utf-8'))
            if task.get('status')=='complete':continue
            row=task['row']
            try:
                options={'requester':requester} if requester is not None else {}
                result=download(row,exporter.article_media_root(row['biz']),should_stop=should_stop,**options)
                exporter.update_media(row['biz'],row['mid'],row['idx'],result)
                task.update(status='complete' if result.get('media_pending')==0 else 'partial',updated_at=now())
                task['media_pending']=result.get('media_pending')
                _atomic_json(path,task)
                on_progress('images_updated',biz=row['biz'],mid=row['mid'],idx=row['idx'],status=task['status'])
            except (OSError,ValueError) as exc:
                task.update(status='failed',error_code=type(exc).__name__,updated_at=now())
                _atomic_json(path,task)
                on_progress('images_failed',biz=row['biz'],mid=row['mid'],idx=row['idx'],error_code=type(exc).__name__)


def journal_work_dir(root,biz):
    import hashlib
    return Path(root)/'journals'/hashlib.sha256(biz.encode()).hexdigest()[:20]


def import_known_catalog(source,root,targets):
    import csv
    source=Path(source)
    if source.suffix.lower()=='.csv':
        with source.open(encoding='utf-8-sig',newline='') as stream:
            items=list(csv.DictReader(stream))
    else:
        data=json.loads(source.read_text(encoding='utf-8-sig'))
        items=data['articles'] if isinstance(data,dict) else data
    allowed={t.biz for t in targets};groups={}
    for raw in items:
        parts=_article_identity(raw.get('url') or raw.get('canonical_url'))
        if parts.biz not in allowed:raise ProbeError('catalog_biz_not_in_roster')
        group=groups.setdefault(parts.biz,{})
        key=(parts.mid,parts.idx)
        if key in group and group[key]['sn']!=parts.sn:raise ProbeError('catalog_identity_conflict')
        group[key]={'biz':parts.biz,'mid':parts.mid,'idx':parts.idx,'sn':parts.sn,'url':parts.canonical_url,
                    'title':raw.get('title',''),'source':'prior_local_catalog'}
    for biz,group in groups.items():
        work=journal_work_dir(root,biz);work.mkdir(parents=True,exist_ok=True)
        existing=merge_known_catalog({'target_biz':biz,'articles':list(group.values())},work)
        _atomic_json(work/'known_catalog.json',{'target_biz':biz,'articles':existing})
    return sum(len(g) for g in groups.values())


def merge_known_catalog(catalog,work):
    path=Path(work)/'known_catalog.json'
    previous=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'target_biz':catalog['target_biz'],'articles':[]}
    if previous['target_biz']!=catalog['target_biz']:raise ProbeError('catalog_biz_mismatch')
    merged={}
    for raw in [*catalog['articles'],*previous['articles']]:
        parts=_article_identity(raw['url']);key=(parts.mid,parts.idx)
        if parts.biz!=catalog['target_biz'] or (key in merged and merged[key]['sn']!=parts.sn):
            raise ProbeError('catalog_identity_conflict')
        merged.setdefault(key,raw)
    return list(merged.values())


def import_snapshots(source,exporter,media_queue,targets,*,should_stop=lambda:False,on_progress=emit,defer_updates=False,journal_work_root=None):
    from utils.public_content import public_snapshot
    registry={t.biz:(f'{n:03d}',t.journal_name) for n,t in enumerate(targets,1)}
    report={'imported':0,'failed':0,'files':0}
    for path in sorted(Path(source).glob('*.json')):
        if should_stop():
            report['phase']='paused_by_operator';break
        report['files']+=1
        try:
            article=public_snapshot(json.loads(path.read_text(encoding='utf-8-sig')))
            journal_id,journal_name=registry[article['biz']]
            article.update(journal_id=journal_id,journal_name=journal_name)
            row=exporter.save(article,defer_updates=True) if defer_updates else exporter.save(article)
            media_queue.enqueue(row)
            if journal_work_root is not None:
                identity=_article_identity(article['url'])
                if identity.biz!=article['biz'] or str(identity.mid)!=str(article['mid']) or identity.idx!=article['idx']:
                    raise ProbeError('import_identity_mismatch')
                checkpoint=journal_work_dir(journal_work_root,identity.biz)/'article_snapshots'
                checkpoint.mkdir(parents=True,exist_ok=True)
                _atomic_json(checkpoint/(str(identity.mid)+'_'+str(identity.idx)+'.json'),article)
            if defer_updates and report['files']%20==0:
                exporter.flush()
            report['imported']+=1
            on_progress('article_imported',number=report['files'],biz=article['biz'],mid=article['mid'],idx=article['idx'])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            report['failed']+=1
            on_progress('import_failed',number=report['files'],error_code=type(exc).__name__)
    return report


def automatic_template(target,private_root,*,should_stop=lambda:False,on_progress=emit):
    from crawl.journal_session_capture import SessionCaptureBroker,verify_fiddler_hook
    from crawl.journal_desktop import open_profile
    if should_stop():raise RuntimeError("paused_by_operator")
    verify_fiddler_hook()
    broker=SessionCaptureBroker(private_root)
    broker.arm(target.biz)
    try:
        on_progress('ui_session_start',journal_name=target.journal_name,biz=target.biz)
        open_profile(target.profile_url,target.biz,should_stop=should_stop)
        on_progress('ui_actions_finished',biz=target.biz)
        until=time.monotonic()+60
        while time.monotonic()<until:
            if should_stop():raise RuntimeError('paused_by_operator')
            template=broker.consume()
            if template is not None:return template
            time.sleep(.25)
        raise RuntimeError('capture_timeout')
    finally:
        broker.disarm()


def collect_one_journal(target,number,root,exporter,media_queue,provider,*,
                        should_stop=lambda:False,on_progress=emit,refresh_attempts=2,governor=None):
    import requests
    from crawl.session_journal_run import run_articles
    work=journal_work_dir(root,target.biz)
    work.mkdir(parents=True,exist_ok=True)
    if not should_stop():_atomic_json(work/'run_control.json',{'stop_requested':False})
    for attempt in range(refresh_attempts):
        if should_stop():return {'phase':'paused_by_operator'}
        try:
            if governor is not None:governor.before_navigation()
            template=provider(target)
            if template.biz!=target.biz:raise ProbeError('session_biz_mismatch')
            with requests.Session() as session:
                session.trust_env=False
                if governor is not None:session=GovernedSession(session,governor)
                active=establish_session(session,template)
                catalog=collect_journal_catalog(session,active,work,should_stop=should_stop,on_progress=on_progress)
                if should_stop():return {'phase':'paused_by_operator'}
                last=catalog.get('last_attempt') or {}
                if last.get('http_status')==429:return {'phase':'rate_limited'}
                if catalog.get('stop_reason')=='network_or_format_error':
                    if governor is not None:governor.defer('unknown_business',governor.restriction_rest)
                    return {'phase':'needs_attention','error_code':'catalog_network_or_format_error',
                            'catalog_stop_reason':catalog['stop_reason']}
                if catalog.get('stop_reason') in ('business_rejected','http_rejected'):
                    if governor is not None:governor.defer('catalog_'+catalog['stop_reason'],governor.restriction_rest)
                    # A rejection is not proof of expiry; do not refresh past a possible restriction.
                    return {'phase':'needs_attention',
                            'error_code':'catalog_'+catalog['stop_reason'],
                            'catalog_stop_reason':catalog['stop_reason']}
                else:
                    result=run_articles(merge_known_catalog(catalog,work),active,work,session,exporter,
                                        should_stop=should_stop,on_progress=on_progress,
                                        journal_meta={'journal_id':f'{number:03d}','journal_name':target.journal_name},
                                        on_article=lambda article,row:media_queue.enqueue(row))
                    if result['phase']=='completed_fields' and not catalog.get('pagination_complete'):
                        result['phase']='completed_with_gaps'
                    result['catalog_stop_reason']=catalog.get('stop_reason')
                if result['phase']!='needs_session':return result
        except TrafficStopped as exc:
            return {'phase':str(exc) if str(exc) in {'paused_by_operator','rate_limited','needs_verification','needs_attention'} else 'needs_attention',
                    'error_code':str(exc) if str(exc) in {'paused_by_operator','rate_limited','needs_verification','needs_attention'} else str(exc) if str(exc) in {'traffic_state_write_failed','traffic_log_failed'} else 'traffic_state_invalid'}
        except requests.RequestException:
            return {'phase':'needs_attention','error_code':'network_error'}
        except (ValueError,RuntimeError,OSError) as exc:
            # Exception text may include credentials or a local invocation; never export it.
            code=str(exc)
            if governor is not None and code in {'home_session_rejected','capture_timeout'}:
                governor.defer('unknown_business',governor.restriction_rest)
                return {'phase':'needs_attention','error_code':code}
            if code=='fiddler_hook_not_ready':
                return {'phase':'capture_not_ready','error_code':code}
            from crawl.journal_desktop import UI_ERROR_CODES
            if code in UI_ERROR_CODES or code in {'ui_invalid_response','ui_navigation_timeout'}:
                return {'phase':'ui_unavailable','error_code':code}
            safe={'paused_by_operator','rate_limited','ui_unavailable','capture_timeout','home_session_rejected','session_biz_mismatch'}
            if code not in safe:
                return {'phase':'capture_not_ready','error_code':type(exc).__name__}
            if code not in {'home_session_rejected','capture_timeout'}:
                return {'phase':code if code in {'paused_by_operator','rate_limited','ui_unavailable'} else 'needs_attention','error_code':code}
            result={'phase':'needs_session','error_code':code}
        on_progress('session_refresh_needed',biz=target.biz,attempt=attempt+1,maximum=refresh_attempts)
    return result


def delivery_flags(fields_phase,export_pending,image_tasks_pending):
    return {'available_data_exported':export_pending==0,
            'delivery_ready':fields_phase in ('completed','completed_fields') and export_pending==0 and image_tasks_pending==0}


def main(argv=None):
    import argparse
    import os
    from config.journal_request_input import load_targets,read_request_input
    from export.global_article_exporter import GlobalArticleExporter
    from utils.runtime_lock import runtime_lock
    project=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all-journals',action='store_true')
    mode.add_argument('--request-file',type=Path)
    mode.add_argument('--import-snapshots',type=Path)
    mode.add_argument('--images-only',action='store_true')
    mode.add_argument('--pause',action='store_true')
    mode.add_argument('--stop',action='store_true')
    mode.add_argument('--status',action='store_true')
    parser.add_argument('--catalog-file',type=Path,help='Optional local prior catalog to merge during snapshot import')
    parser.add_argument('--output',type=Path,default=project/'output/session_production')
    parser.add_argument('--sources',type=Path,default=project/'data/sources/CSSCI来源刊公众号主页链接.md')
    parser.add_argument('--private-root',type=Path,default=Path(os.environ.get('SystemDrive','C:')+'/')/'CssciWechatPrivate/session')
    parser.add_argument('--journal-index',type=int,help='Validate or resume one journal by its original roster position')
    args=parser.parse_args(argv)
    if args.journal_index is not None and not args.all_journals:
        parser.error('journal-index requires all-journals')
    root=args.output.resolve();work=root/'work';control=work/'control.json'
    if args.status:
        report={}
        for name in ('control.json','queue.json','report.json','traffic.json'):
            path=work/name
            if path.exists():report[name]=json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    work.mkdir(parents=True,exist_ok=True)
    if args.pause or args.stop:
        _atomic_json(control,{'action':'stop' if args.stop else 'pause','requested_at':now()})
        emit('control_requested',action='stop' if args.stop else 'pause');return 0
    interrupted=False
    def should_stop():
        return interrupted or (control.exists() and json.loads(control.read_text(encoding='utf-8')).get('action') in ('pause','stop'))
    try:
        with runtime_lock(root/'run.lock'):
            targets=load_targets(args.sources)
            roster_numbers={target.biz:number for number,target in enumerate(targets,1)}
            if args.journal_index is not None:
                if not 1<=args.journal_index<=len(targets):raise ValueError('invalid_journal_index')
                targets=[targets[args.journal_index-1]]
            _atomic_json(control,{'action':'run','requested_at':now()})
            def log(message,**fields):
                record={'time':now(),'event':message,**fields}
                print(json.dumps(record,ensure_ascii=False),flush=True)
                with (work/'events.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            exporter=GlobalArticleExporter(root/'delivery',work_root=work/'export')
            media_queue=MediaQueue(work)
            governor=RequestGovernor(work/'traffic.json',should_stop=should_stop,on_progress=log)
            try:
                if args.import_snapshots:
                    if args.catalog_file:
                        import_known_catalog(args.catalog_file,work,targets)
                    report=import_snapshots(args.import_snapshots,exporter,media_queue,targets,should_stop=should_stop,on_progress=log,defer_updates=True,journal_work_root=work)
                    report.setdefault('phase','imported' if not report['failed'] else 'imported_with_gaps')
                elif args.images_only:
                    report={'phase':'image_pass'}
                    media_queue.drain(exporter,should_stop=should_stop,on_progress=log,requester=governor.request)
                else:
                    if args.request_file:
                        template=read_request_input(args.request_file.read_text(encoding='utf-8-sig'))
                        selected=[(n,t) for n,t in enumerate(targets,1) if t.biz==template.biz]
                        if len(selected)!=1:raise ValueError('target_not_in_roster')
                        number,target=selected[0]
                        report=collect_one_journal(target,number,work,exporter,media_queue,lambda _:template,
                                                   should_stop=should_stop,on_progress=log,refresh_attempts=1,governor=governor)
                    else:
                        def run_one(target,number):
                            return collect_one_journal(target,roster_numbers[target.biz],work,exporter,media_queue,
                                lambda target:automatic_template(target,args.private_root,should_stop=should_stop,on_progress=log),
                                should_stop=should_stop,on_progress=log,governor=governor)
                        report=run_queue(targets,work,run_one,should_stop=should_stop,on_progress=log)
                    if not should_stop() and report['phase'] not in ('needs_session','needs_attention','rate_limited','capture_not_ready','needs_verification','ui_unavailable'):
                        media_queue.drain(exporter,should_stop=should_stop,on_progress=log,requester=governor.request)
            except TrafficStopped as exc:
                report={'phase':str(exc) if str(exc) in {'paused_by_operator','rate_limited','needs_verification','needs_attention'} else 'needs_attention'}
            except KeyboardInterrupt:
                interrupted=True
                _atomic_json(control,{'action':'pause','requested_at':now()})
                report={'phase':'paused_by_operator'}
            exporter.flush()
            report.update(updated_at=now(),export_pending=exporter.pending_count,
                          image_tasks_pending=media_queue.pending_count,csv_path=str(root/'delivery/文章数据.csv'))
            fields_phase=report['phase']
            if args.images_only and (work/'queue.json').exists():
                fields_phase=json.loads((work/'queue.json').read_text(encoding='utf-8')).get('phase')
            report.update(delivery_flags(fields_phase,report['export_pending'],report['image_tasks_pending']))
            _atomic_json(work/'report.json',report)
            log('run_finished',**report)
            return (3 if report['phase'] in ('completed_with_gaps','imported_with_gaps') else
                    0 if report['phase'] in ('completed','completed_fields','image_pass','imported','paused_by_operator') else 2)
    except (ValueError,RuntimeError,OSError) as exc:
        emit('run_rejected',error_code=type(exc).__name__)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
