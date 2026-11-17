"""Offline global article CSV, protected by the runner's single-writer runtime_lock.

save() projects public content BEFORE persistence and immediately attempts CSV
publication. Durable public row snapshots live outside the delivery root; the CSV
is a recoverable projection, not proof that pending rows were published. Only
identities, signatures and spool paths stay in memory (not all article bodies).

Media workers must use article_media_root(biz), NOT root. update_media() accepts
one full download_article_media result, persists it, and defers CSV replacement
until flush()/close(). Both return False on a CSV sharing violation. Other I/O
errors propagate with the durable row still pending. No networking is performed.
"""
from collections import Counter
import csv
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sys

from crawl.bundle_media import asset_plan
from export.session_article_exporter import (
    CSP, HEADERS as SESSION_HEADERS, MEDIA_FIELDS, PATH_FIELDS,
    SessionArticleExporter, _atomic_json, _atomic_stream, _cells, _identity,
)
from utils.public_content import public_snapshot


HEADERS = SESSION_HEADERS + [
    "journal_name", "journal_id", "article_key", "has_text", "has_images",
    "has_audio", "has_video", "media_index",
]
GLOBAL_MEDIA_FIELDS = (*MEDIA_FIELDS, "media_index")
JSON_FIELDS = {
    "image_urls", "media_assets", "field_provenance", "metric_field_status",
    "metric_fields", "media_urls", "media_index", *PATH_FIELDS,
}
INT_FIELDS = {
    "idx", "position_in_batch", "business_ret", "word_count", "title_length", "digest_length",
    "image_count", "audio_count", "video_count", "media_downloaded", "media_pending",
    "read_count", "watching_count", "like_count", "favorite_count", "share_count",
    "comment_count", "collect_num",
}
BOOL_FIELDS = {"identity_matches", "six_metrics_present", "has_text", "has_images", "has_audio", "has_video"}


def _root_path(value):
    path = Path(value).absolute()
    for parent in (path, *path.parents):
        if (parent.name.rstrip(" .").lower() == "production" or parent.is_symlink()
                or getattr(parent, "is_junction", lambda: False)()):
            raise ValueError("unsafe_export_path")
    return path.resolve()


def _key(biz, mid, idx):
    if (not isinstance(biz, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", biz)
            or not re.fullmatch(r"[0-9]{1,30}", str(mid))
            or not re.fullmatch(r"[0-9]{1,3}", str(idx)) or not 1 <= int(idx) <= 100):
        raise ValueError("invalid_article_identity")
    return biz, str(mid), int(idx)


def _signature(row):
    return hashlib.sha256(json.dumps(_cells(row, HEADERS), ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _typed_csv(row):
    for name in JSON_FIELDS:
        if row.get(name):
            row[name] = json.loads(row[name])
    for name in INT_FIELDS:
        row[name] = int(row[name]) if row.get(name) else None
    for name in BOOL_FIELDS:
        if row.get(name) not in ("True", "False"):
            raise ValueError("invalid_csv_boolean")
        row[name] = row[name] == "True"
    return row


class GlobalArticleExporter(SessionArticleExporter):
    """Reuse Session's path guard and row formatter, never its single-biz CSV.

    The inherited formatter's _media cache stays empty: global media state is
    merged explicitly by (biz, mid, idx), so two journals cannot share state.
    """

    def __init__(self, root, *, work_root=None):
        self.root = _root_path(root)
        self.work_root = _root_path(work_root if work_root is not None else
                                    self.root.parent / ("." + self.root.name + ".global-export"))
        if self.work_root.is_relative_to(self.root):
            raise ValueError("work_root_must_be_outside_delivery")
        scope = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:16]
        self._spool = _root_path(self.work_root / ("global-article-rows-" + scope))
        self.csv_path = self._path("文章数据.csv")
        self._records = {}  # global key -> (sn, row signature, durable public row path)
        self._committed = {}
        self._pending = set()
        self._damaged = False
        self._media = {}  # Used only by the inherited, offline row formatter.
        limit = sys.maxsize
        while limit > csv.field_size_limit():
            try:
                csv.field_size_limit(limit)
                break
            except OverflowError:
                limit //= 10
        if self._spool.exists():
            for path in sorted(self._spool.glob("*.json")):
                path = _root_path(path)
                row = json.loads(path.read_text(encoding="utf-8"))
                key = self._validate_row(row)
                self._check_global_identity(key, row["sn"])
                if key in self._records:
                    raise ValueError("duplicate_spool_identity")
                self._records[key] = (row["sn"], _signature(row), path)
        self._recover_csv()
        self._pending = {key for key, (_, signature, _) in self._records.items()
                         if signature != self._committed.get(key)}
        if self._damaged:
            self._pending.update(self._records)
        self.flush()

    @property
    def pending_count(self) -> int:
        """Number of article rows not yet confirmed in the main CSV (not images)."""
        return len(self._pending)

    def article_media_root(self, biz) -> Path:
        """Stable, case-safe per-biz output_root for download_article_media.

        Downloads land in this directory / 附件 / mid_idx. The hash is of the
        complete biz, never a mutable journal name/id. No directories are made.
        """
        _key(biz, "0", 1)
        folder = "刊-" + hashlib.sha256(biz.encode("utf-8")).hexdigest()
        return self._path("附件/" + folder)

    def _check_global_identity(self, key, sn):
        if key in self._records and self._records[key][0] != sn:
            raise ValueError("article_identity_conflict")

    def _article_folder(self, key):
        biz, mid, idx = key
        return (self.article_media_root(biz) / "附件" / f"{mid}_{idx}").relative_to(self.root).as_posix()

    def _validate_row(self, row):
        if not isinstance(row, dict) or set(row) != set(HEADERS):
            raise ValueError("invalid_public_row")
        biz, mid, idx, _, _ = _identity(row)
        key = (biz, mid, idx)
        folder = self._article_folder(key)
        for name, suffix in (("local_article_dir", ""), ("local_html", "/正文.html"),
                             ("local_text", "/正文.txt"), ("local_metadata", "/metadata.json")):
            if row[name] != folder + suffix:
                raise ValueError("invalid_public_row_path")
            self._path(row[name])
        if row["article_key"] != f"{biz}:{mid}:{idx}" or not isinstance(row["media_index"], list):
            raise ValueError("invalid_public_row")
        return key

    def _load_row(self, key):
        path = _root_path(self._records[key][2])
        row = json.loads(path.read_text(encoding="utf-8"))
        if self._validate_row(row) != key or _signature(row) != self._records[key][1]:
            raise ValueError("public_row_changed_outside_writer")
        return row

    def _persist(self, key, row):
        signature = _signature(row)
        old = self._records.get(key)
        if old and old[1] == signature:
            return
        digest = hashlib.sha256(row["article_key"].encode("utf-8")).hexdigest()
        path = old[2] if old else self._spool / f"{len(self._records):012d}-{digest}.json"
        _root_path(path)
        self._spool.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, row)
        self._records[key] = (row["sn"], signature, path)
        self._pending.add(key)

    def _recover_csv(self):
        if not self.csv_path.exists() or not self.csv_path.stat().st_size:
            return
        seen = set()
        try:
            with self.csv_path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                if reader.fieldnames != HEADERS:
                    raise ValueError("incompatible_csv_header")
                for cells in reader:
                    if None in cells or any(value is None for value in cells.values()):
                        self._damaged = True
                        break
                    signature = _signature(cells)
                    try:
                        row = _typed_csv(cells)
                        key = self._validate_row(row)
                    except (ValueError, TypeError):
                        self._damaged = True
                        break
                    self._check_global_identity(key, row["sn"])
                    if key in seen:
                        self._damaged = True
                    else:
                        seen.add(key)
                        # A copied delivery CSV can bootstrap a new work directory.
                        if key not in self._records:
                            self._persist(key, row)
                        self._committed[key] = signature
        except (csv.Error, UnicodeError):
            self._damaged = True
        except PermissionError:
            self._damaged = True
        if self._damaged and not self._records:
            raise ValueError("csv_recovery_requires_public_records")

    def _initial_media(self, row):
        records = []
        for entry in asset_plan({"media_assets": row["media_assets"]}):
            asset = entry["asset"]
            if asset["kind"] == "image" and asset.get("url") == row["cover_image_url"]:
                entry["role"] = "body_and_cover"
            records.append(dict(article_key=row["article_key"], biz=row["biz"], mid=row["mid"], idx=row["idx"],
                                kind=asset["kind"], role=entry["role"], resource_index=entry["resource_index"],
                                asset_index=entry["asset_index"], id=asset.get("id", ""), url=asset.get("url", ""),
                                local_path="", status="not_downloaded" if asset["kind"] == "image" else "metadata_only",
                                bytes=0, sha256="", error_code=""))
        pending = sum(record["kind"] == "image" for record in records)
        row.update(media_index=records, media_downloaded=0, media_pending=pending,
                   media_status="not_downloaded" if pending else "complete", media_manifest="")
        row.update(has_text=bool(row["content_text"]))
        for kind in ("image", "audio", "video"):
            row["has_images" if kind == "image" else "has_" + kind] = any(record["kind"] == kind for record in records)

    def _write_artifacts(self, row):
        paths = {name: self._path(row[name]) for name in ("local_html", "local_text", "local_metadata")}
        paths["local_html"].parent.mkdir(parents=True, exist_ok=True)
        html = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="' + escape(CSP, quote=True)
                + '"></head><body><h1>' + escape(str(row["title"])) + '</h1>' + row["content_html"] + '</body></html>')
        with _atomic_stream(paths["local_html"]) as stream:
            stream.write(html)
        with _atomic_stream(paths["local_text"]) as stream:
            stream.write(row["content_text"])
        _atomic_json(paths["local_metadata"], row)

    def _returned(self, key, row):
        return {**row, "asset_root": str(self.article_media_root(key[0])), "csv_pending": key in self._pending}

    def save(self, article: dict, *, defer_updates=False) -> dict:
        """Public snapshot -> durable typed row -> immediate CSV attempt.

        csv_pending in the returned row is an ephemeral acknowledgement, not a
        CSV field. A pending row is durable but MUST NOT be reported as published.
        """
        original = article
        article = public_snapshot(article)
        if not article.get("url") and isinstance(original.get("canonical_url"), str):
            article["url"] = public_snapshot({"url": original["canonical_url"]})["url"]
        if (article.get("identity_matches") is not True or isinstance(article.get("business_ret"), bool)
                or article.get("business_ret") not in (0, "0")):
            raise ValueError("unverified_article_snapshot")
        identity = _identity(article)
        key = identity[:3]
        self._check_global_identity(key, identity[3])
        # public_snapshot calls public_html; never parse or persist the raw input.
        if not isinstance(article.get("content_html"), str):
            raise ValueError("invalid_article_content")
        row = self._row(article, identity)
        row.update({name: str(article.get(name) or "") if isinstance(article.get(name), (str, int))
                    and not isinstance(article.get(name), bool) else "" for name in ("journal_name", "journal_id")})
        row["article_key"] = f"{key[0]}:{key[1]}:{key[2]}"
        folder = self._article_folder(key)
        for name in ("local_html", "local_text", "local_metadata"):
            row[name] = folder + "/" + Path(row[name]).name
        row["local_article_dir"] = folder
        self._initial_media(row)
        if key in self._records:
            previous = self._load_row(key)
            if (previous["media_assets"] == row["media_assets"]
                    and previous["cover_image_url"] == row["cover_image_url"]):
                row.update({name: previous[name] for name in GLOBAL_MEDIA_FIELDS})
        self._validate_row(row)
        self._persist(key, row)
        if key in self._pending:
            self._write_artifacts(row)
            if key not in self._committed and not self._damaged:
                self._append(key, row)
            elif not defer_updates:
                self.flush()
        return self._returned(key, row)

    def _append(self, key, row):
        try:
            size = self.csv_path.stat().st_size if self.csv_path.exists() else 0
            with self.csv_path.open("a", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=HEADERS)
                if not size:
                    writer.writeheader()
                writer.writerow(_cells(row, HEADERS))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # Even a partial append is recoverable from the public row. Do not
            # attempt a second unsafe truncate against a potentially locked file.
            self._damaged = True
            if not isinstance(exc, PermissionError):
                raise
            return
        self._committed[key] = self._records[key][1]
        self._pending.discard(key)

    def _media_path(self, value, key):
        if not isinstance(value, str):
            raise ValueError("invalid_media_path")
        if not value:
            return ""
        # First reject absolute/drive/ADS/traversal/symlink paths. Then require
        # the file to be inside THIS journal AND THIS article, not just root.
        self._path(value)
        value = value.replace("\\", "/")
        folder = self._article_folder(key)
        prefix = self.article_media_root(key[0]).relative_to(self.root).as_posix()
        relative = value if value.startswith(folder + "/") else prefix + "/" + value
        target = self._path(relative)
        if not target.is_relative_to(self._path(folder)):
            raise ValueError("media_article_path_mismatch")
        return target.relative_to(self.root).as_posix()

    def update_media(self, biz, mid, idx, media_result) -> dict:
        """Persist one full article media result; flush() batches CSV publication.

        Paths may be media-root-relative or already global-relative. Absolute,
        cross-article and cross-journal paths are rejected before any row write.
        Unknown/private result fields are never copied to the public spool.
        """
        key = _key(biz, mid, idx)
        if key not in self._records:
            raise ValueError("unknown_media_article")
        row = self._load_row(key)
        if not isinstance(media_result, dict) or not isinstance(media_result.get("rows"), list):
            raise ValueError("invalid_media_rows")
        if media_result.get("status") not in ("complete", "partial", "not_downloaded", "failed", "error"):
            raise ValueError("invalid_media_status")
        planned = {r["resource_index"]: r for r in row["media_index"]}
        records = {}
        for supplied in media_result["rows"]:
            if not isinstance(supplied, dict):
                raise ValueError("invalid_media_rows")
            for field in ("biz", "mid", "idx", "sn"):
                if field in supplied and str(supplied[field]) != str(row[field]):
                    raise ValueError("media_article_identity_mismatch")
            index = supplied.get("resource_index")
            if type(index) is not int or index not in planned or index in records:
                raise ValueError("invalid_media_resource_index")
            record = dict(planned[index])
            if supplied.get("kind") != record["kind"]:
                raise ValueError("media_kind_mismatch")
            # Reuse the shared public projection for downloader URLs, not a
            # second credential sanitizer or a raw dump of the result object.
            safe = public_snapshot({"media_assets": [supplied]})["media_assets"][0]
            for field in ("url", "id"):
                if safe.get(field) and safe[field] != record[field]:
                    raise ValueError("media_source_mismatch")
            record["url"] = safe.get("url", "")
            status = supplied.get("status")
            if status not in ("downloaded", "skipped", "not_downloaded", "pending", "unsupported", "failed", "error", "metadata_only"):
                raise ValueError("invalid_media_record_status")
            if record["kind"] != "image" and status != "metadata_only":
                raise ValueError("non_image_must_be_metadata_only")
            local = self._media_path(supplied.get("local_path", ""), key)
            if bool(local) != (status in ("downloaded", "skipped")):
                raise ValueError("invalid_media_record_path")
            if local and not self._path(local).is_file():
                raise ValueError("missing_media_file")
            size, sha, error = supplied.get("bytes", 0), supplied.get("sha256", ""), supplied.get("error_code", "")
            if type(size) is not int or size < 0 or not isinstance(sha, str) or (sha and not re.fullmatch(r"[a-fA-F0-9]{64}", sha)):
                raise ValueError("invalid_media_fingerprint")
            if not isinstance(error, str) or (error and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error)):
                raise ValueError("invalid_media_error_code")
            record.update(status=status, local_path=local, bytes=size, sha256=sha, error_code=error)
            records[index] = record
        if records.keys() != planned.keys():
            raise ValueError("incomplete_media_result")
        ordered = [records[i] for i in sorted(records)]
        paths = [record["local_path"] for record in ordered if record["local_path"]]
        counts = {"media_downloaded": len(paths),
                  "media_pending": sum(r["kind"] == "image" and not r["local_path"] for r in ordered)}
        for name, count in counts.items():
            if type(media_result.get(name)) is not int or media_result[name] != count:
                raise ValueError("invalid_media_count")
        if media_result["status"] == "complete" and counts["media_pending"]:
            raise ValueError("incomplete_media_status")
        patch = dict(media_index=ordered, media_status=media_result["status"], **counts)
        for name in PATH_FIELDS:
            expected = paths if name == "media_local_paths" else [r["local_path"] for r in ordered
                        if r["local_path"] and r["kind"] == name.split("_", 1)[0]]
            values = media_result.get(name, [])
            if not isinstance(values, list) or Counter(self._media_path(v, key) for v in values) != Counter(expected):
                raise ValueError("invalid_media_summary_paths")
            patch[name] = expected
        # The downloader owns its manifest; keep it media-root-relative for its
        # restart cache. The CSV embeds the global-relative index separately.
        manifest = self._article_folder(key) + "/media_manifest.json"
        patch["media_manifest"] = manifest if self._path(manifest).is_file() else ""
        row.update(patch)
        self._persist(key, row)
        return self._returned(key, row)

    def flush(self) -> bool:
        """Atomically publish all pending rows once. False means CSV still locked."""
        if not self._pending and not self._damaged:
            return True
        self.root.mkdir(parents=True, exist_ok=True)
        for key in self._records:
            if key in self._pending:
                self._write_artifacts(self._load_row(key))
        try:
            with _atomic_stream(self._path("文章数据.csv"), "utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=HEADERS)
                writer.writeheader()
                for key in self._records:
                    writer.writerow(_cells(self._load_row(key), HEADERS))
        except PermissionError:
            return False
        self._committed = {key: value[1] for key, value in self._records.items()}
        self._pending.clear()
        self._damaged = False
        return True

    def close(self) -> bool:
        """No background worker/lock to release; publish pending rows if possible."""
        return self.flush()
