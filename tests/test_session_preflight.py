from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import requests

from crawl.errors import SessionUnavailable

from crawl.session_preflight import (
    SessionPreflightResult,
    SessionPreflightStatus,
    select_usable_session,
    validate_browser_session_credential,
    validate_session_credential,
)
from metrics.providers.wechat_backend import BackendArticleItem


@dataclass(frozen=True)
class CredentialStub:
    session_id: str = "session-a"
    backend_token: str = "private-token"
    fingerprint: str = "fingerprint"
    cookie: str = "appmsg_token=initial; pass_ticket=captured"
    user_agent: str = "test-agent"


class ResponseStub:
    def __init__(self, payload=None, text="<div id='js_content'>content</div>", url="https://mp.weixin.qq.com/s?__biz=target-biz&mid=1&idx=1&sn=synthetic"):
        self._payload = payload or {}
        self.text = text
        self.url = url
        self.status_code = 200
        self.cookies = type("Cookies", (), {"get_dict": lambda self: {"appmsg_token": "fresh"}})()

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class BackendStub:
    def __init__(self, *args, **kwargs):
        pass

    def fetch_publish_page(self, begin, count):
        return ([BackendArticleItem("title", "", "", "", "https://mp.weixin.qq.com/s?__biz=target-biz&mid=1&idx=1&sn=synthetic", "1", 1, None)], 1)


class ProviderStub:
    def __init__(self, **kwargs):
        self.session = kwargs["session"]

    def fetch(self, article):
        return _metrics()


class SessionStub:
    def __init__(self):
        self.trust_env = True

    def get(self, url, *, headers, timeout):
        return ResponseStub()


def test_browser_preflight_reports_usable_with_same_context_chain():
    class BrowserClientStub:
        def __init__(self, credential, fakeid, **kwargs):
            self.credential = credential
            self.fakeid = fakeid

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def fetch_publish_page(self, begin, count):
            return ([BackendArticleItem("title", "", "", "", "https://mp.weixin.qq.com/s/short", "1", 1, None)], 1)

        def fetch_article_page(self, item):
            return (
                "https://mp.weixin.qq.com/s?__biz=target-biz&mid=1&idx=1&sn=synthetic",
                "<div id='js_content'>content</div><script>var comment_id='comment'; var req_id='request';</script>",
            )

        def fetch_metrics(self, article):
            return _metrics()

    result = validate_browser_session_credential(
        CredentialStub(),
        "target-biz",
        browser_client_factory=BrowserClientStub,
    )

    assert result.session_id == "session-a"
    assert result.status == SessionPreflightStatus.USABLE
    assert result.detail == "browser_ok"


def test_browser_preflight_redacts_runtime_failure_details():
    class BrokenBrowserClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("private-cookie=must-not-escape")

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    result = validate_browser_session_credential(
        CredentialStub(),
        "target-biz",
        browser_client_factory=BrokenBrowserClient,
    )

    assert result.status == SessionPreflightStatus.NETWORK_FAILED
    assert result.detail == "RuntimeError"
    assert "private" not in str(result)


def test_auto_session_selection_falls_back_to_browser_for_matching_fakeid():
    matching = type("Credential", (), {"session_id": "matching", "fakeid": "target-biz"})()
    other = type("Credential", (), {"session_id": "other", "fakeid": "other-biz"})()
    calls = []

    def requests_validator(credential, fakeid):
        calls.append(("requests", credential.session_id, fakeid))
        return type("Result", (), {
            "status": SessionPreflightStatus.DEGRADED,
        })()

    def browser_validator(credential, fakeid):
        calls.append(("browser", credential.session_id, fakeid))
        return type("Result", (), {
            "status": SessionPreflightStatus.USABLE,
        })()

    selection = select_usable_session(
        (other, matching),
        "target-biz",
        transport="auto",
        requests_validator=requests_validator,
        browser_validator=browser_validator,
    )

    assert selection.credential is matching
    assert selection.transport == "browser"
    assert calls == [
        ("requests", "matching", "target-biz"),
        ("browser", "matching", "target-biz"),
    ]


def test_requests_mode_never_starts_browser_fallback():
    credential = type("Credential", (), {"session_id": "candidate", "fakeid": "target-biz"})()
    browser_calls = []

    selection = select_usable_session(
        (credential,),
        "target-biz",
        transport="requests",
        requests_validator=lambda candidate, fakeid: type("Result", (), {
            "status": SessionPreflightStatus.DEGRADED,
        })(),
        browser_validator=lambda candidate, fakeid: browser_calls.append(candidate) or None,
    )

    assert selection is None
    assert browser_calls == []


def test_preflight_reports_usable_after_backend_article_and_metrics_checks():
    result = validate_session_credential(
        CredentialStub(),
        "target-biz",
        session_factory=SessionStub,
        backend_client_factory=BackendStub,
        provider_factory=ProviderStub,
    )

    assert result.session_id == "session-a"
    assert result.status == SessionPreflightStatus.USABLE
    assert result.detail == "ok"


def test_preflight_redacts_runtime_failure_details():
    class BrokenBackend:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_publish_page(self, begin, count):
            raise RuntimeError("private-cookie=must-not-escape")

    result = validate_session_credential(
        CredentialStub(),
        "target-biz",
        session_factory=SessionStub,
        backend_client_factory=BrokenBackend,
        provider_factory=ProviderStub,
    )

    assert result.status == SessionPreflightStatus.NETWORK_FAILED
    assert result.detail == "RuntimeError"
    assert "private" not in str(result)


METRIC_FIELDS = (
    "read_count", "like_count", "watching_count", "share_count",
    "favorite_count", "comment_count",
)


def _metrics(value=0, **overrides):
    return SimpleNamespace(**(dict.fromkeys(METRIC_FIELDS, value) | overrides))


def _probe(transport, snapshot=None, error=None, stage="metrics", final_url=None, html=None):
    class TestBackend(BackendStub):
        def fetch_publish_page(self, begin, count):
            if error is not None and stage == "catalog":
                raise error
            return super().fetch_publish_page(begin, count)

    class TestSession(SessionStub):
        def get(self, url, **kwargs):
            if error is not None and stage == "content":
                raise error
            response = ResponseStub()
            if final_url is not None:
                response.url = final_url
            if html is not None:
                response.text = html
            return response

    class TestProvider(ProviderStub):
        def fetch(self, article):
            if error is not None and stage == "metrics":
                raise error
            return snapshot

    class TestBrowser:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def fetch_publish_page(self, begin, count):
            return TestBackend().fetch_publish_page(begin, count)

        def fetch_article_page(self, item):
            response = TestSession().get(item.link)
            return response.url, response.text

        def fetch_metrics(self, article):
            return TestProvider(session=None).fetch(article)

    if transport == "requests":
        return validate_session_credential(
            CredentialStub(), "target-biz", session_factory=TestSession,
            backend_client_factory=TestBackend, provider_factory=TestProvider,
        )
    return validate_browser_session_credential(
        CredentialStub(), "target-biz", browser_client_factory=TestBrowser,
    )


def test_preflight_result_retains_legacy_constructor_and_marks_unprobed_capabilities():
    result = SessionPreflightResult("session-a", SessionPreflightStatus.USABLE, "legacy")

    assert result.catalog_status is None
    assert result.content_status is None
    assert result.metrics_status is None


@pytest.mark.parametrize("transport", ["requests", "browser"])
@pytest.mark.parametrize("snapshot", [None, _metrics(None), _metrics(0, comment_count=None)])
def test_preflight_missing_metrics_does_not_invalidate_catalog_and_content(transport, snapshot):
    result = _probe(transport, snapshot=snapshot)

    assert result.status == SessionPreflightStatus.USABLE
    assert result.catalog_status == SessionPreflightStatus.USABLE
    assert result.content_status == SessionPreflightStatus.USABLE
    assert result.metrics_status == SessionPreflightStatus.DEGRADED
    assert result.detail == "missing_metrics"


@pytest.mark.parametrize("transport", ["requests", "browser"])
def test_preflight_all_zero_metrics_are_usable(transport):
    result = _probe(transport, snapshot=_metrics(0))

    assert result.status == SessionPreflightStatus.USABLE
    assert result.catalog_status == SessionPreflightStatus.USABLE
    assert result.content_status == SessionPreflightStatus.USABLE
    assert result.metrics_status == SessionPreflightStatus.USABLE
    assert result.detail == ("ok" if transport == "requests" else "browser_ok")


@pytest.mark.parametrize("transport", ["requests", "browser"])
@pytest.mark.parametrize("missing_field", METRIC_FIELDS)
def test_preflight_reports_incomplete_metric_capability_without_disabling_content(transport, missing_field):
    result = _probe(transport, snapshot=_metrics(0, **{missing_field: None}))

    assert result.status == SessionPreflightStatus.USABLE
    assert result.content_status == SessionPreflightStatus.USABLE
    assert result.metrics_status == SessionPreflightStatus.DEGRADED


@pytest.mark.parametrize("transport", ["requests", "browser"])
def test_preflight_records_sanitized_metric_failure_without_discarding_other_capabilities(transport):
    result = _probe(transport, error=RuntimeError("Cookie=private-cookie token=private-token"))

    assert result.status == SessionPreflightStatus.USABLE
    assert result.catalog_status == SessionPreflightStatus.USABLE
    assert result.content_status == SessionPreflightStatus.USABLE
    assert result.metrics_status == SessionPreflightStatus.NETWORK_FAILED
    assert result.detail == "RuntimeError"
    assert "private-" not in repr(result)


@pytest.mark.parametrize("transport", ["requests", "browser"])
@pytest.mark.parametrize("stage", ["catalog", "content", "metrics"])
@pytest.mark.parametrize("code, status", [
    (401, SessionPreflightStatus.EXPIRED),
    (403, SessionPreflightStatus.EXPIRED),
    (429, SessionPreflightStatus.NETWORK_FAILED),
    (500, SessionPreflightStatus.NETWORK_FAILED),
])
def test_preflight_preserves_http_error_classification_and_prior_capabilities(transport, stage, code, status):
    response = requests.Response()
    response.status_code = code
    error = requests.HTTPError("https://mp.weixin.qq.com/?token=private-token", response=response)
    result = _probe(transport, error=error, stage=stage)

    assert result.status == status
    assert getattr(result, f"{stage}_status") == status
    assert result.detail == f"http_{code}"
    stages = ("catalog", "content", "metrics")
    for previous in stages[:stages.index(stage)]:
        assert getattr(result, f"{previous}_status") == SessionPreflightStatus.USABLE
    for unprobed in stages[stages.index(stage) + 1:]:
        assert getattr(result, f"{unprobed}_status") is None
    assert "private-token" not in repr(result)


@pytest.mark.parametrize("transport", ["requests", "browser"])
@pytest.mark.parametrize("reason, status", [
    ("http_403", SessionPreflightStatus.EXPIRED),
    ("expired", SessionPreflightStatus.EXPIRED),
    ("http_429", SessionPreflightStatus.NETWORK_FAILED),
    ("backend_rejected", SessionPreflightStatus.UNAUTHORIZED_FOR_TARGET),
])
def test_preflight_classifies_explicit_access_failures_from_clients(transport, reason, status):
    result = _probe(transport, error=SessionUnavailable(reason))

    assert result.status == status
    assert result.metrics_status == status
    assert result.content_status == SessionPreflightStatus.USABLE
    assert result.detail == reason


@pytest.mark.parametrize("transport", ["requests", "browser"])
def test_preflight_session_unavailable_missing_field_is_only_a_metrics_failure(transport):
    result = _probe(transport, error=SessionUnavailable("missing_metrics"))

    assert result.status == SessionPreflightStatus.USABLE
    assert result.metrics_status == SessionPreflightStatus.DEGRADED
    assert result.detail == "missing_metrics"


@pytest.mark.parametrize("transport", ["requests", "browser"])
@pytest.mark.parametrize("biz, mid, idx, status, detail", [
    ("other-biz", "1", 1, SessionPreflightStatus.UNAUTHORIZED_FOR_TARGET, "article_biz_mismatch"),
    ("target-biz", "999", 1, SessionPreflightStatus.DEGRADED, "article_identity_mismatch"),
    ("target-biz", "1", 2, SessionPreflightStatus.DEGRADED, "article_identity_mismatch"),
])
def test_preflight_never_probes_metrics_of_a_mismatched_article(transport, biz, mid, idx, status, detail):
    result = _probe(
        transport, error=AssertionError("must not request metrics"),
        final_url=f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn=synthetic",
    )

    assert result.status == status
    assert result.catalog_status == SessionPreflightStatus.USABLE
    assert result.content_status == status
    assert result.metrics_status is None
    assert result.detail == detail


@pytest.mark.parametrize("transport", ["requests", "browser"])
def test_preflight_unparseable_body_does_not_erase_catalog_capability(transport):
    result = _probe(transport, html="<html>no article</html>")

    assert result.status == SessionPreflightStatus.DEGRADED
    assert result.catalog_status == SessionPreflightStatus.USABLE
    assert result.content_status == SessionPreflightStatus.DEGRADED
    assert result.metrics_status is None
    assert result.detail == "unparseable_article"
