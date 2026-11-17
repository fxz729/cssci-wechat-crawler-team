from argparse import Namespace
from datetime import datetime, timezone
import csv
import json

from main import cmd_export
from export.realtime_exporter import RealtimeExporter, clean_export_row, default_export_headers
from storage.database import create_session_factory, init_db
from storage.repository import Repository


def _repo_with_dirty_article(tmp_path, **article_fields):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", ["北京大学教育评论"], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99, candidate_profile_url="https://mp.weixin.qq.com/mp/profile_ext?__biz=biz")
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", datetime(2025, 1, 1, tzinfo=timezone.utc), "source")
    article = repo.upsert_article(
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
        content_text="第一段\n第二段",
        image_urls=["https://mmbiz.qpic.cn/paper.jpg", "https://mmbiz.qpic.cn/icon.gif"],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
        content_html="<p>第一段</p><p>第二段</p>",
        cover_image_url="https://mmbiz.qpic.cn/cover.jpg",
        comment_id="comment123",
        word_count=6,
        audio_count=1,
        video_count=2,
        collect_num=8,
        **article_fields,
    )
    repo.insert_metrics_snapshot(
        article_id=article.id,
        provider="wechat_authorized",
        provider_priority=100,
        read_count=100,
        like_count=10,
        watching_count=5,
        share_count=3,
        comment_count=1,
        favorite_count=8,
        reward_count=99,
        is_authorized_source=True,
    )
    return repo


def test_clean_export_rows_exclude_internal_fields_and_include_plain_content(tmp_path):
    repo = _repo_with_dirty_article(tmp_path)

    row = repo.list_articles_for_export("北京大学教育评论")[0]

    assert row["content_html"] == "<p>第一段</p><p>第二段</p>"
    assert row["cover_image_url"] == "https://mmbiz.qpic.cn/cover.jpg"
    assert "raw_html_path" not in row
    assert "raw_text_path" not in row
    assert row["image_urls"] == ["https://mmbiz.qpic.cn/paper.jpg", "https://mmbiz.qpic.cn/icon.gif"]
    assert "subscriber_count" not in row
    assert row["content"] == "第一段 第二段"
    assert row["image_count"] == 1
    assert row["images_folder"] == "images/北京大学教育评论"


def test_cli_export_excludes_internal_and_region_fields(tmp_path):
    repo = _repo_with_dirty_article(tmp_path)
    csv_path = tmp_path / "articles.csv"

    exit_code = cmd_export(Namespace(format="csv", output=str(csv_path), account="北京大学教育评论"), repo)

    csv_text = csv_path.read_text(encoding="utf-8-sig")
    header = next(csv.reader(csv_text.splitlines()))
    preferred = [
        "account_name", "author", "publish_time", "title", "title_length",
        "digest", "digest_length", "comment_id", "content", "word_count",
        "image_count", "images_folder", "video_count", "audio_count",
        "read_count", "watching_count", "like_count", "favorite_count",
        "share_count", "comment_count", "collect_num", "position_in_batch",
    ]
    assert exit_code == 0
    assert header[:len(preferred)] == preferred
    assert "_image_urls_for_download" not in header
    assert "province_or_region" not in header
    assert "hour" not in header
    assert "month" not in header
    assert "reward_count" not in header
    assert "weekday" not in header
    assert "year" not in header

    repo = _repo_with_dirty_article(tmp_path)
    exporter = RealtimeExporter(repo=repo, output_dir=tmp_path / "output", formats=["csv", "json"])

    written = exporter.update(account_name="北京大学教育评论")

    assert tmp_path / "output" / "csv" / "北京大学教育评论.csv" in written
    assert tmp_path / "output" / "json" / "北京大学教育评论.json" in written
    assert not (tmp_path / "output" / "images" / "北京大学教育评论").exists()


def test_cli_export_download_images_uses_parent_for_file_output_path(tmp_path):
    repo = _repo_with_dirty_article(tmp_path)
    output_file = tmp_path / "output" / "articles.csv"

    exit_code = cmd_export(Namespace(
        format="csv",
        output=str(output_file),
        account="北京大学教育评论",
        download_images=True,
    ), repo)

    assert exit_code == 0
    assert (tmp_path / "output" / "images" / "北京大学教育评论").exists()
    assert not output_file.is_dir()


def test_realtime_exporter_downloads_clean_images_by_account(tmp_path):
    repo = _repo_with_dirty_article(tmp_path)
    exporter = RealtimeExporter(
        repo=repo,
        output_dir=tmp_path / "output",
        formats=["csv", "json"],
        image_fetcher=lambda url: b"image-bytes" if url.endswith("paper.jpg") else b"bad",
    )

    exporter.update(account_name="北京大学教育评论", download_images=True)

    image_dir = tmp_path / "output" / "images" / "北京大学教育评论"
    files = list(image_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".jpg"
    assert files[0].read_bytes() == b"image-bytes"


REQUESTED_EXPORT_FIELDS = [
    "profile_url", "cover_image_url", "content_html", "image_urls", "media_assets",
    "field_provenance", "metrics_provider", "metrics_captured_at", "metrics_status",
    "metric_field_status", "crawl_status",
]


def test_export_includes_media_provenance_and_selected_snapshot_metadata(tmp_path):
    media = [{"kind": "audio", "url": "https://example.org/audio.mp3", "title": "讲座"}]
    provenance = {"author": "article_byline", "digest": "article_meta"}
    repo = _repo_with_dirty_article(tmp_path, media_assets=media, field_provenance=provenance)
    article = repo.list_articles()[0]
    snapshot = repo.get_latest_metrics(article.id)

    row = clean_export_row(repo.list_articles_for_export()[0])

    assert set(REQUESTED_EXPORT_FIELDS) <= row.keys()
    assert row["profile_url"] == "https://mp.weixin.qq.com/mp/profile_ext?__biz=biz"
    assert row["media_assets"] == media
    assert row["field_provenance"]["author"] == "article_byline"
    assert row["field_provenance"]["digest"] == "article_meta"
    assert row["metrics_provider"] == "wechat_authorized"
    assert row["metrics_captured_at"] == snapshot.captured_at
    assert row["metrics_status"] == "complete"
    assert row["crawl_status"] == "success"
    assert not any(key.startswith("_") for key in row)


def test_default_headers_are_fixed_for_empty_and_sparse_rows():
    empty = default_export_headers([])
    blank = default_export_headers([{}])
    sparse = default_export_headers([{"title": "标题", "favorite_count": None}])
    missing = default_export_headers([{name: None for name in REQUESTED_EXPORT_FIELDS}])

    assert set(REQUESTED_EXPORT_FIELDS) <= set(empty)
    assert "read_count" in empty
    assert empty == blank == sparse == missing
    assert len(empty) == len(set(empty))
    extra = default_export_headers([{"custom_column": 0}])
    assert extra == empty + ["custom_column"]


def test_export_preserves_known_legacy_field_provenance_without_mutating_storage(tmp_path):
    provenance = {
        "collect_num": {"source": "history_page", "captured_at": "2026-01-01T00:00:00+00:00"},
        "digest": "article_meta",
    }
    repo = _repo_with_dirty_article(tmp_path, field_provenance=provenance)

    row = repo.list_articles_for_export()[0]

    assert row["field_provenance"] == provenance
    assert json.loads(repo.list_articles()[0].field_provenance_json) == provenance
