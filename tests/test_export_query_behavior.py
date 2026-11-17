from datetime import datetime, timezone

from storage.database import create_session_factory, init_db
from storage.repository import Repository
from storage.models import ArticleMetric


def test_export_uses_highest_priority_latest_metric_for_each_article(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    account = repo.get_account_by_name("测试期刊")
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", datetime(2025, 1, 1, tzinfo=timezone.utc), "source")
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="mid",
        idx=1,
        sn="sn",
        title="标题",
        author="作者",
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="",
        raw_text_path="",
    )
    repo.insert_metrics_snapshot(article.id, "low", provider_priority=10, read_count=10)
    repo.insert_metrics_snapshot(article.id, "high", provider_priority=100, read_count=100)

    rows = repo.list_articles_for_export("测试期刊")

    assert rows[0]["read_count"] == 100


METRIC_FIELDS = (
    "read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count",
)


def _repo_and_article(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'snapshots.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "测试", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    account = repo.get_account_by_name("测试期刊")
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", None, "source")
    article = repo.upsert_article(
        account_id=account.id, publish_batch_id=batch.id, biz="biz", mid="mid", idx=1,
        sn="sn", title="标题", author="作者", publish_time=None, digest="摘要",
        content_text="正文", image_urls=[], source_url="source", canonical_url="canonical",
        raw_html_path="", raw_text_path="", collect_num=9, crawl_status="content_only",
    )
    return repo, article


def _raw_snapshots(repo):
    with repo.session_factory() as session:
        return [dict(row._mapping) for row in session.execute(
            ArticleMetric.__table__.select().order_by(ArticleMetric.id)
        )]


def test_sparse_export_keeps_selected_snapshot_and_all_original_observations(tmp_path):
    repo, article = _repo_and_article(tmp_path)
    old = repo.insert_metrics_snapshot(
        article.id, "high-old", provider_priority=100,
        **dict.fromkeys(METRIC_FIELDS, 12), raw_payload={"sample": "old"},
    )
    selected = repo.insert_metrics_snapshot(
        article.id, "high-new", provider_priority=100,
        read_count=0, comment_count=0, raw_payload={"sample": "sparse"},
    )
    low = repo.insert_metrics_snapshot(
        article.id, "low-newest", provider_priority=10,
        **dict.fromkeys(METRIC_FIELDS, 30), raw_payload={"sample": "low"},
    )
    with repo.session_factory() as session:
        for metric, day in [(old, 1), (selected, 2), (low, 3)]:
            session.get(ArticleMetric, metric.id).captured_at = datetime(2026, 9, day)
        session.commit()
    before = _raw_snapshots(repo)

    row = repo.list_articles_for_export()[0]

    assert row["read_count"] == 0
    assert row["comment_count"] == 0
    for name in ("like_count", "watching_count", "share_count", "favorite_count"):
        assert row[name] is None
    assert row["metrics_provider"] == "high-new"
    assert row["metrics_captured_at"] == datetime(2026, 9, 2)
    assert row["metrics_status"] == "partial"
    assert row["metric_field_status"] == {
        name: "observed" if name in ("read_count", "comment_count") else "not_observed"
        for name in METRIC_FIELDS
    }
    assert row["crawl_status"] == "content_only"
    assert row["collect_num"] == 9
    assert repo.get_latest_metrics(article.id).id == selected.id
    assert _raw_snapshots(repo) == before
    assert len(before) == 3


def test_export_without_snapshot_does_not_invent_observations(tmp_path):
    repo, _article = _repo_and_article(tmp_path)

    row = repo.list_articles_for_export()[0]

    assert all(row[name] is None for name in METRIC_FIELDS)
    assert row["metrics_provider"] is None
    assert row["metrics_captured_at"] is None
    assert row["metrics_status"] == "not_observed"
    assert row["metric_field_status"] == dict.fromkeys(METRIC_FIELDS, "not_observed")
    assert row["collect_num"] == 9
    assert row["field_provenance"]["collect_num"]["captured_at"] is None
    assert _raw_snapshots(repo) == []


def test_zero_metrics_are_complete_observations(tmp_path):
    repo, article = _repo_and_article(tmp_path)
    repo.insert_metrics_snapshot(article.id, "client", **dict.fromkeys(METRIC_FIELDS, 0))

    row = repo.list_articles_for_export()[0]

    assert all(row[name] == 0 for name in METRIC_FIELDS)
    assert row["metrics_status"] == "complete"
    assert row["metric_field_status"] == dict.fromkeys(METRIC_FIELDS, "observed")


def test_empty_snapshot_retains_source_and_time_without_claiming_observed_fields(tmp_path):
    repo, article = _repo_and_article(tmp_path)
    repo.insert_metrics_snapshot(article.id, "client-empty", raw_payload={"status": "no_fields"})
    snapshot = repo.get_latest_metrics(article.id)

    row = repo.list_articles_for_export()[0]

    assert row["metrics_provider"] == "client-empty"
    assert row["metrics_captured_at"] == snapshot.captured_at
    assert row["metrics_status"] == "not_observed"
    assert row["metric_field_status"] == dict.fromkeys(METRIC_FIELDS, "not_observed")
    assert all(row[name] is None for name in METRIC_FIELDS)
