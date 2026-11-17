import csv
import json
from datetime import datetime, timezone

from export.realtime_exporter import RealtimeExporter, default_export_headers
from storage.database import create_session_factory, init_db
from storage.repository import Repository


def _repo_with_article(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", ["北京大学教育评论"], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", datetime(2025, 1, 1, tzinfo=timezone.utc), "source")
    repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="mid",
        idx=1,
        sn="sn",
        title="标题",
        author="北京大学教育评论",
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=["https://mmbiz.qpic.cn/a.jpg"],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
        content_html="<p>正文</p>",
        cover_image_url="https://mmbiz.qpic.cn/cover.jpg",
    )
    return repo


def test_realtime_exporter_updates_csv_and_json_snapshots(tmp_path):
    repo = _repo_with_article(tmp_path)
    exporter = RealtimeExporter(repo=repo, output_dir=tmp_path / "output", formats=["csv", "json"])

    written = exporter.update(account_name="北京大学教育评论")

    csv_path = tmp_path / "output" / "csv" / "北京大学教育评论.csv"
    json_path = tmp_path / "output" / "json" / "北京大学教育评论.json"
    assert written == [csv_path, json_path]
    assert csv_path.exists()
    assert json_path.exists()
    assert "标题" in csv_path.read_text(encoding="utf-8-sig")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data[0]["title"] == "标题"
    assert data[0]["digest"] == "摘要"
    assert data[0]["content"] == "正文"
    assert data[0]["content_html"] == "<p>正文</p>"
    assert data[0]["image_urls"] == ["https://mmbiz.qpic.cn/a.jpg"]
    assert data[0]["image_count"] == 1
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "content" in csv_text
    assert "content_html" in csv_text
    assert "image_urls" in csv_text
    assert "image_count" in csv_text
    assert "province_or_region" not in csv_text
    assert "province_or_region" not in data[0]


REQUESTED_EXPORT_FIELDS = {
    "profile_url", "cover_image_url", "content_html", "image_urls", "media_assets",
    "field_provenance", "metrics_provider", "metrics_captured_at", "metrics_status",
    "metric_field_status", "crawl_status",
}


def test_realtime_empty_export_writes_fixed_csv_header_and_empty_json(tmp_path):
    factory = create_session_factory(f"sqlite:///{tmp_path / 'empty.db'}")
    init_db(factory)
    exporter = RealtimeExporter(Repository(factory), tmp_path / "empty-output")

    csv_path, json_path = exporter.update()

    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert REQUESTED_EXPORT_FIELDS <= set(reader.fieldnames)
        assert reader.fieldnames == default_export_headers([])
        assert list(reader) == []
    assert json.loads(json_path.read_text(encoding="utf-8")) == []


def test_realtime_sparse_metrics_roundtrip_preserves_null_zero_and_nested_fields(tmp_path):
    repo = _repo_with_article(tmp_path)
    article = repo.list_articles()[0]
    repo.insert_metrics_snapshot(article.id, "client", read_count=0, comment_count=0)
    snapshot = repo.get_latest_metrics(article.id)
    exporter = RealtimeExporter(repo, tmp_path / "sparse-output")

    csv_path, json_path = exporter.update()

    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == default_export_headers([])
        csv_row = list(reader)[0]
    json_row = json.loads(json_path.read_text(encoding="utf-8"))[0]
    assert REQUESTED_EXPORT_FIELDS <= csv_row.keys()
    assert REQUESTED_EXPORT_FIELDS <= json_row.keys()
    assert csv_row["read_count"] == "0"
    assert json_row["read_count"] == 0
    for name in ("favorite_count", "like_count", "watching_count", "share_count"):
        assert csv_row[name] == ""
        assert json_row[name] is None
    assert json.loads(csv_row["media_assets"]) == json_row["media_assets"] == []
    assert json.loads(csv_row["field_provenance"]) == json_row["field_provenance"]
    assert json.loads(csv_row["image_urls"]) == json_row["image_urls"]
    assert csv_row["content_html"] == json_row["content_html"] == "<p>正文</p>"
    assert csv_row["cover_image_url"] == json_row["cover_image_url"]
    assert csv_row["metrics_provider"] == json_row["metrics_provider"] == "client"
    assert csv_row["metrics_captured_at"] == json_row["metrics_captured_at"] == str(snapshot.captured_at)
    assert csv_row["metrics_status"] == json_row["metrics_status"] == "partial"
    assert json.loads(csv_row["metric_field_status"]) == json_row["metric_field_status"]
    assert json_row["metric_field_status"]["read_count"] == "observed"
    assert json_row["metric_field_status"]["favorite_count"] == "not_observed"
