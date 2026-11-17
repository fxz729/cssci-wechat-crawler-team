from storage.database import create_session_factory, init_db
from storage.repository import Repository


def test_progress_checkpoint_tracks_account_page_and_processed_urls(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)

    repo.save_crawl_checkpoint(
        task_name="crawl:北京大学教育评论",
        account_name="北京大学教育评论",
        page=3,
        processed_urls=["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"],
        status="running",
    )

    checkpoint = repo.get_crawl_checkpoint("crawl:北京大学教育评论")

    assert checkpoint.task_name == "crawl:北京大学教育评论"
    assert checkpoint.account_name == "北京大学教育评论"
    assert checkpoint.page == 3
    assert checkpoint.processed_urls == ["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"]
    assert checkpoint.status == "running"


def test_failed_fetches_are_recorded_and_selected_for_retry(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)

    repo.record_failed_task(
        task_name="crawl:北京大学教育评论",
        account_name="北京大学教育评论",
        url="https://weixin.sogou.com/link",
        error_message="timeout",
    )
    repo.record_failed_task(
        task_name="crawl:北京大学教育评论",
        account_name="北京大学教育评论",
        url="https://weixin.sogou.com/link",
        error_message="timeout again",
    )

    failures = repo.list_retryable_failed_tasks(max_attempts=3)

    assert len(failures) == 1
    assert failures[0].retry_count == 2
    assert failures[0].last_error == "timeout again"
    assert failures[0].status == "failed"
