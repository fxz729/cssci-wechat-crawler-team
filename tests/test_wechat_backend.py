import json
from datetime import timezone

import pytest

from crawl.history_runner import SessionUnavailable
from metrics.providers.wechat_backend import WechatBackendClient, parse_publish_page


def test_parse_publish_page_extracts_article_items():
    publish_info = {
        "msgid": 1000000636,
        "sent_info": {"time": 1781094520},
        "appmsg_info": [{"appmsgid": 2247489783, "itemidx": 1}],
        "appmsgex": [{
            "aid": "2247489783_1",
            "appmsgid": 2247489783,
            "itemidx": 1,
            "title": "标题",
            "author_name": "北京大学教育评论",
            "digest": "摘要",
            "cover": "https://mmbiz.qpic.cn/a.jpg",
            "link": "https://mp.weixin.qq.com/s?__biz=biz&mid=2247489783&idx=1&sn=abc",
            "create_time": 1781094518,
        }],
    }
    payload = {
        "total_count": 1,
        "publish_list": [{"publish_type": 101, "publish_info": json.dumps(publish_info, ensure_ascii=False)}],
    }

    items = parse_publish_page(json.dumps(payload, ensure_ascii=False))

    assert len(items) == 1
    assert items[0].title == "标题"
    assert items[0].author == "北京大学教育评论"
    assert items[0].mid == "2247489783"
    assert items[0].idx == 1
    assert items[0].digest == "摘要"
    assert items[0].cover_url == "https://mmbiz.qpic.cn/a.jpg"
    assert items[0].publish_time.tzinfo == timezone.utc


def test_backend_client_reads_total_count_from_nested_publish_page():
    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "base_resp": {"ret": 0},
                "publish_page": json.dumps({"total_count": 2395, "publish_list": []}),
            }

    class SessionStub:
        def get(self, url, *, params, headers, timeout):
            return ResponseStub()

    client = WechatBackendClient("private-cookie", "private-token", "biz", session=SessionStub())

    items, total_count = client.fetch_publish_page()

    assert items == []
    assert total_count == 2395


def test_backend_client_pauses_when_response_is_not_json():
    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("private login page")

    class SessionStub:
        def get(self, url, *, params, headers, timeout):
            return ResponseStub()

    client = WechatBackendClient("private-cookie", "private-token", "biz", session=SessionStub())

    with pytest.raises(SessionUnavailable, match="invalid_backend_response"):
        client.fetch_publish_items()


def test_backend_client_sanitizes_error_response():
    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"base_resp": {"ret": 1004, "token": "private-token"}}

    class SessionStub:
        def get(self, url, *, params, headers, timeout):
            return ResponseStub()

    client = WechatBackendClient("private-cookie", "private-token", "biz", session=SessionStub())

    with pytest.raises(SessionUnavailable) as exc_info:
        client.fetch_publish_items()

    assert str(exc_info.value) == "backend_rejected"
