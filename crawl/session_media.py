"""Single-article media downloads for exported session rows, without session auth."""
from collections.abc import Callable, Mapping
from pathlib import Path
from html import unescape
import json
import re
from urllib.parse import parse_qsl, urldefrag, urlsplit

from crawl import bundle_media
from crawl.request_governor import TrafficStopped
from utils.public_content import public_url


def _public_media_url(asset):
    """Preserve public links; signed private video URLs are ID-only metadata."""
    value = asset.get("url")
    if not value or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return ""
    clean = public_url(value)
    if clean and asset["kind"] == "video":
        original_query = urlsplit(unescape(value.strip())).query
        signed_keys = {"sign", "sig", "signature", "vkey", "encfilekey", "authkey",
                       "auth_key", "txsecret", "policy", "key-pair-id"}
        if (original_query != urlsplit(clean).query or any(
                key.lower() in signed_keys or key.lower().startswith(("x-amz-", "x-goog-"))
                for key, _ in parse_qsl(original_query))):
            return ""
    return clean


def download_article_media(row: Mapping, output_root: str | Path, *,
                           should_stop: Callable[[], bool] | None = None,
                           requester=None) -> dict:
    """Download body images under 附件/<mid>_<idx>, never HTML/text/CSV or OCR.

    Preserve the result keys and atomically save rows in media_manifest.json.
    Audio/video are metadata_only: no fetch, no local path and no pending count.
    Independent covers are not planned. Public URL paths/queries are retained;
    authentication queries are removed, and private signed videos keep only id.
    resource_index/asset_index are zero-based original media_assets positions.
    Counters and path lists count body image occurrences (including cache skips),
    not unique URLs. Duplicate URLs share a fetch but each occurrence is verified.
    Old media files are never deleted; URL cache validation stays in the downloader.

    media_assets accepts a list, a JSON list string, or None/empty CSV cell.
    Invalid structures raise BundleError("invalid_media_assets") before writes or
    downloads. should_stop is checked before each image; once true, all remaining
    images are pending, while audio/video metadata is still emitted. Resume by
    calling again; the existing downloader revalidates cached files.
    """
    mid, idx = str(row.get("mid", "")), str(row.get("idx", ""))
    if not re.fullmatch(r"[0-9]+", mid) or not re.fullmatch(r"[0-9]+", idx):
        raise bundle_media.BundleError("invalid_article_directory")
    article_key = f"{mid}_{idx}"
    root = Path(output_root).resolve()
    article_dir = bundle_media.safe_path(root, root / "附件" / article_key)
    manifest = bundle_media.safe_path(root, article_dir / "media_manifest.json")
    assets = row.get("media_assets")
    if assets is None or (isinstance(assets, str) and not assets.strip()):
        assets = []
    elif isinstance(assets, str):
        try:
            assets = json.loads(assets)
        except ValueError:
            raise bundle_media.BundleError("invalid_media_assets") from None
    if (not isinstance(assets, list) or any(
            not isinstance(asset, Mapping)
            or not isinstance(asset.get("kind"), str)
            or asset["kind"] not in {"image", "audio", "video"}
            or any(asset.get(key) is not None and not isinstance(asset.get(key), str)
                   for key in ("url", "id")) for asset in assets)):
        raise bundle_media.BundleError("invalid_media_assets")
    # Do not pass the cover into the planner: it must never append a task.
    plan = bundle_media.asset_plan({"media_assets": assets})
    cover = row.get("cover_image_url") or row.get("cover_url") or ""
    for entry in plan:
        if cover and entry["asset"].get("kind") == "image" and entry["asset"].get("url") == cover:
            entry["role"] = "body_and_cover"
    article_dir.mkdir(parents=True, exist_ok=True)
    previous = []
    if manifest.exists():
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            pass
    rows, fetched = [], {}
    stopped = False
    stop_error = None
    for entry in plan:
        asset = entry["asset"]
        # Neither article credentials nor arbitrary per-asset headers are passed
        # to fetch_asset, which creates its own credential-free TLS session.
        if isinstance(asset, Mapping):
            asset = {key: asset[key] for key in ("kind", "url", "id") if key in asset}
        public_id = asset.get("id") or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", public_id):
            public_id = ""
        record = {
            "article_key": article_key, "mid": mid, "idx": int(idx),
            "biz": row.get("biz", ""), "title": row.get("title", ""),
            "kind": asset["kind"], "url": _public_media_url(asset), "id": public_id,
            "local_path": "", "status": "metadata_only", "bytes": 0,
            "sha256": "", "error_code": "", "role": entry["role"],
            "resource_index": entry["resource_index"], "asset_index": entry["asset_index"],
        }
        if asset["kind"] != "image":
            rows.append(record)
            continue
        if stopped or (should_stop is not None and should_stop()):
            stopped = True
            record.update(status="pending", error_code="stopped")
            rows.append(record)
            continue
        cached = None
        if isinstance(asset, Mapping) and asset.get("id") and not asset.get("url"):
            for old in previous:
                if (not isinstance(old, dict) or old.get("id") != asset["id"]
                        or old.get("kind") != asset.get("kind") or old.get("status") not in {"downloaded", "skipped"}):
                    continue
                try:
                    if not isinstance(old.get("local_path"), str):
                        continue
                    relative = Path(old["local_path"])
                    if relative.is_absolute() or relative.drive or ".." in relative.parts:
                        continue
                    target = bundle_media.safe_path(article_dir, root / relative)
                    if bundle_media.fingerprint(target) != (old.get("bytes"), old.get("sha256")):
                        continue
                    cached = dict(record, local_path=target.relative_to(root).as_posix(),
                                  bytes=old["bytes"], sha256=old["sha256"], status="skipped")
                    break
                except (OSError, ValueError, bundle_media.BundleError):
                    continue
        if cached:
            rows.append(cached)
            continue
        # Deduplicate actual request URLs in memory, not sanitized public URLs.
        url_key = urldefrag(asset["url"])[0] if record["url"] else ""
        if url_key and url_key in fetched:
            downloaded = dict(fetched[url_key], status="skipped")
        else:
            try:
                if requester is None:
                    downloaded, _ = bundle_media.fetch_asset(asset, article_dir)
                else:
                    downloaded, _ = bundle_media.fetch_asset(asset, article_dir, requester=requester)
            except TrafficStopped as exc:
                stopped, stop_error = True, exc
                record.update(status="pending", error_code="stopped")
                rows.append(record)
                continue
        record.update(status=downloaded["status"], error_code=downloaded["error_code"])
        if record["status"] in {"downloaded", "skipped"}:
            try:
                relative = Path(downloaded["local_path"])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise bundle_media.BundleError("unsafe_path")
                target = bundle_media.safe_path(root, bundle_media.safe_path(article_dir, article_dir / relative))
                if not target.is_file() or bundle_media.fingerprint(target) != (downloaded["bytes"], downloaded["sha256"]):
                    raise bundle_media.BundleError("file_verification_failed")
                record.update(local_path=target.relative_to(root).as_posix(),
                              bytes=downloaded["bytes"], sha256=downloaded["sha256"])
                if url_key:
                    fetched[url_key] = downloaded
            except (OSError, bundle_media.BundleError) as exc:
                code = str(exc) if isinstance(exc, bundle_media.BundleError) else "file_verification_failed"
                record.update(status="error", error_code=code)
        rows.append(record)

    bundle_media.atomic_json(root, manifest, rows)
    if stop_error is not None:
        raise stop_error
    images = [record for record in rows if record["kind"] == "image"]
    paths = [record["local_path"] for record in images if record["local_path"]]
    pending = len(images) - len(paths)
    result = {"status": "partial" if pending else "complete", "rows": rows,
              "media_local_paths": paths, "media_downloaded": len(paths), "media_pending": pending}
    for kind in ("image", "audio", "video"):
        result[kind + "_local_paths"] = [record["local_path"] for record in rows
                                         if record["kind"] == kind and record["local_path"]]
    return result
