from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
import requests

from crawl.history_runner import SessionUnavailable
from metrics.providers.base import MetricsSnapshot
from metrics.providers.public_html import PublicHtmlProvider
from metrics.providers.wechat_authorized import WechatAuthorizedProvider, parse_article_ext_metrics, parse_backend_article_metrics


def test_public_html_provider_extracts_available_inline_metrics():
    html = "<script>var read_num = 3269; var like_num = 32; var old_like_num = 6;</script>"

    snapshot = PublicHtmlProvider().fetch_from_html(html)

    assert isinstance(snapshot, MetricsSnapshot)
    assert snapshot.provider == "public-html"
    assert snapshot.read_count == 3269
    assert snapshot.like_count == 6
    assert snapshot.watching_count == 32
    assert snapshot.is_authorized_source is False


def test_wechat_authorized_provider_parses_backend_article_metrics():
    payload = {
        "base_resp": {"ret": 0, "token": "must-not-be-kept"},
        "article_list": [
            {
                "msg_id": "2247488874",
                "item_idx": 1,
                "title": "教育评价悖论：表征、成因与化解",
                "ref_date": "2025-04-11",
                "total_read_uv": "3269",
                "like_num": "32",
                "old_like_num": "6",
                "share_num": "355",
                "comment_count": "0",
                "collect_num": "12",
                "identity_name": "private-name",
                "nested": {"appmsg_token": "private-token"},
            }
        ],
    }

    snapshots = parse_backend_article_metrics(payload)

    assert list(snapshots.keys()) == [("2247488874", 1)]
    snapshot = snapshots[("2247488874", 1)]
    assert snapshot.provider == "wechat-authorized"
    assert snapshot.provider_priority == 100
    assert snapshot.read_count == 3269
    assert snapshot.like_count == 6
    assert snapshot.watching_count == 32
    assert snapshot.share_count == 355
    assert snapshot.comment_count == 0
    assert snapshot.favorite_count == 12
    assert snapshot.is_authorized_source is True
    assert "base_resp" not in snapshot.raw_payload
    assert "private-name" not in str(snapshot.raw_payload)
    assert "private-token" not in str(snapshot.raw_payload)


def test_wechat_authorized_provider_raises_sanitized_network_error():
    class BrokenSession:
        def get(self, url, params, headers, timeout, **kwargs):
            raise RuntimeError(f"boom {url}?token={params['token']}&cookie=secret")

    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        mp_token="secret-token",
        fakeid="fakeid",
        session=BrokenSession(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        provider.fetch_for_range(datetime(2025, 4, 11, tzinfo=timezone.utc), datetime(2025, 4, 12, tzinfo=timezone.utc))

    message = str(exc_info.value)
    assert "secret" not in message
    assert "token=" not in message


def test_wechat_authorized_provider_redacts_missing_config_message(monkeypatch):
    monkeypatch.delenv("WECHAT_COOKIE", raising=False)
    monkeypatch.delenv("WECHAT_APPMSG_TOKEN", raising=False)

    provider = WechatAuthorizedProvider.from_env()

    assert provider.is_configured is False
    assert "WECHAT_COOKIE" in provider.status_message
    assert "cookie" not in provider.redacted_config.lower()


def test_wechat_authorized_provider_requires_mp_backend_token_for_fetch(monkeypatch):
    monkeypatch.setenv("WECHAT_COOKIE", "secret-cookie")
    monkeypatch.setenv("WECHAT_APPMSG_TOKEN", "secret-token")
    monkeypatch.delenv("WECHAT_MP_TOKEN", raising=False)
    monkeypatch.delenv("WECHAT_FAKEID", raising=False)
    provider = WechatAuthorizedProvider.from_env()

    with pytest.raises(RuntimeError, match="公众号后台指标回填"):
        provider.fetch_for_range(datetime(2025, 4, 11, tzinfo=timezone.utc), datetime(2025, 4, 12, tzinfo=timezone.utc))


def test_wechat_authorized_provider_preserves_encoded_appmsg_token(monkeypatch):
    monkeypatch.setenv("WECHAT_COOKIE", "secret-cookie")
    monkeypatch.setenv("WECHAT_APPMSG_TOKEN", "token%2Bwith%2Fslash%3D%3D")
    monkeypatch.setenv("WECHAT_PASS_TICKET", "ticket%3D%2B%2F")
    monkeypatch.setenv("WECHAT_UIN", "uin%3D%3D")
    monkeypatch.setenv("WECHAT_KEY", "key%2Bvalue%3D")

    provider = WechatAuthorizedProvider.from_env()

    assert provider.appmsg_token == "token%2Bwith%2Fslash%3D%3D"
    assert provider.pass_ticket == "ticket=+/"
    assert provider.uin == "uin=="
    assert provider.key == "key+value="


@dataclass(frozen=True)
class ArticleStub:
    mid: str
    idx: int
    publish_time: datetime
    sn: str = "sn"
    canonical_url: str = "https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=sn"
    raw_html_path: str = ""


def test_wechat_authorized_provider_pauses_on_rate_limited_metrics_request():
    class ResponseStub:
        url = "https://mp.weixin.qq.com/s?refreshed=1"
        cookies = type("CookieStub", (), {"get_dict": lambda self: {}})()

        def raise_for_status(self):
            response = type("ErrorResponse", (), {"status_code": 429})()
            raise requests.HTTPError(response=response)

    class SessionStub:
        def get(self, *args, **kwargs):
            return ResponseStub()

    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        appmsg_token="article-token",
        session=SessionStub(),
    )

    with pytest.raises(SessionUnavailable, match="http_429"):
        provider.fetch(ArticleStub("2247488874", 1, datetime(2025, 4, 11, tzinfo=timezone.utc)))


def test_wechat_authorized_provider_rejects_degraded_article_ext_response():
    class ResponseStub:
        url = "https://mp.weixin.qq.com/s?refreshed=1"
        cookies = type("CookieStub", (), {"get_dict": lambda self: {}})()

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "base_resp": {"ret": 0},
                "advertisement_info": [],
                "more_read_list": [],
                "sec_control": {},
            }

    class SessionStub:
        def get(self, url, params, headers, timeout, **kwargs):
            assert url.endswith("/s")
            return ResponseStub()

        def post(self, url, params, data, headers, timeout, **kwargs):
            return ResponseStub()

    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        appmsg_token="article-token",
        session=SessionStub(),
    )

    with pytest.raises(RuntimeError, match="未返回 appmsgstat"):
        provider.fetch(ArticleStub("2247488874", 1, datetime(2025, 4, 11, tzinfo=timezone.utc)))


def test_wechat_authorized_provider_fetches_article_ext_and_comment_metrics(tmp_path):
    article_html = tmp_path / "article.html"
    article_html.write_text("<script>var comment_id = '123456';</script>", encoding="utf-8")

    class ResponseStub:
        def __init__(self, payload):
            self.payload = payload
            self.cookies = type("CookieStub", (), {"get_dict": lambda self: {}})()
            self.url = "https://mp.weixin.qq.com/s?refreshed=1"

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class SessionStub:
        def __init__(self):
            self.calls = []

        def post(self, url, params, data, headers, timeout, **kwargs):
            self.calls.append((url, {**params, **data}))
            if url.endswith("/mp/getappmsgext"):
                return ResponseStub({
                    "base_resp": {"ret": 0},
                    "appmsgstat": {
                        "read_num": "3269",
                        "like_num": "32",
                        "old_like_num": "6",
                        "share_num": "355",
                        "favorite_num": "12",
                    },
                    "comment_count": "3",
                })
            raise AssertionError(url)

        def get(self, url, params, headers, timeout, **kwargs):
            self.calls.append((url, params))
            if url.endswith("/s"):
                return ResponseStub({})
            if url.endswith("/mp/appmsg_comment"):
                return ResponseStub({"base_resp": {"ret": 0}, "elected_comment_total_cnt": "5"})
            raise AssertionError(url)

    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        appmsg_token="article-token",
        uin="test-uin",
        key="test-key",
        session=SessionStub(),
    )
    article = ArticleStub(
        mid="2247488874",
        idx=1,
        canonical_url="https://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&mid=2247488874&idx=1&sn=abc",
        raw_html_path=str(article_html),
        publish_time=datetime(2025, 4, 11, tzinfo=timezone.utc),
    )

    snapshot = provider.fetch(article)

    assert snapshot.read_count == 3269
    assert snapshot.like_count == 6
    assert snapshot.watching_count == 32
    assert snapshot.favorite_count == 12
    assert snapshot.share_count == 355
    assert snapshot.comment_count == 5
    assert snapshot.is_authorized_source is True
    urls = [url for url, _params in provider.session.calls]
    assert "https://mp.weixin.qq.com/s" in urls
    assert "https://mp.weixin.qq.com/mp/getappmsgext" in urls
    assert "https://mp.weixin.qq.com/mp/appmsg_comment" in urls
    ext_params = provider.session.calls[1][1]
    assert ext_params["__biz"] == "MzI3MDE3Nzg0OA=="
    assert ext_params["mid"] == "2247488874"
    assert ext_params["idx"] == "1"
    assert ext_params["appmsg_token"] == "article-token"
    assert ext_params["uin"] == "777"
    assert ext_params["key"] == "777"


def test_parse_article_ext_metrics_preserves_collect_album_and_zero_comments():
    snapshot = parse_article_ext_metrics(
        {
            "base_resp": {"ret": 0},
            "appmsgstat": {
                "read_num": "3146",
                "like_num": "23",
                "old_like_num": "35",
                "share_num": "132",
                "collect_num": "8",
                "star_num": "3",
                "friend_seen_info": {
                    "user_info": {
                        "openid": "secret-openid",
                        "nick_name": "private-nickname",
                    }
                },
            },
            "appmsg_album_extinfo": {
                "album_id_str": "album-123",
                "album_name": "专题合集",
            },
        },
        {"base_resp": {"ret": 0}, "elected_comment_total_cnt": "0", "total_count": "1"},
    )

    assert snapshot.like_count == 35
    assert snapshot.watching_count == 23
    assert snapshot.favorite_count == 8
    assert snapshot.collect_num == 8
    assert snapshot.comment_count == 1
    assert snapshot.raw_payload["appmsgext"]["_album_id"] == "album-123"
    assert snapshot.raw_payload["appmsgext"]["_album_name"] == "专题合集"
    assert "secret-openid" not in str(snapshot.raw_payload)
    assert "private-nickname" not in str(snapshot.raw_payload)


def test_wechat_authorized_provider_uses_article_comment_id_without_raw_html():
    class ResponseStub:
        def __init__(self, payload):
            self.payload = payload
            self.cookies = type("CookieStub", (), {"get_dict": lambda self: {}})()
            self.url = "https://mp.weixin.qq.com/s?refreshed=1"

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class SessionStub:
        def __init__(self):
            self.calls = []

        def post(self, url, params, data, headers, timeout, **kwargs):
            self.calls.append((url, {**params, **data}))
            return ResponseStub({"base_resp": {"ret": 0}, "appmsgstat": {"read_num": "1"}})

        def get(self, url, params, headers, timeout, **kwargs):
            self.calls.append((url, params))
            if url.endswith("/s"):
                return ResponseStub({})
            return ResponseStub({"base_resp": {"ret": 0}, "elected_comment_total_cnt": "2"})

    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        appmsg_token="article-token",
        uin="test-uin",
        key="test-key",
        session=SessionStub(),
    )
    class ArticleWithComment:
        mid = "2247488874"
        idx = 1
        sn = "abc"
        canonical_url = "https://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&mid=2247488874&idx=1&sn=abc"
        publish_time = datetime(2025, 4, 11, tzinfo=timezone.utc)
        raw_html_path = ""
        comment_id = "comment-from-parser"

    snapshot = provider.fetch(ArticleWithComment())

    assert snapshot.comment_count == 2
    comment_call = provider.session.calls[2][1]
    assert comment_call["comment_id"] == "comment-from-parser"


def test_wechat_authorized_provider_refreshes_article_session_before_metrics():
    class CookieStub:
        def get_dict(self):
            return {
                "appmsg_token": "fresh%2Farticle%2Btoken",
                "wap_sid2": "fresh-wap-session",
            }

    class ResponseStub:
        def __init__(self, payload=None, cookies=None, url=""):
            self.payload = payload or {}
            self.cookies = cookies or CookieStubEmpty()
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class CookieStubEmpty:
        def get_dict(self):
            return {}

    class SessionStub:
        def __init__(self):
            self.calls = []

        def get(self, url, params, headers, timeout, **kwargs):
            self.calls.append(("GET", url, params, headers))
            if url.endswith("/s"):
                return ResponseStub(
                    cookies=CookieStub(),
                    url="https://mp.weixin.qq.com/s?refreshed=1",
                )
            if url.endswith("/mp/appmsg_comment"):
                return ResponseStub({"base_resp": {"ret": 0}, "total_count": 0})
            raise AssertionError(url)

        def post(self, url, params, data, headers, timeout, **kwargs):
            self.calls.append(("POST", url, params, headers))
            assert params["appmsg_token"] == "fresh%2Farticle%2Btoken"
            assert "appmsg_token=fresh%2Farticle%2Btoken" in headers["Cookie"]
            assert "wap_sid2=fresh-wap-session" in headers["Cookie"]
            assert headers["Referer"] == "https://mp.weixin.qq.com/s?refreshed=1"
            return ResponseStub({"base_resp": {"ret": 0}, "appmsgstat": {"read_num": 1}})

    provider = WechatAuthorizedProvider(
        cookie="appmsg_token=stale-token; wap_sid2=stale-session",
        appmsg_token="stale-token",
        pass_ticket="ticket",
        uin="uin",
        key="key",
        session=SessionStub(),
    )
    article = ArticleStub(
        "2247488874",
        1,
        datetime(2025, 4, 11, tzinfo=timezone.utc),
        canonical_url="https://mp.weixin.qq.com/s?__biz=biz&mid=2247488874&idx=1&sn=sn",
    )

    snapshot = provider.fetch(article)

    assert snapshot.read_count == 1
    assert provider.session.calls[0][0:2] == ("GET", "https://mp.weixin.qq.com/s")
    assert provider.session.calls[1][0:2] == ("POST", "https://mp.weixin.qq.com/mp/getappmsgext")
    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"base_resp": {"ret": 0}, "article_list": []}

    class SessionStub:
        def __init__(self):
            self.calls = []

        def get(self, url, params, headers, timeout, **kwargs):
            self.calls.append(params)
            return ResponseStub()

    session = SessionStub()
    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        mp_token="secret-token",
        fakeid="fakeid",
        session=session,
    )

    provider.fetch_for_articles([
        ArticleStub("1", 1, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        ArticleStub("2", 1, datetime(2025, 6, 1, tzinfo=timezone.utc)),
    ])

    assert len(session.calls) == 2
    for params in session.calls:
        assert int(params["end_timestamp"]) - int(params["begin_timestamp"]) <= 90 * 24 * 60 * 60


def test_wechat_authorized_provider_accepts_explicit_article_page_session():
    provider = WechatAuthorizedProvider(
        cookie="secret-cookie",
        force_article_page_session=True,
    )

    assert provider.is_configured is True
    assert provider.has_article_page_session is True
    assert provider.status_message == "文章页会话指标配置已存在"
    assert "secret" not in provider.redacted_config
    assert "WECHAT_ARTICLE_PAGE_SESSION=1" in provider.redacted_config


def test_wechat_authorized_provider_accepts_mp_backend_token(monkeypatch):
    monkeypatch.setenv("WECHAT_COOKIE", "secret-cookie")
    monkeypatch.setenv("WECHAT_MP_TOKEN", "secret-mp-token")
    monkeypatch.setenv("WECHAT_FAKEID", "fakeid")
    monkeypatch.delenv("WECHAT_APPMSG_TOKEN", raising=False)

    provider = WechatAuthorizedProvider.from_env()

    assert provider.is_configured is True
    assert provider.has_mp_backend_token is True
    assert "公众号后台授权配置已存在" == provider.status_message
    assert "secret" not in provider.redacted_config
