from datetime import datetime, timezone

from crawl.account_crawler import crawl_account_from_results
from storage.database import create_session_factory, init_db
from storage.repository import Repository


HTML = """
<html><head><meta name="description" content="摘要" /></head><body>
<h1 id="activity-name">文章标题</h1>
<a id="js_name">北京大学教育评论</a>
<span id="publish_time">2025年04月11日</span>
<div id="js_content"><p>正文</p><img data-src="https://mmbiz.qpic.cn/a.jpg" /></div>
</body></html>
"""


def test_crawl_account_from_results_persists_article_batch_and_raw_files(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", ["北京大学教育评论"], "catalog.md", 1)
    candidate = repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")

    stored = crawl_account_from_results(
        repo=repo,
        account=account,
        results=[{"url": "https://weixin.sogou.com/link", "summary": "搜索摘要"}],
        fetch_html=lambda _url: (
            "https://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&mid=2247488874&idx=2&sn=abc",
            HTML,
        ),
        data_dir=tmp_path / "data",
        since=datetime(2016, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    rows = repo.list_articles_for_export("北京大学教育评论")
    refreshed_account = repo.get_account_by_name("北京大学教育评论")
    assert stored == 1
    assert refreshed_account.biz == "MzI3MDE3Nzg0OA=="
    assert len(rows) == 1
    assert rows[0]["title"] == "文章标题"
    assert rows[0]["position_in_batch"] == 2
    assert rows[0]["canonical_url"].startswith("https://mp.weixin.qq.com/s?")
    assert (tmp_path / "data" / "raw_html" / "MzI3MDE3Nzg0OA==" / "2247488874_2.html").exists()


def test_crawl_account_extracts_url_parts_from_html_when_final_url_is_sogou(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", ["北京大学教育评论"], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    html = HTML + "<script>var biz = 'MzI3MDE3Nzg0OA=='; var mid = '2247488874'; var idx = '1'; var sn = 'abc';</script>"

    stored = crawl_account_from_results(
        repo=repo,
        account=account,
        results=[{"url": "https://weixin.sogou.com/link", "summary": "搜索摘要"}],
        fetch_html=lambda _url: ("https://weixin.sogou.com/link", html),
        data_dir=tmp_path / "data",
        since=datetime(2016, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    rows = repo.list_articles_for_export("北京大学教育评论")
    assert stored == 1
    assert rows[0]["position_in_batch"] == 1
    assert "__biz=MzI3MDE3Nzg0OA" in rows[0]["canonical_url"]
