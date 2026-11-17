"""UTF-8-BOM CSVs with the legacy column contract and portable media paths.

No cell truncation, inferred author/digest, credential export, or Excel formulas.
"""
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from export.realtime_exporter import default_export_headers

METRICS = ("read_count", "watching_count", "like_count", "favorite_count", "share_count", "comment_count")
EXTRA_HEADERS = ["journal_name", "article_url", "content_text", "content_fragment_html",
                 "local_html", "local_text", "local_article_dir", "local_readable_html", "capture_evidence",
                 "media_local_paths", "image_local_paths", "audio_local_paths", "video_local_paths",
                 "media_downloaded", "media_pending", "media_manifest_status", "metric_fields",
                 "business_ret", "http_status", "fiddler_session_id", "article_event_id", "metric_event_id"]
MEDIA_HEADERS = ["article_key", "biz", "mid", "idx", "article_title", "kind", "role",
                 "resource_index", "asset_index", "url", "local_path", "status", "bytes", "sha256", "error_code"]


def _local(bundle, value):
    if not value:
        return ""
    path = Path(str(value))
    target = bundle / path
    if path.is_absolute() or ".." in path.parts or not target.resolve().is_relative_to(bundle):
        raise ValueError("unsafe_bundle_path")
    for parent in (target, *target.parents):
        if parent == bundle:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("unsafe_bundle_path")
    return path.as_posix()


def _atomic_csv(path, headers, rows):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list))
                                 else row.get(key) for key in headers})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_journal_csv_bundle(bundle_root) -> dict:
    bundle = Path(bundle_root).resolve()
    if not bundle.is_dir():
        raise ValueError("bundle_missing")
    headers = default_export_headers([]) + EXTRA_HEADERS
    records = []
    for path in sorted(bundle.glob("articles/*/metadata.json")):
        _local(bundle, path.relative_to(bundle))
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if path.parent.name != f"{record.get('mid')}_{record.get('idx')}":
            raise ValueError("article_identity_mismatch")
        records.append(record)
    records.sort(key=lambda row: (str(row.get("publish_time") or ""), str(row.get("mid") or ""), int(row.get("idx") or 0)), reverse=True)
    media_path = bundle / "media_rows.json"
    media = json.loads(media_path.read_text(encoding="utf-8-sig")) if media_path.exists() else []
    if not isinstance(media, list):
        raise ValueError("invalid_media_manifest")
    by_article = defaultdict(list)
    for resource in media:
        local = _local(bundle, resource.get("local_path"))
        item = {key: resource.get(key) for key in MEDIA_HEADERS}
        item["local_path"] = local
        by_article[str(resource.get("article_key") or "")].append(item)
    rows, media_rows = [], []
    for record in records:
        row = {key: record.get(key) for key in headers}
        article_key = f"{record['mid']}_{record['idx']}"
        folder = f"articles/{article_key}"
        row["content"] = row["content_text"] = record.get("content_text") or ""
        row["digest"] = record.get("digest") or ""
        row["title_length"] = len(record.get("title") or "")
        row["digest_length"] = len(row["digest"])
        row["batch_key"] = f"{record.get('biz')}:{record['mid']}"
        row["canonical_url"] = record.get("article_url") or record.get("canonical_url")
        row["source_url"] = record.get("article_url") or record.get("source_url")
        row["metrics_provider"] = record.get("metrics_source") or record.get("metrics_provider")
        metrics = record.get("metrics") or {}
        for metric in METRICS:
            row[metric] = metrics.get(metric, record.get(metric))
        row["metric_field_status"] = {key: "observed" if row[key] is not None else "not_observed" for key in METRICS}
        row["metrics_status"] = "complete" if all(row[key] is not None for key in METRICS) else "partial"
        try:
            date = datetime.fromisoformat(str(record.get("publish_time") or "").replace("Z", "+00:00"))
            if date.tzinfo:
                date = date.astimezone(timezone(timedelta(hours=8)))
            row["quarter"] = (date.month - 1) // 3 + 1
            row["is_weekday"] = date.weekday() < 5
        except ValueError:
            pass
        for name, fallback in (("local_html", folder + "/article.html"), ("local_text", folder + "/article.txt"),
                               ("local_article_dir", folder), ("capture_evidence", folder + "/capture_evidence.json")):
            row[name] = _local(bundle, record.get(name) or fallback)
        row["local_readable_html"] = folder + "/article.local.html" if (bundle / folder / "article.local.html").is_file() else row["local_html"]
        resources = by_article.get(article_key, [])
        for resource in resources:
            resource.update(biz=record.get("biz"), mid=record["mid"], idx=record["idx"], article_title=record.get("title"))
        good = [item for item in resources if item["status"] in ("downloaded", "skipped") and item["local_path"] and (bundle / item["local_path"]).is_file()]
        row["media_local_paths"] = [item["local_path"] for item in good]
        for kind in ("image", "audio", "video"):
            row[kind + "_local_paths"] = [item["local_path"] for item in good if item["kind"] == kind]
        row["images_folder"] = folder + "/images" if (bundle / folder / "images").is_dir() else ""
        row["media_downloaded"] = len(good)
        row["media_pending"] = len(resources) - len(good)
        manifest = bundle / folder / "media_manifest.json"
        row["media_manifest_status"] = "partial" if row["media_pending"] else "complete" if resources or manifest.exists() else "not_collected"
        rows.append(row)
        media_rows.extend(resources)
    _atomic_csv(bundle / "文章数据.csv", headers, rows)
    _atomic_csv(bundle / "媒体索引.csv", MEDIA_HEADERS, media_rows)
    return {"article_count": len(rows), "media_count": len(media_rows), "article_csv": "文章数据.csv", "media_csv": "媒体索引.csv",
            "column_count": len(headers)}
