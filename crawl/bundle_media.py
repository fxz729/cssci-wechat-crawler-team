"""Reusable, snapshot-bounded media integration for an explicit article bundle.

Only media artifacts are written; metadata.json, article.html, article.txt and
DB contents are never changed. asset_index is the original zero-based index;
resource_index includes an appended cover (asset_index=None). Manifest paths are
bundle-relative; image attributes in article.local.html are HTML-relative.
Unresolved players are left unchanged, never turned into videos.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urldefrag, urljoin

import requests
from crawl.media_download import download_media_assets

FOLDERS = {"image": "images", "audio": "audio", "video": "video"}
ATTRIBUTE = re.compile(r"(?P<space>\s+)(?P<name>[^\s=/>]+)(?:\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s>]+))?")


class BundleError(Exception):
    """Fixed, non-sensitive error code only."""


def safe_path(root, path):
    root, path = Path(root).resolve(), Path(path)
    if not path.resolve().is_relative_to(root):
        raise BundleError("unsafe_path")
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise BundleError("unsafe_path")
        if current.parent == current:
            raise BundleError("unsafe_path")
        current = current.parent
    return path


def atomic_text(bundle, target, text):
    target = safe_path(bundle, target)
    safe_path(bundle, target.parent).mkdir(parents=True, exist_ok=True)
    part = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=target.parent,
                                         prefix="." + target.name + "-", suffix=".part", delete=False) as stream:
            part = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(bundle, target)
        os.replace(part, target)
    finally:
        if part is not None:
            with suppress(OSError, BundleError):
                safe_path(bundle, part).unlink(missing_ok=True)


def atomic_json(bundle, target, data):
    atomic_text(bundle, target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def fingerprint(path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def freeze_snapshot(bundle):
    # Freeze the roster before reading any contents; never rescan in this run.
    paths = tuple(sorted(bundle.glob("articles/*/metadata.json")))
    snapshots, errors = [], []
    if not paths:
        errors.append({"article_key": "", "error_code": "missing_metadata"})
    for path in paths:
        key = path.parent.name
        if not re.fullmatch(r"[0-9]+_[0-9]+", key):
            errors.append({"article_key": "invalid", "error_code": "invalid_article_directory"})
            continue
        try:
            safe_path(bundle, path)
            raw = path.read_bytes()
            metadata = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(metadata, dict) or not metadata:
                raise BundleError("invalid_metadata")
            if key != f"{metadata.get('mid')}_{metadata.get('idx')}":
                raise BundleError("article_id_mismatch")
            source = metadata.get("content_html")
            is_fragment = isinstance(source, str) and bool(source.strip())
            if not is_fragment:
                source = safe_path(bundle, path.parent / "article.html").read_text(encoding="utf-8-sig")
            if not source.strip():
                raise BundleError("missing_article_html")
            snapshots.append({"article_key": key, "article_dir": path.parent, "metadata": metadata,
                              "metadata_sha256": hashlib.sha256(raw).hexdigest(),
                              "html_source": source, "is_fragment": is_fragment})
        except (OSError, ValueError, BundleError) as exc:
            code = str(exc) if isinstance(exc, BundleError) else "metadata_unavailable_or_incomplete"
            errors.append({"article_key": key, "error_code": code, "exception_type": type(exc).__name__})
    return snapshots, errors, len(paths)


def asset_plan(metadata):
    assets = metadata.get("media_assets")
    if assets is None:
        assets = metadata.get("media_assets_json", [])
    if isinstance(assets, str):
        assets = json.loads(assets)
    if not isinstance(assets, list):
        raise BundleError("invalid_media_assets")
    cover = metadata.get("cover_image_url") or ""
    if not isinstance(cover, str):
        raise BundleError("invalid_cover_url")
    plan, cover_present = [], False
    for index, asset in enumerate(assets):
        is_cover = bool(cover) and isinstance(asset, dict) and asset.get("kind") == "image" and asset.get("url") == cover
        cover_present |= is_cover
        plan.append({"resource_index": index, "asset_index": index,
                     "role": "body_and_cover" if is_cover else "body", "asset": asset})
    if cover and not cover_present:
        plan.append({"resource_index": len(plan), "asset_index": None, "role": "cover",
                     "asset": {"kind": "image", "url": cover}})
    return plan


def seed_smoke(bundle, article_dir, plan, smoke):
    """Optional verified copy from a matching article-key directory."""
    if article_dir.name != smoke.name or not (smoke / "manifest.json").is_file():
        return 0
    records = json.loads(safe_path(smoke, smoke / "manifest.json").read_text(encoding="utf-8"))
    wanted = set()
    for entry in plan:
        asset = entry["asset"]
        if isinstance(asset, dict) and isinstance(asset.get("url"), str):
            key = hashlib.sha256(urldefrag(asset["url"])[0].encode()).hexdigest()[:24]
            wanted.add((asset.get("kind"), key))
    copied = 0
    for record in records:
        if record.get("status") not in {"downloaded", "skipped"}:
            continue
        relative = Path(record.get("local_path", ""))
        if relative.is_absolute() or len(relative.parts) != 2:
            raise BundleError("invalid_smoke_manifest")
        match = re.fullmatch(r"([0-9a-f]{24})-([0-9a-f]{64})\.[a-z0-9]{1,5}", relative.name)
        if match is None or (record.get("kind"), match[1]) not in wanted:
            continue
        if relative.parts[0] != FOLDERS.get(record.get("kind")) or match[2] != record.get("sha256"):
            raise BundleError("invalid_smoke_manifest")
        source = safe_path(smoke, smoke / relative)
        target = safe_path(bundle, safe_path(article_dir, article_dir / relative))
        if target.exists():
            continue  # The downloader still verifies it before any skip.
        if fingerprint(source) != (record["bytes"], record["sha256"]):
            raise BundleError("smoke_checksum_mismatch")
        safe_path(bundle, target.parent).mkdir(parents=True, exist_ok=True)
        part = None
        try:
            with source.open("rb") as src, tempfile.NamedTemporaryFile("wb", dir=target.parent,
                    prefix=".media-seed-", suffix=".part", delete=False) as dst:
                part = Path(dst.name)
                for chunk in iter(lambda: src.read(65536), b""):
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if fingerprint(part) != (record["bytes"], record["sha256"]):
                raise BundleError("smoke_copy_checksum_mismatch")
            safe_path(bundle, target)
            os.replace(part, target)
            if fingerprint(target) != (record["bytes"], record["sha256"]):
                raise BundleError("smoke_copy_checksum_mismatch")
            copied += 1
        finally:
            if part is not None:
                with suppress(OSError, BundleError):
                    safe_path(bundle, part).unlink(missing_ok=True)
    return copied


def fetch_asset(asset, article_dir, requester=None):
    events = []
    with requests.Session() as session:
        session.trust_env = False
        # Do not import credentials or cookies; keep default TLS verification.
        ordinary_get = session.get

        def tracked_get(url, **kwargs):
            event = {}
            started = time.monotonic()
            try:
                if requester is None:
                    response = ordinary_get(url, **kwargs)
                else:
                    response = requester(ordinary_get, url, kind="image", **kwargs)
                event["http_status"] = response.status_code
                return response
            except Exception as exc:
                event["exception_type"] = type(exc).__name__
                raise
            finally:
                event["elapsed_seconds"] = round(time.monotonic() - started, 4)
                events.append(event)

        session.get = tracked_get
        row, = download_media_assets([asset], article_dir, session=session, timeout=(10, 30))
    return row, events


def localize_tag(raw, href):
    """Edit only actual img attributes, not matching text inside other values."""
    edits, present = [], set()
    for match in ATTRIBUTE.finditer(raw, raw.lower().find("img") + 3):
        name = match["name"].lower()
        if name in {"src", "data-src"}:
            present.add(name)
            edits.append((match.start("name"), match.end(), match["name"] + '="' + escape(href, quote=True) + '"'))
        elif name in {"srcset", "data-srcset"}:
            edits.append((match.start(), match.end(), ""))
    for start, end, replacement in reversed(edits):
        raw = raw[:start] + replacement + raw[end:]
    missing = ''.join(' ' + name + '="' + escape(href, quote=True) + '"' for name in ("src", "data-src") if name not in present)
    end = raw.rfind("/>") if raw.endswith("/>") else raw.rfind(">")
    return raw[:end] + missing + raw[end:]


class ImageRewriter(HTMLParser):
    def __init__(self, source, by_url, base_url):
        super().__init__(convert_charrefs=False)
        self.source, self.by_url, self.base_url = source, by_url, base_url
        self.lines = [0] + [m.end() for m in re.finditer("\n", source)]
        self.edits = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        values = dict(attrs)
        for name in ("data-src", "src"):
            value = values.get(name)
            if not value:
                continue
            identity = urldefrag(urljoin(self.base_url, value.strip()))[0]
            if identity not in self.by_url:
                # Match the parser's data-src-first rule; a downloaded thumbnail
                # must not stand in for a failed/unresolved original image.
                return
            raw = self.get_starttag_text()
            line, column = self.getpos()
            start = self.lines[line - 1] + column
            self.edits.append((start, start + len(raw), localize_tag(raw, self.by_url[identity])))
            break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def body_text(source):
    parser = TextReader()
    parser.feed(source)
    parser.close()
    return "".join(parser.parts)


def local_html(snapshot, plan, rows, bundle):
    by_url = {}
    # Pair by the original resource indices in memory, never by redacted URLs.
    by_index = {row["resource_index"]: row for row in rows}
    for entry in plan:
        row = by_index[entry["resource_index"]]
        asset = entry["asset"]
        if row["kind"] != "image" or row["status"] not in {"downloaded", "skipped"}:
            continue
        target = safe_path(bundle, bundle / row["local_path"])
        target = safe_path(snapshot["article_dir"], target)
        href = target.relative_to(snapshot["article_dir"]).as_posix()
        by_url.setdefault(urldefrag(asset["url"])[0], href)
    source = snapshot["html_source"]
    metadata = snapshot["metadata"]
    base_url = metadata.get("canonical_url") or metadata.get("source_url") or "https://mp.weixin.qq.com/"
    parser = ImageRewriter(source, by_url, base_url)
    parser.feed(source)
    parser.close()
    result = source
    for start, end, replacement in reversed(parser.edits):
        result = result[:start] + replacement + result[end:]
    if body_text(source) != body_text(result):
        raise BundleError("body_text_changed")
    if snapshot["is_fragment"]:
        title = escape(str(metadata.get("title") or snapshot["article_key"]))
        result = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><title>' + title + '</title></head><body>\n' + result + '\n</body></html>\n'
    return result, len(parser.edits)


def process_article(snapshot, bundle, smoke):
    key, article_dir = snapshot["article_key"], snapshot["article_dir"]
    safe_path(bundle, article_dir)
    plan = asset_plan(snapshot["metadata"])
    seed_count, seed_error = 0, ""
    if smoke is not None:
        try:
            seed_count = seed_smoke(bundle, article_dir, plan, smoke)
        except (OSError, ValueError, BundleError, KeyError, TypeError):
            seed_error = "smoke_seed_failed"  # Optional cache seed; do not stop downloads.
    rows, events = [], []
    for entry in plan:
        row, attempts = fetch_asset(entry["asset"], article_dir)
        if row["status"] in {"downloaded", "skipped"}:
            try:
                relative = Path(row["local_path"])
                if relative.is_absolute():
                    raise BundleError("unsafe_path")
                target = safe_path(bundle, safe_path(article_dir, article_dir / relative))
                if fingerprint(target) != (row["bytes"], row["sha256"]):
                    raise BundleError("file_verification_failed")
                row["local_path"] = target.relative_to(bundle).as_posix()
            except (OSError, BundleError) as exc:
                row.update(status="error", error_code=str(exc) if isinstance(exc, BundleError) else "file_verification_failed",
                           local_path="", bytes=0, sha256="")
        row.update(article_key=key, mid=key.rsplit("_", 1)[0], idx=int(key.rsplit("_", 1)[1]),
                   resource_index=entry["resource_index"], asset_index=entry["asset_index"], role=entry["role"])
        rows.append(row)
        events.extend(dict(event, resource_index=entry["resource_index"]) for event in attempts)
    manifest = article_dir / "media_manifest.json"
    atomic_json(bundle, manifest, rows)
    html, image_count = local_html(snapshot, plan, rows, bundle)
    html_path = article_dir / "article.local.html"
    atomic_text(bundle, html_path, html)
    counts = dict(Counter(row["status"] for row in rows))
    return {"rows": rows, "report": {
        "article_key": key, "metadata_sha256": snapshot["metadata_sha256"],
        "media_manifest": manifest.relative_to(bundle).as_posix(),
        "article_local_html": html_path.relative_to(bundle).as_posix(),
        "resource_count": len(rows), "status_counts": counts,
        "error_codes": dict(Counter(row["error_code"] for row in rows if row["error_code"])),
        "smoke_seeded_files": seed_count, "smoke_seed_error": seed_error,
        "rewritten_image_tags": image_count, "body_text_preserved": True,
        "http_request_count": len(events), "http_events": events,
        "status": "partial" if counts.get("error") or counts.get("needs_resolution") else "complete",
    }}


def run_bundle(workers=4, *, bundle_root: Path, smoke_root: Path | None = None):
    """Process a fixed metadata snapshot; return 0 only when it is complete.

    Missing/invalid metadata, article errors and unresolved media return 2.
    smoke_root is opt-in and names an article-key directory with manifest.json;
    default calls never inspect historical smoke artifacts. Existing local media
    are reused only after the downloader verifies their hash and signature.
    Root/path safety errors and failures to persist reports propagate to callers.
    """
    bundle_root = Path(bundle_root)
    bundle = bundle_root.resolve()
    if bundle_root.is_symlink() or getattr(bundle_root, "is_junction", lambda: False)():
        raise BundleError("unsafe_bundle_root")
    smoke = Path(smoke_root).resolve() if smoke_root is not None else None
    started = datetime.now(timezone.utc).isoformat()
    snapshots, snapshot_errors, candidate_count = freeze_snapshot(bundle)
    print(f"SNAPSHOT metadata={candidate_count} ready={len(snapshots)} read_errors={len(snapshot_errors)} workers={workers}", flush=True)
    results, article_errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_article, s, bundle, smoke): s["article_key"] for s in snapshots}
        for number, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            try:
                result = future.result()
                results.append(result)
                counts = result["report"]["status_counts"]
                print(f"[{number}/{len(snapshots)}] {key} " + " ".join(f"{name}={counts.get(name, 0)}" for name in
                      ("downloaded", "skipped", "needs_resolution", "error")), flush=True)
            except Exception as exc:
                code = str(exc) if isinstance(exc, BundleError) else "article_media_processing_failed"
                article_errors.append({"article_key": key, "error_code": code, "exception_type": type(exc).__name__})
                print(f"[{number}/{len(snapshots)}] {key} error={code}", flush=True)
    results.sort(key=lambda r: r["report"]["article_key"])
    rows = [row for result in results for row in result["rows"]]
    counts = dict(Counter(row["status"] for row in rows))
    files = {row["local_path"]: row["bytes"] for row in rows if row["status"] in {"downloaded", "skipped"}}
    report = {
        "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_metadata_count": candidate_count, "snapshot_ready_count": len(snapshots),
        "snapshot_article_keys": [s["article_key"] for s in snapshots],
        "snapshot_errors": snapshot_errors, "article_errors": article_errors,
        "processed_article_count": len(results), "resource_count": len(rows), "status_counts": counts,
        "error_codes": dict(Counter(row["error_code"] for row in rows if row["error_code"])),
        "reusable_file_count": len(files), "reusable_bytes": sum(files.values()),
        "http_request_count": sum(r["report"]["http_request_count"] for r in results),
        "smoke_seeded_files": sum(r["report"]["smoke_seeded_files"] for r in results),
        "client": {"library": "requests", "trust_env": False, "tls_verification": True,
                   "imported_cookies_or_credentials": False},
        "index_contract": {"asset_index": "zero-based original media_assets index; null for appended cover",
                           "resource_index": "zero-based order including cover", "local_path": "relative to bundle root"},
        "articles": [result["report"] for result in results],
        "new_articles_after_start": "not scanned or processed; rerun run_bundle to include them",
    }
    failed = bool(not snapshots or snapshot_errors or article_errors or counts.get("error") or counts.get("needs_resolution"))
    report["status"] = "partial" if failed else "complete"
    atomic_json(bundle, bundle / "media_rows.json", rows)
    atomic_json(bundle, bundle / "media_download_report.json", report)
    print("SUMMARY " + json.dumps({k: report[k] for k in ("status", "snapshot_metadata_count", "processed_article_count",
          "resource_count", "status_counts", "reusable_file_count", "reusable_bytes", "http_request_count", "error_codes")}), flush=True)
    return 2 if failed else 0
