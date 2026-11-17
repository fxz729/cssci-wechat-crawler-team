from dataclasses import dataclass
from enum import Enum
import re
from types import SimpleNamespace

import requests

from crawl.article_fetcher import parse_fetched_article
from crawl.errors import SessionUnavailable
from crawl.wechat_browser import WechatBrowserClient
from crawl.wechat_urls import is_official_article_url
from metrics.providers.wechat_authorized import WechatAuthorizedProvider
from metrics.providers.wechat_backend import WechatBackendClient


class SessionPreflightStatus(str, Enum):
    USABLE = "usable"
    EXPIRED = "expired"
    DEGRADED = "degraded"
    UNAUTHORIZED_FOR_TARGET = "unauthorized_for_target"
    NETWORK_FAILED = "network_failed"


@dataclass(frozen=True)
class SessionPreflightResult:
    """Each capability is separate; None means unprobed, not unavailable."""

    session_id: str
    status: SessionPreflightStatus
    detail: str
    catalog_status: SessionPreflightStatus | None = None
    content_status: SessionPreflightStatus | None = None
    metrics_status: SessionPreflightStatus | None = None


@dataclass(frozen=True)
class SessionSelection:
    credential: object
    transport: str


def select_usable_session(
    credentials,
    fakeid: str,
    *,
    transport: str = "auto",
    requests_validator=None,
    browser_validator=None,
):
    if transport not in {"auto", "requests", "browser"}:
        raise ValueError("transport must be auto, requests, or browser")
    requests_validator = requests_validator or validate_session_credential
    browser_validator = browser_validator or validate_browser_session_credential
    candidates = [
        credential for credential in credentials
        if not getattr(credential, "fakeid", "") or credential.fakeid == fakeid
    ]
    for credential in candidates:
        if transport in {"auto", "requests"}:
            result = requests_validator(credential, fakeid)
            if result.status == SessionPreflightStatus.USABLE:
                return SessionSelection(credential, "requests")
        if transport in {"auto", "browser"}:
            result = browser_validator(credential, fakeid)
            if result.status == SessionPreflightStatus.USABLE:
                return SessionSelection(credential, "browser")
    return None


def validate_browser_session_credential(
    credential,
    fakeid: str,
    *,
    browser_client_factory=WechatBrowserClient,
    headless: bool = True,
) -> SessionPreflightResult:
    capabilities = {}
    stage = "catalog_status"
    try:
        with browser_client_factory(credential, fakeid, headless=headless) as client:
            items, _ = client.fetch_publish_page(0, 1)
            if not items:
                raise SessionUnavailable("empty_catalog")
            capabilities[stage] = SessionPreflightStatus.USABLE
            stage = "content_status"
            final_url, html = client.fetch_article_page(items[0])
            article = _article_view(html, final_url, items[0], fakeid)
            capabilities[stage] = SessionPreflightStatus.USABLE
            stage = "metrics_status"
            snapshot = client.fetch_metrics(article)
            return _metrics_result(credential, capabilities, snapshot, "browser_ok")
    except Exception as exc:
        return _failure_result(credential, capabilities, stage, exc)


def validate_session_credential(
    credential,
    fakeid: str,
    *,
    session_factory=requests.Session,
    backend_client_factory=WechatBackendClient,
    provider_factory=WechatAuthorizedProvider,
) -> SessionPreflightResult:
    capabilities = {}
    stage = "catalog_status"
    try:
        session = session_factory()
        if hasattr(session, "trust_env"):
            session.trust_env = False
        backend = _backend_client(backend_client_factory, credential, fakeid, session)
        items, _ = backend.fetch_publish_page(0, 1)
        if not items:
            raise SessionUnavailable("empty_catalog")
        capabilities[stage] = SessionPreflightStatus.USABLE
        stage = "content_status"
        item = items[0]
        if not is_official_article_url(item.link):
            raise SessionUnavailable("untrusted_article_url")
        response = session.get(item.link, headers={"Cookie": credential.cookie, "User-Agent": credential.user_agent}, timeout=30)
        response.raise_for_status()
        article = _article_view(response.text, response.url, item, fakeid)
        capabilities[stage] = SessionPreflightStatus.USABLE
        stage = "metrics_status"
        provider = provider_factory(
            cookie=credential.cookie,
            appmsg_token=_cookie_value(credential.cookie, "appmsg_token"),
            user_agent=credential.user_agent,
            session=session,
        )
        snapshot = provider.fetch(article)
        return _metrics_result(credential, capabilities, snapshot, "ok")
    except Exception as exc:
        return _failure_result(credential, capabilities, stage, exc)


def _article_view(html, final_url, item, fakeid):
    parsed, parts = parse_fetched_article(html, final_url)
    if parsed is None or not parts.biz or not parts.mid or parts.idx is None:
        raise SessionUnavailable("unparseable_article")
    if parts.biz != fakeid:
        raise SessionUnavailable("article_biz_mismatch")
    if str(parts.mid) != str(item.mid) or int(parts.idx) != int(item.idx):
        raise SessionUnavailable("article_identity_mismatch")
    return SimpleNamespace(
        mid=parts.mid,
        idx=parts.idx,
        sn=parts.sn or "",
        canonical_url=parts.canonical_url,
        comment_id=parsed.comment_id,
        req_id=parsed.req_id,
        title=parsed.title,
        raw_html_path="",
    )


def _metrics_result(credential, capabilities, snapshot, success_detail):
    complete = all(getattr(snapshot, name, None) is not None for name in (
        "read_count", "like_count", "watching_count", "share_count",
        "favorite_count", "comment_count",
    ))
    capabilities["metrics_status"] = (
        SessionPreflightStatus.USABLE if complete else SessionPreflightStatus.DEGRADED
    )
    return SessionPreflightResult(
        credential.session_id,
        SessionPreflightStatus.USABLE,
        success_detail if complete else "missing_metrics",
        **capabilities,
    )


def _failure_result(credential, capabilities, stage, exc):
    status, detail = _classify_failure(exc)
    capabilities[stage] = status
    overall = status
    # Optional metrics failures do not discard working catalog/content access.
    # Explicit HTTP/auth/transport failures still stop session selection.
    if stage == "metrics_status" and not (
        isinstance(exc, requests.RequestException)
        or status in {SessionPreflightStatus.EXPIRED, SessionPreflightStatus.UNAUTHORIZED_FOR_TARGET}
        or detail.startswith("http_")
    ):
        overall = SessionPreflightStatus.USABLE
    return SessionPreflightResult(credential.session_id, overall, detail, **capabilities)


def _classify_failure(exc):
    if isinstance(exc, requests.HTTPError):
        code = getattr(exc.response, "status_code", None)
        status = SessionPreflightStatus.EXPIRED if code in {401, 403} else SessionPreflightStatus.NETWORK_FAILED
        detail = f"http_{code}" if isinstance(code, int) else "http_error"
        return status, detail
    if isinstance(exc, SessionUnavailable):
        reason = str(exc)
        if reason == "expired":
            return SessionPreflightStatus.EXPIRED, reason
        if re.fullmatch(r"http_[1-5][0-9]{2}", reason):
            status = SessionPreflightStatus.EXPIRED if reason in {"http_401", "http_403"} else SessionPreflightStatus.NETWORK_FAILED
            return status, reason
        if reason in {"empty_catalog", "article_biz_mismatch", "backend_rejected"}:
            return SessionPreflightStatus.UNAUTHORIZED_FOR_TARGET, reason
        if reason in {
            "missing_metrics", "unparseable_article", "article_identity_mismatch",
            "untrusted_article_url", "invalid_backend_response", "invalid_browser_response",
        }:
            return SessionPreflightStatus.DEGRADED, reason
        return SessionPreflightStatus.DEGRADED, type(exc).__name__
    return SessionPreflightStatus.NETWORK_FAILED, type(exc).__name__


def _backend_client(factory, credential, fakeid: str, session):
    if hasattr(factory, "from_session_credential"):
        return factory.from_session_credential(credential, fakeid, session=session)
    return factory(credential, fakeid, session=session)


def _cookie_value(cookie: str, name: str) -> str:
    prefix = f"{name}="
    for part in cookie.split(";"):
        candidate = part.strip()
        if candidate.startswith(prefix):
            return candidate[len(prefix):]
    return ""
