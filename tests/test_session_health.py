"""Native, offline health tests; synthetic sessions never enter a proxy socket."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'capture/fiddler_session.js'
HEALTH='http://cssci-session.invalid/health'
VERSION='cssci-session-health-v1'


@pytest.fixture(scope='module')
def native_health(tmp_path_factory):
    compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/jsc.exe'
    fiddler=ROOT.parent/'Fiddler/Fiddler.exe'
    powershell=shutil.which('powershell.exe')
    if not compiler.exists() or not fiddler.exists() or not powershell:
        pytest.skip('local JScript.NET/Fiddler runtime unavailable')
    temp=tmp_path_factory.mktemp('session_health')
    dll=temp/'session.dll'
    result=subprocess.run([str(compiler),'/nologo','/target:library','/utf8output',
        f'/out:{dll}',f'/reference:{fiddler}',str(SOURCE)],capture_output=True,timeout=30)
    assert result.returncode==0,(result.stdout+result.stderr).decode('utf-8',errors='replace')
    assert b'warning JS' not in result.stdout
    def inspect(cases, candidate=None, bad_error=False, hold_gate=False):
        payload=base64.b64encode(json.dumps(dict(cases=cases,candidate=candidate,bad_error=bad_error,hold_gate=hold_gate)).encode()).decode()
        command=r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
[void][Reflection.Assembly]::LoadFrom('__FIDDLER__')
[void][Reflection.Assembly]::LoadFrom('__DLL__')
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))|ConvertFrom-Json
function Session($method,$url,$status) {
 $request=[Text.Encoding]::ASCII.GetBytes("$method $url HTTP/1.1`r`nHost: cssci-session.invalid`r`nCookie: PRIVATE_COOKIE`r`nUser-Agent: PRIVATE_UA`r`n`r`n")
 $response=[Text.Encoding]::ASCII.GetBytes("HTTP/1.1 $status TEST`r`nContent-Length: 0`r`n`r`n")
 $s=[Fiddler.Session]::new($request,$response)
 $s.fullUrl=$url
 # Preserve the wire Host too; fullUrl's setter normalizes an explicit :80.
 $s.oRequest.headers['Host']=([regex]::Match($url,'^https?://([^/]+)')).Groups[1].Value
 $s['x-processinfo']="powershell:$PID"
 return $s
}
if($null -ne $p.candidate) {
 foreach($c in $p.candidate) { [CssciSessionCapture]::Record((Session $c.method $c.url $c.status)) }
}
if($p.bad_error) { [CssciSessionCapture]::LastError='PRIVATE_EXCEPTION https://example.invalid/?key=PRIVATE_KEY' }
if($p.hold_gate) {
 Add-Type @"
using System;using System.Threading;
public class HeldGate {
 public static void Start(Object gate) {
  var ready=new ManualResetEvent(false);
  var thread=new Thread(()=>{lock(gate){ready.Set();Thread.Sleep(3000);}});
  thread.IsBackground=true;thread.Start();ready.WaitOne();
 }
}
"@
 [HeldGate]::Start([CssciSessionCapture]::Gate)
}
$results=@(foreach($case in $p.cases) {
 $s=Session $case.method $case.url 204
 # Match the OnBeforeRequest lifecycle; byte constructors default to Done.
 $s.state=[Fiddler.SessionStates]::AutoTamperRequestBefore
 $watch=[Diagnostics.Stopwatch]::StartNew()
 $handled=[CssciSessionCapture]::HandleHealth($s)
 $watch.Stop()
 $body=[Text.Encoding]::UTF8.GetString($s.responseBodyBytes)
 @{elapsed_ms=$watch.ElapsedMilliseconds;handled=$handled;status=$s.responseCode;body=$body;content_type=$s.oResponse.headers['Content-Type'];cache=$s.oResponse.headers['Cache-Control'];bypassed=$s.isFlagSet([Fiddler.SessionFlags]::ResponseGeneratedByFiddler)}
})
ConvertTo-Json -InputObject $results -Depth 8 -Compress
'''.replace('__FIDDLER__',str(fiddler).replace("'","''")).replace('__DLL__',str(dll).replace("'","''")).replace('__PAYLOAD__',payload)
        encoded=base64.b64encode(command.encode('utf-16le')).decode()
        result=subprocess.run([powershell,'-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                              capture_output=True,timeout=20,creationflags=0x08000000)
        assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
        assert not result.stderr
        return json.loads(result.stdout.decode('utf-8-sig'))
    return inspect


def test_health_strict_scope_and_local_bypass(native_health):
    cases=[{'method':'GET','url':HEALTH}]
    cases += [{'method':method,'url':url} for method,url in [
        ('POST',HEALTH),('HEAD',HEALTH),('GET',HEALTH+'?x=1'),('GET',HEALTH+'/'),
        ('GET',HEALTH+'#fragment'),('GET',HEALTH.replace('http:','https:')),
        ('GET',HEALTH.replace('.invalid/','.invalid.evil/')),
        ('GET',HEALTH.replace('/health','/Health')),
        ('GET',HEALTH.replace('.invalid/','.invalid:80/')),
        ('GET',HEALTH.replace('cssci-session','user@cssci-session')),
    ]]
    rows=native_health(cases)
    assert [row['handled'] for row in rows]==[True]+[False]*(len(cases)-1)
    first=rows[0]
    assert first['status']==200 and first['bypassed']
    assert first['content_type']=='application/json; charset=utf-8'
    assert first['cache']=='no-store'
    assert json.loads(first['body'])=={'version':VERSION,'last_error':'','last_candidate':None}
    assert all(row['status']==204 and not row['bypassed'] for row in rows[1:])


@pytest.mark.parametrize('path,method,status,kind,code',[
    ('/mp/profile_ext?action=home','GET',403,'home','http_status_rejected'),
    ('/mp/profile_ext?action=urlcheck','GET',200,'urlcheck','process_unavailable'),
    ('/mp/urlcheck?scene=1','POST',200,'urlcheck','method_rejected'),
    ('/mp/profile_ext?action=getmsg','GET',200,'unknown','action_rejected'),
    ('/mp/profile_ext?action=home&action=urlcheck','GET',200,'unknown','query_rejected'),
])
def test_candidate_diagnostics_are_fixed_and_secret_free(native_health,path,method,status,kind,code):
    candidates=[{'url':'https://mp.weixin.qq.com'+path+'&key=PRIVATE_KEY&__biz=PRIVATE_BIZ',
                 'method':method,'status':status},
                {'url':'https://example.invalid/private?key=OTHER_SECRET','method':'GET','status':500}]
    row=native_health([{'url':HEALTH,'method':'GET'}],candidates)[0]
    body=json.loads(row['body'])
    assert set(body)=={'version','last_error','last_candidate'}
    assert body['version']==VERSION and body['last_error']==code
    c=body['last_candidate']
    assert set(c)=={'kind','http_status','process_id','process_name','captured_at'}
    assert c['kind']==kind and c['http_status']==status
    # The local reference assembly exposes LocalProcessID=0 for byte sessions.
    assert c['process_id']==0
    assert c['process_name'] in ('','powershell')
    assert isinstance(c['captured_at'],(float,int)) and c['captured_at']>0
    assert all(secret not in row['body'] for secret in ('PRIVATE','OTHER_SECRET','https://','Cookie','sessionparams'))


def test_unrelated_traffic_leaves_diagnostics_empty(native_health):
    candidates=[{'url':u,'method':'GET','status':200} for u in (
        'https://mp.weixin.qq.com/s?key=PRIVATE',
        'https://mp.weixin.qq.com.evil/mp/profile_ext?action=home',
        'http://mp.weixin.qq.com/mp/profile_ext?action=home',HEALTH)]
    body=json.loads(native_health([{'url':HEALTH,'method':'GET'}],candidates)[0]['body'])
    assert body['last_candidate'] is None and body['last_error']==''


def test_health_never_exports_arbitrary_exception_text(native_health):
    body=json.loads(native_health([{'url':HEALTH,'method':'GET'}],bad_error=True)[0]['body'])
    assert body['last_error']=='capture_rejected'
    assert 'PRIVATE' not in json.dumps(body)


def test_health_does_not_wait_on_or_acquire_capture_gate(native_health):
    row=native_health([{'url':HEALTH,'method':'GET'}],hold_gate=True)[0]
    assert row['handled'] and row['status']==200
    assert row['elapsed_ms']<1000
    body=SOURCE.read_text(encoding='utf-8').split('static function HandleHealth',1)[1].split('static function Record(',1)[0]
    assert 'Monitor.Enter(Gate)' not in body and 'Monitor.TryEnter(Gate)' not in body
