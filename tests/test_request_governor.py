import json
from types import SimpleNamespace
from email.utils import formatdate
import pytest
import requests
from crawl.request_governor import RequestGovernor, TrafficStopped, response_signal, retry_after_seconds

class Clock:
    def __init__(self):self.value=100000.;self.waits=[]
    def now(self):return self.value
    def sleep(self,n):self.waits.append(n);self.value+=n

def response(status=200,text='',headers=None):
    r=requests.Response();r.status_code=status;r._content=text.encode();r.encoding='utf-8';r._content_consumed=True;r.headers.update(headers or {})
    return r

def governor(tmp_path,clock,**kw):
    return RequestGovernor(tmp_path/'traffic.json',clock=clock.now,sleep=clock.sleep,**kw)

def test_spacing_and_restart_preserve_next_request(tmp_path):
    c=Clock();g=governor(tmp_path,c);times=[]
    def get(*a,**k):times.append(c.now());return response()
    g.request(get,'https://example.test',kind='article')
    assert not c.waits
    g=governor(tmp_path,c)
    g.request(get,'https://example.test',kind='catalog')
    assert times==[100000.,100015.]

def test_batch_pause_is_global_and_persistent(tmp_path):
    c=Clock();g=governor(tmp_path,c)
    for _ in range(60):g.request(lambda *a,**k:response(),'x')
    before=c.now();g=governor(tmp_path,c)
    g.request(lambda *a,**k:response(),'x')
    assert c.now()-before>=300

def test_retry_after_seconds_and_dates():
    assert retry_after_seconds('120',1000)==120
    assert retry_after_seconds(formatdate(1120,usegmt=True),1000)==120
    assert retry_after_seconds('-2',1000) is None
    assert retry_after_seconds('bad',1000) is None

def test_rate_limit_waits_before_one_retry_and_persists(tmp_path):
    c=Clock();g=governor(tmp_path,c);times=[];events=[]
    def get(*a,**k):
        times.append(c.now());return response(429,headers={'Retry-After':'7200'}) if len(times)==1 else response()
    g.on_progress=lambda event,**kw:events.append(event)
    assert g.request(get,'x').status_code==200
    assert times[1]-times[0]>=7200
    assert 'traffic_wait' in events
    assert json.loads(g.path.read_text())['requests_total']==2

def test_repeated_rate_limit_exits_and_restart_still_waits(tmp_path):
    c=Clock();g=governor(tmp_path,c);calls=[]
    def get(*a,**k):calls.append(c.now());return response(429)
    with pytest.raises(TrafficStopped,match='rate_limited'):g.request(get,'x')
    assert len(calls)==2 and calls[1]-calls[0]>=3600
    g=governor(tmp_path,c)
    assert g.state['cooldown_until']-c.now()>=3600

def test_wait_can_be_stopped_before_network(tmp_path):
    c=Clock();g=governor(tmp_path,c,should_stop=lambda:c.now()>=100002)
    g.defer('operator_reported_frequency',3600)
    with pytest.raises(TrafficStopped,match='paused_by_operator'):
        g.request(lambda *a,**k:pytest.fail('network during wait'),'x')
    assert c.now()<=100003
    assert g.state['cooldown_until']>c.now()

def test_restriction_detection_does_not_scan_normal_article_body():
    assert response_signal(response(200,'{"ret":-6,"errmsg":"操作频繁，请稍后再试"}'))=='rate_limited'
    assert response_signal(response(200,'{"ret":-6,"errmsg":"unknown"}'))=='unknown_business'
    assert response_signal(response(200,'<html><body>操作频繁，请稍后再试</body></html>'))=='rate_limited'
    assert response_signal(response(200,'<html><div id="js_content">操作频繁，请稍后再试</div></html>')) is None
    assert response_signal(response(200,'<html><body>请完成安全验证</body></html>'))=='needs_verification'

def test_unknown_business_and_verification_are_not_retried(tmp_path):
    c=Clock();g=governor(tmp_path,c);calls=[]
    def get(*a,**k):calls.append(1);return response(200,'{"ret":-6,"errmsg":"unknown"}')
    assert g.request(get,'x',kind='catalog').status_code==200 and len(calls)==1
    with pytest.raises(TrafficStopped,match='needs_verification'):
        g.request(lambda *a,**k:response(200,'<html>请完成安全验证</html>'),'x')

def test_timeout_backoff_and_success_never_go_below_floor(tmp_path):
    c=Clock();g=governor(tmp_path,c);calls=[]
    def get(*a,**k):
        calls.append(c.now())
        if len(calls)==1:raise requests.Timeout('secret-url')
        return response()
    g.request(get,'x')
    assert calls[1]-calls[0]>=60
    assert g.state['interval_seconds']>=30
    for _ in range(120):g.request(get,'x')
    assert g.state['interval_seconds']>=15
    assert 'secret' not in g.path.read_text()

def test_image_response_is_not_read_for_detection(tmp_path):
    c=Clock();g=governor(tmp_path,c)
    class Streaming:
        status_code=200;headers={}
        @property
        def text(self):raise AssertionError('do not load binary')
    assert g.request(lambda *a,**k:Streaming(),'x',kind='image',stream=True).status_code==200


def test_forbidden_http_is_not_refreshed_or_retried(tmp_path):
    c=Clock();g=governor(tmp_path,c);calls=[]
    def get(*a,**k):calls.append(1);return response(403)
    with pytest.raises(TrafficStopped,match='needs_attention'):g.request(get,'x')
    assert len(calls)==1 and g.state['cooldown_until']>c.now()

@pytest.mark.parametrize('kind',['home','article'])
def test_unknown_non_catalog_business_rejection_stops(tmp_path,kind):
    c=Clock();g=governor(tmp_path,c)
    with pytest.raises(TrafficStopped,match='needs_attention'):
        g.request(lambda *a,**k:response(200,'{"ret":-6,"errmsg":"unknown"}'),'x',kind=kind)


def test_long_verification_page_is_not_success():
    body='<html><script>'+(' '*20000)+'</script><div id="js_content"></div><p>请完成安全验证</p></html>'
    assert response_signal(response(200,body))=='needs_verification'
    assert response_signal(response(200,'<script>var cgiDataNew={};</script><p>操作繁忙</p>'))=='rate_limited'

def test_state_save_failure_is_fatal_not_media_io_error(tmp_path,monkeypatch):
    c=Clock();g=governor(tmp_path,c)
    def fail(*a,**k):raise PermissionError('SECRET')
    monkeypatch.setattr(type(g.path),'replace',fail)
    with pytest.raises(TrafficStopped,match='traffic_state_write_failed'):
        g.request(lambda *a,**k:pytest.fail('no network without durable reservation'),'x')

def stream_response(clock,*,content=b'PNG',fail=False,mime='image/png'):
    class Stream:
        status_code=200;headers={'Content-Type':mime}
        def iter_content(self,chunk_size):
            clock.value+=40
            if fail:raise requests.ConnectionError('SECRET')
            yield content
        def close(self):pass
    return Stream()

def test_stream_completion_not_headers_controls_pacing(tmp_path):
    c=Clock();g=governor(tmp_path,c)
    r=g.request(lambda *a,**k:stream_response(c),'x',kind='image',stream=True)
    assert g.state['success_streak']==0
    assert list(r.iter_content(chunk_size=8192))==[b'PNG']
    r.finish_media(True)
    assert g.state['next_allowed_at']>=c.now()+15
    assert g.state['success_streak']==1

def test_stream_error_stops_and_backs_off(tmp_path):
    c=Clock();g=governor(tmp_path,c)
    r=g.request(lambda *a,**k:stream_response(c,fail=True),'x',kind='image',stream=True)
    with pytest.raises(TrafficStopped,match='needs_attention'):list(r.iter_content(chunk_size=8192))
    assert g.state['success_streak']==0 and g.state['cooldown_until']>=c.now()+60

def test_html_in_image_response_stops_even_with_wrong_mime(tmp_path):
    c=Clock();g=governor(tmp_path,c)
    r=g.request(lambda *a,**k:stream_response(c,content='<html>请完成安全验证</html>'.encode()),'x',kind='image',stream=True)
    with pytest.raises(TrafficStopped,match='needs_verification'):list(r.iter_content(chunk_size=8192))
    assert g.state['success_streak']==0


def test_429_save_failure_stops_before_next_request(tmp_path,monkeypatch):
    c=Clock();g=governor(tmp_path,c);calls=[];saves=[]
    original=type(g.path).replace
    def fail_second(path,target):
        saves.append(1)
        if len(saves)==2:raise PermissionError('SECRET')
        return original(path,target)
    monkeypatch.setattr(type(g.path),'replace',fail_second)
    def get(*a,**k):calls.append(c.now());return response(429,headers={'Retry-After':'7200'})
    with pytest.raises(TrafficStopped,match='traffic_state_write_failed'):g.request(get,'x')
    with pytest.raises(TrafficStopped):g.request(get,'y')
    assert len(calls)==1

def test_response_log_failure_cannot_skip_rate_limit(tmp_path):
    c=Clock();g=governor(tmp_path,c);calls=[]
    def log(event,**kw):
        if event=='network_response':raise PermissionError('SECRET')
    g.on_progress=log
    def get(*a,**k):calls.append(1);return response(429,headers={'Retry-After':'7200'})
    with pytest.raises(TrafficStopped,match='traffic_log_failed'):g.request(get,'x')
    with pytest.raises(TrafficStopped):g.request(get,'x')
    assert len(calls)==1
