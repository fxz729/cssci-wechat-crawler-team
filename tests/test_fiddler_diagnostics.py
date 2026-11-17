"""Executable contract tests for the data-only Fiddler response diagnostic."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JSC = Path(r'C:\Windows\Microsoft.NET\Framework\v4.0.30319\jsc.exe')
FIDDLER = ROOT.parent / 'Fiddler' / 'Fiddler.exe'


@pytest.fixture(scope='module')
def inspect_bodies(tmp_path_factory):
    assert (ROOT / 'capture' / 'fiddler_diagnostics.js').exists(), 'diagnostic not implemented'
    if not JSC.exists() or not FIDDLER.exists() or not shutil.which('powershell'):
        pytest.skip('Fiddler Classic JScript runtime is only available on the Windows host')
    temp = tmp_path_factory.mktemp('fiddler_diagnostic')
    dll = temp / 'diagnostic.dll'
    cmd = [str(JSC), '/nologo', '/target:library', f'/out:{dll}',
           f'/lib:{FIDDLER.parent}', f'/reference:{FIDDLER}',
           '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
           str(ROOT / 'capture' / 'fiddler_diagnostics.js'), str(ROOT / 'capture' / 'fiddler_classic.js')]
    compile_result = subprocess.run(cmd, capture_output=True, text=True)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    ps = temp / 'inspect.ps1'
    ps.write_text('''param($Fiddler,$Dll,$Payload,$Mode)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
[void][Reflection.Assembly]::LoadFrom($Fiddler)
[void][Reflection.Assembly]::LoadFrom($Dll)
$body=[IO.File]::ReadAllText($Payload)
if($Mode -eq 'bootstrap' -or $Mode -eq 'guarded_server'){
    try{
        if($Mode -eq 'guarded_server'){$result=[CssciDiagnostics]::ServerInspection($body)}
        else{
            # Parse synthetic bytes only; never start Fiddler or execute a request.
            $request=[Text.Encoding]::ASCII.GetBytes("GET /s HTTP/1.1`r`nHost: example.invalid`r`n`r`n")
            $response=[Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Length: 0`r`n`r`n")
            $session=[Fiddler.Session]::new($request,$response)
            $session['x-processinfo']='wechatappex:1234'
            $article=@{schema_version=1;captured_at='2026-09-06T00:00:00+00:00';source='fiddler_classic';endpoint='/s';biz='offline-biz';mid='123';idx=1;sn='offline-sn';event_id='offline'}
            $result=[CssciDiagnostics]::BootstrapEvent($session,$body,$article)
        }
        [Fiddler.WebFormats.JSON]::JsonEncode($result)
    }catch{[Fiddler.WebFormats.JSON]::JsonEncode(@{error=$_.Exception.GetBaseException().Message})}
    exit
}
if($Mode -eq 'article_meta'){[Fiddler.WebFormats.JSON]::JsonEncode([CssciCapture]::ArticleMetadata($body,'offline-biz','123',1,'offline-sn'))}elseif($Mode -eq 'identity'){[Fiddler.WebFormats.JSON]::JsonEncode(@{biz=[CssciCapture]::JS($body,'biz');mid=[CssciCapture]::JS($body,'mid')})}elseif($Mode -eq 'server'){[Fiddler.WebFormats.JSON]::JsonEncode([CssciDiagnostics]::ServerInspection($body))}elseif($Mode -eq 'html'){[Fiddler.WebFormats.JSON]::JsonEncode([CssciDiagnostics]::BootstrapFields($body))}else{[CssciDiagnostics]::InspectBody($body)}
''', encoding='utf-8-sig')

    def inspect(payload, mode="body"):
        input_path = temp / 'input.json'
        input_path.write_text(payload if mode in ("html", "identity", "article_meta", "raw", "server", "bootstrap", "guarded_server") else json.dumps(payload), encoding='utf-8')
        p = subprocess.run(['powershell', '-NoProfile', '-File', str(ps),
                            str(FIDDLER), str(dll), str(input_path), mode],
                           capture_output=True, encoding='utf-8')
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    return inspect


def test_business_failure_and_missing_metrics(inspect_bodies):
    result = inspect_bodies({'ret': -3, 'errmsg': 'invalid session'})
    assert result['fields']['$.ret']['value'] == -3
    assert '$.appmsgstat.read_num' not in result['fields']


def test_exact_paths_zero_and_flags_are_not_counts(inspect_bodies):
    result = inspect_bodies({'base_resp': {'ret': 0}, 'appmsgstat': {
        'read_num': 120, 'old_like_num': 0, 'like_num': 4, 'share_num': 8,
        'collect_num': 6, 'is_star': 1}, 'comment_count': 2})
    f = result['fields']
    assert f['$.appmsgstat.old_like_num']['value'] == 0
    assert f['$.appmsgstat.collect_num']['value'] == 6
    assert f['$.appmsgstat.is_star']['type'] == 'number'
    assert 'value' not in f['$.appmsgstat.is_star']
    assert f['$.comment_count']['value'] == 2


def test_sensitive_values_and_comments_never_leave_inspector(inspect_bodies):
    result = inspect_bodies({'token': 'SECRET_MARKER', 'uin': 9988776655,
        'comments': [{'content': 'PRIVATE_COMMENT', 'openid': 'OPENID_MARKER'}],
        'nested': {'key': 'PRIVATE_KEY'}})
    text = json.dumps(result)
    for secret in ['SECRET_MARKER', '9988776655', 'PRIVATE_COMMENT', 'OPENID_MARKER', 'PRIVATE_KEY']:
        assert secret not in text


def test_nested_json_string_preserves_path(inspect_bodies):
    result = inspect_bodies({'data': json.dumps({'stats': {'read_num': 42}})})
    assert result['fields']['$.data<json>.stats.read_num']['value'] == 42


def test_absent_ret_never_fabricates_success(inspect_bodies):
    result = inspect_bodies({'appmsgstat': {'read_num': 1}})
    assert '$.ret' not in result['fields']
    assert '$.base_resp.ret' not in result['fields']

def test_empty_response_is_not_successful_json(inspect_bodies):
    # JSON null is distinguishable from a response with a business status.
    result=inspect_bodies(None)
    assert not any(p.endswith('.ret') for p in result['fields'])


def test_html_bootstrap_is_fields_not_uia(inspect_bodies):
    html="""<script>window.appmsg_bar_data = {old_like_count: "9" * 1,
    like_count: "4" * 1, share_count: "46" * 1, collect_count: "6" * 1,
    comment_count: "0" * 1}; var cgiData = {read_num: "546" * 1};</script>"""
    f=inspect_bodies(html, "html")
    assert f['$html.appmsg_bar_data.collect_count'][0]['value']==6
    assert f['$html.appmsg_bar_data.comment_count'][0]['value']==0
    assert f['$html.cgiData.read_num'][0]['value']==546


def test_html_empty_and_negative_do_not_become_zero(inspect_bodies):
    f=inspect_bodies('<script>window.appmsg_bar_data={collect_count:"" * 1, comment_count:"-1" * 1};</script>', "html")
    assert 'value' not in f['$html.appmsg_bar_data.collect_count'][0]
    assert f['$html.appmsg_bar_data.comment_count'][0]['value']==-1


def test_html_no_semicolon_and_does_not_scan_comment_example(inspect_bodies):
    html='''<script>// window.appmsg_bar_data = {collect_count:"900" * 1};
window.appmsg_bar_data = {collect_count:"6" * 1}
</script>'''
    f=inspect_bodies(html, "html")
    assert [v['value'] for v in f['$html.appmsg_bar_data.collect_count']]==[6]


def test_native_article_identity_empty_or_literal_is_not_filtered_out(inspect_bodies):
    data=inspect_bodies('<script>var biz="" || "Mzk3NTE3NTU0Ng==";var mid=""||"2247487163";</script>', "identity")
    assert data=={'biz':'Mzk3NTE3NTU0Ng==','mid':'2247487163'}


def test_malformed_json_does_not_turn_into_success(inspect_bodies):
    data=inspect_bodies('not json', 'raw')
    assert data['format']=='not_json'
    assert not data['fields']


def test_article_metadata_prefers_server_fields_for_author_and_account(inspect_bodies):
    html = """<script>window.cgiDataNew={author:'张三',nick_name:'测试公众号',desc:'服务端摘要',title:'服务端标题',cdn_url:'https://mmbiz.qpic.cn/mmbiz_jpg/x/0?wx_fmt=jpeg',create_timestamp:'1770000000',link:'https://mp.weixin.qq.com/s?__biz=offline-biz&mid=123&idx=1&sn=offline-sn'};</script>"""
    result = inspect_bodies(html, "article_meta")
    assert result["author"] == "张三"
    assert result["account_name"] == "测试公众号"
    assert result["digest"] == "服务端摘要"
    assert result["title"] == "服务端标题"
    assert result["cover_url"].startswith("https://mmbiz.qpic.cn/")


def test_server_inspection_exposes_safe_selected_scalars(inspect_bodies):
    html = """<script>window.cgiDataNew={author:'张三',nick_name:'测试公众号',desc:'服务端摘要',title:'服务端标题',create_timestamp:'1770000000',link:'https://mp.weixin.qq.com/s?__biz=offline-biz&mid=123&idx=1&sn=offline-sn'};</script>"""
    result = inspect_bodies(html, "server")
    assert result["selected"]["author"] == "张三"
    assert result["selected"]["nick_name"] == "测试公众号"
    assert result["selected"]["desc"] == "服务端摘要"


def test_article_metadata_reads_author_from_article_dom_when_server_value_is_empty(inspect_bodies):
    html = """<span id='js_author_name'>DOM作者</span><script>window.cgiDataNew={nick_name:'测试公众号',title:'标题',link:'https://mp.weixin.qq.com/s?__biz=offline-biz&mid=123&idx=1&sn=offline-sn'};</script>"""
    result = inspect_bodies(html, "article_meta")
    assert result["author"] == "DOM作者"


def test_cgidatanew_literal_data_and_gallery_are_not_javascript_execution(inspect_bodies):
    html='''<script>window.cgiDataNew={base_resp:{ret:"0"*1,wxtoken:"SECRET"},
    appmsg_bar_data:{read_num:"80"*1,collect_count:"0"*1},desc:"picture caption",
    picture_page_info_list:[{cdn_url:"https://mmbiz.qpic.cn/safe/0?wx_fmt=png",width:"300"*1,},],};</script>'''
    result=inspect_bodies(html,"server")
    assert result['parsed'] is True
    assert result['fields']['$.cgiDataNew.appmsg_bar_data.read_num']['value']==80
    assert result['fields']['$.cgiDataNew.base_resp.ret']['value']==0
    assert 'mmbiz.qpic.cn/safe/0' in result['gallery_html']
    assert 'SECRET' not in json.dumps(result)


def test_cgidatanew_refuses_dynamic_expressions(inspect_bodies):
    result=inspect_bodies('<script>window.cgiDataNew={x:eval("side effect"),appmsg_bar_data:{read_num:999}};</script>',"server")
    assert result['parsed'] is False


@pytest.mark.parametrize('assignments', [
    'window.cgiDataNew={base_resp:{ret:0}};window.cgiDataNew={base_resp:{ret:-3}};',
    'window.cgiDataNew={};</script><script>window.cgiDataNew={};',
    'window.cgiDataNew={};window.cgiDataNew={};',
    'window.cgiDataNew={};window.cgiDataNew=loadServer();',
    'window.cgiDataNew=loadServer();window.cgiDataNew={};',
    'window.cgiDataNew={};window /* gap */ . cgiDataNew = null;',
])
def test_cgidatanew_duplicate_assignments_fail_closed(inspect_bodies, assignments):
    result=inspect_bodies('<script>'+assignments+'</script>', 'guarded_server')
    assert result == {'error': 'duplicate_cgidatanew_assignment'}


def test_cgidatanew_assignment_examples_and_comparisons_are_not_duplicates(inspect_bodies):
    html='''<script>
    // window.cgiDataNew={base_resp:{ret:-3}};
    /* window.cgiDataNew={}; */
    var example="window.cgiDataNew={};";
    var template=`window.cgiDataNew={};`;
    window.cgiDataNew={base_resp:{ret:0}};
    if(window.cgiDataNew == null || window.cgiDataNew === null){}
    </script>'''
    result=inspect_bodies(html, 'guarded_server')
    assert result['parsed'] is True
    assert result['fields']['$.cgiDataNew.base_resp.ret']['value'] == 0


def bootstrap_html(server):
    assignment='' if server is None else 'window.cgiDataNew='+json.dumps(server)+';'
    return ('<script>var biz="offline-biz";var mid="123";var idx="1";var sn="offline-sn";'
            'var cgiData={read_num:"12"*1};'+assignment+'</script>')


@pytest.mark.parametrize('ret', [None, True, False, '', 'SECRET_INVALID_RET', '0x0',
                                 '1.5', 1.5, {}, [], [0], 2147483648, '-2147483649'])
def test_bootstrap_invalid_present_business_ret_is_rejected(inspect_bodies, ret):
    result=inspect_bodies(bootstrap_html({'base_resp': {'ret': ret}}), 'bootstrap')
    assert result == {'error': 'invalid_cgidatanew_business_ret'}


@pytest.mark.parametrize('ret', [0, '0', -3, '-3', 2147483647, -2147483648])
def test_bootstrap_valid_business_ret_is_preserved(inspect_bodies, ret):
    result=inspect_bodies(bootstrap_html({'base_resp': {'ret': ret}}), 'bootstrap')
    assert result['business_ret'] == int(ret)
    assert result['bootstrap_fields']['$html.cgiData.read_num'][0]['raw_value'] == '12'


@pytest.mark.parametrize('server', [None, {}, {'base_resp': {}}, {'base_resp': None}])
def test_bootstrap_absent_business_ret_remains_compatible(inspect_bodies, server):
    result=inspect_bodies(bootstrap_html(server), 'bootstrap')
    assert result['kind'] == 'metrics'
    assert 'business_ret' not in result


def test_bootstrap_duplicate_server_objects_never_emit_metrics(inspect_bodies):
    html=bootstrap_html({'base_resp': {'ret': 0}}).replace(
        '</script>', 'window.cgiDataNew={base_resp:{ret:-3}};</script>')
    assert inspect_bodies(html, 'bootstrap') == {'error': 'duplicate_cgidatanew_assignment'}
