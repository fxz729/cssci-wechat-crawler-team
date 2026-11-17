"""Serial request pacing and durable, interruptible cooldowns; never stores URLs."""
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from types import SimpleNamespace
import json
import math
from pathlib import Path
import re
import time

import requests


class TrafficStopped(RuntimeError):
    """Operational stop that must not be swallowed by per-article/media recovery."""


def retry_after_seconds(value, now):
    if not isinstance(value,str):return None
    value=value.strip()
    if re.fullmatch(r"[0-9]{1,12}",value):return int(value)
    try:
        date=parsedate_to_datetime(value)
        if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
        return max(0,date.timestamp()-now)
    except (TypeError,ValueError,OverflowError):return None


class _VisibleErrorText(HTMLParser):
    def __init__(self):
        super().__init__();self.hidden=0;self.article_depth=0;self.article_content=False;self.visible=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if self.article_depth:self.article_depth+=1
        elif dict(attrs).get('id')=='js_content':self.article_depth=1
        if self.article_depth and tag in {'img','video','audio'}:self.article_content=True
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
        if self.article_depth:self.article_depth-=1
    def handle_data(self,data):
        if self.hidden:return
        if self.article_depth and data.strip():self.article_content=True
        self.visible.append(data)


def response_signal(response, *, inspect_body=True):
    if response.status_code==429:return 'rate_limited'
    if response.status_code in (502,503,504):return 'server_busy'
    if not inspect_body or response.status_code!=200:return None
    text=response.text
    message=text;unknown=False
    try:payload=json.loads(text)
    except (ValueError,TypeError):payload=None
    if isinstance(payload,dict):
        base=payload.get('base_resp')
        base=base if isinstance(base,dict) else {}
        ret=payload.get('ret',base.get('ret'))
        if type(ret) is not int or ret==0:return None
        message=payload.get('errmsg') or payload.get('err_msg') or base.get('err_msg') or ''
        if not isinstance(message,str):message=''
        unknown=True
    else:
        parser=_VisibleErrorText();parser.feed(text)
        # A marker in a script or an empty div is not proof of an article.
        if parser.article_content:return None
        message=' '.join(parser.visible)
    if any(term in message.lower() for term in ('操作频繁','操作过于频繁','操作太频繁','操作繁忙','访问过于频繁','请求过于频繁','too many requests','freq limit')):
        return 'rate_limited'
    if any(term in message for term in ('请完成安全验证','请输入验证码','完成验证后继续','环境异常')):
        return 'needs_verification'
    return 'unknown_business' if unknown else None


class RequestGovernor:
    # Conservative engineering defaults, NOT a platform-guaranteed safe rate.
    minimum_interval=15.
    batch_size=60
    batch_rest=300.
    restriction_rest=3600.

    def __init__(self,path,*,clock=time.time,sleep=time.sleep,should_stop=lambda:False,on_progress=lambda *a,**k:None):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.sleep=sleep;self.should_stop=should_stop;self.on_progress=on_progress;self.fatal=False
        self.state={'next_allowed_at':0.,'cooldown_until':0.,'interval_seconds':self.minimum_interval,
                    'requests_total':0,'since_break':0,'success_streak':0,'cooldown_reason':''}
        if self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            for key in self.state:
                if key=='cooldown_reason':continue
                value=raw.get(key,self.state[key])
                if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                    raise TrafficStopped('traffic_state_invalid')
                self.state[key]=value
            self.state['interval_seconds']=max(self.minimum_interval,self.state['interval_seconds'])
            self.state['cooldown_reason']=raw.get('cooldown_reason') if raw.get('cooldown_reason') in self._reasons else ''

    _reasons={'rate_limited','server_busy','network_error','unknown_business','catalog_business_rejected',
              'catalog_http_rejected','operator_reported_frequency','batch_rest','needs_verification','http_forbidden'}

    def save(self):
        tmp=self.path.with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            tmp.replace(self.path)
        except OSError:
            self.fatal=True
            raise TrafficStopped('traffic_state_write_failed') from None

    def emit(self,event,**fields):
        try:self.on_progress(event,**fields)
        except Exception:
            self.fatal=True
            raise TrafficStopped('traffic_log_failed') from None

    def check_stop(self):
        if self.fatal:raise TrafficStopped('traffic_state_write_failed')
        if self.should_stop():raise TrafficStopped('paused_by_operator')

    def wait(self,kind='request'):
        next_log=0.
        while True:
            self.check_stop()
            now=self.clock()
            until=max(self.state['next_allowed_at'],self.state['cooldown_until'])
            remaining=until-now
            if remaining<=0:return
            if now>=next_log:
                self.emit('traffic_wait',kind=kind,remaining_seconds=math.ceil(remaining),
                                 resume_at=until,reason=self.state['cooldown_reason'] if self.state['cooldown_until']>now else 'request_interval')
                next_log=now+30
            self.sleep(min(.5,remaining))

    def defer(self,reason,seconds):
        if reason not in self._reasons or not math.isfinite(seconds) or seconds<0:raise ValueError('invalid_traffic_delay')
        self.state['cooldown_until']=max(self.state['cooldown_until'],self.clock()+seconds)
        self.state['cooldown_reason']=reason
        self.save()
        self.emit('traffic_cooldown',reason=reason,resume_at=self.state['cooldown_until'],seconds=math.ceil(seconds))

    def before_navigation(self):
        self.wait('session_navigation')
        self.state['next_allowed_at']=self.clock()+self.state['interval_seconds']
        self.save()
        self.emit('traffic_navigation',note='client_subrequests_not_individually_controlled')

    def _after(self,elapsed,success):
        if elapsed*2>self.state['interval_seconds']:
            self.state['interval_seconds']=max(self.state['interval_seconds'],min(120.,elapsed*2))
        if success:
            self.state['success_streak']+=1
            if self.state['success_streak']>=50:
                self.state['interval_seconds']=max(self.minimum_interval,self.state['interval_seconds']*.9)
                self.state['success_streak']=0
        else:
            self.state['success_streak']=0
            self.state['interval_seconds']=min(120.,self.state['interval_seconds']*2)
        self.state['next_allowed_at']=self.clock()+self.state['interval_seconds']
        if self.state['since_break']>=self.batch_size:
            self.state['since_break']=0
            self.defer('batch_rest',self.batch_rest)
        self.save()

    def request(self,get,url,*,kind='article',**kwargs):
        if kind not in {'article','catalog','home','image'}:raise ValueError('invalid_request_kind')
        for attempt in range(2):
            self.wait(kind)
            started=self.clock()
            self.state['requests_total']+=1;self.state['since_break']+=1
            # Reserve before IO so crashes cannot create an immediate restart burst.
            self.state['next_allowed_at']=started+self.state['interval_seconds'];self.save()
            self.emit('network_request',kind=kind,request_number=self.state['requests_total'],attempt=attempt+1)
            try:
                response=get(url,**kwargs)
            except (requests.Timeout,requests.ConnectionError):
                self._after(self.clock()-started,False)
                self.defer('network_error',60*(2**attempt))
                if attempt:raise TrafficStopped('needs_attention') from None
                continue
            if kind=='image' and response.status_code==200 and kwargs.get('stream'):
                return GovernedMediaResponse(response,self,started)
            signal=response_signal(response,inspect_body=kind!='image')
            self.emit('network_response',kind=kind,http_status=response.status_code,
                             elapsed_seconds=round(self.clock()-started,3),signal=signal or 'none')
            self._after(self.clock()-started,response.status_code==200 and signal is None)
            if response.status_code==403:
                response.close();self.defer('http_forbidden',self.restriction_rest)
                raise TrafficStopped('needs_attention')
            if signal=='needs_verification':
                response.close();self.defer(signal,self.restriction_rest)
                raise TrafficStopped('needs_verification')
            if signal=='unknown_business':
                self.defer(signal,self.restriction_rest)
                if kind!='catalog':
                    response.close();raise TrafficStopped('needs_attention')
                return response  # Preserve original ret and failed cursor in the catalog parser.
            if signal not in {'rate_limited','server_busy'}:return response
            delay=retry_after_seconds(response.headers.get('Retry-After'),self.clock())
            fallback=self.restriction_rest if signal=='rate_limited' else 60*(2**attempt)
            self.defer(signal,max(self.state['interval_seconds'],delay if delay is not None else fallback))
            response.close()
            if attempt:raise TrafficStopped('rate_limited' if signal=='rate_limited' else 'needs_attention')
        raise AssertionError('unreachable')


class GovernedSession:
    """Delegate cookies and settings to an existing credential-bearing session."""
    def __init__(self,session,governor):self.session=session;self.governor=governor
    def __getattr__(self,name):return getattr(self.session,name)
    def get(self,url,**kwargs):
        params=kwargs.get('params') or {}
        kind='catalog' if params.get('action')=='getmsg' else 'home' if params else 'article'
        return self.governor.request(self.session.get,url,kind=kind,**kwargs)


class GovernedMediaResponse:
    """Finish pacing only after streamed media has been consumed and validated."""
    def __init__(self,response,governor,started):
        self.response=response;self.governor=governor;self.started=started;self.finished=False
    def __getattr__(self,name):return getattr(self.response,name)
    def finish_media(self,success):
        if self.finished:return
        self.finished=True
        g=self.governor
        g._after(g.clock()-self.started,success)
        g.emit('network_response',kind='image',http_status=self.status_code,
                      elapsed_seconds=round(g.clock()-self.started,3),signal='none' if success else 'media_incomplete')
    def iter_content(self,chunk_size=8192,**kwargs):
        prefix=bytearray()
        mime=self.headers.get('Content-Type','').split(';')[0].lower()
        try:
            for chunk in self.response.iter_content(chunk_size=chunk_size,**kwargs):
                self.governor.check_stop()
                prefix.extend(chunk[:max(0,16384-len(prefix))])
                start=bytes(prefix).lstrip().lower()
                if mime in {'text/html','application/xhtml+xml','application/json'} or start.startswith((b'<html',b'<!doctype html',b'{')):
                    text=bytes(prefix).decode('utf-8',errors='replace')
                    signal=response_signal(SimpleNamespace(status_code=200,text=text))
                    reason=signal if signal in {'rate_limited','needs_verification'} else 'unknown_business'
                    self.finish_media(False)
                    self.governor.defer(reason,self.governor.restriction_rest)
                    raise TrafficStopped(reason if reason in {'rate_limited','needs_verification'} else 'needs_attention')
                yield chunk
        except (requests.Timeout,requests.ConnectionError):
            self.finish_media(False)
            self.governor.defer('network_error',60)
            raise TrafficStopped('needs_attention') from None
    def close(self):
        try:self.finish_media(False)
        finally:self.response.close()
