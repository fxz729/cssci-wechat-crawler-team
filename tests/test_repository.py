from datetime import datetime, timezone
import json

import pytest

from metrics.providers.base import MetricsSnapshot
from storage.database import create_session_factory, init_db
from storage.repository import Repository


def test_repository_upserts_seed_candidate_account_article_batch_and_metrics(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)

    seed = repo.upsert_journal_seed(
        catalog_version="2025-2026",
        discipline="教育学",
        tier="cssci",
        journal_name="北京大学教育评论",
        normalized_name="北京大学教育评论",
        aliases=["北京大学教育评论"],
        source_file="catalog.md",
        source_line=10,
    )

    candidate = repo.upsert_account_candidate(
        journal_seed_id=seed.id,
        query_text="北京大学教育评论",
        candidate_name="北京大学教育评论",
        candidate_wechat_id="PKUER",
        candidate_description="北京大学教育评论编辑部官方公众号",
        evidence={"matched": ["编辑部"]},
        score=0.98,
    )

    accepted = repo.auto_accept_high_score_candidates(threshold=0.92)
    account = repo.get_account_by_name("北京大学教育评论")

    assert accepted == 1
    assert candidate.id is not None
    assert account is not None
    assert account.confirmation_method == "auto_high_score"

    published_at = datetime(2025, 4, 11, 9, 0, tzinfo=timezone.utc)
    batch = repo.upsert_publish_batch(
        account_id=account.id,
        biz="MzI3MDE3Nzg0OA==",
        mid="2247488874",
        publish_time=published_at,
        source_url="https://mp.weixin.qq.com/s/test",
    )
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="MzI3MDE3Nzg0OA==",
        mid="2247488874",
        idx=1,
        sn="bf59a19d7899ec62b8b40ac12de35ef8",
        title="教育评价悖论：表征、成因与化解",
        author="刘振天 周子怡",
        publish_time=published_at,
        digest="作者单位信息",
        content_text="正文内容",
        image_urls=["https://mmbiz.qpic.cn/example.jpg"],
        source_url="https://weixin.sogou.com/link",
        canonical_url="https://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&mid=2247488874&idx=1&sn=bf59a19d7899ec62b8b40ac12de35ef8",
        raw_html_path="data/raw_html/MzI3MDE3Nzg0OA==/2247488874_1.html",
        raw_text_path="data/raw_text/MzI3MDE3Nzg0OA==/2247488874_1.txt",
    )
    duplicate = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="MzI3MDE3Nzg0OA==",
        mid="2247488874",
        idx=1,
        sn="bf59a19d7899ec62b8b40ac12de35ef8",
        title="教育评价悖论：表征、成因与化解",
        author="刘振天 周子怡",
        publish_time=published_at,
        digest="作者单位信息",
        content_text="正文内容更新",
        image_urls=[],
        source_url="https://weixin.sogou.com/link",
        canonical_url="https://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&mid=2247488874&idx=1&sn=bf59a19d7899ec62b8b40ac12de35ef8",
        raw_html_path="data/raw_html/MzI3MDE3Nzg0OA==/2247488874_1.html",
        raw_text_path="data/raw_text/MzI3MDE3Nzg0OA==/2247488874_1.txt",
    )

    repo.insert_metrics_snapshot(
        article_id=article.id,
        provider="wechat_authorized",
        provider_priority=100,
        read_count=3269,
        like_count=32,
        watching_count=6,
        favorite_count=12,
        share_count=355,
        comment_count=0,
        is_authorized_source=True,
    )
    metrics = repo.get_latest_metrics(article.id)
    stats = repo.get_stats()

    assert duplicate.id == article.id
    assert article.image_count == 1
    assert article.position_in_batch == 1
    assert metrics.read_count == 3269
    assert metrics.favorite_count == 12
    rows = repo.list_articles_for_export("北京大学教育评论")
    assert rows[0]["favorite_count"] == 12
    assert "province_or_region" not in rows[0]
    assert stats["journal_seeds"] == 1
    assert stats["accounts"] == 1
    assert stats["articles"] == 1
    assert stats["metrics"] == 1


def test_repository_completes_article_and_metrics_in_one_operation(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "测试", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    account = repo.get_account_by_name("测试期刊")
    batch = repo.upsert_publish_batch(account.id, "biz", "123", None, "source")
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="123",
        idx=1,
        sn="sn",
        title="标题",
        author="作者",
        publish_time=None,
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
        crawl_status="pending",
    )
    snapshot = MetricsSnapshot(
        provider="wechat-authorized",
        provider_priority=100,
        read_count=101,
        like_count=11,
        watching_count=12,
        favorite_count=13,
        share_count=14,
        comment_count=15,
        collect_num=13,
        raw_payload={"appmsgext": {"_album_id": "album", "_album_name": "合集"}},
        is_authorized_source=True,
    )

    repo.complete_article_with_metrics(article.id, snapshot)

    stored = repo.list_articles(account_name="测试期刊")[0]
    metrics = repo.get_latest_metrics(article.id)
    assert stored.crawl_status == "success"
    assert stored.collect_num == 13
    assert stored.album_id == "album"
    assert stored.album_name == "合集"
    assert metrics.read_count == 101
    assert metrics.comment_count == 15


def test_repository_export_keeps_legacy_favorite_separate_from_snapshot(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "政治学", "cssci", "当代世界社会主义问题", "当代世界社会主义问题", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "当代世界社会主义问题", "当代世界社会主义问题", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("当代世界社会主义问题")
    published_at = datetime(2026, 6, 18, 15, 1, tzinfo=timezone.utc)
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", published_at, "source")
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="mid",
        idx=1,
        sn="sn",
        title="标题",
        author="当代世界社会主义问题",
        publish_time=published_at,
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
        copyright_stat=100,
        collect_num=8,
    )
    repo.insert_metrics_snapshot(
        article_id=article.id,
        provider="wechat_authorized",
        provider_priority=100,
        read_count=3146,
        like_count=23,
        favorite_count=None,
        subscriber_count=999,
        comment_count=0,
        is_authorized_source=True,
    )

    row = repo.list_articles_for_export("当代世界社会主义问题")[0]

    assert row["favorite_count"] is None
    assert row["collect_num"] == 8
    assert row["metric_field_status"]["favorite_count"] == "not_observed"
    assert row["field_provenance"]["collect_num"] == {
        "source": "article.collect_num",
        "captured_at": None,
        "status": "capture_time_unknown",
    }
    assert row["copyright_text"] == "原创"
    assert "subscriber_count" not in row



def _article_upsert_case(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'metadata.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "测试", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    account = repo.get_account_by_name("测试期刊")
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", None, "source")
    fields = dict(
        account_id=account.id, publish_batch_id=batch.id, biz="biz", mid="mid", idx=1,
        sn="sn", title="标题", author="作者", publish_time=None, digest="摘要",
        content_text="正文", image_urls=[], source_url="source", canonical_url="canonical",
        raw_html_path="", raw_text_path="",
    )
    return repo, fields


def test_article_metadata_defaults_are_json_text(tmp_path):
    repo, fields = _article_upsert_case(tmp_path)

    article = repo.upsert_article(**fields)

    assert article.media_assets_json == "[]"
    assert article.field_provenance_json == "{}"
    stored = repo.list_articles()[0]
    assert stored.media_assets_json == "[]"
    assert stored.field_provenance_json == "{}"


@pytest.mark.parametrize("unobserved", [{}, {"media_assets": None, "field_provenance": None}])
def test_upsert_unobserved_metadata_does_not_erase_known_values(tmp_path, unobserved):
    repo, fields = _article_upsert_case(tmp_path)
    media = [{"kind": "video", "id": "video-1", "title": "学术访谈"}]
    provenance = {"title": "article_html", "media_assets": "article_player"}
    article = repo.upsert_article(**fields, media_assets=media, field_provenance=provenance)

    duplicate = repo.upsert_article(**fields, **unobserved)

    assert duplicate.id == article.id
    stored = repo.list_articles()[0]
    assert json.loads(stored.media_assets_json) == media
    assert json.loads(stored.field_provenance_json) == provenance
    assert repo.get_stats()["articles"] == 1


@pytest.mark.parametrize("replacement", [
    {"media_assets": [], "field_provenance": {}},
    {"media_assets": [{"kind": "audio", "url": "https://example.org/audio.mp3"}]},
    {"field_provenance": {"author": "article_byline"}},
])
def test_upsert_explicit_metadata_updates_only_supplied_fields(tmp_path, replacement):
    repo, fields = _article_upsert_case(tmp_path)
    media = [{"kind": "video", "id": "old"}]
    provenance = {"title": "article_html"}
    repo.upsert_article(**fields, media_assets=media, field_provenance=provenance)

    repo.upsert_article(**fields, **replacement)

    stored = repo.list_articles()[0]
    assert json.loads(stored.media_assets_json) == replacement.get("media_assets", media)
    assert json.loads(stored.field_provenance_json) == replacement.get("field_provenance", provenance)
