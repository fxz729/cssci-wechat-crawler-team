"""Offline, single-writer incremental exports of validated session snapshots.

Only identities, row hashes and media summaries stay in memory. New articles are
flushed append-only; changed rows and media batches stream through an atomic CSV
replacement. Call save/update_media serially in the runner, not media workers.
"""
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
from html import escape, unescape
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from export.realtime_exporter import default_export_headers
from parsers.article_parser import parse_article_html, _parse_time


EXTRA_HEADERS = [
    "url", "content_text", "content_origin", "publish_timestamp", "cover_url",
    "local_html", "local_text", "local_metadata", "local_article_dir",
    "metric_fields", "identity_matches", "business_ret", "six_metrics_present",
    "body_sha256", "captured_at", "media_urls", "media_manifest", "media_status",
    "media_downloaded", "media_pending", "media_local_paths", "image_local_paths",
    "audio_local_paths", "video_local_paths",
]
HEADERS = default_export_headers([]) + EXTRA_HEADERS
METRICS = {
    "read_count": "read_num", "like_count": "old_like_count", "watching_count": "like_count",
    "share_count": "share_count", "favorite_count": "collect_count", "comment_count": "comment_count",
}
SOURCE = "$html.cgiDataNew.user_info.appmsg_bar_data."
PATH_FIELDS = ("media_local_paths", "image_local_paths", "audio_local_paths", "video_local_paths")
MEDIA_FIELDS = ("media_manifest", "media_status", "media_downloaded", "media_pending", *PATH_FIELDS)
MEDIA_HEADERS = [
    "article_key", "biz", "mid", "idx", "kind", "role", "resource_index", "asset_index",
    "id", "url", "local_path", "status", "bytes", "sha256", "error_code",
]
CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; "
       "style-src 'unsafe-inline'; img-src data:; object-src 'none'; frame-src 'none'; "
       "base-uri 'none'; form-action 'none'")


def _identity(article):
    url = article.get("url") or article.get("canonical_url")
    if not isinstance(url, str):
        raise ValueError("invalid_article_identity")
    parsed = urlsplit(unescape(url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    keys = ("__biz", "mid", "idx", "sn")
    if (parsed.scheme not in ("http", "https") or parsed.netloc != "mp.weixin.qq.com"
            or parsed.path != "/s" or any(len(query.get(k, [])) != 1 for k in keys)):
        raise ValueError("invalid_article_identity")
    biz, mid, idx, sn = (query[k][0] for k in keys)
    if (not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", biz)
            or not re.fullmatch(r"[0-9]{1,30}", mid)
            or not re.fullmatch(r"[0-9]{1,3}", idx) or not 1 <= int(idx) <= 100
            or not re.fullmatch(r"[a-fA-F0-9]{32}", sn)):
        raise ValueError("invalid_article_identity")
    if (article.get("biz") != biz or str(article.get("mid")) != mid
            or str(article.get("idx")) != str(int(idx)) or article.get("sn") != sn):
        raise ValueError("article_identity_mismatch")
    canonical = "https://mp.weixin.qq.com/s?" + urlencode(dict(__biz=biz, mid=mid, idx=int(idx), sn=sn))
    return biz, mid, int(idx), sn, canonical


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _cells(row, headers=HEADERS):
    return {key: _cell(row.get(key)) for key in headers}


def _signature(row):
    digest = hashlib.sha256()
    for key in HEADERS:
        value = _cell(row.get(key)).encode("utf-8")
        digest.update(str(len(value)).encode("ascii") + b":" + value)
    return digest.hexdigest()


@contextmanager
def _atomic_stream(path, encoding="utf-8"):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path, value):
    with _atomic_stream(path) as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")


def _numeric(value):
    return not isinstance(value, bool) and isinstance(value, (str, int)) and re.fullmatch(r"[0-9]{1,15}", str(value)) is not None


class SessionArticleExporter:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(part.rstrip(" .").lower() == "production" for part in self.root.parts):
            raise ValueError("unsafe_export_path")
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ValueError("unsafe_export_path")
        self.root = self.root.resolve()
        self.csv_path = self._path("文章数据.csv")
        self._index = {}  # mid_idx -> (sn, committed CSV hash or None for an orphan)
        self._media = {}
        self._biz = None
        limit = sys.maxsize
        while limit > csv.field_size_limit():
            try:
                csv.field_size_limit(limit)
                break
            except OverflowError:
                limit //= 10
        if self.csv_path.exists() and self.csv_path.stat().st_size:
            for row in self._csv_rows():
                biz, mid, idx, sn, _ = _identity(row)
                key = f"{mid}_{idx}"
                self._check_identity(biz, key, sn)
                if key in self._index:
                    raise ValueError("duplicate_csv_identity")
                self._biz = biz
                self._index[key] = (sn, _signature(row))
                if row["media_status"] != "not_downloaded":
                    self._media[key] = self._media_from_csv(row)
        # CSV is authoritative. Orphan metadata only reserves identity/biz, never
        # proves that a row was appended successfully.
        attachments = self._path("附件")
        if attachments.exists():
            for path in attachments.glob("*/metadata.json"):
                if path.parent.name in self._index:
                    continue
                path = self._path(path.relative_to(self.root).as_posix())
                with path.open(encoding="utf-8") as stream:
                    row = json.load(stream)
                biz, mid, idx, sn, _ = _identity(row)
                key = f"{mid}_{idx}"
                if path.parent.name != key:
                    raise ValueError("invalid_metadata_identity")
                self._check_identity(biz, key, sn)
                self._biz = biz
                self._index[key] = (sn, None)

    def _path(self, value):
        if not isinstance(value, str) or not value or ":" in value:
            raise ValueError("unsafe_export_path")
        relative = Path(value.replace("\\", "/"))
        if (relative.is_absolute() or PureWindowsPath(value).drive or ".." in relative.parts
                or any(part.rstrip(" .").lower() == "production" for part in relative.parts)):
            raise ValueError("unsafe_export_path")
        target = self.root / relative
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("unsafe_export_path")
        for parent in (target, *target.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ValueError("unsafe_export_path")
            if parent == self.root:
                break
        return target

    def _check_identity(self, biz, key, sn):
        if self._biz is not None and self._biz != biz:
            raise ValueError("single_journal_biz_mismatch")
        if key in self._index and self._index[key][0] != sn:
            raise ValueError("article_identity_conflict")

    def _csv_rows(self):
        with self._path("文章数据.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != HEADERS:
                raise ValueError("incompatible_csv_header")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("incomplete_csv_row")
                yield row

    def _media_from_csv(self, row):
        values = {key: row[key] for key in MEDIA_FIELDS}
        for key in PATH_FIELDS:
            values[key] = json.loads(row[key] or "[]")
        for key in ("media_downloaded", "media_pending"):
            values[key] = int(row[key] or 0)
        return values

    def _row(self, article, identity):
        biz, mid, idx, sn, url = identity
        content_html = article.get("content_html")
        if not isinstance(content_html, str):
            raise ValueError("invalid_article_content")
        parsed = parse_article_html('<div id="js_content">' + content_html + '</div>', url)
        extra = []
        if str(article.get("article_type")) == "9":
            for asset in article.get("media_assets") or []:
                if (isinstance(asset, dict) and asset.get("kind") == "video"
                        and isinstance(asset.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", asset["id"])):
                    extra.append({"kind": "video", "id": asset["id"]})
                elif isinstance(asset, dict) and asset.get("kind") == "image":
                    from utils.public_content import public_url
                    image_url = public_url(asset.get("url"))
                    if image_url:
                        extra.append({"kind": "image", "url": image_url})
        if parsed is None:
            if not extra:
                raise ValueError("empty_article_content")
            parsed = SimpleNamespace(content_text="", word_count=0, image_urls=[], image_count=0,
                                     audio_count=0, video_count=0, media_assets=[])
        if extra:
            assets = list(parsed.media_assets)
            for asset in extra:
                key = "url" if asset["kind"] == "image" else "id"
                if not any(old.get("kind") == asset["kind"] and old.get(key) == asset[key] for old in assets):
                    assets.append(asset)
            images = list(dict.fromkeys(a["url"] for a in assets if a["kind"] == "image" and a.get("url")))
            parsed = SimpleNamespace(**{**vars(parsed), "media_assets": assets, "image_urls": images,
                                        "image_count": len(images),
                                        "video_count": sum(a["kind"] == "video" for a in assets)})
        row = dict.fromkeys(HEADERS, "")
        for key in ("title", "author", "account_name", "digest", "cover_url", "comment_id", "captured_at"):
            value = article.get(key)
            row[key] = value if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        fields, metrics = {}, {}
        raw_fields = article.get("metric_fields") or {}
        for name, raw_name in METRICS.items():
            source = SOURCE + raw_name
            supplied = raw_fields.get(source, {})
            raw = supplied.get("raw_value")
            present = supplied.get("present") is True
            fields[source] = {"present": present, "raw_value": raw if _numeric(raw) else None}
            value = (article.get("metrics") or {}).get(name)
            metrics[name] = value if (type(value) is int and _numeric(value) and present
                                      and _numeric(raw) and int(raw) == value) else None
        key = f"{mid}_{idx}"
        folder = "附件/" + key
        row.update(
            biz=biz, mid=mid, idx=idx, sn=sn, url=url, source_url=url, canonical_url=url,
            batch_key=f"{biz}:{mid}", position_in_batch=idx,
            profile_url="https://mp.weixin.qq.com/mp/profile_ext?" + urlencode({"action":"home","__biz":biz,"scene":"124"}),
            content_html=content_html, content=parsed.content_text, content_text=parsed.content_text,
            word_count=parsed.word_count, title_length=len(str(row["title"])), digest_length=len(str(row["digest"])),
            content_origin=("cgiDataNew.picture_page_info_list" if any(a["kind"] == "image" for a in extra) else "cgiDataNew.video_page_info") if extra and not content_html else "cgiDataNew.content_noencode",
            cover_image_url=row["cover_url"],
            image_urls=parsed.image_urls, media_assets=parsed.media_assets,
            media_urls=list(dict.fromkeys(asset["url"] for asset in parsed.media_assets if asset.get("url"))),
            image_count=parsed.image_count, audio_count=parsed.audio_count, video_count=parsed.video_count,
            local_article_dir=folder, local_html=folder + "/正文.html", local_text=folder + "/正文.txt",
            local_metadata=folder + "/metadata.json", metric_fields=fields,
            metric_field_status={name: "observed" if value is not None else "not_observed" for name, value in metrics.items()},
            identity_matches=True, business_ret=0, six_metrics_present=all(value is not None for value in metrics.values()),
            metrics_provider=article.get("metrics_provider") if article.get("metrics_provider") in ("background_client_session_html", "fiddler_classic:/s") else "background_client_session_html", metrics_captured_at=row["captured_at"],
            metrics_status="complete" if all(value is not None for value in metrics.values()) else "partial",
            field_provenance={"content": "cgiDataNew.content_noencode", "digest": "cgiDataNew.desc" if row["digest"] else "missing",
                              "metrics": {name: SOURCE + raw_name for name, raw_name in METRICS.items()}},
            crawl_status="success", media_status="metadata_only", media_downloaded=0,
            media_pending=0, **{field: [] for field in PATH_FIELDS},
        )
        if any(a["kind"] == "image" for a in extra):
            row["field_provenance"]["images"] = "cgiDataNew.picture_page_info_list"
        if any(a["kind"] == "video" for a in extra) and url not in row["media_urls"]:
            row["media_urls"].append(url)
            row["field_provenance"]["video_links"] = "cgiDataNew.link"
        row.update(metrics)
        row["collect_num"] = metrics["favorite_count"]
        body_hash = article.get("body_sha256")
        row["body_sha256"] = body_hash if isinstance(body_hash, str) and re.fullmatch(r"[a-fA-F0-9]{64}", body_hash) else ""
        timestamp = article.get("publish_timestamp")
        if isinstance(timestamp, (str, int)) and not isinstance(timestamp, bool):
            row["publish_timestamp"] = timestamp
            try:
                published = (datetime.fromtimestamp(int(timestamp), timezone.utc) if _numeric(timestamp)
                             else _parse_time(timestamp) if isinstance(timestamp, str) else None)
                if published:
                    row["publish_time"] = published.isoformat()
                    row["field_provenance"]["publish_time"] = "cgiDataNew.create_time"
            except (ValueError, OverflowError, OSError):
                pass
        if row["metrics_provider"] == "fiddler_classic:/s":
            # Historical cached fields are not a new background response.
            row["content_origin"] = "legacy_capture.article_event.article.content_html"
            row["field_provenance"]["content"] = row["content_origin"]
            row["field_provenance"]["digest"] = "legacy_capture.article_event.article.digest" if row["digest"] else "missing"
            if row["publish_time"]:
                row["field_provenance"]["publish_time"] = "legacy_capture.article_event.article.publish_time"
            row["field_provenance"]["metrics_captured_at"] = "legacy_capture.metric_event.captured_at"
        row.update(self._media.get(key, {}))
        row.update(media_status="metadata_only", media_pending=0)
        return row

    def save(self, article: dict) -> dict:
        """Persist one validated snapshot and return the complete, typed export row."""
        if (article.get("identity_matches") is not True or isinstance(article.get("business_ret"), bool)
                or article.get("business_ret") not in (0, "0")):
            raise ValueError("unverified_article_snapshot")
        identity = _identity(article)
        biz, mid, idx, sn, _ = identity
        key = f"{mid}_{idx}"
        self._check_identity(biz, key, sn)
        row = self._row(article, identity)
        paths = {field: self._path(row[field]) for field in ("local_html", "local_text", "local_metadata")}
        self._path("文章数据.csv")
        paths["local_html"].parent.mkdir(parents=True, exist_ok=True)
        self._biz = biz
        self._index.setdefault(key, (sn, None))
        html = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="' + escape(CSP, quote=True)
                + '"></head><body><h1>' + escape(str(row["title"])) + '</h1>' + row["content_html"] + '</body></html>')
        with _atomic_stream(paths["local_html"]) as stream:
            stream.write(html)
        with _atomic_stream(paths["local_text"]) as stream:
            stream.write(row["content_text"])
        _atomic_json(paths["local_metadata"], row)
        signature = _signature(row)
        committed = self._index[key][1]
        if committed is None:
            size = self.csv_path.stat().st_size if self.csv_path.exists() else 0
            try:
                with self.csv_path.open("a", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=HEADERS)
                    if not size:
                        writer.writeheader()
                    writer.writerow(_cells(row))
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                if self.csv_path.exists():
                    with self.csv_path.open("r+b") as stream:
                        stream.truncate(size)
                raise
            self._index[key] = (sn, signature)
        elif committed != signature:
            self._rewrite({key: row})
        return row

    def _rewrite(self, changes):
        committed = {}
        with _atomic_stream(self._path("文章数据.csv"), "utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADERS)
            writer.writeheader()
            for row in self._csv_rows():
                key = row["mid"] + "_" + row["idx"]
                if key in changes:
                    row.update(changes[key])
                    committed[key] = (row["sn"], _signature(row))
                writer.writerow(_cells(row))
            if committed.keys() != changes.keys():
                raise ValueError("missing_csv_identity")
        self._index.update(committed)

    def _media_rows(self, key, rows):
        if not isinstance(rows, list):
            raise ValueError("invalid_media_rows")
        cleaned = []
        mid, idx = key.split("_")
        for supplied in rows:
            if not isinstance(supplied, dict):
                raise ValueError("invalid_media_rows")
            row = {name: supplied[name] for name in MEDIA_HEADERS
                   if name in supplied and isinstance(supplied[name], (str, int)) and not isinstance(supplied[name], bool)}
            row.update(article_key=key, biz=self._biz, mid=mid, idx=int(idx))
            if row.get("local_path"):
                row["local_path"] = self._path(row["local_path"]).relative_to(self.root).as_posix()
            cleaned.append(row)
        return cleaned

    def update_media(self, updates):
        """Merge one runner batch; rewrite the article CSV once, never per asset.

        Unknown articles and unsafe paths reject the entire batch before writing.
        A failed CSV replacement leaves its original intact; retry the same batch
        to complete any interrupted manifest/metadata synchronization.
        """
        if not updates:
            return
        patches, manifests = {}, {}
        for key, result in updates.items():
            if key not in self._index or self._index[key][1] is None:
                raise ValueError("unknown_media_article")
            status = result.get("status")
            if status not in ("complete", "partial", "not_downloaded", "failed", "error"):
                raise ValueError("invalid_media_status")
            patch = {"media_status": status, "media_manifest": "附件/" + key + "/media_manifest.json"}
            self._path(patch["media_manifest"])
            self._path("附件/" + key + "/metadata.json")
            for field in ("media_downloaded", "media_pending"):
                value = result.get(field)
                if type(value) is not int or value < 0:
                    raise ValueError("invalid_media_count")
                patch[field] = value
            for field in PATH_FIELDS:
                values = result.get(field)
                if not isinstance(values, list):
                    raise ValueError("invalid_media_paths")
                patch[field] = [self._path(value).relative_to(self.root).as_posix() for value in values]
            patches[key] = patch
            manifests[key] = self._media_rows(key, result.get("rows"))
        self._path("媒体索引.csv")
        for key, rows in manifests.items():
            _atomic_json(self._path(patches[key]["media_manifest"]), rows)
        self._rewrite(patches)
        self._media.update(patches)
        for key, patch in patches.items():
            path = self._path("附件/" + key + "/metadata.json")
            with path.open(encoding="utf-8") as stream:
                stored = json.load(stream)
            row = {name: stored.get(name, "") for name in HEADERS}
            row.update(patch)
            _atomic_json(path, row)
        with _atomic_stream(self._path("媒体索引.csv"), "utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=MEDIA_HEADERS)
            writer.writeheader()
            for key in self._index:
                path = self._path("附件/" + key + "/media_manifest.json")
                if not path.exists():
                    continue
                with path.open(encoding="utf-8") as source:
                    rows = self._media_rows(key, json.load(source))
                for row in rows:
                    writer.writerow(_cells(row, MEDIA_HEADERS))
