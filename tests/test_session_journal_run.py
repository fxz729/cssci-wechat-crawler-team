import json
from pathlib import Path
from types import SimpleNamespace

from crawl.profile_session_probe import HomeRequest
from crawl.session_journal_run import merge_catalog, run_articles

BIZ = "target-biz"
SN = "a" * 32


def job(mid=1):
    return {"biz": BIZ, "mid": str(mid), "idx": 1, "sn": SN, "title": f"Article {mid}",
            "url": f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={mid}&idx=1&sn={SN}"}


def snapshot(mid=1):
    return {**job(mid), "identity_matches": True, "six_metrics_present": True,
            "content_html": "<p>Content</p>", "metrics": {"read_count": 1}, "business_ret": 0}


class Exporter:
    def __init__(self):
        self.rows = []
        self.updated = []

    def save(self, article):
        self.rows.append(article)
        return article

    def update_media(self, updates):
        self.updated.append(updates)


class Session:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status, text="<p>source</p>")


def template():
    return HomeRequest(BIZ, {}, {"Cookie": "TEST_PRIVATE"})


def test_success_is_saved_per_article_and_state_can_resume_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr("crawl.session_journal_run.article_snapshot", lambda body, url: snapshot(int(url.split('mid=')[1].split('&')[0])))
    first, session = Exporter(), Session()
    state = run_articles([job(1), job(2)], template(), tmp_path, session, first, sleep=lambda _: None)
    assert state["phase"] == "completed_fields"
    assert state["complete_count"] == 2 and len(first.rows) == 2
    assert "TEST_PRIVATE" not in (tmp_path / 'state.json').read_text()
    assert all(call[1]['allow_redirects'] is False for call in session.calls)
    second, no_requests = Exporter(), Session()
    resumed = run_articles([job(1), job(2)], template(), tmp_path, no_requests, second, sleep=lambda _: None)
    assert no_requests.calls == [] and len(second.rows) == 2
    assert resumed["complete_count"] == 2


def test_http_auth_failure_stops_instead_of_requesting_remaining_articles(tmp_path):
    session = Session(403)
    result = run_articles([job(1), job(2)], template(), tmp_path, session, Exporter(), sleep=lambda _: None)
    assert result["phase"] == "needs_session"
    assert len(session.calls) == 1 and result["complete_count"] == 0


def test_stop_request_preserved_and_no_new_network(tmp_path):
    (tmp_path/'run_control.json').write_text('{"stop_requested":true}')
    session = Session()
    result = run_articles([job()], template(), tmp_path, session, Exporter(), sleep=lambda _: None)
    assert result["phase"] == "paused_by_operator" and session.calls == []


def test_bounded_parser_failures_stop_with_safe_error_code(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("TEST_PRIVATE")
    monkeypatch.setattr("crawl.session_journal_run.article_snapshot", fail)
    result = run_articles([job(i) for i in range(1, 8)], template(), tmp_path, Session(), Exporter(), sleep=lambda _: None)
    assert result["phase"] == "needs_attention"
    assert len(result["jobs"]) == 3
    assert "TEST_PRIVATE" not in json.dumps(result)


def test_one_partial_article_does_not_prevent_later_success(tmp_path, monkeypatch):
    def parse(body, url):
        mid=int(url.split('mid=')[1].split('&')[0]); result=snapshot(mid)
        result['six_metrics_present'] = mid != 1
        return result
    monkeypatch.setattr("crawl.session_journal_run.article_snapshot", parse)
    exporter=Exporter()
    result=run_articles([job(1),job(2)],template(),tmp_path,Session(),exporter,sleep=lambda _:None)
    assert len(exporter.rows)==2 and result['complete_count']==1
    assert result['phase']=='completed_with_gaps'


def test_previous_cache_identity_supplements_api_without_overwriting_new_fields(tmp_path):
    folder=tmp_path/'capture_cache/articles/000000';folder.mkdir(parents=True)
    def event(mid):
        return {'biz':BIZ,'mid':str(mid),'idx':1,'sn':SN,'article':{'url':job(mid)['url'],'title':'cached title'}}
    (folder/'capture_evidence.json').write_text(json.dumps({'article_event':event(2)}))
    rows,summary=merge_catalog({'target_biz':BIZ,'articles':[job(1)]},tmp_path)
    assert len(rows)==2
    assert rows[0]['title']=='Article 1'
    assert summary['baseline_only_count']==1
    assert summary['catalog_complete'] is False


def test_wrong_biz_jobs_fail_before_any_network(tmp_path):
    import pytest
    other={**job(), 'biz':'other'}
    session=Session()
    with pytest.raises(ValueError):
        run_articles([other],template(),tmp_path,session,Exporter(),sleep=lambda _:None)
    assert session.calls==[]

def test_cookie_jar_can_receive_server_refresh_without_fixed_cookie_header():
    import requests
    from crawl.session_journal_run import install_session_cookies
    original=template()
    session=requests.Session()
    configured=install_session_cookies(session,original)
    assert 'Cookie' not in configured.headers
    assert original.headers['Cookie']=='TEST_PRIVATE'
    real=HomeRequest(BIZ,{}, {'Cookie':'wap_sid2=FIRST; wxuin=USER','User-Agent':'client'})
    configured=install_session_cookies(session,real)
    assert session.cookies.get('wap_sid2',domain='mp.weixin.qq.com',path='/')=='FIRST'
    session.cookies.set('wap_sid2','REFRESHED',domain='mp.weixin.qq.com',path='/')
    prepared=session.prepare_request(requests.Request('GET',job()['url'],headers=configured.headers))
    assert 'wap_sid2=REFRESHED' in prepared.headers['Cookie']
    assert 'wap_sid2=FIRST' not in prepared.headers['Cookie']

def test_video_is_metadata_only_and_does_not_call_media_downloader(tmp_path, monkeypatch):
    video={**snapshot(),'article_type':'9','content_html':'','media_assets':[{'kind':'video','id':'wxv_123'}]}
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',lambda *args:video)
    def forbidden(*args):raise AssertionError('media network forbidden by operator')
    monkeypatch.setattr('crawl.bundle_media.fetch_asset',forbidden)
    exporter=Exporter();session=Session()
    state=run_articles([job()],template(),tmp_path,session,exporter,sleep=lambda _:None)
    assert state['complete_count']==1 and len(session.calls)==1
    assert exporter.updated==[]
    assert state['media_policy']=='metadata_only'

def test_cached_video_metadata_does_not_need_local_video_or_new_request(tmp_path, monkeypatch):
    video={**snapshot(),'article_type':'9','content_html':'','media_assets':[{'kind':'video','id':'wxv_123'}]}
    folder=tmp_path/'article_snapshots';folder.mkdir();(folder/'1_1.json').write_text(json.dumps(video))
    session=Session();result=run_articles([job()],template(),tmp_path,session,Exporter(),sleep=lambda _:None)
    assert session.calls==[] and result['complete_count']==1

def test_callbacks_checkpoint_each_article_stop_and_strip_private_snapshot(tmp_path,monkeypatch):
    source={**snapshot(),'cookie':'PRIVATE','content_html':'<p>正文</p><script>PRIVATE</script>'}
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',lambda *args:source)
    seen=[]
    result=run_articles([job(1),job(2)],template(),tmp_path,Session(),Exporter(),sleep=lambda _:None,
                       should_stop=lambda:bool(seen),on_article=lambda article,row:seen.append(row),
                       journal_meta={'journal_name':'刊名','journal_id':'002'})
    assert result['phase']=='paused_by_operator' and len(seen)==1
    saved=json.loads((tmp_path/'article_snapshots/1_1.json').read_text(encoding='utf-8'))
    assert 'PRIVATE' not in str(saved) and saved['journal_name']=='刊名'


def test_probe_error_code_not_generic_and_rate_limit_is_distinct(tmp_path,monkeypatch):
    from crawl.profile_session_probe import ProbeError
    def fail(*args):raise ProbeError('article_identity_mismatch')
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',fail)
    result=run_articles([job()],template(),tmp_path,Session(),Exporter(),sleep=lambda _:None)
    assert result['jobs']['1_1']['error_code']=='article_identity_mismatch'
    result=run_articles([job()],template(),tmp_path,Session(429),Exporter(),sleep=lambda _:None)
    assert result['phase']=='rate_limited'

def test_empty_normal_body_is_partial_even_when_six_metrics_exist(tmp_path,monkeypatch):
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',lambda *args:{**snapshot(),'article_type':'0','content_html':''})
    result=run_articles([job()],template(),tmp_path,Session(),Exporter(),sleep=lambda _:None)
    assert result['complete_count']==0 and result['partial_count']==1
    assert result['phase']=='completed_with_gaps'


def test_governor_stop_preserves_completed_jobs_and_pending_identity(tmp_path,monkeypatch):
    from crawl.request_governor import TrafficStopped
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',lambda *a:snapshot(1))
    class Stopping(Session):
        def get(self,*a,**kw):
            if self.calls:raise TrafficStopped('rate_limited')
            return super().get(*a,**kw)
    state=run_articles([job(1),job(2)],template(),tmp_path,Stopping(),Exporter(),sleep=lambda _:None)
    assert state['phase']=='rate_limited' and state['complete_count']==1
    assert '2_1' not in state['jobs']
    assert (tmp_path/'article_snapshots/1_1.json').exists()


def test_unknown_article_business_rejection_does_not_request_session_refresh(tmp_path,monkeypatch):
    from crawl.profile_session_probe import ProbeError
    def reject(*a):raise ProbeError('article_business_rejected')
    monkeypatch.setattr('crawl.session_journal_run.article_snapshot',reject)
    state=run_articles([job(1),job(2)],template(),tmp_path,Session(),Exporter(),sleep=lambda _:None)
    assert state['phase']=='needs_attention'
