"""Offline contract tests for the single-writer, cross-journal export."""
import copy
import csv
import json
import socket
from pathlib import Path
from urllib.parse import urlencode

import pytest

from export.session_article_exporter import HEADERS as SESSION_HEADERS


BIZ = "journal-A"
SN = "a" * 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Real networking is forbidden in exporter tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def article(biz=BIZ, mid="123", idx=1, sn=SN, **changes):
    row = dict(biz=biz, mid=mid, idx=idx, sn=sn,
               url="https://mp.weixin.qq.com/s?" + urlencode(dict(__biz=biz, mid=mid, idx=idx, sn=sn)),
               identity_matches=True, business_ret=0, title='同名，"文章"',
               journal_name="测试期刊", journal_id="J01", account_name="测试公众号",
               content_html="<p>正文，完整保留。</p><p>第二段</p>",
               metrics={}, metric_fields={}, captured_at="2026-09-09T00:00:00+00:00")
    row.update(changes)
    return row


def exporter(root, **kwargs):
    # Import inside tests: the initial RED run fails on the missing feature.
    from export.global_article_exporter import GlobalArticleExporter
    return GlobalArticleExporter(root, **kwargs)


def read_rows(root):
    with (root / "文章数据.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        return reader.fieldnames, list(reader)


def watch_replaces(monkeypatch, root):
    import export.global_article_exporter as module
    original, calls = module.os.replace, []

    def replace(src, dst):
        if Path(dst) == root / "文章数据.csv":
            calls.append(dst)
        return original(src, dst)

    monkeypatch.setattr(module.os, "replace", replace)
    return calls


def downloaded(row, *, pending=False):
    records = copy.deepcopy(row["media_index"])
    for i, record in enumerate(records):
        record.update(local_path="", status="not_downloaded", bytes=0, sha256="", error_code="")
        if not pending or i == 0:
            local = f"附件/{row['mid']}_{row['idx']}/images/{i}.jpg"
            target = Path(row["asset_root"]) / local
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"offline-test-media")
            record.update(local_path=local, status="downloaded", bytes=18)
    paths = [r["local_path"] for r in records if r["local_path"]]
    result = dict(rows=records, status="partial" if len(paths) < len(records) else "complete",
                  media_downloaded=len(paths), media_pending=len(records) - len(paths), media_local_paths=paths)
    for kind in ("image", "audio", "video"):
        result[kind + "_local_paths"] = [r["local_path"] for r in records if r["kind"] == kind and r["local_path"]]
    return result


SOUND_MEDIA = '<p>正文</p><img data-src="https://mmbiz.qpic.cn/a.png"><mpvoice voice_encode_fileid="voice1"></mpvoice>'

MEDIA = '<p>正文</p><img data-src="https://mmbiz.qpic.cn/a.png"><img data-src="https://mmbiz.qpic.cn/b.png">'


def test_immediate_typed_csv_and_old_header_prefix(tmp_path):
    root = tmp_path / "delivery"
    out = exporter(root)
    row = out.save(article(content_html=SOUND_MEDIA))
    headers, rows = read_rows(root)
    assert headers[:len(SESSION_HEADERS)] == SESSION_HEADERS
    assert len(set(headers)) == len(headers)
    assert {"journal_name", "journal_id", "article_key", "has_text", "has_images", "has_audio", "has_video", "media_index"} <= set(headers)
    assert row["idx"] == 1 and row["journal_id"] == "J01"
    assert row["article_key"] == f"{BIZ}:123:1"
    assert row["has_text"] is True and row["has_images"] is True and row["has_audio"] is True
    assert row["has_video"] is False
    assert isinstance(row["media_index"], list) and len(row["media_index"]) == 2
    assert json.loads(rows[0]["media_index"]) == row["media_index"]
    assert row["media_pending"] == 1 and row["media_status"] == "not_downloaded"
    assert row["csv_pending"] is False and out.pending_count == 0
    assert (root / "文章数据.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert set(p.name for p in root.iterdir()) == {"文章数据.csv", "附件"}
    assert list(root.rglob("*.csv")) == [root / "文章数据.csv"]
    assert "asset_root" not in headers and "csv_pending" not in headers and "has_image" not in headers
    for field in ("local_html", "local_text", "local_metadata"):
        assert not Path(row[field]).is_absolute() and (root / row[field]).is_file()


def test_cross_journal_same_mid_title_and_case_safe_paths(tmp_path):
    root = tmp_path / "delivery"
    out = exporter(root)
    a = out.save(article())
    b = out.save(article(biz="journal-a", journal_id="J02"))
    assert a["asset_root"].casefold() != b["asset_root"].casefold()
    assert Path(a["asset_root"]) == out.article_media_root(BIZ)
    assert a["local_article_dir"] != b["local_article_dir"]
    assert "123_1" in a["local_article_dir"]
    assert len(read_rows(root)[1]) == 2
    assert out.save(article()) == a
    with pytest.raises(ValueError, match="identity_conflict"):
        out.save(article(sn="b" * 32))
    assert len(read_rows(root)[1]) == 2
    resumed = exporter(root)
    with pytest.raises(ValueError, match="identity_conflict"):
        resumed.save(article(sn="b" * 32))


def test_new_rows_append_without_reading_or_replacing_csv(tmp_path, monkeypatch):
    out = exporter(tmp_path / "delivery")
    out.save(article())
    original = Path.open

    def only_append(path, mode="r", *args, **kwargs):
        if path == out.csv_path:
            assert mode == "a"
        return original(path, mode, *args, **kwargs)

    calls = watch_replaces(monkeypatch, out.root)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", only_append)
        out.save(article(mid="124"))
    assert calls == []
    assert len(read_rows(out.root)[1]) == 2


def test_media_is_durable_batched_source_ordered_and_globally_relative(tmp_path, monkeypatch):
    root = tmp_path / "delivery"
    out = exporter(root)
    a = out.save(article(content_html=MEDIA))
    b = out.save(article(biz="journal-B", content_html=MEDIA))
    before = out.csv_path.read_bytes()
    calls = watch_replaces(monkeypatch, root)
    for row in (a, b):
        result = downloaded(row, pending=True)
        result["rows"].reverse()
        out.update_media(row["biz"], row["mid"], row["idx"], result)
    assert out.pending_count == 2 and calls == []
    assert out.csv_path.read_bytes() == before
    assert out.flush() is True
    assert len(calls) == 1 and out.pending_count == 0
    rows = read_rows(root)[1]
    for row in rows:
        index = json.loads(row["media_index"])
        assert [r["resource_index"] for r in index] == [0, 1]
        assert index[0]["status"] == "downloaded" and index[1]["status"] == "not_downloaded"
        assert (root / index[0]["local_path"]).is_file()
        assert row["media_pending"] == "1" and row["media_downloaded"] == "1"
        assert json.loads(row["image_local_paths"]) == [index[0]["local_path"]]
    assert rows[0]["image_local_paths"] != rows[1]["image_local_paths"]
    assert out.save(article(content_html=MEDIA))["media_downloaded"] == 1
    assert out.pending_count == 0
    assert out.flush() is True and len(calls) == 1


def test_pending_media_recovers_without_caller_resubmission(tmp_path):
    root = tmp_path / "delivery"
    out = exporter(root)
    row = out.save(article(content_html=MEDIA))
    out.update_media(BIZ, "123", 1, downloaded(row))
    assert out.pending_count == 1
    resumed = exporter(root)
    assert resumed.pending_count == 0
    assert read_rows(root)[1][0]["media_status"] == "complete"
    assert resumed.save(article(content_html=MEDIA))["media_downloaded"] == 2


def block_csv(monkeypatch, path):
    import export.global_article_exporter as module
    original_open, original_replace = Path.open, module.os.replace

    def open_file(self, mode="r", *args, **kwargs):
        if self == path and any(c in mode for c in "wa+"):
            raise PermissionError("synthetic CSV sharing violation")
        return original_open(self, mode, *args, **kwargs)

    def replace(src, dst):
        if Path(dst) == path:
            raise PermissionError("synthetic CSV sharing violation")
        return original_replace(src, dst)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(module.os, "replace", replace)


def test_locked_csv_persists_public_rows_and_reports_pending_until_restart(tmp_path, monkeypatch):
    root, work = tmp_path / "delivery", tmp_path / "work"
    out = exporter(root, work_root=work)
    with monkeypatch.context() as patch:
        block_csv(patch, out.csv_path)
        row = out.save(article())
        assert row["csv_pending"] is True and out.pending_count == 1
        assert out.flush() is False
        out.save(article(biz="journal-B"))
        assert out.pending_count == 2
        with pytest.raises(ValueError, match="identity_conflict"):
            out.save(article(sn="b" * 32))
    assert list(work.rglob("*.json"))
    resumed = exporter(root, work_root=work)
    assert resumed.pending_count == 0 and len(read_rows(root)[1]) == 2


def test_locked_replacement_preserves_previous_csv_and_latest_update(tmp_path, monkeypatch):
    root = tmp_path / "delivery"
    out = exporter(root)
    out.save(article())
    before = out.csv_path.read_bytes()
    with monkeypatch.context() as patch:
        block_csv(patch, out.csv_path)
        row = out.save(article(title="更新标题"))
        assert row["csv_pending"] is True
        assert out.csv_path.read_bytes() == before
    assert out.flush() is True
    assert read_rows(root)[1][0]["title"] == "更新标题"
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("damage", ["duplicate", "truncated_quote", "truncated_utf8", "missing"])
def test_restart_repairs_csv_from_durable_records(tmp_path, damage):
    root = tmp_path / "delivery"
    out = exporter(root)
    out.save(article())
    out.save(article(mid="124"))
    raw = out.csv_path.read_bytes()
    if damage == "duplicate":
        headers, rows = read_rows(root)
        with out.csv_path.open("a", encoding="utf-8", newline="") as stream:
            csv.DictWriter(stream, fieldnames=headers).writerow(rows[-1])
    elif damage == "truncated_quote":
        with out.csv_path.open("ab") as stream:
            stream.write(b'"incomplete row')
    elif damage == "truncated_utf8":
        out.csv_path.write_bytes(raw[:-9] + b"\xe4\xb8")
    else:
        out.csv_path.unlink()
    resumed = exporter(root)
    assert resumed.pending_count == 0
    assert [r["mid"] for r in read_rows(root)[1]] == ["123", "124"]


def test_public_projection_runs_before_any_spool_write(tmp_path, monkeypatch):
    import export.global_article_exporter as module
    root, work = tmp_path / "delivery", tmp_path / "work"
    out = exporter(root, work_root=work)
    original, calls = module.public_snapshot, []

    def project(value):
        assert not list(work.rglob("*.json"))
        calls.append(True)
        return original(value)

    raw = article(content_html='<p>正文</p><script>PRIVATE_SCRIPT</script><img onerror="PRIVATE_EVENT" data-src="https://mmbiz.qpic.cn/a.png?token=PRIVATE_QUERY">',
                  body="PRIVATE_RAW", cookies="PRIVATE_COOKIE", cgiDataNew={"key": "PRIVATE_OBJECT"})
    raw["url"] += "&pass_ticket=PRIVATE_TICKET"
    before = copy.deepcopy(raw)
    monkeypatch.setattr(module, "public_snapshot", project)
    out.save(raw)
    assert calls and raw == before
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig")
            assert "PRIVATE_" not in text and "<script" not in text and "onerror" not in text


def test_large_multiline_fields_survive_restart_and_media_flush(tmp_path):
    root = tmp_path / "delivery"
    text = ('正文，"完整"\n' * 45000)
    out = exporter(root)
    row = out.save(article(content_html="<p>" + text + "</p>"))
    assert len(row["content"]) > 131072 and row["content_html"].count("正文") == 45000
    exporter(root)
    assert read_rows(root)[1][0]["content_html"] == row["content_html"]
    assert read_rows(root)[1][0]["content"] == row["content"]


@pytest.mark.parametrize("invalid", ["../escape.jpg", "/absolute.jpg", "C:\\outside.jpg", "附件/999_1/images/a.jpg"])
def test_media_rejects_escape_and_other_article_paths_without_writes(tmp_path, invalid):
    out = exporter(tmp_path / "delivery")
    row = out.save(article(content_html=MEDIA))
    result = downloaded(row)
    result["rows"][0]["local_path"] = invalid
    before = out.csv_path.read_bytes()
    with pytest.raises(ValueError):
        out.update_media(BIZ, "123", 1, result)
    assert out.csv_path.read_bytes() == before and out.pending_count == 0


def test_media_rejects_cross_journal_and_unknown_identity(tmp_path):
    out = exporter(tmp_path / "delivery")
    row = out.save(article(content_html=MEDIA))
    result = downloaded(row)
    result["rows"][0]["biz"] = "journal-B"
    with pytest.raises(ValueError):
        out.update_media(BIZ, "123", 1, result)
    with pytest.raises(ValueError, match="unknown_media_article"):
        out.update_media("journal-B", "123", 1, downloaded(row))


def test_work_root_must_not_pollute_delivery(tmp_path):
    root = tmp_path / "delivery"
    with pytest.raises(ValueError, match="work_root"):
        exporter(root, work_root=root / "internal")


def test_invalid_snapshot_creates_no_article_records(tmp_path):
    out = exporter(tmp_path / "delivery")
    with pytest.raises(ValueError):
        out.save(article(identity_matches=False))
    assert out.pending_count == 0 and not list(tmp_path.rglob("*.json"))


def test_actual_image_agent_metadata_only_and_no_independent_cover_contract(tmp_path, monkeypatch):
    import hashlib
    from crawl import bundle_media
    from crawl.session_media import download_article_media
    out = exporter(tmp_path / "delivery")
    a = article(content_html=SOUND_MEDIA, article_type="9", media_assets=[{"kind": "video", "id": "wxv_123"}],
                cover_url="https://mmbiz.qpic.cn/separate-cover.png")
    row = out.save(a)
    calls = []

    def fake_fetch(asset, folder):
        assert asset["kind"] == "image"
        calls.append(asset["url"])
        path = folder / "images" / "body.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"offline-body-image"
        path.write_bytes(payload)
        return dict(kind="image", source_url=asset["url"], local_path="images/body.png", status="downloaded",
                    bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), error_code=""), []

    monkeypatch.setattr(bundle_media, "fetch_asset", fake_fetch)
    result = download_article_media(row, out.article_media_root(row["biz"]))
    manifest = out.article_media_root(BIZ) / "附件/123_1/media_manifest.json"
    before = manifest.read_bytes()
    out.update_media(BIZ, "123", 1, result)
    assert out.flush()
    actual = read_rows(out.root)[1][0]
    assert actual["media_pending"] == "0" and actual["media_status"] == "complete"
    assert len(calls) == 1 and "separate-cover" not in calls[0]
    index = json.loads(actual["media_index"])
    assert [r["kind"] for r in index] == ["image", "audio", "video"]
    assert [r["status"] for r in index] == ["downloaded", "metadata_only", "metadata_only"]
    assert manifest.read_bytes() == before  # Global paths must not break agent cache.
    assert (out.root / actual["media_manifest"]) == manifest


def test_metadata_only_video_has_no_pending_downloads(tmp_path):
    out = exporter(tmp_path / "delivery")
    row = out.save(article(content_html="", article_type="9", media_assets=[{"kind": "video", "id": "wxv_123"}]))
    assert row["has_video"] and not row["has_text"]
    assert row["media_pending"] == 0
    assert row["media_index"][0]["status"] == "metadata_only"


def test_session_canonical_url_fallback_is_accepted(tmp_path):
    out = exporter(tmp_path / "delivery")
    value = article()
    value["canonical_url"] = value.pop("url") + "&key=PRIVATE_CANONICAL"
    row = out.save(value)
    assert row["url"] == article()["url"]


def test_sn_conflict_in_duplicate_csv_tail_is_not_silently_repaired(tmp_path):
    out = exporter(tmp_path / "delivery")
    out.save(article())
    headers, rows = read_rows(out.root)
    duplicate = rows[0]
    duplicate.update(sn="b" * 32, url=article(sn="b" * 32)["url"])
    with out.csv_path.open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=headers).writerow(duplicate)
    before = out.csv_path.read_bytes()
    with pytest.raises(ValueError, match="identity_conflict"):
        exporter(out.root)
    assert out.csv_path.read_bytes() == before


def test_restart_restores_typed_row_when_only_delivery_csv_is_available(tmp_path):
    out = exporter(tmp_path / "delivery", work_root=tmp_path / "work1")
    before = out.save(article(content_html=MEDIA))
    fresh = exporter(out.root, work_root=tmp_path / "work2")
    after = fresh.save(article(content_html=MEDIA))
    assert after == before
    assert len(read_rows(out.root)[1]) == 1


def test_spool_durability_failure_never_claims_or_writes_csv(tmp_path, monkeypatch):
    import export.global_article_exporter as module
    out = exporter(tmp_path / "delivery")
    original = module._atomic_json

    def disk_full(path, row):
        if Path(path).is_relative_to(out.work_root):
            raise OSError("synthetic disk full")
        return original(path, row)

    monkeypatch.setattr(module, "_atomic_json", disk_full)
    with pytest.raises(OSError, match="disk full"):
        out.save(article())
    assert not out.csv_path.exists()


def test_failure_between_spool_and_artifacts_recovers_on_restart(tmp_path, monkeypatch):
    out = exporter(tmp_path / "delivery")

    def crash(row):
        raise OSError("synthetic crash after spool")

    monkeypatch.setattr(out, "_write_artifacts", crash)
    with pytest.raises(OSError, match="after spool"):
        out.save(article())
    assert out.pending_count == 1 and not out.csv_path.exists()
    resumed = exporter(out.root)
    assert resumed.pending_count == 0 and len(read_rows(out.root)[1]) == 1

def test_offline_existing_row_updates_can_defer_csv_rewrite(tmp_path,monkeypatch):
    root=tmp_path/'delivery';out=exporter(root)
    out.save(article())
    replaces=watch_replaces(monkeypatch,root)
    result=out.save(article(title='修正标题'),defer_updates=True)
    assert result['csv_pending'] is True and replaces==[]
    assert out.flush() is True and len(replaces)==1
    assert read_rows(root)[1][0]['title']=='修正标题'


def test_picture_post_has_gallery_count_and_index_without_inventing_text(tmp_path):
    url='https://mmbiz.qpic.cn/mmbiz_jpg/GALLERY/0?wx_fmt=jpeg'
    saved=exporter(tmp_path).save(article(article_type='9',content_html='',media_assets=[{'kind':'image','url':url}]))
    assert saved['has_images'] is True
    assert saved['has_text'] is False
    assert saved['image_count']==1 and saved['video_count']==0
    assert saved['content_html']=='' and saved['content_text']==''
    assert saved['content_origin']=='cgiDataNew.picture_page_info_list'
    assert saved['media_index'][0]['url']==url
    assert saved['media_index'][0]['kind']=='image'


def test_legacy_capture_does_not_become_new_background_capture(tmp_path):
    row=exporter(tmp_path).save(article(metrics_provider='fiddler_classic:/s',publish_timestamp='1700000000',digest='Original digest'))
    assert row['metrics_provider']=='fiddler_classic:/s'
    assert row['content_origin']=='legacy_capture.article_event.article.content_html'
    assert row['field_provenance']['content']=='legacy_capture.article_event.article.content_html'
    assert row['field_provenance']['publish_time']=='legacy_capture.article_event.article.publish_time'
    assert row['field_provenance']['digest']=='legacy_capture.article_event.article.digest'
    _,rows=read_rows(tmp_path)
    assert rows[0]['metrics_provider']=='fiddler_classic:/s'
