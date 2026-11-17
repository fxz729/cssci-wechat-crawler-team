"""Runtime checks for output-boundary safety in the actual JScript.NET observer."""
import json,subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture(scope='module')
def probe(tmp_path_factory):
    temp=tmp_path_factory.mktemp('capture_safety');dll=temp/'capture.dll';fiddler=ROOT.parent/'Fiddler'/'Fiddler.exe'
    jsc=Path(r'C:\Windows\Microsoft.NET\Framework\v4.0.30319\jsc.exe')
    if not jsc.exists() or not fiddler.exists():pytest.skip('Windows Fiddler runtime required')
    p=subprocess.run([str(jsc),'/nologo','/target:library',f'/out:{dll}',f'/lib:{fiddler.parent}',f'/reference:{fiddler}','/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll',str(ROOT/'capture/fiddler_classic.js'),str(ROOT/'capture/fiddler_diagnostics.js')],capture_output=True,text=True)
    assert p.returncode==0,p.stdout
    ps=temp/'probe.ps1';ps.write_text('''param($Fiddler,$Dll,$InputFile,$Mode)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
[void][Reflection.Assembly]::LoadFrom($Fiddler);[void][Reflection.Assembly]::LoadFrom($Dll)
$inputText=[IO.File]::ReadAllText($InputFile)
if($Mode -eq 'url'){@{url=[CssciCapture]::PublicUrl($inputText)}|ConvertTo-Json -Compress}
else{$x=[CssciCapture]::ArticleContent($inputText,'BIZ==','123','1','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');@{present=($null -ne $x);data=$x}|ConvertTo-Json -Depth 8 -Compress}
''',encoding='utf-8-sig')
    def call(text,mode):
        source=temp/'input.txt';source.write_text(text,encoding='utf-8')
        r=subprocess.run(['powershell','-NoProfile','-File',str(ps),str(fiddler),str(dll),str(source),mode],capture_output=True,encoding='utf-8')
        assert r.returncode==0,r.stderr
        return json.loads(r.stdout)
    return call


def gallery(mid='123',placeholder=''):
    return placeholder+f'''<script>window.cgiDataNew={{link:"https://mp.weixin.qq.com/s?__biz=BIZ%3D%3D&mid={mid}&idx=1&sn=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",desc:"caption",picture_page_info_list:[{{cdn_url:"https://mmbiz.qpic.cn/original/0?wx_fmt=png"}}]}};</script>'''


def test_secret_query_names_are_filtered_before_output(probe):
    result=probe('https://mmbiz.qpic.cn/a?cookie=SECRET1&authorization=SECRET2&u%69n=SECRET3&wx_fmt=png#SECRET4','url')['url']
    assert result=='https://mmbiz.qpic.cn/a?wx_fmt=png'
    assert probe('https://user:SECRET@mmbiz.qpic.cn/a','url')['url']==''


def test_gallery_identity_conflict_cannot_create_article_content(probe):
    assert not probe(gallery(mid='999'),'content')['present']


def test_empty_content_falls_back_to_verified_gallery(probe):
    result=probe(gallery(placeholder='<div id="js_content"><p> </p></div>'),'content')
    assert result['present']
    assert 'mmbiz.qpic.cn/original/0' in result['data']['content_html']
    assert result['data']['content_origin']=='cgiDataNew.picture_page_info_list'


def test_article_fragment_redacts_encoded_secret_query_values(probe):
    result=probe('<div id="js_content"><img src="https://mmbiz.qpic.cn/a?%63ookie=SECRET&amp;wx_fmt=png"></div>','content')
    assert result['present']
    assert 'SECRET' not in result['data']['content_html']
