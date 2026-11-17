import json
from pathlib import Path
import pytest
from crawl.pilot import run_pilot
from storage.database import create_session_factory, init_db
from storage.repository import Repository

BIZ='Mzk3NTE3NTU0Ng=='

def test_pilot_saves_content_and_pending_metrics_then_refills_without_duplicate_content(tmp_path):
    sf=create_session_factory('sqlite:///'+str(tmp_path/'pilot.db'));init_db(sf);repo=Repository(sf)
    source=tmp_path/'targets.md'
    source.write_text('| 1 | 学科 | cssci | 期刊 | 期刊 | https://mp.weixin.qq.com/mp/profile_ext?__biz='+BIZ+' | sample |',encoding='utf-8')
    inbox=tmp_path/'events.jsonl'
    art={'schema_version':1,'event_id':'a1','source':'fiddler_classic','captured_at':'2026-09-06T10:00:00Z',
         'kind':'article','endpoint':'/s','biz':BIZ,'mid':'123','idx':1,'sn':'abc',
         'article':{'url':f'https://mp.weixin.qq.com/s?__biz={BIZ}&mid=123&idx=1&sn=abc', 'title':'sample','account_name':'期刊','author':'Writer','publish_time':'1720000000', 'content_html':'<div id="js_content">Body</div>'}}
    inbox.write_text(json.dumps(art)+'\n',encoding='utf-8'); out=tmp_path/'out'
    report=run_pilot(repo,source,inbox,out,'期刊',3)
    assert report['sample_articles']==1 and not report['full_history_complete']
    rows=json.loads((out/'articles.json').read_text(encoding='utf-8'))
    assert rows[0]['read_count'] is None
    assert rows[0]['content'] == 'Body'
    assert len(json.loads((out/'pending_metrics.json').read_text(encoding='utf-8')))==1
    metric={**{k:art[k] for k in ('schema_version','source','captured_at','biz','mid','idx','sn')},'event_id':'m1','kind':'metrics','endpoint':'/mp/getappmsgext','stats':{'read_num':0,'old_like_num':2,'like_num':3,'share_num':4,'collect_num':5},'comments':{'total_count':0}}
    with inbox.open('a',encoding='utf-8') as f:f.write(json.dumps(metric)+'\n')
    report=run_pilot(repo,source,inbox,out,'期刊',3)
    assert report['sample_articles']==1
    assert report['all_six_metrics_complete_articles']==1
    assert json.loads((out/'articles.json').read_text(encoding='utf-8'))[0]['read_count']==0
    assert (out/'articles.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    assert len(repo.list_articles_for_export(account_name='期刊'))==1

def test_unknown_target_does_not_create_an_unlisted_account(tmp_path):
    source=tmp_path/'targets.md'; source.write_text('',encoding='utf-8')
    sf=create_session_factory('sqlite:///'+str(tmp_path/'pilot.db'));init_db(sf)
    with pytest.raises(ValueError,match='target_not_in_source'):
        run_pilot(Repository(sf),source,tmp_path/'missing',tmp_path/'out','foreign',3)

def test_pilot_persists_explicit_utc_and_refills_from_a_new_metrics_only_file(tmp_path):
    sf=create_session_factory('sqlite:///'+str(tmp_path/'pilot.db'));init_db(sf);repo=Repository(sf)
    source=tmp_path/'targets.md';source.write_text('| 1 | 学科 | cssci | 期刊 | 期刊 | https://mp.weixin.qq.com/mp/profile_ext?__biz='+BIZ+' | sample |',encoding='utf-8')
    identity={'schema_version':1,'source':'fiddler_classic','captured_at':'2026-09-06T10:00:00Z','biz':BIZ,'mid':'123','idx':1,'sn':'abc'}
    art={**identity,'event_id':'a','kind':'article','endpoint':'/s','article':{'url':f'https://mp.weixin.qq.com/s?__biz={BIZ}&mid=123&idx=1&sn=abc','title':'sample','account_name':'期刊','publish_time':'1720000000','content_html':'<div id="js_content">Body</div>'}}
    metrics={**identity,'event_id':'m1','kind':'metrics','endpoint':'/mp/getappmsgext','stats':{'read_num':1,'old_like_num':2,'like_num':3,'share_num':4},'comments':{}}
    inbox=tmp_path/'first.jsonl';inbox.write_text(json.dumps(art)+'\n'+json.dumps(metrics)+'\n',encoding='utf-8')
    out=tmp_path/'out';run_pilot(repo,source,inbox,out,'期刊')
    row=json.loads((out/'articles.json').read_text(encoding='utf-8'))[0]
    assert row['publish_time'].endswith('+00:00')
    missing={**identity,'event_id':'m2','captured_at':'2026-09-06T11:00:00Z','kind':'metrics','endpoint':'/mp/getappmsgext','stats':{'collect_num':0},'comments':{'total_count':0}}
    refill=tmp_path/'refill.jsonl';refill.write_text(json.dumps(missing)+'\n',encoding='utf-8')
    report=run_pilot(repo,source,refill,out,'期刊')
    assert report['all_six_metrics_complete_articles']==1
    row=json.loads((out/'articles.json').read_text(encoding='utf-8'))[0]
    assert row['read_count']==1 and row['comment_count']==0
    assert row['metrics_captured_at'] is None
    assert row['metric_observations']['read_count']['captured_at'] != row['metric_observations']['comment_count']['captured_at']
