from pathlib import Path
from openpyxl import load_workbook
import pytest
from export.journal_bundle import write_journal_workbook


def test_workbook_links_resolve_to_original_files_and_counts_keep_zero(tmp_path):
    article=tmp_path/'articles'/'123_1';(article/'images').mkdir(parents=True)
    (article/'article.html').write_text('<p>ok</p>',encoding='utf-8')
    (article/'article.txt').write_text('ok',encoding='utf-8')
    (article/'capture_evidence.json').write_text('{}',encoding='utf-8')
    (article/'images'/'a.jpg').write_bytes(b'original')
    rows=[{'title':'测试文章','journal_name':'测试期刊','biz':'BIZ','mid':'123','idx':1,'sn':'sn',
           'article_url':'https://mp.weixin.qq.com/s?mid=123','cover_url':'https://mmbiz.qpic.cn/cover',
           'comment_id':'comment-1','business_ret':0,'http_status':200,'fiddler_session_id':9,
           'content_origin':'html_fragment','comment_count':0,'favorite_count':6,
           'local_html':'articles/123_1/article.html','local_text':'articles/123_1/article.txt',
           'local_article_dir':'articles/123_1','capture_evidence':'articles/123_1/capture_evidence.json'}]
    media=[{'mid':'123','idx':1,'kind':'image','status':'downloaded','local_path':'articles/123_1/images/a.jpg','bytes':8,'sha256':'abc'}]
    path=tmp_path/'文章索引.xlsx'
    write_journal_workbook(rows,media,path,bundle_root=tmp_path)
    wb=load_workbook(path);ws=wb['文章索引'];headers={c.value:c.column for c in ws[1]}
    assert ws.cell(2,headers['评论数']).value==0
    assert ws.cell(2,headers['收藏数']).value==6
    assert ws.cell(2,headers['文章URL']).value=='https://mp.weixin.qq.com/s?mid=123'
    assert ws.cell(2,headers['封面图URL']).value=='https://mmbiz.qpic.cn/cover'
    assert ws.cell(2,headers['comment_id']).value=='comment-1'
    assert ws.cell(2,headers['业务返回码']).value==0
    assert ws.cell(2,headers['HTTP状态']).value==200
    assert (path.parent/ws.cell(2,headers['采集证据']).hyperlink.target).exists()
    assert ws.cell(2,headers['mid']).data_type=='s'
    assert (path.parent/ws.cell(2,headers['正文HTML']).hyperlink.target).exists()
    ms=wb['媒体清单'];mh={c.value:c.column for c in ms[1]}
    assert (path.parent/ms.cell(2,mh['原文件']).hyperlink.target).read_bytes()==b'original'


def test_workbook_rejects_paths_outside_bundle(tmp_path):
    root=tmp_path/'bundle';root.mkdir();outside=tmp_path/'outside.txt';outside.write_text('x')
    with pytest.raises(ValueError,match='outside_bundle'):
        write_journal_workbook([{'mid':'1','idx':1,'local_text':'../outside.txt'}],[],root/'index.xlsx',bundle_root=root)


def test_untrusted_text_is_not_excel_formula_and_missing_is_not_zero(tmp_path):
    p=tmp_path/'index.xlsx'
    write_journal_workbook([{'title':'=HYPERLINK("http://evil","x")','mid':'123','idx':1,'read_count':None}],[],p,bundle_root=tmp_path)
    ws=load_workbook(p)['文章索引'];headers={c.value:c.column for c in ws[1]}
    assert ws.cell(2,headers['文章标题']).data_type=='s'
    assert ws.cell(2,headers['阅读数']).value is None


@pytest.fixture
def asset_transport(monkeypatch):
    import socket
    from unittest.mock import Mock
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Real network access is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ])
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    body = b"\x89PNG\r\n\x1a\n" + b"original image"
    response = Mock(status_code=200, headers={"Content-Type": "image/png"})
    response.iter_content.side_effect = lambda **kwargs: iter([body])
    ordinary_get = Mock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session, "get", ordinary_get)
    return ordinary_get, response, body


def test_fetch_asset_requester_runs_only_for_actual_gets(tmp_path, asset_transport):
    from unittest.mock import Mock
    from crawl.bundle_media import fetch_asset

    ordinary_get, response, body = asset_transport
    asset = {"kind": "image", "url": "https://mmbiz.qpic.cn/body.png"}
    def request(get, url, *, kind, **kwargs):
        assert kind == "image"
        return get(url, **kwargs)
    requester = Mock(side_effect=request)
    first, events = fetch_asset(asset, tmp_path, requester=requester)
    cached, cached_events = fetch_asset(asset, tmp_path, requester=requester)
    assert first["status"] == "downloaded"
    assert cached["status"] == "skipped"
    assert (tmp_path / first["local_path"]).read_bytes() == body
    requester.assert_called_once_with(ordinary_get, asset["url"], kind="image",
                                    stream=True, allow_redirects=False, timeout=(10, 30))
    ordinary_get.assert_called_once_with(asset["url"], stream=True, allow_redirects=False, timeout=(10, 30))
    assert len(events) == 1 and events[0]["http_status"] == 200
    assert cached_events == []
    response.close.assert_called_once()


def test_fetch_asset_traffic_stopped_propagates(tmp_path, asset_transport):
    from unittest.mock import Mock
    from crawl.bundle_media import fetch_asset
    from crawl.request_governor import TrafficStopped

    ordinary_get, _, _ = asset_transport
    stopped = TrafficStopped("traffic stopped")
    requester = Mock(side_effect=stopped)
    with pytest.raises(TrafficStopped) as exc:
        fetch_asset({"kind": "image", "url": "https://mmbiz.qpic.cn/body.png"},
                    tmp_path, requester=requester)
    assert exc.value is stopped
    requester.assert_called_once()
    ordinary_get.assert_not_called()
