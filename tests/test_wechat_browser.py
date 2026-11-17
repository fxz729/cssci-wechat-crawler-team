from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from crawl.history_runner import SessionUnavailable
from crawl.wechat_browser import WechatBrowserClient


@dataclass(frozen=True)
class CredentialStub:
    session_id: str = "desktop-01"
    backend_token: str = "backend-token"
    fingerprint: str = "fingerprint"
    cookie: str = (
        "slave_sid=backend-session; data_ticket=backend-ticket; "
        "appmsg_token=article-token; wap_sid2=article-session; "
        "pass_ticket=article-ticket; wxuin=123456"
    )
    user_agent: str = "synthetic-agent"


class PageStub:
    def __init__(self):
        self.calls = []
        self.url = "https://mp.weixin.qq.com/"

    def goto(self, url, **kwargs):
        self.url = url
        self.calls.append(("goto", url))

    def evaluate(self, _script, argument):
        self.calls.append((argument["operation"], argument))
        if argument["operation"] == "catalog":
            return {
                "http_status": 200,
                "payload": {
                    "base_resp": {"ret": 0},
                    "publish_page": {
                        "total_count": 1,
                        "publish_list": [{
                            "publish_info": {
                                "sent_info": {"time": 1700000000},
                                "appmsgex": [{
                                    "appmsgid": "123",
                                    "itemidx": 1,
                                    "title": "title",
                                    "link": "https://mp.weixin.qq.com/s/short-id",
                                }],
                            },
                        }],
                    },
                },
            }
        if argument["operation"] == "article":
            return {
                "http_status": 200,
                "url": "https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=article-sn",
                "html": "<div id='js_content'>article body</div>",
            }
        if argument["operation"] == "metrics":
            return {
                "http_status": 200,
                "ext_payload": {
                    "base_resp": {"ret": 0},
                    "appmsgstat": {
                        "read_num": 100,
                        "old_like_num": 10,
                        "like_num": 5,
                        "collect_num": 3,
                        "share_num": 2,
                    },
                },
                "comment_payload": {"base_resp": {"ret": 0}, "total_count": 0},
            }
        raise AssertionError(argument)


class ContextStub:
    def __init__(self, page):
        self.page = page
        self.injected_cookies = []

    def add_cookies(self, cookies):
        self.injected_cookies.extend(cookies)

    def cookies(self):
        return [
            *self.injected_cookies,
            {"name": "appmsg_token", "value": "fresh-article-token"},
        ]


class BrowserSessionStub:
    def __init__(self, *args, **kwargs):
        self.page = PageStub()
        self.context = ContextStub(self.page)
        self.closed = False

    def __enter__(self):
        return self

    def new_page(self):
        return self.page

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True


def test_browser_client_injects_cookie_and_fetches_catalog_article_and_metrics():
    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=BrowserSessionStub,
    )

    with client:
        items, total_count = client.fetch_publish_page(0, 5)
        final_url, html = client.fetch_article_page(items[0])
        snapshot = client.fetch_metrics(SimpleNamespace(
            mid="123",
            idx=1,
            sn="article-sn",
            canonical_url=final_url,
            comment_id="comment-id",
            req_id="request-id",
            title="title",
        ))

    assert total_count == 1
    assert items[0].mid == "123"
    assert "article body" in html
    assert snapshot.read_count == 100
    assert snapshot.like_count == 10
    assert snapshot.watching_count == 5
    assert snapshot.favorite_count == 3
    assert snapshot.share_count == 2
    assert snapshot.comment_count == 0
    assert {cookie["name"] for cookie in client._browser.context.injected_cookies} >= {
        "slave_sid", "data_ticket", "appmsg_token", "wap_sid2", "pass_ticket",
    }
    metrics_call = next(call for call in client._page.calls if call[0] == "metrics")
    assert metrics_call[1]["ext_query"]["appmsg_token"] == "fresh-article-token"
    assert metrics_call[1]["comment_query"]["appmsg_token"] == "fresh-article-token"


def test_browser_client_pauses_when_catalog_response_is_not_json():
    class InvalidCatalogPage(PageStub):
        def evaluate(self, script, argument):
            if argument["operation"] == "catalog":
                return {"http_status": 200, "invalid_payload": True}
            return super().evaluate(script, argument)

    class InvalidCatalogSession(BrowserSessionStub):
        def __init__(self, *args, **kwargs):
            self.page = InvalidCatalogPage()
            self.context = ContextStub(self.page)
            self.closed = False

    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=InvalidCatalogSession,
    )

    with client, pytest.raises(SessionUnavailable, match="invalid_browser_response"):
        client.fetch_publish_page(0, 5)


def test_browser_client_pauses_when_catalog_is_rejected():
    class RejectedCatalogPage(PageStub):
        def evaluate(self, script, argument):
            if argument["operation"] == "catalog":
                return {"http_status": 200, "payload": {"base_resp": {"ret": 1004}}}
            return super().evaluate(script, argument)

    class RejectedCatalogSession(BrowserSessionStub):
        def __init__(self, *args, **kwargs):
            self.page = RejectedCatalogPage()
            self.context = ContextStub(self.page)
            self.closed = False

    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=RejectedCatalogSession,
    )

    with client, pytest.raises(SessionUnavailable, match="backend_rejected"):
        client.fetch_publish_page(0, 5)


def test_browser_client_rejects_untrusted_article_url_before_page_evaluation():
    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=BrowserSessionStub,
    )
    article = SimpleNamespace(link="https://example.invalid/s/credential-sink")

    with client, pytest.raises(RuntimeError, match="untrusted article URL"):
        client.fetch_article_page(article)

    assert [call[0] for call in client._page.calls] == ["goto"]


def test_browser_client_pauses_on_rate_limited_article_request():
    class RateLimitedPage(PageStub):
        def evaluate(self, script, argument):
            if argument["operation"] == "article":
                return {"http_status": 429, "url": "https://mp.weixin.qq.com/s/short", "html": ""}
            return super().evaluate(script, argument)

    class RateLimitedSession(BrowserSessionStub):
        def __init__(self, *args, **kwargs):
            self.page = RateLimitedPage()
            self.context = ContextStub(self.page)
            self.closed = False

    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=RateLimitedSession,
    )

    with client, pytest.raises(SessionUnavailable, match="http_429"):
        client.fetch_article_page(SimpleNamespace(link="https://mp.weixin.qq.com/s/short"))


def test_browser_client_pauses_when_metrics_response_is_not_json():
    class InvalidMetricsPage(PageStub):
        def evaluate(self, script, argument):
            if argument["operation"] == "metrics":
                return {"http_status": 200, "invalid_payload": True}
            return super().evaluate(script, argument)

    class InvalidMetricsSession(BrowserSessionStub):
        def __init__(self, *args, **kwargs):
            self.page = InvalidMetricsPage()
            self.context = ContextStub(self.page)
            self.closed = False

    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=InvalidMetricsSession,
    )

    with client, pytest.raises(SessionUnavailable, match="invalid_browser_response"):
        client.fetch_metrics(SimpleNamespace(
            mid="123",
            idx=1,
            sn="article-sn",
            canonical_url="https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=article-sn",
            comment_id="",
            req_id="",
            title="title",
        ))


def test_browser_client_rejects_degraded_metric_response():
    class DegradedPage(PageStub):
        def evaluate(self, script, argument):
            if argument["operation"] == "metrics":
                return {
                    "http_status": 200,
                    "ext_payload": {"base_resp": {"ret": 0}},
                    "comment_payload": {},
                }
            return super().evaluate(script, argument)

    class DegradedSession(BrowserSessionStub):
        def __init__(self, *args, **kwargs):
            self.page = DegradedPage()
            self.context = ContextStub(self.page)
            self.closed = False

    client = WechatBrowserClient(
        CredentialStub(),
        "target-biz",
        browser_session_factory=DegradedSession,
    )

    with client, pytest.raises(RuntimeError, match="appmsgstat"):
        client.fetch_metrics(SimpleNamespace(
            mid="123",
            idx=1,
            sn="article-sn",
            canonical_url="https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=article-sn",
            comment_id="",
            req_id="",
            title="title",
        ))
