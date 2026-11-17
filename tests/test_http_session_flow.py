from dataclasses import dataclass
from datetime import datetime, timezone

from metrics.providers.wechat_authorized import WechatAuthorizedProvider
from metrics.providers.wechat_backend import WechatBackendClient


@dataclass(frozen=True)
class ArticleStub:
    mid: str = "123"
    idx: int = 1
    sn: str = "article-sn"
    canonical_url: str = "https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=article-sn"
    publish_time: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)
    comment_id: str = "comment-id"
    req_id: str = "request-id"
    title: str = "article title"
    raw_html_path: str = ""


class CookieStub:
    def get_dict(self):
        return {"appmsg_token": "fresh-token", "wap_sid2": "fresh-wap"}


class ResponseStub:
    def __init__(self, payload=None, *, cookies=None, url=""):
        self._payload = payload or {}
        self.cookies = cookies or CookieStub()
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class SessionStub:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append(("GET", url, params, headers, timeout))
        if url.endswith("/s"):
            return ResponseStub(cookies=CookieStub(), url="https://mp.weixin.qq.com/s?refreshed=1")
        assert url.endswith("/mp/appmsg_comment")
        return ResponseStub({"base_resp": {"ret": 0}, "total_count": 0}, cookies=EmptyCookieStub())

    def post(self, url, *, params, data, headers, timeout):
        self.calls.append(("POST", url, params, data, headers, timeout))
        return ResponseStub({"base_resp": {"ret": 0}, "appmsgstat": {"read_num": 1}}, cookies=EmptyCookieStub())


class EmptyCookieStub:
    def get_dict(self):
        return {}


def test_article_metrics_refreshes_session_then_uses_fixed_verified_request_shape():
    session = SessionStub()
    provider = WechatAuthorizedProvider(
        cookie="appmsg_token=stale-token; pass_ticket=captured-ticket",
        appmsg_token="stale-token",
        session=session,
    )

    snapshot = provider.fetch(ArticleStub())

    assert snapshot.read_count == 1
    page_call, ext_call, comment_call = session.calls
    assert page_call[0:2] == ("GET", "https://mp.weixin.qq.com/s")
    assert page_call[2]["uin"] == "777"
    assert page_call[2]["key"] == "777"
    assert page_call[2]["pass_ticket"] == ""
    assert ext_call[0:2] == ("POST", "https://mp.weixin.qq.com/mp/getappmsgext")
    assert ext_call[2]["uin"] == "777"
    assert ext_call[2]["key"] == "777"
    assert ext_call[2]["pass_ticket"] == ""
    assert ext_call[3]["pass_ticket"] == ""
    assert ext_call[2]["clientversion"] == "f2541b36"
    assert ext_call[2]["version"] == "f2541b36"
    assert ext_call[2]["appmsg_token"] == "fresh-token"
    assert "appmsg_token=fresh-token" in ext_call[4]["Cookie"]
    assert "pass_ticket=captured-ticket" in ext_call[4]["Cookie"]
    assert comment_call[2]["uin"] == "777"
    assert comment_call[2]["key"] == "777"
    assert comment_call[2]["pass_ticket"] == ""
    assert provider.appmsg_token == "stale-token"


def test_backend_client_uses_supplied_session_and_session_credential():
    class BackendSession:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, headers, timeout):
            self.calls.append((url, params, headers, timeout))
            return ResponseStub({"base_resp": {"ret": 0}, "publish_page": "{}"}, cookies=EmptyCookieStub())

    class Credential:
        backend_token = "backend-token"
        fingerprint = "fingerprint"
        cookie = "session-cookie"
        user_agent = "session-agent"

    session = BackendSession()
    client = WechatBackendClient.from_session_credential(Credential(), "target-biz", session=session)

    assert client.fetch_publish_items() == []
    assert session.calls[0][1]["token"] == "backend-token"
    assert session.calls[0][1]["fingerprint"] == "fingerprint"
    assert session.calls[0][2]["Cookie"] == "session-cookie"
