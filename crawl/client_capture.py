"""Helpers for merging verified WeChat client capture events into bundle records."""

from copy import deepcopy


METRIC_FIELDS = (
    "read_count",
    "like_count",
    "watching_count",
    "share_count",
    "favorite_count",
    "comment_count",
)


def build_capture_metadata(
    article_event: dict,
    metric_event: dict | None,
    base_row: dict | None,
    *,
    journal_name: str,
    profile_url: str,
    local_html: str,
    local_text: str,
    local_article_dir: str,
) -> dict:
    """Combine parsed content with the original, validated client event.

    ``base_row`` contains parser/database-derived fields such as text and media.
    The client event remains authoritative for identity, public URL and the
    values observed in the article HTTP response. Missing values stay ``None``
    or an empty string; this function never fabricates zeroes.
    """
    if not isinstance(article_event, dict) or not isinstance(article_event.get("article"), dict):
        raise ValueError("invalid_article_event")

    row = deepcopy(base_row) if isinstance(base_row, dict) else {}
    article = article_event["article"]
    metric = metric_event if isinstance(metric_event, dict) else None
    metric_values = metric.get("metrics") if metric else None
    metric_values = metric_values if isinstance(metric_values, dict) else {}
    source = "fiddler_classic:/s"

    metrics = {field: metric_values.get(field) for field in METRIC_FIELDS}
    metric_fields = {}
    for field in METRIC_FIELDS:
        value = metrics[field]
        existing = (metric.get("field_meta") or {}).get(field) if metric else None
        if isinstance(existing, dict):
            metric_fields[field] = deepcopy(existing)
        else:
            metric_fields[field] = {
                "status": "observed" if value is not None else "not_observed",
                "precision": "exact" if value is not None else "unknown",
                "raw_field": None,
                "source": source if metric else "not_observed",
            }

    observed = sum(value is not None for value in metrics.values())
    metrics_status = (
        "complete" if observed == len(METRIC_FIELDS)
        else "partial" if observed
        else "not_observed"
    )

    row.update(
        journal_name=journal_name,
        account_name=journal_name,
        article_account_name_raw=str(article.get("account_name") or ""),
        profile_url=profile_url,
        biz=str(article_event.get("biz") or ""),
        mid=str(article_event.get("mid") or ""),
        idx=article_event.get("idx"),
        sn=str(article_event.get("sn") or ""),
        title=str(article.get("title") or row.get("title") or ""),
        author=str(article.get("author") or row.get("author") or ""),
        digest=str(article.get("digest") or row.get("digest") or ""),
        cover_url=str(article.get("cover_url") or row.get("cover_image_url") or ""),
        article_url=str(article.get("url") or row.get("canonical_url") or ""),
        content_fragment_html=str(article.get("content_html") or row.get("content_html") or ""),
        content_origin=str(article.get("content_origin") or row.get("content_origin") or ""),
        published_at_raw=str(article.get("publish_time") or ""),
        local_html=local_html,
        local_text=local_text,
        local_article_dir=local_article_dir,
        metrics=metrics,
        metric_fields=metric_fields,
        metric_observations=metric_fields,
        metrics_status=metrics_status,
        metrics_source=source if metric else "not_observed",
        metrics_captured_at=metric.get("captured_at") if metric else None,
        comment_id=(str(metric.get("comment_id") or row.get("comment_id") or "") if metric
                    else str(row.get("comment_id") or "")),
        business_ret=(metric.get("business_ret") if metric and "business_ret" in metric else None),
        http_status=metric.get("http_status") if metric else None,
        fiddler_session_id=metric.get("session_id") if metric else None,
        capture_process=str(metric.get("process") or "") if metric else "",
        article_event_id=str(article_event.get("event_id") or ""),
        metric_event_id=str(metric.get("event_id") or "") if metric else "",
        content_status="captured",
    )
    # Exporters and spreadsheet rows use flat metric columns; keep the nested
    # evidence map above as well so both access patterns retain the exact zero.
    row.update(metrics)
    if not row.get("canonical_url"):
        row["canonical_url"] = row["article_url"]
    if not row.get("source_url"):
        row["source_url"] = row["article_url"]
    if not row.get("cover_image_url"):
        row["cover_image_url"] = row["cover_url"]

    provenance = deepcopy(row.get("field_provenance") or {})
    for field in ("biz", "mid", "idx", "sn", "title", "author", "digest", "cover_url", "article_url", "publish_time", "content_html"):
        if field in {"biz", "mid", "idx", "sn"}:
            value = article_event.get(field)
        else:
            source_field = "cover_url" if field == "cover_url" else field
            value = article.get(source_field)
        provenance[field] = {
            "source": "fiddler_classic:/s:article" if value not in (None, "") else "not_returned",
            "status": "observed" if value not in (None, "") else "not_observed",
        }
    provenance["metrics"] = {
        field: {
            "source": source if metric else "not_observed",
            "status": "observed" if metrics[field] is not None else "not_observed",
            "raw_field": metric_fields[field].get("raw_field"),
            "captured_at": metric.get("captured_at") if metric else None,
        }
        for field in METRIC_FIELDS
    }
    if metric:
        provenance["comment_id"] = {
            "source": source,
            "status": "observed" if row["comment_id"] else "not_observed",
        }
    row["field_provenance"] = provenance
    return row
