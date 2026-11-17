import json
import pytest
from types import SimpleNamespace

from crawl.profile_session_probe import HomeRequest
from crawl.journal_runner import collect_journal_catalog, run_queue

BIZ='target-biz'


def page(offset, *, empty=False, end=False):
    articles=[] if empty else [{'biz':BIZ,'mid':str(offset+1),'idx':1,'sn':'a'*32,'url':f'https://mp.weixin.qq.com/s?__biz={BIZ}&mid={offset+1}&idx=1&sn='+('a'*32)}]
    return {'target_biz':BIZ,'articles':articles,'pages':[{'offset':offset,'next_offset':offset+11,'group_count':0 if empty else 1,'http_status':200,'business_ret':0}], 'pagination_complete':empty,'catalog_complete':False,'stop_reason':'empty_page' if empty else 'page_limit'}


def test_catalog_uses_real_cursor_persists_each_page_and_resumes(tmp_path,monkeypatch):
    calls=[]
    def collect(session,template,**kw):
        offset=kw['start_offset']; calls.append(offset)
        return page(offset,empty=offset==22)
    monkeypatch.setattr('crawl.journal_runner.collect_catalog',collect)
    templ=HomeRequest(BIZ,{}, {})
    first=collect_journal_catalog(None,templ,tmp_path,should_stop=lambda:len(calls)==1,sleep=lambda _:None)
    assert calls==[0] and first['next_offset']==11
    second=collect_journal_catalog(None,templ,tmp_path,sleep=lambda _:None)
    assert calls==[0,11,22] and len(second['articles'])==2 and second['pagination_complete']
    collect_journal_catalog(None,templ,tmp_path,sleep=lambda _:None)
    assert calls==[0,11,22]


def test_catalog_rejection_does_not_advance_cursor(tmp_path,monkeypatch):
    def denied(*args,**kwargs):
        return {'target_biz':BIZ,'articles':[], 'pages':[{'offset':0,'http_status':200,'business_ret':-3}], 'pagination_complete':False,'stop_reason':'business_rejected'}
    monkeypatch.setattr('crawl.journal_runner.collect_catalog',denied)
    result=collect_journal_catalog(None,HomeRequest(BIZ,{},{}),tmp_path,sleep=lambda _:None)
    assert result['next_offset']==0 and result['stop_reason']=='business_rejected'


def test_queue_automatic_next_journal_and_resume_without_redo(tmp_path):
    targets=[SimpleNamespace(biz='a',journal_name='A'),SimpleNamespace(biz='b',journal_name='B')]
    calls=[]
    def run(target,number): calls.append(target.biz); return {'phase':'completed_fields'}
    result=run_queue(targets,tmp_path,run)
    assert calls==['a','b'] and result['phase']=='completed'
    run_queue(targets,tmp_path,run)
    assert calls==['a','b']


def test_queue_session_or_rate_failure_stops_global_not_next_journal(tmp_path):
    targets=[SimpleNamespace(biz='a',journal_name='A'),SimpleNamespace(biz='b',journal_name='B')]
    calls=[]
    def run(target,number):calls.append(target.biz);return {'phase':'rate_limited'}
    result=run_queue(targets,tmp_path,run)
    assert calls==['a'] and result['phase']=='rate_limited'


def test_queue_stop_between_journals_is_persistent(tmp_path):
    targets=[SimpleNamespace(biz='a',journal_name='A'),SimpleNamespace(biz='b',journal_name='B')]
    calls=[]
    result=run_queue(targets,tmp_path,lambda target,number:(calls.append(target.biz) or {'phase':'completed_fields'}),should_stop=lambda:bool(calls))
    assert calls==['a'] and result['phase']=='paused_by_operator'

def test_prepare_home_uses_cookie_jar_and_does_not_follow_redirects():
    import requests
    from crawl.journal_runner import establish_session
    class Session(requests.Session):
        def get(self,url,**kwargs):
            assert kwargs['allow_redirects'] is False
            assert 'Cookie' not in kwargs['headers']
            assert self.cookies.get('wap_sid2')=='TEST'
            return SimpleNamespace(status_code=200,text='<html>home</html>')
    configured=establish_session(Session(),HomeRequest(BIZ,{'action':'home'},{'Cookie':'wap_sid2=TEST','User-Agent':'client'}))
    assert 'Cookie' not in configured.headers


def test_media_queue_does_not_download_until_separate_drain(tmp_path):
    from crawl.journal_runner import MediaQueue
    rows=[{'biz':'b','mid':'1','idx':1,'title':'t','media_assets':[{'kind':'image','url':'https://example.org/i'}]}]
    queue=MediaQueue(tmp_path)
    queue.enqueue(rows[0])
    calls=[]
    class Exporter:
        def article_media_root(self,biz):return tmp_path/'delivery'/biz
        def update_media(self,biz,mid,idx,result):calls.append((biz,mid,idx,result))
    def download(row,root,**kwargs):
        return {'status':'complete','rows':[],'media_pending':0}
    assert queue.pending_count==1 and not calls
    queue.drain(Exporter(),download=download)
    assert queue.pending_count==0 and len(calls)==1
    queue.enqueue(rows[0])
    assert queue.pending_count==0

def test_offline_import_enqueues_images_and_preserves_journal_identity(tmp_path):
    from crawl.journal_runner import import_snapshots
    raw=tmp_path/'old';raw.mkdir()
    item={'biz':BIZ,'mid':'1','idx':1,'sn':'a'*32,'url':f'https://mp.weixin.qq.com/s?__biz={BIZ}&mid=1&idx=1&sn='+('a'*32),'content_html':'<p>Text</p>','identity_matches':True,'business_ret':0,'six_metrics_present':True}
    (raw/'1_1.json').write_text(json.dumps(item))
    seen=[]
    class Exporter:
        def save(self,article):seen.append(article);return article
    class Queue:
        def enqueue(self,row):assert row['journal_name']=='刊名'
    result=import_snapshots(raw,Exporter(),Queue(),[SimpleNamespace(biz=BIZ,journal_name='刊名')],on_progress=lambda *a,**k:None)
    assert result['imported']==1 and seen[0]['journal_id']=='001'


def test_run_queue_gap_result_does_not_claim_complete_delivery(tmp_path):
    targets=[SimpleNamespace(biz='a',journal_name='A')]
    result=run_queue(targets,tmp_path,lambda *a:{'phase':'completed_with_gaps'})
    assert result['phase']=='completed_with_gaps'

def test_ui_navigation_rejects_private_or_other_host_before_subprocess(monkeypatch):
    import pytest
    from crawl.journal_desktop import open_profile
    called=[]
    monkeypatch.setattr('crawl.journal_desktop.subprocess.run',lambda *a,**k:called.append(a))
    with pytest.raises(ValueError):
        open_profile('https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=b&key=PRIVATE','b')
    assert not called


def test_ui_navigation_retries_bounded_without_idle_check(monkeypatch):
    from crawl.journal_desktop import open_profile
    calls=[]
    def run(*args,**kwargs):
        calls.append((args,kwargs));return SimpleNamespace(returncode=1,stdout=b'',stderr=b'PRIVATE')
    monkeypatch.setattr('crawl.journal_desktop.subprocess.run',run)
    import pytest
    with pytest.raises(RuntimeError,match='ui_unavailable'):
        open_profile('https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=b','b',sleep=lambda _:None)
    assert len(calls)==2
    assert all(x[1]['timeout']==25 for x in calls)

def test_operator_scripts_expose_run_pause_stop_without_credentials():
    from pathlib import Path
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    run=(scripts/'run_journal.ps1').read_text(encoding='utf-8-sig')
    assert 'crawl.journal_runner' in run and '--all-journals' in run
    for action in ('pause','stop','status'):
        text=(scripts/f'{action}_journal.ps1').read_text(encoding='utf-8-sig')
        assert 'run_journal.ps1' in text and f"-Action '{action}'" in text
    assert 'pass_ticket' not in run and 'Set-ExecutionPolicy' not in run

def test_media_queue_metadata_only_is_not_a_pending_download(tmp_path):
    from crawl.journal_runner import MediaQueue
    queue=MediaQueue(tmp_path)
    queue.enqueue({'biz':'b','mid':'1','idx':1,'media_assets':[{'kind':'video','id':'public_id'}]})
    assert queue.pending_count==0

def test_changed_public_media_replaces_stale_queue_task(tmp_path):
    from crawl.journal_runner import MediaQueue
    queue=MediaQueue(tmp_path)
    row={'biz':'b','mid':'1','idx':1,'media_assets':[]}
    queue.enqueue(row)
    queue.enqueue({**row,'media_assets':[{'kind':'image','url':'https://example.org/new'}]})
    assert queue.pending_count==1

def test_queue_retries_gaps_on_next_start_but_not_successful_journal(tmp_path):
    targets=[SimpleNamespace(biz='a',journal_name='A'),SimpleNamespace(biz='b',journal_name='B')]
    run_queue(targets,tmp_path,lambda target,n:{'phase':'completed_fields' if target.biz=='a' else 'completed_with_gaps'})
    calls=[]
    result=run_queue(targets,tmp_path,lambda target,n:(calls.append(target.biz) or {'phase':'completed_fields'}))
    assert calls==['b'] and result['phase']=='completed'


def test_gap_run_has_available_data_but_is_not_complete_delivery():
    from crawl.journal_runner import delivery_flags
    result=delivery_flags('completed_with_gaps',0,0)
    assert result['available_data_exported'] is True and result['delivery_ready'] is False
    assert delivery_flags('completed',0,0)['delivery_ready'] is True
    assert delivery_flags('completed',1,0)['delivery_ready'] is False

def test_import_seeds_article_checkpoint_so_production_does_not_refetch(tmp_path):
    from crawl.journal_runner import import_snapshots, journal_work_dir
    source=tmp_path/'old';source.mkdir();work=tmp_path/'work'
    article={**page(0)['articles'][0],'identity_matches':True,'business_ret':0,'six_metrics_present':True,'content_html':'<p>saved</p>'}
    (source/'1_1.json').write_text(json.dumps(article))
    class Exporter:
        def save(self,article):return article
    class Queue:
        def enqueue(self,row):pass
    import_snapshots(source,Exporter(),Queue(),[SimpleNamespace(biz=BIZ,journal_name='刊')],journal_work_root=work)
    assert (journal_work_dir(work,BIZ)/'article_snapshots/1_1.json').is_file()

def test_imported_catalog_supplements_only_matching_journal(tmp_path):
    from crawl.journal_runner import import_known_catalog, merge_known_catalog, journal_work_dir
    source=tmp_path/'catalog.json';source.write_text(json.dumps({'articles':page(0)['articles']}))
    targets=[SimpleNamespace(biz=BIZ,journal_name='刊')]
    assert import_known_catalog(source,tmp_path/'work',targets)==1
    catalog={'target_biz':BIZ,'articles':[]}
    result=merge_known_catalog(catalog,journal_work_dir(tmp_path/'work',BIZ))
    assert len(result)==1 and result[0]['mid']=='1'

def test_cli_offline_import_and_controls_are_usable(tmp_path,monkeypatch):
    import config.journal_request_input
    from crawl.journal_runner import main
    source=tmp_path/'source';source.mkdir()
    article={**page(0)['articles'][0],'identity_matches':True,'business_ret':0,'six_metrics_present':True,'content_html':'<p>saved</p>','metrics':{}}
    (source/'1_1.json').write_text(json.dumps(article))
    monkeypatch.setattr(config.journal_request_input,'load_targets',lambda _: [SimpleNamespace(biz=BIZ,journal_name='刊')])
    output=tmp_path/'out'
    assert main(['--import-snapshots',str(source),'--output',str(output)])==0
    assert (output/'delivery/文章数据.csv').is_file()
    assert main(['--pause','--output',str(output)])==0
    assert json.loads((output/'work/control.json').read_text())['action']=='pause'
    assert main(['--status','--output',str(output)])==0


def test_specific_ui_failure_is_actionable_and_stops_without_session_retry(tmp_path):
    from crawl.journal_runner import collect_one_journal
    calls=[]
    def provider(target):
        calls.append(target.biz)
        raise RuntimeError('profile_link_missing_or_ambiguous')
    result=collect_one_journal(SimpleNamespace(biz=BIZ,journal_name='TEST'),1,tmp_path,None,None,provider)
    assert result=={'phase':'ui_unavailable','error_code':'profile_link_missing_or_ambiguous'}
    assert calls==[BIZ]


def test_single_journal_cli_preserves_original_roster_number(tmp_path,monkeypatch):
    import config.journal_request_input as inputs
    import crawl.journal_runner as runner
    targets=[SimpleNamespace(biz='A',journal_name='First'),SimpleNamespace(biz='B',journal_name='Second')]
    monkeypatch.setattr(inputs,'load_targets',lambda path:targets)
    calls=[]
    def collect(target,number,*args,**kwargs):
        calls.append((target.biz,number))
        return {'phase':'completed_fields','complete_count':0,'total':0}
    monkeypatch.setattr(runner,'collect_one_journal',collect)
    assert runner.main(['--all-journals','--journal-index','2','--output',str(tmp_path)])==0
    assert calls==[('B',2)]
    state=json.loads((tmp_path/'work/queue.json').read_text(encoding='utf-8'))
    assert set(state['journals'])=={'B'}


def test_session_expiry_refreshes_same_journal_and_reuses_catalog(tmp_path,monkeypatch):
    import requests
    import crawl.journal_runner as runner
    import crawl.session_journal_run as articles
    calls=[]; offsets=[]; article_calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(requests,'Session',Session)
    monkeypatch.setattr(runner,'establish_session',lambda session,template:template)
    def collect(session,template,**kw):
        offset=kw['start_offset'];offsets.append(offset)
        return page(offset,empty=offset==11)
    monkeypatch.setattr(runner,'collect_catalog',collect)
    original=runner.collect_journal_catalog
    monkeypatch.setattr(runner,'collect_journal_catalog',lambda *a,**k:original(*a,**k,sleep=lambda _:None))
    def run(jobs,template,*a,**kw):
        article_calls.append(template.params['key'])
        return {'phase':'needs_session' if len(article_calls)==1 else 'completed_fields'}
    monkeypatch.setattr(articles,'run_articles',run)
    def provider(target):
        calls.append(target.biz)
        return HomeRequest(target.biz,{'key':'TEST_'+str(len(calls))},{})
    result=runner.collect_one_journal(SimpleNamespace(biz=BIZ,journal_name='TEST'),1,tmp_path,None,None,provider,on_progress=lambda *a,**k:None)
    assert result['phase']=='completed_fields'
    assert calls==[BIZ,BIZ] and article_calls==['TEST_1','TEST_2']
    assert offsets==[0,11]


def test_repeated_session_rejection_has_finite_refresh_limit(tmp_path,monkeypatch):
    import requests
    import crawl.journal_runner as runner
    import crawl.session_journal_run as articles
    calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(requests,'Session',Session)
    monkeypatch.setattr(runner,'establish_session',lambda session,template:template)
    monkeypatch.setattr(runner,'collect_journal_catalog',lambda *a,**k:{'target_biz':BIZ,'articles':[],'pagination_complete':True,'stop_reason':'empty_page'})
    monkeypatch.setattr(articles,'run_articles',lambda *a,**k:{'phase':'needs_session'})
    def provider(target):
        calls.append(target.biz)
        return HomeRequest(target.biz,{}, {})
    result=runner.collect_one_journal(SimpleNamespace(biz=BIZ,journal_name='TEST'),1,tmp_path,None,None,provider,on_progress=lambda *a,**k:None)
    assert result['phase']=='needs_session' and calls==[BIZ,BIZ]


@pytest.mark.parametrize('http_status,business_ret,reason,phase',[(200,-6,'business_rejected','needs_attention'),(200,-3,'business_rejected','needs_attention'),(403,None,'http_rejected','needs_attention'),(429,None,'http_rejected','rate_limited')])
def test_unknown_catalog_rejection_stops_without_refresh_or_next_journal(tmp_path,monkeypatch,http_status,business_ret,reason,phase):
    import requests
    import crawl.journal_runner as runner
    import crawl.session_journal_run as articles
    calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(requests,'Session',Session)
    monkeypatch.setattr(runner,'establish_session',lambda session,template:template)
    def denied(*args,**kwargs):
        return {'target_biz':BIZ,'articles':[], 'pages':[{'offset':321,'http_status':http_status,'business_ret':business_ret}],
                'pagination_complete':False,'stop_reason':reason}
    monkeypatch.setattr(runner,'collect_catalog',denied)
    monkeypatch.setattr(articles,'run_articles',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must_not_fetch_articles')))
    target=SimpleNamespace(biz=BIZ,journal_name='TEST')
    work=runner.journal_work_dir(tmp_path,BIZ);work.mkdir(parents=True)
    saved={'target_biz':BIZ,'articles':page(0)['articles'],'pages':[], 'next_offset':321,'pagination_complete':False}
    (work/'catalog.json').write_text(json.dumps(saved),encoding='utf-8')
    def provider(target):
        calls.append(target.biz)
        return HomeRequest(target.biz,{}, {})
    def run(target,number):
        return runner.collect_one_journal(target,number,tmp_path,None,None,provider,on_progress=lambda *a,**k:None)
    result=runner.run_queue([target,SimpleNamespace(biz='next',journal_name='NEXT')],tmp_path,run,on_progress=lambda *a,**k:None)
    assert result['phase']==phase
    if phase=='needs_attention':
        assert result['journals'][BIZ]['error_code']=='catalog_'+reason
    assert calls==[BIZ]
    resumed=json.loads((work/'catalog.json').read_text(encoding='utf-8'))
    assert resumed['next_offset']==321 and resumed['articles']==saved['articles']
    assert not resumed['pagination_complete']
    assert resumed['last_attempt']['business_ret']==business_ret


def test_cli_catalog_attention_does_not_drain_media(tmp_path,monkeypatch):
    import config.journal_request_input
    import crawl.journal_runner as runner
    monkeypatch.setattr(config.journal_request_input,'load_targets',lambda _: [SimpleNamespace(biz=BIZ,journal_name='TEST')])
    monkeypatch.setattr(runner,'run_queue',lambda *a,**k:{'phase':'needs_attention'})
    def forbidden(*a,**k):
        raise AssertionError('must_not_download_after_catalog_rejection')
    monkeypatch.setattr(runner.MediaQueue,'drain',forbidden)
    output=tmp_path/'out'
    assert runner.main(['--all-journals','--output',str(output)])==2
    report=json.loads((output/'work/report.json').read_text(encoding='utf-8'))
    assert report['phase']=='needs_attention' and report['delivery_ready'] is False


def test_cooldown_prevents_ui_session_provider(tmp_path):
    from crawl.journal_runner import collect_one_journal
    from crawl.request_governor import RequestGovernor
    g=RequestGovernor(tmp_path/'traffic.json',should_stop=lambda:True)
    g.defer('operator_reported_frequency',3600)
    def provider(*a):raise AssertionError('must not touch UI')
    result=collect_one_journal(SimpleNamespace(biz=BIZ,journal_name='TEST'),1,tmp_path,None,None,provider,governor=g)
    assert result['phase']=='paused_by_operator'


def test_catalog_format_failure_stops_without_next_journal(tmp_path,monkeypatch):
    import requests
    import crawl.journal_runner as runner
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(requests,'Session',Session)
    monkeypatch.setattr(runner,'establish_session',lambda s,t:t)
    monkeypatch.setattr(runner,'collect_journal_catalog',lambda *a,**k:{'stop_reason':'network_or_format_error','articles':[]})
    calls=[]
    def provider(t):calls.append(t.biz);return HomeRequest(t.biz,{}, {})
    def run(t,n):return runner.collect_one_journal(t,n,tmp_path,None,None,provider)
    result=runner.run_queue([SimpleNamespace(biz=BIZ,journal_name='TEST'),SimpleNamespace(biz='next',journal_name='NEXT')],tmp_path,run)
    assert result['phase']=='needs_attention' and calls==[BIZ]


def test_capture_timeout_with_governor_does_not_repeat_ui(tmp_path):
    import crawl.journal_runner as runner
    from crawl.request_governor import RequestGovernor
    g=RequestGovernor(tmp_path/'traffic.json',sleep=lambda _:None)
    calls=[]
    def provider(t):calls.append(t.biz);raise RuntimeError('capture_timeout')
    # A second UI attempt must fail the test instead of waiting through the reservation.
    original=g.before_navigation
    def before():
        assert not calls,'must not repeat UI after unknown capture failure'
        original()
    g.before_navigation=before
    result=runner.collect_one_journal(SimpleNamespace(biz=BIZ,journal_name='TEST'),1,tmp_path,None,None,provider,governor=g)
    assert result['phase']=='needs_attention' and calls==[BIZ]
    assert result['error_code']=='capture_timeout'
    assert g.state['cooldown_until']>g.clock()
