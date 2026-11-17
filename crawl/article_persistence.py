from dataclasses import dataclass
from pathlib import Path
import re
from types import SimpleNamespace

import requests

from crawl.article_fetcher import parse_fetched_article, persist_article_files
from crawl.errors import SessionUnavailable


@dataclass(frozen=True)
class PersistedHistoryArticle:
    id: int
    content_text: str
    image_count: int
    latest_metrics: object | None = None


METRIC_FIELDS = (
    "read_count", "like_count", "watching_count", "share_count",
    "favorite_count", "comment_count",
)


def persist_history_article(repo, account, client, item, data_dir, with_metrics=True):
    """Save verified content first; missing metrics are independent refill tasks."""
    final_url, html = client.fetch_article_page(item)
    parsed, parts = parse_fetched_article(html, final_url)
    if parsed is None or not parts.biz or not parts.mid or parts.idx is None:
        raise RuntimeError("unparseable_article")
    if parts.biz != account.biz:
        raise RuntimeError("article_biz_mismatch")
    if str(parts.mid) != str(item.mid) or int(parts.idx) != int(item.idx):
        raise RuntimeError("article_identity_mismatch")

    raw_html_path, raw_text_path = persist_article_files(
        Path(data_dir), parts.biz, parts.mid, parts.idx, html, parsed.content_text,
    )
    article = _persist_article(repo, account, item, parsed, parts, raw_html_path, raw_text_path)
    snapshot = None
    if with_metrics:
        article_view = SimpleNamespace(
            mid=parts.mid,
            idx=parts.idx,
            sn=parts.sn or "",
            canonical_url=parts.canonical_url,
            comment_id=parsed.comment_id,
            req_id=parsed.req_id,
            title=parsed.title or item.title,
            raw_html_path=raw_html_path,
            publish_time=getattr(item, "publish_time", None) or getattr(parsed, "publish_time", None),
        )
        failure = None
        try:
            candidate = client.fetch_metrics(article_view)
            observed = [getattr(candidate, name, None) for name in METRIC_FIELDS]
            if any(value is not None for value in observed):
                repo.complete_article_with_metrics(article.id, candidate)
                snapshot = candidate
            if any(value is None for value in observed):
                failure = "missing_metrics"
        except Exception as exc:
            failure = _safe_metrics_error(exc)
        if failure and callable(getattr(repo, "record_failed_task", None)):
            repo.record_failed_task(
                task_name=f"metrics:{parts.biz}",
                account_name=getattr(account, "canonical_name", ""),
                url=parts.canonical_url,
                error_message=failure,
            )

    return PersistedHistoryArticle(
        id=article.id,
        content_text=parsed.content_text,
        image_count=len(parsed.image_urls),
        latest_metrics=snapshot,
    )


def _safe_metrics_error(exc):
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return f"http_{status}" if isinstance(status, int) else "http_error"
    if isinstance(exc, SessionUnavailable):
        reason = str(exc)
        if re.fullmatch(r"http_[1-5][0-9]{2}", reason) or reason in {
            "missing_metrics", "expired", "backend_rejected", "invalid_backend_response",
            "invalid_browser_response",
        }:
            return reason
    return type(exc).__name__


def _persist_article(repo, account, item, parsed, parts, raw_html_path, raw_text_path):
    publish_time = getattr(item, "publish_time", None) or getattr(parsed, "publish_time", None)
    # None means unobserved; older parser stubs must not erase stored metadata.
    metadata = {
        name: getattr(parsed, name)
        for name in ("media_assets", "field_provenance")
        if getattr(parsed, name, None) is not None
    }
    batch = repo.upsert_publish_batch(
        account.id,
        parts.biz,
        parts.mid,
        publish_time,
        parts.canonical_url,
    )
    return repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz=parts.biz,
        mid=parts.mid,
        idx=parts.idx,
        sn=parts.sn or "",
        title=parsed.title or item.title,
        author=parsed.author or item.author,
        publish_time=publish_time,
        digest=parsed.digest or item.digest,
        content_text=parsed.content_text,
        content_html=parsed.content_html,
        image_urls=parsed.image_urls,
        source_url=parts.canonical_url,
        canonical_url=parts.canonical_url,
        raw_html_path=raw_html_path,
        raw_text_path=raw_text_path,
        cover_image_url=parsed.cover_image_url or item.cover_url,
        req_id=parsed.req_id,
        comment_id=parsed.comment_id,
        word_count=parsed.word_count,
        is_original=parsed.is_original,
        audio_count=parsed.audio_count,
        video_count=parsed.video_count,
        crawl_status="success",
        **metadata,
    )
