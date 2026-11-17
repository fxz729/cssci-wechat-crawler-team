from datetime import datetime, timezone

from crawl.account_crawler import crawl_account_from_results
from storage.database import create_session_factory, init_db
from storage.repository import Repository


HTML = """
<html><body>
<h1 id="activity-name">招聘 | 化学工业出版社、新经典、《北京大学教育评论》招聘</h1>
<a id="js_name">月亮与稿便士</a>
<span id="publish_time">2025年04月11日</span>
<div id="js_content"><p>正文</p></div>
<script>var biz = 'biz'; var mid = 'mid'; var idx = '1'; var sn = 'sn';</script>
</body></html>
"""


def _repo_with_account(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", ["北京大学教育评论"], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    return repo, repo.get_account_by_name("北京大学教育评论")


def test_crawl_skips_articles_not_published_by_confirmed_account(tmp_path):
    repo, account = _repo_with_account(tmp_path)

    stored = crawl_account_from_results(
        repo=repo,
        account=account,
        results=[{"url": "https://weixin.sogou.com/link", "summary": ""}],
        fetch_html=lambda _url: ("https://weixin.sogou.com/link", HTML),
        data_dir=tmp_path / "data",
        since=datetime(2016, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    assert stored == 0
    assert repo.list_articles_for_export("北京大学教育评论") == []


def test_crawl_does_not_use_aliases_to_accept_unrelated_publisher(tmp_path):
    repo, account = _repo_with_account(tmp_path)
    account.account_aliases_json = '["北京大学教育评论"]'

    stored = crawl_account_from_results(
        repo=repo,
        account=account,
        results=[{"url": "https://weixin.sogou.com/link", "summary": ""}],
        fetch_html=lambda _url: ("https://weixin.sogou.com/link", HTML),
        data_dir=tmp_path / "data",
        since=datetime(2016, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    assert stored == 0
    assert repo.list_articles_for_export("北京大学教育评论") == []


def test_export_keeps_official_source_article_with_paper_author(tmp_path):
    repo, account = _repo_with_account(tmp_path)
    batch = repo.upsert_publish_batch(account.id, "biz", "mid", datetime(2025, 1, 1, tzinfo=timezone.utc), "source")
    repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="mid",
        idx=1,
        sn="sn",
        title="教育评价悖论：表征、成因与化解",
        author="刘振天 周子怡",
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )

    rows = repo.list_articles_for_export("北京大学教育评论")
    assert len(rows) == 1
    assert rows[0]["author"] == "刘振天 周子怡"
