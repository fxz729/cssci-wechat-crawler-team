"""Validation and export helpers for a completed single-journal bundle."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import json

from export.journal_bundle import write_journal_workbook


METRIC_FIELDS = (
    "read_count",
    "like_count",
    "watching_count",
    "share_count",
    "favorite_count",
    "comment_count",
)


def summarize_bundle(catalog: dict, state: dict, records: list[dict], media: list[dict]) -> dict:
    """Return auditable coverage counts without converting missing values to zero."""
    jobs = state.get("jobs", {}) if isinstance(state, dict) else {}
    state_status_counts = dict(Counter(str(job.get("status", "unknown")) for job in jobs.values()))
    resolved_identity_count = sum(
        1
        for job in jobs.values()
        if job.get("status") in {"captured", "partial"}
        and str(job.get("mid") or "").isdigit()
        and isinstance(job.get("idx"), int)
    )
    metric_field_counts = {
        field: sum(record.get(field) is not None for record in records)
        for field in METRIC_FIELDS
    }
    article_fields = (
        "biz", "mid", "idx", "sn", "title", "author", "digest", "cover_url",
        "article_url", "publish_time", "content_html", "content_text", "comment_id",
    )
    article_field_counts = {
        field: sum(record.get(field) not in (None, "") for record in records)
        for field in article_fields
    }
    media_status_counts = dict(Counter(str(row.get("status", "unknown")) for row in media))
    pending_statuses = set(media_status_counts) - {"downloaded", "skipped"}
    article_keys = [f"{record.get('mid')}_{record.get('idx')}" for record in records]
    duplicate_keys = len(article_keys) - len(set(article_keys))
    return {
        "catalog_count": len((catalog or {}).get("items", [])),
        "resolved_identity_count": resolved_identity_count,
        "unresolved_identity_count": max(0, len((catalog or {}).get("items", [])) - resolved_identity_count),
        "record_count": len(records),
        "duplicate_article_key_count": duplicate_keys,
        "content_html_count": sum(bool(str(record.get("content_html") or "").strip()) for record in records),
        "content_text_count": sum(bool(str(record.get("content_text") or "").strip()) for record in records),
        "metrics_complete_count": sum(record.get("metrics_status") == "complete" for record in records),
        "metrics_partial_count": sum(record.get("metrics_status") == "partial" for record in records),
        "metrics_not_observed_count": sum(record.get("metrics_status") == "not_observed" for record in records),
        "metric_field_counts": metric_field_counts,
        "article_field_counts": article_field_counts,
        "business_ret_count": sum(record.get("business_ret") is not None for record in records),
        "http_status_count": sum(record.get("http_status") is not None for record in records),
        "fiddler_session_count": sum(record.get("fiddler_session_id") is not None for record in records),
        "state_status_counts": state_status_counts,
        "media_total_count": len(media),
        "media_status_counts": media_status_counts,
        "pending_media_count": sum(count for status, count in media_status_counts.items() if status in pending_statuses),
        "media_article_key_count": len({str(row.get("article_key") or "") for row in media if row.get("article_key")}),
    }


def load_bundle_records(bundle_root: Path | str) -> tuple[list[dict], list[dict]]:
    """Load metadata and the media manifest rows in deterministic order."""
    bundle = Path(bundle_root).resolve()
    records: list[dict] = []
    for path in sorted(bundle.glob("articles/*/metadata.json")):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            records.append(value)
    media_path = bundle / "media_rows.json"
    media = json.loads(media_path.read_text(encoding="utf-8-sig")) if media_path.exists() else []
    if not isinstance(media, list):
        media = []
    return records, [row for row in media if isinstance(row, dict)]


def prepare_workbook_rows(records: list[dict], media: list[dict]) -> list[dict]:
    """Add the derived media columns expected by ``write_journal_workbook``."""
    by_article: dict[str, list[dict]] = {}
    for row in media:
        by_article.setdefault(str(row.get("article_key") or ""), []).append(row)
    result = []
    for record in records:
        row = dict(record)
        article_key = f"{record.get('mid')}_{record.get('idx')}"
        row.setdefault("capture_evidence", f"articles/{article_key}/capture_evidence.json")
        rows = by_article.get(article_key, [])
        row["media_downloaded"] = sum(item.get("status") in {"downloaded", "skipped"} for item in rows)
        row["media_pending"] = sum(item.get("status") not in {"downloaded", "skipped"} for item in rows)
        row.setdefault("canonical_url", row.get("article_url", ""))
        row.setdefault("content_status", "captured")
        result.append(row)
    return result


def write_validation_outputs(base_root: Path | str, *, workbook_name: str = "文章索引.xlsx") -> dict:
    """Write the workbook and machine-/human-readable validation reports."""
    base = Path(base_root).resolve()
    bundle = base / "bundle"
    catalog = json.loads((base / "catalog_verified_snapshot.json").read_text(encoding="utf-8"))
    state_path = base / "capture_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"jobs": {}}
    records, media = load_bundle_records(bundle)
    summary = summarize_bundle(catalog, state, records, media)
    workbook = bundle / workbook_name
    write_journal_workbook(
        prepare_workbook_rows(records, media),
        media,
        workbook,
        bundle_root=bundle,
    )
    report = {
        "schema_version": 1,
        "catalog": "catalog_verified_snapshot.json",
        "state": "capture_state.json",
        "summary": summary,
        "workbook": workbook.relative_to(bundle).as_posix(),
    }
    report_path = base / "journal_validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 一刊历史目录与逐篇字段验收报告",
        "",
        f"- 目录条数：{summary['catalog_count']}",
        f"- 已解析文章记录：{summary['record_count']}",
        f"- 已核验身份：{summary['resolved_identity_count']}",
        f"- 正文 HTML：{summary['content_html_count']}",
        f"- 正文文本：{summary['content_text_count']}",
        f"- 六项指标完整：{summary['metrics_complete_count']}",
        f"- `comment_id`：{summary['article_field_counts']['comment_id']}",
        f"- 业务返回码：{summary['business_ret_count']}/{summary['record_count']}",
        f"- 媒体资源：{summary['media_total_count']}，待处理：{summary['pending_media_count']}",
        f"- 作者非空：{summary['article_field_counts']['author']}/{summary['record_count']}（其余按源未返回保留）",
        f"- 摘要非空：{summary['article_field_counts']['digest']}/{summary['record_count']}（其余按源未返回保留）",
        "",
        "## 六项指标字段覆盖",
        "",
    ]
    for field, count in summary["metric_field_counts"].items():
        lines.append(f"- `{field}`：{count}/{summary['record_count']}")
    lines.extend(["", f"Excel：`{summary['record_count']} 条记录`，文件见 `{workbook.relative_to(base).as_posix()}`。", ""])
    (base / "journal_validation_report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
