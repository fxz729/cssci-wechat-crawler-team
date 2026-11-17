from types import SimpleNamespace

import pytest

from crawl.wechat_requests import WechatRequestsClient


class CredentialStub:
    cookie = "appmsg_token=initial-token; pass_ticket=ticket"
    backend_token = "backend-token"
    fingerprint = "fingerprint"
    user_agent = "synthetic-agent"


class ResponseStub:
    url = "https://mp.weixin.qq.com/s?__biz=target-biz&mid=1&idx=1&sn=sn"
    text = "<div id='js_content'>body</div>"

    def raise_for_status(self):
        return None


class SessionStub:
    def __init__(self):
        self.trust_env = True
        self.closed = False
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append(url)
        return ResponseStub()

    def close(self):
        self.closed = True


class BackendStub:
    @classmethod
    def from_session_credential(cls, credential, fakeid, *, session):
        instance = cls()
        instance.session = session
        instance.fakeid = fakeid
        return instance

    def fetch_publish_page(self, begin, count):
        return ([SimpleNamespace(link="https://mp.weixin.qq.com/s/short")], 1)


class ProviderStub:
    def __init__(self, **kwargs):
        self.session = kwargs["session"]

    def fetch(self, article):
        return "snapshot"


def test_requests_client_exposes_common_history_transport_interface():
    session = SessionStub()
    client = WechatRequestsClient(
        CredentialStub(),
        "target-biz",
        session_factory=lambda: session,
        backend_client_factory=BackendStub,
        provider_factory=ProviderStub,
    )

    with client:
        items, total = client.fetch_publish_page(0, 5)
        final_url, html = client.fetch_article_page(items[0])
        snapshot = client.fetch_metrics(SimpleNamespace(mid="1"))

    assert total == 1
    assert final_url == ResponseStub.url
    assert "body" in html
    assert snapshot == "snapshot"
    assert session.trust_env is False
    assert session.closed is True


def test_requests_client_rejects_untrusted_article_url_before_request():
    session = SessionStub()
    client = WechatRequestsClient(
        CredentialStub(),
        "target-biz",
        session_factory=lambda: session,
        backend_client_factory=BackendStub,
        provider_factory=ProviderStub,
    )

    with client, pytest.raises(RuntimeError, match="untrusted article URL"):
        client.fetch_article_page(SimpleNamespace(link="https://example.invalid/s/secret"))

    assert session.calls == []
