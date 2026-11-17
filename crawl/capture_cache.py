"""Sanitized, resumable evidence cache for the client-first capture phase."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from capture.reader import METRIC_FIELDS, normalize_event, to_wire_event
from crawl.catalog_contract import normalize_catalog_text

_SCHEMA_VERSION = 1
_FORBIDDEN_KEYS = {
    "authorization", "cookie", "cookies", "headers", "request_headers",
    "response_headers", "pass_ticket", "token", "uin", "key",
}


def cache_entry_path(base: str | Path, index: int) -> Path:
    if type(index) is not int or index < 0:
        raise ValueError("invalid_cache_index")
    root = Path(base).resolve()
    path = root / "capture_cache" / "articles" / f"{index:06d}" / "capture_evidence.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("unsafe_cache_path")
    return path


def _identity(event: dict) -> tuple[str, str, int, str]:
    return event["biz"], str(event["mid"]), event["idx"], event["sn"]


def _missing(metric: dict | None) -> list[str]:
    values = metric.get("metrics") if isinstance(metric, dict) else {}
    values = values if isinstance(values, dict) else {}
    return [field for field in METRIC_FIELDS if values.get(field) is None]


def _assert_sanitized(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise ValueError("unsafe_cache_field")
            _assert_sanitized(item)
    elif isinstance(value, list):
        for item in value:
            _assert_sanitized(item)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def _validated_events(article_event: dict, metric_event: dict | None, biz: str) -> tuple[dict, dict | None]:
    article = normalize_event(to_wire_event(article_event), {biz})
    if article.get("kind") != "article":
        raise ValueError("invalid_cache_article_event")
    metric = None
    if metric_event is not None:
        metric = normalize_event(to_wire_event(metric_event), {biz})
        if metric.get("kind") != "metrics" or metric.get("metric_format") != "client_html_bootstrap_v1":
            raise ValueError("invalid_cache_metric_event")
        if _identity(article) != _identity(metric):
            raise ValueError("cache_identity_mismatch")
    return article, metric


def write_capture_entry(
    base: str | Path,
    index: int,
    catalog_item: dict,
    article_event: dict,
    metric_event: dict | None,
    *,
    biz: str,
    cache_origin: str = "live_client",
) -> dict:
    path = cache_entry_path(base, index)
    if cache_origin not in {"live_client", "prior_local_event"}:
        raise ValueError("invalid_cache_origin")
    article, metric = _validated_events(article_event, metric_event, biz)
    title = normalize_catalog_text((article.get("article") or {}).get("title"))
    expected_title = normalize_catalog_text((catalog_item or {}).get("title"))
    if not expected_title or title != expected_title:
        raise ValueError("cache_catalog_title_mismatch")
    missing = _missing(metric)
    identity = dict(zip(("biz", "mid", "idx", "sn"), _identity(article)))
    body = {
        "schema_version": _SCHEMA_VERSION,
        "index": index,
        "catalog_title": expected_title,
        "catalog_runtime": str((catalog_item or {}).get("runtime") or ""),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cache_origin": cache_origin,
        "identity": identity,
        "article_event": to_wire_event(article),
        "metric_event": to_wire_event(metric) if metric else None,
        "missing_metric_fields": missing,
        "complete": not missing,
    }
    _assert_sanitized(body)
    body["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    _atomic_json(path, body)
    return body


def read_capture_entry(
    base: str | Path,
    index: int,
    catalog_item: dict,
    *,
    biz: str,
) -> dict:
    path = cache_entry_path(base, index)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION or raw.get("index") != index:
        raise ValueError("invalid_cache_entry")
    supplied_hash = raw.get("sha256")
    unsigned = dict(raw)
    unsigned.pop("sha256", None)
    if not isinstance(supplied_hash, str) or hashlib.sha256(_canonical(unsigned)).hexdigest() != supplied_hash:
        raise ValueError("cache_digest_mismatch")
    _assert_sanitized(unsigned)
    article, metric = _validated_events(raw.get("article_event"), raw.get("metric_event"), biz)
    if raw.get("identity") != dict(zip(("biz", "mid", "idx", "sn"), _identity(article))):
        raise ValueError("cache_identity_mismatch")
    title = normalize_catalog_text((article.get("article") or {}).get("title"))
    expected_title = normalize_catalog_text((catalog_item or {}).get("title"))
    if not expected_title or title != expected_title or raw.get("catalog_title") != expected_title:
        raise ValueError("cache_catalog_title_mismatch")
    if raw.get("catalog_runtime", "") != str((catalog_item or {}).get("runtime") or ""):
        raise ValueError("cache_catalog_runtime_mismatch")
    missing = _missing(metric)
    return {
        **raw,
        "article_event": article,
        "metric_event": metric,
        "missing_metric_fields": missing,
        "complete": not missing,
    }


def rebind_capture_entry_runtime(
    base: str | Path,
    index: int,
    catalog_item: dict,
    *,
    biz: str,
) -> bool:
    """Re-sign a verified cache entry after a safe catalog runtime refresh."""
    path = cache_entry_path(base, index)
    raw = json.loads(path.read_text(encoding="utf-8"))
    current_runtime = str((catalog_item or {}).get("runtime") or "")
    if not current_runtime:
        raise ValueError("invalid_catalog_runtime")
    prior_item = dict(catalog_item or {})
    prior_item["runtime"] = str((raw or {}).get("catalog_runtime") or "")
    validated = read_capture_entry(base, index, prior_item, biz=biz)
    if validated["catalog_runtime"] == current_runtime:
        return False
    body = dict(raw)
    body.pop("sha256", None)
    body["catalog_runtime"] = current_runtime
    _assert_sanitized(body)
    body["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    _atomic_json(path, body)
    read_capture_entry(base, index, catalog_item, biz=biz)
    return True


def build_capture_summary(base: str | Path, catalog: dict, *, biz: str) -> dict:
    items = catalog.get("items") if isinstance(catalog, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("invalid_cache_catalog")
    rows = []
    complete_count = partial_count = missing_count = prior_local_count = 0
    recapture = []
    for index, item in enumerate(items):
        try:
            entry = read_capture_entry(base, index, item, biz=biz)
            status = "complete" if entry["complete"] else "partial"
            missing_fields = entry["missing_metric_fields"]
            identity = entry["identity"]
            cache_origin = entry.get("cache_origin", "live_client")
            if cache_origin == "prior_local_event":
                prior_local_count += 1
            if status == "complete":
                complete_count += 1
            else:
                partial_count += 1
                recapture.append(index)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            status = "missing" if isinstance(exc, FileNotFoundError) else "invalid"
            missing_count += 1
            recapture.append(index)
            missing_fields = list(METRIC_FIELDS)
            identity = None
            cache_origin = "missing"
        rows.append({
            "index": index,
            "title": str((item or {}).get("title") or ""),
            "status": status,
            "identity": identity,
            "cache_origin": cache_origin,
            "missing_metric_fields": missing_fields,
        })
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog_count": len(items),
        "complete_count": complete_count,
        "partial_count": partial_count,
        "missing_count": missing_count,
        "prior_local_count": prior_local_count,
        "complete": complete_count == len(items),
        "recapture_indices": recapture,
        "items": rows,
    }

