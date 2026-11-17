"""Offline rollout acceptance tests; no live client/window/network is required."""
import csv
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from capture.reader import METRIC_FIELDS, normalize_event, to_wire_event
from crawl.client_capture import build_capture_metadata

BIZ = "test-biz"


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def csv_write(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                             for k, v in row.items() if k in fields})


def export_fixture_csv(base):
    from export.journal_csv import write_journal_csv_bundle
    write_journal_csv_bundle(base / "bundle")


def bundle_fixture(base, count=1, media_enabled=True):
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    catalog, identities, jobs, media = [], [], {}, []
    with sqlite3.connect(bundle / "journal.db") as db:
        db.execute("CREATE TABLE articles (biz TEXT, mid TEXT, idx INTEGER, sn TEXT)")
        for i in range(count):
            mid = str(1000 + i)
            identity = {"biz": BIZ, "mid": mid, "idx": 1, "sn": "sn-" + mid}
            title = "文章 " + mid
            raw = {"schema_version": 1, "kind": "article", "captured_at": "2026-09-07T00:00:00Z",
                   "source": "fiddler_classic", "endpoint": "/s", "event_id": "a-" + mid, **identity,
                   "article": {"title": title, "author": "", "digest": "摘要，保留原文", "url":
                               f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={mid}&idx=1&sn=sn-{mid}",
                               "content_html": '<div id="js_content"><p>正文详情</p></div>'}}
            metric_raw = {**raw, "kind": "metrics", "endpoint": "/mp/getappmsgext", "event_id": "m-" + mid,
                          "stats": {"read_num": 0, "old_like_num": 2, "like_num": 3, "share_num": 4, "collect_num": 5},
                          "comments": {"total_count": 0}}
            article, metric = normalize_event(raw, {BIZ}), normalize_event(metric_raw, {BIZ})
            key = mid + "_1"
            folder = bundle / "articles" / key
            folder.mkdir(parents=True)
            local = "articles/" + key
            record = build_capture_metadata(article, metric, {"content_text": "正文详情", "content_html": article["article"]["content_html"]},
                journal_name="测试期刊", profile_url="", local_html=local + "/article.html",
                local_text=local + "/article.txt", local_article_dir=local)
            record["capture_evidence"] = local + "/capture_evidence.json"
            (folder / "article.html").write_text(article["article"]["content_html"], encoding="utf-8")
            (folder / "content_fragment.html").write_text(record["content_fragment_html"], encoding="utf-8")
            (folder / "article.txt").write_text(record["content_text"], encoding="utf-8")
            dump(folder / "metadata.json", record)
            dump(folder / "capture_evidence.json", {"schema_version": 1, "article_event": to_wire_event(article),
                 "metric_event": to_wire_event(metric), "metric_fields": record["metric_fields"]})
            item = {"title": title, "runtime": "42," + mid, "card_class": "article-item"}
            catalog.append(item)
            identities.append({**item, **identity, "status": "captured"})
            jobs[str(i)] = {"status": "captured", "title": title, "mid": mid, "idx": 1,
                            "article_event_id": article["event_id"], "metric_event_id": metric["event_id"]}
            db.execute("INSERT INTO articles VALUES (?,?,?,?)", tuple(identity.values()))
            if media_enabled:
                (folder / "images").mkdir()
                data = b"original-media"
                (folder / "images" / "a.png").write_bytes(data)
                media.append({"article_key": key, "mid": mid, "idx": 1, "kind": "image",
                              "local_path": local + "/images/a.png", "status": "downloaded", "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest(), "source_url": "https://example.test/a.png", "error_code": ""})
    evidence_paths = []
    for pass_number in (1, 2):
        relative = f"catalog_scan/test/pass_{pass_number:02d}/catalog_snapshot.json"
        evidence_paths.append(relative)
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        dump(path, {"schema_version": 2, "scope_observed": True, "active_tab": "全部",
                    "terminal_visible": True, "items": catalog, "account": "测试期刊"})
    dump(base / "catalog_verified_snapshot.json", {"schema_version": 2, "scope_observed": True, "active_tab": "全部",
         "terminal_visible": True, "items": catalog, "account": "测试期刊",
         "catalog_consensus": {"required_matches": 2, "attempted_passes": 2,
                               "matching_passes": [1, 2], "item_count": count,
                               "evidence_paths": evidence_paths}})
    dump(base / "catalog_identity.json", {"items": identities, "account": "测试期刊"})
    dump(base / "capture_state.json", {"jobs": jobs})
    dump(bundle / "media_rows.json", media)
    dump(bundle / "media_download_report.json", {"status": "complete", "resource_count": len(media),
         "status_counts": {"downloaded": len(media)} if media else {}, "snapshot_metadata_count": count})
    export_fixture_csv(base)
    return base


@pytest.fixture
def audit(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline audit must not use networking")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    def call(*args, **kwargs):
        assert importlib.util.find_spec("export.rollout_audit") is not None, "offline audit implementation is missing"
        return importlib.import_module("export.rollout_audit").audit_journal(*args, **kwargs)
    return call


def errors(result, check):
    return [e for e in result["errors"] if e["check"] == check]


def snapshot(base):
    return {p.relative_to(base).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in base.rglob("*") if p.is_file()}


def test_valid_csv_only_bundle_is_read_only_and_keeps_missing_author(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    before = snapshot(base)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result
    assert result["counts"]["record_count"] == result["counts"]["csv_article_count"] == 1
    assert result["warnings"]
    assert snapshot(base) == before
    assert not list(base.rglob("*.xlsx"))


@pytest.mark.parametrize("change", [lambda c: c.pop("schema_version"), lambda c: c.update(schema_version=1),
    lambda c: c.update(scope_observed=False), lambda c: c.update(active_tab="文章"),
    lambda c: c.update(terminal_visible="true"), lambda c: c.update(items=[]), lambda c: c.update(load_error=True),
    lambda c: c.pop("catalog_consensus"),
    lambda c: c["catalog_consensus"].update(matching_passes=[1]),
    lambda c: c["catalog_consensus"].update(item_count=999)])
def test_invalid_catalog_is_not_accepted(audit, tmp_path, change):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_verified_snapshot.json"
    catalog = read(path)
    change(catalog)
    dump(path, catalog)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "catalog")


@pytest.mark.parametrize("change", ["bytes", "hash", "corrupt", "outside", "absolute", "missing", "needs_resolution", "error"])
def test_media_integrity_and_completion(audit, tmp_path, change):
    base = bundle_fixture(tmp_path / "run")
    path = base / "bundle/media_rows.json"
    rows = read(path)
    row = rows[0]
    if change == "corrupt":
        (base / "bundle" / row["local_path"]).write_bytes(b"tampered-media")
    elif change == "bytes":
        row["bytes"] += 1
    elif change == "hash":
        row["sha256"] = "0" * 64
    elif change == "outside":
        row["local_path"] = "../outside.bin"
    elif change == "absolute":
        row["local_path"] = str((base / "bundle" / row["local_path"]).resolve())
    elif change == "missing":
        row["local_path"] = "articles/1000_1/images/missing.png"
    else:
        row["status"] = change
    dump(path, rows)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "media")


def test_missing_media_report_fails(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    (base / "bundle/media_download_report.json").unlink()
    assert errors(audit(base, expected_biz=BIZ, write_outputs=False), "media")


def test_baseline_175_vs_171_lists_exact_four_public_identities(audit, tmp_path):
    baseline = bundle_fixture(tmp_path / "baseline", 175, media_enabled=False)
    base = bundle_fixture(tmp_path / "run", 171, media_enabled=False)
    before = snapshot(baseline)
    result = audit(base, expected_biz=BIZ, baseline_base=baseline, write_outputs=False)
    assert not result["ok"]
    assert result["counts"]["baseline_record_count"] == 175
    assert result["counts"]["record_count"] == 171
    assert result["missing_baseline"] == [{"biz": BIZ, "mid": str(i), "idx": 1, "sn": "sn-" + str(i)} for i in range(1171, 1175)]
    assert snapshot(baseline) == before


def test_baseline_does_not_require_equal_media_count(audit, tmp_path):
    baseline = bundle_fixture(tmp_path / "baseline", media_enabled=False)
    base = bundle_fixture(tmp_path / "run")
    assert audit(base, expected_biz=BIZ, baseline_base=baseline, write_outputs=False)["ok"]


@pytest.mark.parametrize("target", ["metadata", "catalog_identity", "state", "database", "evidence", "metric_zero"])
def test_identity_and_evidence_mismatch_fails(audit, tmp_path, target):
    base = bundle_fixture(tmp_path / "run")
    folder = base / "bundle/articles/1000_1"
    if target == "database":
        with sqlite3.connect(base / "bundle/journal.db") as db:
            db.execute("UPDATE articles SET sn='other'")
    elif target == "catalog_identity":
        path = base / "catalog_identity.json"
        value = read(path)
        value["items"][0]["sn"] = "other"
        dump(path, value)
    elif target == "state":
        path = base / "capture_state.json"
        value = read(path)
        value["jobs"]["0"]["mid"] = "9999"
        dump(path, value)
    elif target in {"evidence", "metric_zero"}:
        path = folder / "capture_evidence.json"
        value = read(path)
        if target == "evidence":
            value["article_event"]["sn"] = "other"
        else:
            value["metric_event"]["stats"].pop("read_num")
        dump(path, value)
    else:
        path = folder / "metadata.json"
        value = read(path)
        value["sn"] = "other"
        dump(path, value)
    assert not audit(base, expected_biz=BIZ, write_outputs=False)["ok"]


@pytest.mark.parametrize("change", ["missing_article", "missing_media", "identity", "digest", "content", "attachment", "row_count", "missing_header", "media_hash"])
def test_csv_must_match_records_and_attachments(audit, tmp_path, change):
    base = bundle_fixture(tmp_path / "run")
    path = base / "bundle" / ("媒体索引.csv" if change in {"missing_media", "media_hash"} else "文章数据.csv")
    if change.startswith("missing_") and change != "missing_header":
        path.unlink()
    else:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows, fields = list(reader), reader.fieldnames
        if change == "identity": rows[0]["sn"] = "wrong"
        if change == "digest": rows[0]["digest"] = "伪造摘要"
        if change == "content": rows[0]["content"] = "伪造正文"
        if change == "attachment": rows[0]["local_text"] = "articles/1000_1/missing.txt"
        if change == "row_count": rows = []
        if change == "missing_header": fields.remove("digest")
        if change == "media_hash": rows[0]["sha256"] = "0" * 64
        csv_write(path, rows, fields)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "csv")


def test_report_is_independent_and_never_overwrites_originals_or_excel(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    old = base / "bundle/old.xlsx"
    old.write_bytes(b"legacy workbook, do not delete")
    before = snapshot(base)
    first = audit(base, expected_biz=BIZ)
    second = audit(base, expected_biz=BIZ)
    assert first["ok"] and second["ok"]
    for path, value in before.items():
        assert snapshot(base)[path] == value
    assert first["audit_path"] != second["audit_path"]
    assert read(Path(first["audit_path"]))["ok"]
    assert set(snapshot(base)) - set(before) == {Path(r["audit_path"]).relative_to(base).as_posix() for r in (first, second)}
    assert list(base.rglob("*.xlsx")) == [old]


def test_missing_base_is_reported_without_creating_directory(audit, tmp_path):
    base = tmp_path / "absent"
    result = audit(base, expected_biz=BIZ)
    assert not result["ok"] and not base.exists()



def test_real_csv_exporter_interoperates(audit, tmp_path):
    from export.journal_csv import write_journal_csv_bundle
    base = bundle_fixture(tmp_path / "run", 2)
    write_journal_csv_bundle(base / "bundle")
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result


@pytest.mark.parametrize("field", ["media_local_paths", "image_local_paths", "local_readable_html"])
def test_csv_additional_attachment_paths_are_verified(audit, tmp_path, field):
    from export.journal_csv import write_journal_csv_bundle
    base = bundle_fixture(tmp_path / "run")
    write_journal_csv_bundle(base / "bundle")
    path = base / "bundle/文章数据.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows, fields = list(reader), reader.fieldnames
    rows[0][field] = json.dumps(["../outside.png"]) if field.endswith("_paths") else "../outside.html"
    csv_write(path, rows, fields)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "csv")


def test_csv_media_business_identity_is_verified(audit, tmp_path):
    from export.journal_csv import write_journal_csv_bundle
    base = bundle_fixture(tmp_path / "run")
    write_journal_csv_bundle(base / "bundle")
    path = base / "bundle/媒体索引.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows, fields = list(reader), reader.fieldnames
    rows[0]["biz"] = "other-biz"
    csv_write(path, rows, fields)
    assert errors(audit(base, expected_biz=BIZ, write_outputs=False), "csv")


def test_long_csv_content_has_no_excel_or_default_csv_limit(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "bundle/articles/1000_1/metadata.json"
    record = read(path)
    content = "正文详情\n" * 30000
    record["content_text"] = content
    dump(path, record)
    (path.parent / "article.txt").write_text(content, encoding="utf-8")
    export_fixture_csv(base)
    before = csv.field_size_limit()
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result
    assert csv.field_size_limit() == before


def test_baseline_is_metadata_only_even_with_corrupt_old_media(audit, tmp_path):
    baseline = bundle_fixture(tmp_path / "baseline")
    (baseline / "bundle/media_rows.json").write_text("broken old manifest", encoding="utf-8")
    base = bundle_fixture(tmp_path / "run")
    assert audit(base, expected_biz=BIZ, baseline_base=baseline, write_outputs=False)["ok"]


def test_linked_media_is_rejected_even_with_correct_hash(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    folder = base / "bundle/articles/1000_1/images"
    original = folder / "a.png"
    alias = folder / "linked.png"
    try:
        alias.symlink_to(original)
    except OSError:
        pytest.skip("OS does not permit unprivileged symlinks")
    path = base / "bundle/media_rows.json"
    rows = read(path)
    rows[0]["local_path"] = "articles/1000_1/images/linked.png"
    dump(path, rows)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert errors(result, "media")


def test_database_wal_is_read_without_touching_source(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    db = sqlite3.connect(base / "bundle/journal.db")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE articles SET sn='wal-sn'")
        db.commit()
        before = snapshot(base)
        result = audit(base, expected_biz=BIZ, write_outputs=False)
        assert errors(result, "sqlite")
        assert snapshot(base) == before
    finally:
        db.close()


@pytest.mark.parametrize("name,value", [("catalog_verified_snapshot.json", []), ("capture_state.json", {"jobs": []}),
    ("catalog_identity.json", {"items": [None]}), ("bundle/media_rows.json", [None]),
    ("bundle/media_download_report.json", [])])
def test_malformed_artifacts_return_structured_failure(audit, tmp_path, name, value):
    base = bundle_fixture(tmp_path / "run")
    dump(base / name, value)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"] is False and result["errors"]


def test_duplicate_catalog_identity_never_collapses_to_success(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run", 2)
    path = base / "catalog_identity.json"
    value = read(path)
    value["items"][1].update({k: value["items"][0][k] for k in ("mid", "idx", "sn")})
    dump(path, value)
    assert errors(audit(base, expected_biz=BIZ, write_outputs=False), "identity")


def test_csv_derived_fields_use_export_mapping_not_stale_stored_values(audit, tmp_path):
    from export.journal_csv import write_journal_csv_bundle
    base = bundle_fixture(tmp_path / "run")
    path = base / "bundle/articles/1000_1/metadata.json"
    record = read(path)
    record.update(metrics_status="legacy", metric_field_status={"read_count": "legacy"},
                  metrics_provider="legacy", source_url="https://mp.weixin.qq.com/s/old",
                  canonical_url="https://mp.weixin.qq.com/s/old", title_length=999, digest_length=999,
                  batch_key="legacy", quarter=9, is_weekday=False, publish_time="2026-09-07T00:00:00Z")
    dump(path, record)
    write_journal_csv_bundle(base / "bundle")
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result


@pytest.mark.parametrize("field", ["metrics_status", "metric_field_status", "metrics_provider", "source_url", "canonical_url"])
def test_forged_csv_derived_fields_still_fail(audit, tmp_path, field):
    from export.journal_csv import write_journal_csv_bundle
    base = bundle_fixture(tmp_path / "run")
    write_journal_csv_bundle(base / "bundle")
    path = base / "bundle/文章数据.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows, fields = list(reader), reader.fieldnames
    rows[0][field] = "forged"
    csv_write(path, rows, fields)
    assert errors(audit(base, expected_biz=BIZ, write_outputs=False), "csv")



def test_catalog_consensus_evidence_files_are_required(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_scan/test/pass_02/catalog_snapshot.json"
    path.unlink()
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "catalog")


def test_catalog_consensus_evidence_sequence_must_match_final_catalog(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_scan/test/pass_02/catalog_snapshot.json"
    snapshot = read(path)
    snapshot["items"][0]["title"] = "另一篇文章"
    dump(path, snapshot)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "catalog")


def test_catalog_consensus_evidence_path_cannot_escape_run(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_verified_snapshot.json"
    catalog = read(path)
    catalog["catalog_consensus"]["evidence_paths"][0] = "../outside.json"
    dump(path, catalog)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert not result["ok"] and errors(result, "catalog")


def test_catalog_consensus_allows_final_job_order_to_differ_from_observed_order(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run", count=2)
    for pass_number in (1, 2):
        path = base / f"catalog_scan/test/pass_{pass_number:02d}/catalog_snapshot.json"
        snapshot = read(path)
        snapshot["items"] = list(reversed(snapshot["items"]))
        dump(path, snapshot)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result


def test_missing_catalog_evidence_reports_specific_error_code(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_verified_snapshot.json"
    catalog = read(path)
    catalog.pop("catalog_consensus")
    dump(path, catalog)
    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert any(error["code"] == "catalog_evidence_unverified" for error in errors(result, "catalog"))


def test_single_terminal_catalog_scan_is_accepted(audit, tmp_path):
    base = bundle_fixture(tmp_path / "run")
    path = base / "catalog_verified_snapshot.json"
    catalog = read(path)
    catalog.pop("catalog_consensus")
    catalog["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": len(catalog["items"]),
        "evidence_path": "catalog_scan/test/pass_01/catalog_snapshot.json",
    }
    dump(path, catalog)

    result = audit(base, expected_biz=BIZ, write_outputs=False)
    assert result["ok"], result
