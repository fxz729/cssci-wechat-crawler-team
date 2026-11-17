"""Synthetic-only tests for the captured-home-session experiment."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from crawl.profile_session_probe import (
    ProbeError, read_home_request, collect_catalog, parse_server_data, article_snapshot,
)

BIZ = "target-biz"
SN = "a" * 32
URL = f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=1&idx=1&sn={SN}"


def request_text():
    return ("GET https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=target-biz"
            "&scene=124&uin=TEST_UIN&key=TEST_KEY&pass_ticket=TEST_TICKET HTTP/1.1\n"
            "Host: mp.weixin.qq.com\nUser-Agent: test-client\nCookie: sid=TEST_COOKIE\n\n"
            "HTTP/1.1 200 OK\nSet-Cookie: ignored=RESPONSE_SECRET\n\n")


def group(mid):
    return {"comm_msg_info": {"id": mid, "datetime": 1700000000},
            "app_msg_ext_info": {"title": f"Article {mid}", "digest": "Digest", "author": "Author",
                "content_url": URL.replace("mid=1&", f"mid={mid}&"), "cover": "", "multi_app_msg_item_list": []}}


def page(groups, next_offset, flag=0):
    return {"ret": 0, "errmsg": "ok", "can_msg_continue": flag,
            "next_offset": next_offset, "general_msg_list": json.dumps({"list": groups})}


class Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.pages[len(self.calls) - 1]
        return SimpleNamespace(status_code=200, content=json.dumps(payload).encode(), json=lambda: payload)


def test_request_headers_stop_before_response_and_repr_hides_credentials():
    template = read_home_request(request_text())
    assert template.biz == BIZ
    assert template.headers == {"User-Agent": "test-client", "Cookie": "sid=TEST_COOKIE"}
    assert "TEST_" not in repr(template)
    assert "RESPONSE_SECRET" not in repr(template.headers)


@pytest.mark.parametrize("text", [request_text().replace("mp.weixin.qq.com/mp", "evil.invalid/mp"),
                                  request_text().replace("action=home", "action=getmsg"),
                                  request_text().replace(" HTTP/1.1", "&__biz=other HTTP/1.1", 1),
                                  request_text().replace("GET https:", "GET http:")])
def test_invalid_home_request_is_rejected(text):
    with pytest.raises(ProbeError):
        read_home_request(text)


def test_false_continue_flag_does_not_stop_and_server_cursor_is_followed():
    session = Session([page([group(1)], 11), page([group(2)], 12), page([], 12)])
    out = collect_catalog(session, read_home_request(request_text()), max_pages=5, sleep=lambda _: None)
    assert [p["offset"] for p in out["pages"]] == [0, 11, 12]
    assert out["pagination_complete"] is True
    assert out["catalog_complete"] is False
    assert len(out["articles"]) == 2
    assert out["stop_reason"] == "empty_page"
    assert "TEST_" not in json.dumps(out)
    for _, options in session.calls:
        assert options["allow_redirects"] is False
        assert "verify" not in options
        assert options["params"]["key"] == "TEST_KEY"


def test_nonzero_ret_stops_without_losing_success_or_echoing_secrets():
    session = Session([page([group(1)], 10), {"ret": -3, "errmsg": "TEST_KEY"}])
    out = collect_catalog(session, read_home_request(request_text()), max_pages=5, sleep=lambda _: None)
    assert out["stop_reason"] == "business_rejected"
    assert len(out["articles"]) == 1
    assert "TEST_KEY" not in json.dumps(out)


def test_repeated_identity_is_bounded_and_never_completed():
    session = Session([page([group(1)], 10), page([group(1)], 20)])
    out = collect_catalog(session, read_home_request(request_text()), sleep=lambda _: None)
    assert out["stop_reason"] == "no_new_articles"
    assert out["pagination_complete"] is False


def test_cross_biz_is_not_imported():
    item = group(1)
    item["app_msg_ext_info"]["content_url"] = URL.replace(BIZ, "other")
    session = Session([page([item], 10)])
    out = collect_catalog(session, read_home_request(request_text()), sleep=lambda _: None)
    assert out["stop_reason"] == "unresolved_identity"
    assert out["articles"] == []


def server_html(**changes):
    data = {"base_resp": {"ret": "0"}, "link": URL, "title": "Test title", "author": "Author",
            "nick_name": "Target journal", "desc": "Digest", "content_noencode": "<p>Article text</p>",
            "create_time": "1700000000", "comment_id": "123",
            "user_info": {"appmsg_bar_data": {"read_num": "10", "old_like_count": "2", "like_count": "3",
                                               "share_count": "4", "collect_count": "0", "comment_count": "0"}}}
    data.update(changes)
    return "<script>window.cgiDataNew = " + json.dumps(data) + "; if (window.cgiDataNew == null) {}</script>"


def test_literal_parser_does_not_confuse_comparison_or_suffix_fields():
    html = server_html()
    result = article_snapshot(html, URL)
    assert result["metrics"]["like_count"] == 2
    assert result["metrics"]["watching_count"] == 3
    assert result["metrics"]["favorite_count"] == 0
    assert result["six_metrics_present"] is True
    assert result["identity_matches"] is True
    assert result["business_ret"] == 0


def test_js_literal_syntax_accepted_without_evaluation():
    data = parse_server_data("<script>window.cgiDataNew={base_resp:{ret:'0'},x:'\\u4e2d\\x41',a:2*1,};</script>")
    assert data["x"] == "中A" and data["a"] == 2


@pytest.mark.parametrize("body", ["<script>window.cgiDataNew=fetch('secret')</script>",
                                  "<script>window.cgiDataNew={a:process.exit(1)}</script>",
                                  "<script>window.cgiDataNew={};window.cgiDataNew={};</script>"])
def test_expressions_and_multiple_assignments_are_rejected(body):
    with pytest.raises(ProbeError):
        parse_server_data(body)


def test_missing_metric_stays_null():
    body = server_html(user_info={"appmsg_bar_data": {"collect_count": "0"}})
    result = article_snapshot(body, URL)
    assert result["metrics"]["favorite_count"] == 0
    assert result["metrics"]["read_count"] is None
    assert result["six_metrics_present"] is False


def test_conflicting_article_response_is_rejected():
    with pytest.raises(ProbeError):
        article_snapshot(server_html(link=URL.replace("mid=1&", "mid=2&")), URL)

def test_multi_article_groups_expand_into_separate_identity_rows():
    item = group(1)
    item["app_msg_ext_info"]["multi_app_msg_item_list"] = [
        {"content_url": URL.replace("idx=1&", "idx=2&"), "title": "Second item", "author": "", "digest": ""}]
    session = Session([page([item], 1), page([], 1)])
    result = collect_catalog(session, read_home_request(request_text()), sleep=lambda _: None)
    assert [a["idx"] for a in result["articles"]] == [1, 2]
    assert result["pagination_complete"] is True
    assert result["catalog_complete"] is False


def test_exported_zero_missing_and_body_files_are_distinct(tmp_path):
    import csv
    from crawl.profile_session_probe import export_samples
    sample = article_snapshot(server_html(user_info={"appmsg_bar_data": {"collect_count": "0"}}), URL)
    assert export_samples([sample], tmp_path) == 1
    file = tmp_path / "文章数据.csv"
    assert file.read_bytes().startswith(b"\xef\xbb\xbf")
    with file.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["favorite_count"] == "0"
    assert row["read_count"] == ""
    assert row["content"] == "Article text"
    assert row["metrics_status"] == "partial"
    assert (tmp_path / row["local_text"]).read_text(encoding="utf-8") == row["content"]


def test_catalog_response_error_is_not_coerced_to_success():
    session = Session([{"ret": False, "general_msg_list": '{"list":[]}'}])
    result = collect_catalog(session, read_home_request(request_text()), sleep=lambda _: None)
    assert result["stop_reason"] == "network_or_format_error"
    assert result["pagination_complete"] is False

def test_empty_placeholder_does_not_block_later_articles_or_next_page():
    empty = {"comm_msg_info": {"id": 99}, "app_msg_ext_info": {"title": "", "content_url": "", "content": "", "multi_app_msg_item_list": []}}
    session = Session([page([empty, group(1)], 11), page([], 11)])
    result = collect_catalog(session, read_home_request(request_text()), sleep=lambda _: None)
    assert result["pagination_complete"] is True
    assert len(result["articles"]) == 1
    assert result["pages"][0]["placeholder_count"] == 1
    assert result["catalog_complete"] is False


def test_explicit_start_offset_resumes_without_returning_to_zero():
    session = Session([page([group(1)], 431), page([], 431)])
    result = collect_catalog(session, read_home_request(request_text()), start_offset=420, sleep=lambda _: None)
    assert [p["offset"] for p in result["pages"]] == [420, 431]

def test_video_snapshot_keeps_public_video_id_but_not_signed_download_credentials():
    info={'video_id':'wxv_123','mp_video_trans_info':[{'url':'https://mpvideo.qpic.cn/a.mp4?auth_key=SECRET','width':'720','height':'1280','filesize':'100'}], 'ckey':'PRIVATE_VIDEO_KEY'}
    out=article_snapshot(server_html(type='9',content_noencode='',video_page_info=info),URL)
    assert out['article_type']=='9'
    assert out['media_assets']==[{'kind':'video','id':'wxv_123'}]
    assert 'SECRET' not in json.dumps(out) and 'PRIVATE_VIDEO_KEY' not in json.dumps(out)




def test_bundle_idempotent_initializers_do_not_conflict_with_article_literal():
    noop='<script>window.cgiDataNew=window.cgiDataNew||{}; window.cgiDataNew=window.cgiDataNew||{};</script>'
    for html in (server_html()+noop,noop+server_html()):
        assert article_snapshot(html,URL)['six_metrics_present'] is True


@pytest.mark.parametrize('assignment',[
    'window.cgiDataNew=window.other||{};',
    'window.cgiDataNew=window.cgiDataNew||{}+evil();',
    'window.cgiDataNew=window.cgiDataNew||evil();',
    'window.cgiDataNew={different:true};',
])
def test_initializer_exception_does_not_allow_other_assignments(assignment):
    with pytest.raises(ProbeError,match='ambiguous_server_data'):
        parse_server_data(server_html()+'<script>'+assignment+'</script>')


def test_initializer_without_article_data_is_not_a_valid_payload():
    with pytest.raises(ProbeError,match='missing_server_data'):
        parse_server_data('<script>window.cgiDataNew=window.cgiDataNew||{};</script>')


def test_picture_post_retains_body_gallery_order_not_share_cover():
    first='https://mmbiz.qpic.cn/mmbiz_jpg/TEST/0?wx_fmt=jpeg'
    second='https://mmbiz.qpic.cn/mmbiz_png/TEST2/0?wx_fmt=png'
    html=server_html(type='9',content_noencode='',picture_page_info_list=[
        {'cdn_url':first,'share_cover':{'cdn_url':'https://example.invalid/not-body.jpg'}},
        {'cdn_url':second},
    ])
    result=article_snapshot(html,URL)
    assert result['content_html']==''
    assert result['media_assets']==[{'kind':'image','url':first},{'kind':'image','url':second}]
    assert result['six_metrics_present'] is True


def test_malformed_picture_entries_are_not_media():
    result=article_snapshot(server_html(picture_page_info_list=[None,{},'not-an-image',{'cdn_url':{}},{'cdn_url':''}]),URL)
    assert result['media_assets']==[]
