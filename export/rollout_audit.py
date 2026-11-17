"""Strict offline rollout audit. Never repair capture state or source artifacts.

``base`` is the run directory containing catalog/state files and ``bundle``.
CSV export belongs to the caller. ``write_outputs`` only creates a uniquely named
JSON report in the existing run directory; it never writes CSV or Excel files.
Checks are boolean gates, errors/warnings contain codes and public identities,
and baseline coverage compares article identities, not media resource counts.
"""
from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from uuid import uuid4

from capture.reader import METRIC_FIELDS, normalize_event
from crawl.catalog_contract import (catalog_evidence_refs, catalog_runtime_membership,
                                    catalog_scan_metadata_valid, catalog_sequence)
from export.journal_validation import load_bundle_records
from export.realtime_exporter import default_export_headers

_ID_FIELDS = ("biz", "mid", "idx", "sn")
_LOCAL_FIELDS = ("local_html", "local_text", "local_article_dir", "capture_evidence")
_READ_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, csv.Error, sqlite3.Error)


def _identity(row, expected_biz):
    if not isinstance(row, dict):
        raise ValueError("invalid_identity")
    biz, mid, idx, sn = (row.get(k) for k in _ID_FIELDS)
    if (biz != expected_biz or not isinstance(biz, str)
            or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", biz)
            or not isinstance(mid, str) or not re.fullmatch(r"[0-9]{1,100}", mid)
            or type(idx) is not int or not 1 <= idx <= 100
            or not isinstance(sn, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", sn)):
        raise ValueError("invalid_identity")
    return biz, mid, idx, sn


def _public(identity):
    return dict(zip(_ID_FIELDS, identity))


def _title(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _safe_path(root, value, *, directory=False):
    """Reject absolute/traversing paths and links, including linked ancestors."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_path")
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.drive or windows.root or ":" in value or ".." in windows.parts or ".." in path.parts:
        raise ValueError("unsafe_path")
    target = root / path
    for component in (target, *target.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("linked_path")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("outside_bundle")
    if not (target.is_dir() if directory else target.is_file()):
        raise ValueError("missing_path")
    return target


def _json(root, relative, kind):
    value = json.loads(_safe_path(root, relative).read_text(encoding="utf-8-sig"))
    if not isinstance(value, kind):
        raise ValueError("invalid_json_shape")
    return value


def _metadata(bundle, *, metadata_only=False):
    _safe_path(bundle, "articles", directory=True)
    paths = sorted(bundle.glob("articles/*/metadata.json"))
    values = []
    for path in paths:
        value = _json(bundle, path.relative_to(bundle).as_posix(), dict)
        if not value:
            raise ValueError("empty_metadata")
        values.append(value)
    if metadata_only:
        return paths, values
    # The established loader is deliberately read-only. Validate before calling:
    # it otherwise silently drops malformed metadata/manifest objects.
    records, _ = load_bundle_records(bundle)
    return paths, records


def _database_identities(bundle):
    source = _safe_path(bundle, "journal.db")
    files = [source]
    for suffix in ("-wal", "-journal"):
        if source.with_name(source.name + suffix).exists():
            files.append(_safe_path(bundle, source.name + suffix))
    stamps = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    # Open a disposable copy, not the original: SQLite read-only WAL mode can
    # still create shared-memory files in the original directory.
    with tempfile.TemporaryDirectory(prefix="rollout-audit-db-") as temporary:
        copy = Path(temporary) / source.name
        for path in files:
            shutil.copyfile(path, Path(temporary) / path.name)
        if stamps != [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]:
            raise ValueError("database_changed_during_copy")
        if any(source.with_name(source.name + s).exists() != any(p.name == source.name + s for p in files)
               for s in ("-wal", "-journal")):
            raise ValueError("database_changed_during_copy")
        connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("database_corrupt")
            return [dict(zip(_ID_FIELDS, row)) for row in connection.execute("SELECT biz, mid, idx, sn FROM articles")]
        finally:
            connection.close()


def _csv(root, name, required):
    path = _safe_path(root, name)
    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(max(previous_limit, 64 * 1024 * 1024))
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)) or not set(required).issubset(headers):
                raise ValueError("csv_headers")
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError("csv_shape")
    finally:
        csv.field_size_limit(previous_limit)
    return headers, rows


def _csv_equal(text, value):
    if isinstance(value, (dict, list)):
        try:
            return json.loads(text) == value
        except (ValueError, TypeError):
            return False
    return text == ("" if value is None else str(value))


def _csv_source_values(record):
    """Mirror the CSV exporter's explicit derivations, not stale DB labels."""
    values = dict(record)
    metrics = record.get("metrics") or {}
    values.update({field: metrics.get(field, record.get(field)) for field in METRIC_FIELDS})
    values.update(
        content=record.get("content_text") or "",
        content_text=record.get("content_text") or "",
        digest=record.get("digest") or "",
        title_length=len(record.get("title") or ""),
        digest_length=len(record.get("digest") or ""),
        batch_key=f"{record.get('biz')}:{record.get('mid')}",
        source_url=record.get("article_url") or record.get("source_url"),
        canonical_url=record.get("article_url") or record.get("canonical_url"),
        metrics_provider=record.get("metrics_source") or record.get("metrics_provider"),
        metric_field_status={f: "observed" if values[f] is not None else "not_observed" for f in METRIC_FIELDS},
        metrics_status="complete" if all(values[f] is not None for f in METRIC_FIELDS) else "partial",
    )
    try:
        date = datetime.fromisoformat(str(record.get("publish_time") or "").replace("Z", "+00:00"))
        if date.tzinfo:
            date = date.astimezone(timezone(timedelta(hours=8)))
        values.update(quarter=(date.month - 1) // 3 + 1, is_weekday=date.weekday() < 5)
    except ValueError:
        pass
    return values


def audit_journal(base, *, expected_biz, baseline_base=None, write_outputs=True) -> dict:
    """Audit saved evidence only; no window, network, crawling or export calls.

    Missing metrics fail acceptance, but remain missing (never coerced to zero).
    ``missing_baseline`` is a sorted list of public biz/mid/idx/sn dictionaries.
    An absent base is an error and is never created. Existing reports, Excel,
    CSVs, capture state, catalogs, media and SQLite remain untouched.
    """
    root = Path(base).absolute()
    bundle = root / "bundle"
    result = {"ok": False, "checks": {name: True for name in
              ("catalog", "identity", "evidence", "sqlite", "media", "csv", "baseline", "outputs")},
              "errors": [], "warnings": [], "counts": {}, "missing_baseline": [],
              "audit_path": None, "live_verified": False}

    def error(check, code, *, index=None, identity=None):
        item = {"check": check, "code": code}
        if index is not None:
            item["index"] = index
        if identity is not None:
            item["identity"] = _public(identity)
        result["errors"].append(item)
        result["checks"][check] = False

    def read_json(parent, name, kind, check):
        try:
            return _json(parent, name, kind)
        except _READ_ERRORS:
            error(check, "missing_or_invalid_" + Path(name).stem)
            return kind()

    def identity_map(rows, check):
        found = {}
        keys = set()
        for index, row in enumerate(rows):
            try:
                identity = _identity(row, expected_biz)
                if identity in found or identity[:3] in keys:
                    error(check, "duplicate_identity", index=index, identity=identity)
                found[identity] = row
                keys.add(identity[:3])
            except _READ_ERRORS:
                error(check, "invalid_identity", index=index)
        return found

    catalog = read_json(root, "catalog_verified_snapshot.json", dict, "catalog")
    items = catalog.get("items")
    if not isinstance(items, list):
        items = []
    evidence_refs = catalog_evidence_refs(catalog, len(items))
    evidence_valid = False
    single_scan = catalog_scan_metadata_valid(catalog.get("catalog_scan"), len(items))
    try:
        final_catalog_sequence = (catalog_runtime_membership(items) if single_scan else catalog_sequence(items))
    except _READ_ERRORS:
        final_catalog_sequence = ()
    if evidence_refs and final_catalog_sequence:
        evidence_valid = True
        try:
            observed_sequence = None
            for pass_number, relative in evidence_refs:
                if not isinstance(relative, str) or "\\" in relative:
                    raise ValueError("invalid_catalog_evidence_path")
                parts = Path(relative).parts
                if (len(parts) < 4 or parts[0] != "catalog_scan"
                        or parts[-2] != f"pass_{pass_number:02d}"
                        or parts[-1] != "catalog_snapshot.json"):
                    raise ValueError("invalid_catalog_evidence_path")
                evidence = _json(root, relative, dict)
                if (evidence.get("schema_version") != 2
                        or evidence.get("scope_observed") is not True
                        or evidence.get("account") != catalog.get("account")
                        or evidence.get("active_tab") != "全部"
                        or evidence.get("terminal_visible") is not True
                        or evidence.get("loading") or evidence.get("load_error")):
                    raise ValueError("catalog_evidence_mismatch")
                sequence = (catalog_runtime_membership(evidence.get("items")) if single_scan
                            else catalog_sequence(evidence.get("items")))
                if observed_sequence is None:
                    observed_sequence = sequence
                elif sequence != observed_sequence:
                    raise ValueError("catalog_evidence_mismatch")
            if observed_sequence is None or (observed_sequence != final_catalog_sequence if single_scan
                                               else set(observed_sequence) != set(final_catalog_sequence)):
                raise ValueError("catalog_evidence_mismatch")
        except _READ_ERRORS:
            evidence_valid = False
    scope_valid = not (catalog.get("schema_version") != 2 or catalog.get("scope_observed") is not True
            or catalog.get("active_tab") != "全部" or catalog.get("terminal_visible") is not True
            or catalog.get("loading") or catalog.get("load_error") or not items)
    if not scope_valid:
        error("catalog", "unverified_catalog_scope_or_terminal")
    elif not evidence_valid:
        error("catalog", "catalog_evidence_unverified")
    runtimes = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not _title(item.get("title")) or not isinstance(item.get("runtime"), str) or not item["runtime"].strip():
            error("catalog", "invalid_catalog_item", index=index)
        elif item["runtime"] in runtimes:
            error("catalog", "duplicate_catalog_runtime", index=index)
        else:
            runtimes.add(item["runtime"])
    result["counts"]["catalog_count"] = len(items)

    state = read_json(root, "capture_state.json", dict, "identity")
    identities = read_json(root, "catalog_identity.json", dict, "identity")
    jobs, mapped = state.get("jobs"), identities.get("items")
    if not isinstance(jobs, dict):
        jobs = {}
        error("identity", "invalid_state_jobs")
    if not isinstance(mapped, list):
        mapped = []
        error("identity", "invalid_catalog_identity_items")
    if set(jobs) != {str(i) for i in range(len(items))} or len(mapped) != len(items):
        error("identity", "catalog_state_identity_coverage")
    if identities.get("account") != catalog.get("account"):
        error("identity", "catalog_account_mismatch")
    catalog_ids = identity_map(mapped, "identity")

    # Read manifest before the shared loader to prevent silent shape filtering.
    media = read_json(bundle, "media_rows.json", list, "media")
    if any(not isinstance(row, dict) for row in media):
        error("media", "invalid_manifest_row")
        media = [row for row in media if isinstance(row, dict)]
    try:
        metadata_paths, records = _metadata(bundle)
    except _READ_ERRORS:
        metadata_paths, records = [], []
        error("identity", "missing_or_invalid_metadata")
    if not records:
        error("identity", "empty_metadata")
    record_map = identity_map(records, "identity")
    result["counts"].update(record_count=len(records), resolved_identity_count=len(catalog_ids))
    if set(catalog_ids) != set(record_map):
        error("identity", "catalog_metadata_identity_mismatch")

    for index, item in enumerate(mapped):
        try:
            identity = _identity(item, expected_biz)
            record = record_map[identity]
            job = jobs[str(index)]
            title = _title(items[index]["title"])
            if item.get("status") != "captured" or job.get("status") != "captured":
                raise ValueError("uncaptured")
            if not title or any(_title(row.get("title")) != title for row in (item, record, job)):
                raise ValueError("catalog_title_mismatch")
            if item.get("runtime") != items[index].get("runtime"):
                raise ValueError("catalog_runtime_mismatch")
            if job.get("mid") != identity[1] or type(job.get("idx")) is not int or job["idx"] != identity[2]:
                raise ValueError("state_identity_mismatch")
            if any(k in job and job[k] != record[k] for k in ("biz", "sn")):
                raise ValueError("state_identity_mismatch")
            for field in ("article_event_id", "metric_event_id"):
                if not job.get(field) or job[field] != record.get(field):
                    raise ValueError("state_event_mismatch")
        except (*_READ_ERRORS, IndexError):
            error("identity", "catalog_state_record_mismatch", index=index)

    for index, (path, record) in enumerate(zip(metadata_paths, records)):
        try:
            identity = _identity(record, expected_biz)
            folder = f"articles/{identity[1]}_{identity[2]}"
            if path.parent != bundle / folder:
                raise ValueError("metadata_folder_mismatch")
            evidence_path = folder + "/capture_evidence.json"
            if record.get("capture_evidence") != evidence_path:
                raise ValueError("evidence_path_mismatch")
            evidence = _json(bundle, evidence_path, dict)
            article = normalize_event(evidence["article_event"], {expected_biz})
            metric = normalize_event(evidence["metric_event"], {expected_biz})
            if article.get("kind") != "article" or metric.get("kind") != "metrics":
                raise ValueError("wrong_event_kind")
            if any(_identity(event, expected_biz) != identity for event in (article, metric)):
                raise ValueError("event_identity_mismatch")
            if record.get("article_event_id") != article["event_id"] or record.get("metric_event_id") != metric["event_id"]:
                raise ValueError("event_id_mismatch")
            for field, event_field in (("title", "title"), ("author", "author"), ("digest", "digest"),
                                       ("article_url", "url"), ("cover_url", "cover_url"),
                                       ("content_fragment_html", "content_html")):
                if record.get(field, "") != article["article"].get(event_field, ""):
                    raise ValueError("article_content_mismatch")
            values = metric["metrics"]
            if record.get("metrics") != values:
                raise ValueError("metrics_mismatch")
            for field in METRIC_FIELDS:
                if type(values.get(field)) is not int or values[field] < 0 or type(record.get(field)) is not int or record[field] != values[field]:
                    raise ValueError("missing_or_mismatched_metric")
            if record.get("metric_fields") != metric["field_meta"] or evidence.get("metric_fields") != metric["field_meta"]:
                raise ValueError("metric_provenance_mismatch")
            for field in _LOCAL_FIELDS:
                expected = folder if field == "local_article_dir" else folder + "/" + {
                    "local_html": "article.html", "local_text": "article.txt", "capture_evidence": "capture_evidence.json"}[field]
                if record.get(field) != expected:
                    raise ValueError("article_path_mismatch")
                _safe_path(bundle, record[field], directory=field == "local_article_dir")
            text = _safe_path(bundle, record["local_text"]).read_text(encoding="utf-8-sig")
            fragment = _safe_path(bundle, folder + "/content_fragment.html").read_text(encoding="utf-8-sig")
            if not text.strip() or text != record.get("content_text") or not fragment.strip() or fragment != record.get("content_fragment_html"):
                raise ValueError("local_content_mismatch")
            if _safe_path(bundle, record["local_html"]).stat().st_size == 0:
                raise ValueError("empty_html")
        except _READ_ERRORS:
            error("evidence", "invalid_or_inconsistent_capture_evidence", index=index)
        for field in ("author", "digest"):
            if not str(record.get(field) or "").strip():
                result["warnings"].append({"code": "missing_" + field, "index": index})
    result["counts"]["metric_field_counts"] = {field: sum(r.get(field) is not None for r in records) for field in METRIC_FIELDS}

    try:
        db_rows = _database_identities(bundle)
        db_map = identity_map(db_rows, "sqlite")
        result["counts"]["sqlite_article_count"] = len(db_rows)
        if set(db_map) != set(record_map):
            error("sqlite", "database_metadata_identity_mismatch")
    except _READ_ERRORS:
        error("sqlite", "missing_or_invalid_database")
        result["counts"]["sqlite_article_count"] = 0

    media_report = read_json(bundle, "media_download_report.json", dict, "media")
    statuses = dict(Counter(row.get("status") if isinstance(row.get("status"), str) else "invalid" for row in media))
    article_keys = {f"{i[1]}_{i[2]}" for i in record_map}
    if media_report.get("status") != "complete" or any(media_report.get(k) for k in ("snapshot_errors", "article_errors", "error_codes")):
        error("media", "media_report_incomplete")
    for field, expected in (("resource_count", len(media)), ("status_counts", statuses), ("snapshot_metadata_count", len(records))):
        if field in media_report and media_report[field] != expected:
            error("media", "media_report_count_mismatch")
    result["counts"].update(media_total_count=len(media), media_status_counts=statuses,
                            pending_media_count=sum(v for k, v in statuses.items() if k not in {"downloaded", "skipped"}))
    for index, row in enumerate(media):
        try:
            if row.get("status") not in {"downloaded", "skipped"} or row.get("error_code"):
                raise ValueError("unresolved_media")
            if row.get("article_key") not in article_keys or f"{row.get('mid')}_{row.get('idx')}" != row.get("article_key"):
                raise ValueError("media_identity_mismatch")
            local = _safe_path(bundle, row.get("local_path"))
            if type(row.get("bytes")) is not int or row["bytes"] <= 0 or local.stat().st_size != row["bytes"]:
                raise ValueError("media_size_mismatch")
            with local.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if digest != row.get("sha256"):
                raise ValueError("media_hash_mismatch")
        except _READ_ERRORS:
            error("media", "invalid_media_file_or_status", index=index)

    try:
        headers, csv_rows = _csv(bundle, "文章数据.csv", default_export_headers([]) + list(_LOCAL_FIELDS) + ["content_text"])
        result["counts"]["csv_article_count"] = len(csv_rows)
        identities_csv = []
        for index, row in enumerate(csv_rows):
            identity = _identity({**row, "idx": int(row["idx"])}, expected_biz)
            identities_csv.append(identity)
            record = record_map.get(identity)
            if record is None:
                error("csv", "csv_metadata_identity_mismatch", index=index)
                continue
            if not _csv_equal(row["content"], record.get("content_text")):
                error("csv", "csv_content_mismatch", index=index)
            # Compare explicit CSV derivations; original source fields stay exact.
            expected_values = _csv_source_values(record)
            for field in set(headers).intersection(expected_values):
                if not _csv_equal(row[field], expected_values[field]):
                    error("csv", "csv_metadata_value_mismatch", index=index)
                    break
            for field in (*_LOCAL_FIELDS, "images_folder", "local_readable_html"):
                if row.get(field):
                    _safe_path(bundle, row[field], directory=field in {"local_article_dir", "images_folder"})
            resources = [m for m in media if m.get("article_key") == f"{identity[1]}_{identity[2]}"
                         and m.get("status") in {"downloaded", "skipped"}]
            for field, kind in (("media_local_paths", None), ("image_local_paths", "image"),
                                ("audio_local_paths", "audio"), ("video_local_paths", "video")):
                if field not in row:
                    continue
                paths = json.loads(row[field])
                if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                    raise ValueError("csv_invalid_attachment_list")
                expected_paths = [m.get("local_path") for m in resources if kind is None or m.get("kind") == kind]
                if Counter(paths) != Counter(expected_paths):
                    raise ValueError("csv_attachment_manifest_mismatch")
                for local in paths:
                    _safe_path(bundle, local)
            if any(not row.get(field) for field in _LOCAL_FIELDS):
                raise ValueError("csv_missing_attachment_path")
        if len(csv_rows) != len(records) or Counter(identities_csv) != Counter(record_map.keys()):
            error("csv", "csv_article_identity_or_count_mismatch")
    except _READ_ERRORS:
        error("csv", "missing_or_invalid_article_csv")
        result["counts"].setdefault("csv_article_count", 0)

    try:
        required = ("mid", "idx", "kind", "local_path", "status", "bytes", "sha256")
        headers, csv_media = _csv(bundle, "媒体索引.csv", required)
        result["counts"]["csv_media_count"] = len(csv_media)
        compared = set(required) | (set(headers) & {k for row in media for k in row})
        fields = sorted(compared)
        expected = Counter(tuple("" if row.get(f) is None else str(row[f]) for f in fields) for row in media)
        actual = Counter(tuple(row[f] for f in fields) for row in csv_media)
        if actual != expected:
            error("csv", "csv_media_manifest_mismatch")
        for row in csv_media:
            _safe_path(bundle, row["local_path"])
            if "biz" in row and row["biz"] != expected_biz:
                raise ValueError("csv_media_biz_mismatch")
            owner = next((r for i, r in record_map.items() if i[1] == row["mid"] and str(i[2]) == row["idx"]), None)
            if owner is None or ("article_title" in row and row["article_title"] != owner.get("title")):
                raise ValueError("csv_media_owner_mismatch")
    except _READ_ERRORS:
        error("csv", "missing_or_invalid_media_csv")
        result["counts"].setdefault("csv_media_count", 0)

    if baseline_base is not None:
        try:
            _, baseline_records = _metadata(Path(baseline_base).absolute() / "bundle", metadata_only=True)
            if not baseline_records:
                raise ValueError("empty_baseline")
            baseline = identity_map(baseline_records, "baseline")
            result["counts"]["baseline_record_count"] = len(baseline_records)
            result["missing_baseline"] = [_public(i) for i in sorted(set(baseline) - set(record_map))]
            if result["missing_baseline"]:
                error("baseline", "missing_baseline_identities")
        except _READ_ERRORS:
            error("baseline", "missing_or_invalid_baseline_metadata")
    result["counts"]["missing_baseline_count"] = len(result["missing_baseline"])
    result["ok"] = all(result["checks"].values())
    if write_outputs:
        try:
            # Validate the existing directory without resolving away its links.
            _safe_path(root, ".", directory=True)
            name = "rollout_audit_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ_") + uuid4().hex + ".json"
            destination = root / name
            result["audit_path"] = str(destination)
            with destination.open("x", encoding="utf-8") as file:
                json.dump(result, file, ensure_ascii=False, indent=2)
                file.write("\n")
        except _READ_ERRORS:
            result["audit_path"] = None
            error("outputs", "audit_report_write_failed")
            result["ok"] = False
    return result
