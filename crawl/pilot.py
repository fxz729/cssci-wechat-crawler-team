"""Bounded, resumable pilot using data-only events rather than copied cookies."""
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from types import SimpleNamespace
import csv
import hashlib
import json

from capture.reader import METRIC_FIELDS, load_events
from crawl.article_persistence import persist_history_article
from export.realtime_exporter import default_export_headers
from sources.journal_catalog import parse_profile_links_file
from storage.models import ArticleMetric


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))


def _time(value):
    if not value: return None
    try:
        text=str(value)
        if text.isdigit(): return datetime.fromtimestamp(int(text), timezone.utc)
        date=datetime.fromisoformat(text.replace('Z','+00:00'))
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError): return None


def _key(event):
    return event['biz'],str(event['mid']),int(event['idx'])


def _html(event):
    a=event['article']; stamp=_time(a['publish_time'])
    meta=''.join(f'<meta name="{k}" content="{escape(a.get(v,""),quote=True)}">' for k,v in [('author','author'),('description','digest')])
    meta+=f'<meta property="og:image" content="{escape(a.get("cover_url",""),quote=True)}">'
    public_vars=''.join(f'var {k}={json.dumps(str(v))};' for k,v in [('biz',event['biz']),('mid',event['mid']),('idx',event['idx']),('sn',event['sn'])])
    if stamp: public_vars+=f'var ct="{int(stamp.timestamp())}";'
    return '<html><head>'+meta+'</head><body><h1 id="activity-name">'+escape(a['title'])+'</h1><span id="js_name">'+escape(a['account_name'])+'</span>'+a['content_html']+'<script>'+public_vars+'</script></body></html>'


def _write_csv(path, rows, fields):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:
            writer.writerow({k:json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(list,dict)) else v for k,v in row.items()})


def run_pilot(repo, journal_links, inbox, output_dir, account_name, limit=3):
    if not 1<=limit<=10: raise ValueError('pilot_limit_must_be_1_to_10')
    targets=parse_profile_links_file(str(journal_links))
    matching=[t for t in targets if t.account_name==account_name]
    if len(matching)!=1: raise ValueError('target_not_in_source_or_ambiguous')
    target=matching[0]; repo.sync_target_accounts([target])
    account=next(a for a in repo.list_accounts(limit=None) if a.biz==target.biz)
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    events,issues=load_events(inbox,{target.biz}) if Path(inbox).exists() else ([],[])
    article_events={}
    for e in events:
        if e['kind']=='article': article_events[_key(e)]=e
    existing={ (r['biz'],str(r['mid']),int(r['idx'])):r for r in repo.list_articles_for_export(account_name=account_name) if r['biz']==target.biz }
    selected=list(existing)[:limit]
    for key in article_events:
        if key not in selected and len(selected)<limit:selected.append(key)
    failures=[]
    for key in selected:
        if key in existing: continue
        e=article_events[key];a=e['article'];html=_html(e)
        item=SimpleNamespace(mid=e['mid'],idx=e['idx'],title=a['title'],author=a['author'],digest=a['digest'],cover_url=a['cover_url'],link=a['url'],publish_time=_time(a['publish_time']))
        client=SimpleNamespace(fetch_article_page=lambda item, html=html:(item.link,html))
        try:
            persist_history_article(repo,account,client,item,out/'content',with_metrics=False)
        except Exception as exc:
            failures.append({'mid':e['mid'],'idx':e['idx'],'stage':'content','error_type':type(exc).__name__})
    rows=[r for r in repo.list_articles_for_export(account_name=account_name) if (r['biz'],str(r['mid']),int(r['idx'])) in selected]
    # Store individual observed snapshots; never label a composite as one observation.
    with repo.session_factory() as db:
        from storage.models import Article
        ids={(a.biz,str(a.mid),a.idx):a.id for a in db.query(Article).filter(Article.account_id==account.id)}
        prior=set()
        for m in db.query(ArticleMetric).filter(ArticleMetric.article_id.in_(list(ids.values()))):
            try:prior.add(json.loads(m.raw_payload_json or '{}').get('capture_event_id'))
            except ValueError:pass
    for e in events:
        if e['kind']!='metrics' or _key(e) not in ids or _key(e) not in selected or e['event_id'] in prior:continue
        if all(v is None for v in e['metrics'].values()):continue
        snapshot=repo.insert_metrics_snapshot(ids[_key(e)],provider='wechat-client-observed' if e['source']=='wechat_uia' else 'fiddler-observed',provider_priority=110,
            raw_payload={'capture_event_id':e['event_id'],'field_meta':e['field_meta'],'observed_at':e['captured_at'],'source':e['source'],'endpoint':e['endpoint']},**e['metrics'])
        # Existing API has no observed-time argument; set the timestamp in this independent pilot DB.
        with repo.session_factory() as db:
            stored=db.get(ArticleMetric,snapshot.id);stored.captured_at=_time(e['captured_at']);db.commit()
        prior.add(e['event_id'])
    stored_observations = {}
    with repo.session_factory() as db:
        reverse_ids = {value:key for key,value in ids.items()}
        snapshots = db.query(ArticleMetric).filter(ArticleMetric.article_id.in_(list(ids.values()))).all()
        for snapshot in snapshots:
            try: payload = json.loads(snapshot.raw_payload_json or '{}')
            except ValueError: continue
            if not payload.get('capture_event_id'): continue
            key = reverse_ids[snapshot.article_id]
            fields = stored_observations.setdefault(key, {})
            for field, meta in payload.get('field_meta', {}).items():
                if field not in METRIC_FIELDS or getattr(snapshot,field) is None: continue
                stamp = _time(meta.get('captured_at'))
                if stamp is None: continue
                previous = fields.get(field)
                if previous is None or stamp >= _time(previous['captured_at']):
                    fields[field] = {**meta, 'value':getattr(snapshot,field),
                        'event_id':payload['capture_event_id'], 'source':payload.get('source','unknown')}
    coverage=[];pending=[]
    # Explicit per-field view: every adopted value carries its own time, source and precision.
    for row in rows:
        row={k:v for k,v in row.items() if not k.startswith('_')}
        identity=(row['biz'],str(row['mid']),int(row['idx']))
        observations=dict(stored_observations.get(identity, {}))
        # This pilot writes parser-normalized UTC datetimes; SQLite drops tzinfo on read.
        if row.get('publish_time') is not None:
            row['publish_time']=_time(row['publish_time'])
            row['publish_time_timezone']='UTC'
        applicable=sorted((e for e in events if e['kind']=='metrics' and _key(e)==identity),key=lambda e:_time(e['captured_at']))
        for e in applicable:
            for field in METRIC_FIELDS:
                if e['metrics'][field] is not None:
                    previous = observations.get(field)
                    if previous is None or _time(e['captured_at']) >= _time(previous['captured_at']):
                        observations[field]={**e['field_meta'][field],'value':e['metrics'][field],'event_id':e['event_id'],'source':e['source']}
        missing=[]
        for field in METRIC_FIELDS:
            item=observations.get(field)
            row[field]=item['value'] if item else None
            if not item:missing.append(field)
        row['metric_observations']=observations
        row['metrics_status']='complete' if not missing else 'partial' if observations else 'not_observed'
        row['metrics_provider']='explicit_per_field_observations'
        stamps={o['captured_at'] for o in observations.values()}
        row['metrics_captured_at']=next(iter(stamps)) if len(stamps)==1 else None
        row['metric_field_status']={f:observations.get(f,{}).get('status','not_observed') for f in METRIC_FIELDS}
        row['journal_name']=target.journal_name;row['discipline']=target.discipline;row['tier']=target.tier;row['target_source_line']=target.source_line
        row['missing_metric_fields']=missing
        coverage.append(row)
        if missing:pending.append({'biz':target.biz,'mid':row['mid'],'idx':row['idx'],'url':row['canonical_url'],'missing_fields':missing,'reason':'not_observed_in_available_sources'})
    coverage.sort(key=lambda r:(str(r.get('publish_time') or ''),r['mid'],r['idx']),reverse=True)
    (out/'articles.json').write_text(_json(coverage),encoding='utf-8')
    fields=default_export_headers(coverage)
    _write_csv(out/'articles.csv',coverage,fields)
    (out/'pending_metrics.json').write_text(_json(pending),encoding='utf-8')
    field_rows=[{'field':f,'observed':sum(r.get(f) is not None for r in coverage),'exact':sum(r.get('metric_observations',{}).get(f,{}).get('precision')=='exact' for r in coverage),'total':len(coverage)} for f in METRIC_FIELDS]
    base_fields=('account_name','biz','profile_url','title','author','digest','cover_image_url','canonical_url','mid','idx','sn','publish_time','content_html','content','image_urls','media_assets','audio_count','video_count')
    for field in base_fields:
        count=sum(field in row and row[field] is not None and (not isinstance(row[field], str) or bool(row[field].strip())) for row in coverage)
        field_rows.append({'field':field,'observed':count,'exact':count,'total':len(coverage)})
    _write_csv(out/'field_coverage.csv',field_rows,['field','observed','exact','total'])
    catalog_events=[e for e in events if e['kind']=='catalog']
    report={'scope':'one_journal_pilot','target_count_in_source':len(targets),'target_account':target.account_name,'target_biz':target.biz,
            'source_sha256':hashlib.sha256(Path(journal_links).read_bytes()).hexdigest(),'sample_limit':limit,'sample_articles':len(coverage),
            'all_six_metrics_complete_articles':sum(all(r.get(f) is not None and r['metric_observations'][f]['precision']=='exact' for f in METRIC_FIELDS) for r in coverage),
            'full_history_complete':False,'catalog_event_count':len(catalog_events),'catalog_business_codes':sorted(set(e['ret'] for e in catalog_events if e.get('ret') is not None)),
            'pending_metrics_articles':len(pending),'capture_issues':issues,'content_failures':failures,'field_coverage':field_rows,
            'evidence_event_ids':[e['event_id'] for e in events],'generated_at':datetime.now(timezone.utc).isoformat(),
            'status':'sample_collected' if coverage else 'waiting_for_verified_articles'}
    (out/'report.json').write_text(_json(report),encoding='utf-8')
    text=f'# {target.account_name}：小样本验证\n\n- 目标文件：{Path(journal_links).name}\n- 样本：{len(coverage)}/{limit} 篇。\n- 六项均有精确观测：{report["all_six_metrics_complete_articles"]} 篇。\n- 历史目录完整性：未证明；小样本不等于全历史。\n- 待补指标：{len(pending)} 篇。\n- 目录业务码：{report["catalog_business_codes"]}。\n\n## 指标覆盖\n\n| 字段 | 已观测 | 精确值 | 样本数 |\n|---|---:|---:|---:|\n'
    text+=''.join(f'| {f["field"]} | {f["observed"]} | {f["exact"]} | {f["total"]} |\n' for f in field_rows)
    text+='\n## 数据解释\n\n指标为采集时观测，不是文章发布当年的历史快照。每字段保留来源、原始口径、精度和采集时间；不同时间的值不伪装为同一快照。空值不是零。公开内容经身份核验独立保存；无指标不抛弃正文。\n'
    (out/'report.md').write_text(text,encoding='utf-8')
    return report

