from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from crawl.history_runner import SessionUnavailable, run_account_history
from metrics.providers.wechat_backend import BackendArticleItem
from storage.database import create_session_factory, init_db
from storage.models import FailedCrawlTask
from storage.repository import Repository
from utils.progress_logger import ProgressLogger


@dataclass(frozen=True)
class AccountStub:
    id: int = 1
    canonical_name: str = "target-account"
    biz: str = "target-biz"


@dataclass
class CheckpointStub:
    page: int
    processed_urls: list[str]
    status: str


class RepositoryStub:
    def __init__(self, checkpoint=None):
        self.checkpoint = checkpoint
        self.saved = []
        self.failures = []

    def get_crawl_checkpoint(self, task_name):
        return self.checkpoint

    def save_crawl_checkpoint(self, task_name, account_name, page, processed_urls, status="running"):
        self.checkpoint = CheckpointStub(page=page, processed_urls=processed_urls, status=status)
        self.saved.append(self.checkpoint)
        return self.checkpoint

    def record_failed_task(self, task_name, account_name, url, error_message):
        self.failures.append((task_name, account_name, url, error_message))

    def mark_failed_task_success(self, task_name, url):
        return None


def _item(mid: str, idx: int = 1) -> BackendArticleItem:
    return BackendArticleItem(
        title="title",
        author="author",
        digest="",
        cover_url="",
        link=f"https://mp.weixin.qq.com/s?mid={mid}&idx={idx}",
        mid=mid,
        idx=idx,
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class ClientStub:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_publish_page(self, begin, count):
        self.calls.append((begin, count))
        return self.pages[begin]


def test_resumes_current_catalog_page_without_reprocessing_completed_items():
    repo = RepositoryStub(CheckpointStub(page=0, processed_urls=["1:1"], status="running"))
    client = ClientStub({0: ([_item("1"), _item("2")], 2)})
    processed = []

    result = run_account_history(repo, AccountStub(), client, lambda item: processed.append((item.mid, item.idx)), page_size=2)

    assert processed == [("2", 1)]
    assert client.calls == [(0, 2)]
    assert result.completed is True
    assert repo.checkpoint.page == 2
    assert repo.checkpoint.processed_urls == ["1:1", "2:1"]
    assert repo.checkpoint.status == "completed"


def test_failure_keeps_item_unprocessed_records_sanitized_error_and_does_not_complete():
    repo = RepositoryStub()
    client = ClientStub({0: ([_item("1"), _item("2")], 2)})

    def process(item):
        if item.mid == "1":
            raise RuntimeError("private-cookie=must-not-store")

    result = run_account_history(repo, AccountStub(), client, process, page_size=2)

    assert result.completed is False
    assert result.failed == 1
    assert repo.checkpoint.status == "failed"
    assert repo.checkpoint.processed_urls == ["2:1"]
    assert repo.failures[0][3] == "RuntimeError"


def test_failed_item_on_earlier_page_is_reconsidered_when_resuming():
    repo = RepositoryStub()
    client = ClientStub({
        0: ([_item("1")], 2),
        1: ([_item("2")], 2),
    })
    run_account_history(
        repo,
        AccountStub(),
        client,
        lambda item: (_ for _ in ()).throw(RuntimeError("failed")) if item.mid == "1" else None,
        page_size=1,
    )

    resumed = run_account_history(repo, AccountStub(), client, lambda item: None, page_size=1)

    assert resumed.completed is True
    assert client.calls == [(0, 1), (1, 1), (0, 1), (1, 1)]


def test_session_unavailable_pauses_current_page_without_creating_article_failure():
    repo = RepositoryStub()
    client = ClientStub({0: ([_item("1")], 1)})

    result = run_account_history(repo, AccountStub(), client, lambda item: (_ for _ in ()).throw(SessionUnavailable("expired")))

    assert result.completed is False
    assert result.failed == 0
    assert repo.failures == []
    assert repo.checkpoint.page == 0
    assert repo.checkpoint.status == "paused"


def test_failed_final_page_remains_failed_when_resumed():
    repo = RepositoryStub()
    client = ClientStub({0: ([_item("1"), _item("2")], 2)})

    first_run = run_account_history(
        repo,
        AccountStub(),
        client,
        lambda item: (_ for _ in ()).throw(RuntimeError("failed")) if item.mid == "1" else None,
        page_size=2,
    )
    second_run = run_account_history(repo, AccountStub(), client, lambda item: None, page_size=2)

    assert first_run.completed is False
    assert second_run.completed is True
    assert client.calls == [(0, 2), (0, 2)]
    assert repo.checkpoint.status == "completed"
    assert repo.checkpoint.processed_urls == ["2:1", "1:1"]


def test_overlapping_catalog_pages_never_mark_account_completed():
    repo = RepositoryStub()
    client = ClientStub({
        0: ([_item("1"), _item("2")], 4),
        2: ([_item("2"), _item("3")], 4),
    })

    result = run_account_history(repo, AccountStub(), client, lambda item: None, page_size=2)

    assert result.completed is False
    assert repo.checkpoint.status == "failed"


def test_history_runner_logs_page_article_and_checkpoint_without_sensitive_values():
    repo = RepositoryStub()
    source = _item("private-mid")
    item = BackendArticleItem(
        title="安全标题",
        author=source.author,
        digest=source.digest,
        cover_url=source.cover_url,
        link="https://mp.weixin.qq.com/s?token=private-token",
        mid=source.mid,
        idx=source.idx,
        publish_time=source.publish_time,
    )
    client = ClientStub({0: ([item], 1)})
    stream = StringIO()

    def process(_item):
        metric = SimpleNamespace(
            read_count=1,
            like_count=2,
            watching_count=3,
            favorite_count=4,
            share_count=5,
            comment_count=0,
        )
        return SimpleNamespace(content_text="正文内容", image_count=2, latest_metrics=metric)

    result = run_account_history(
        repo,
        AccountStub(),
        client,
        process,
        page_size=1,
        progress_logger=ProgressLogger(stream=stream),
    )

    output = stream.getvalue()
    assert result.completed is True
    assert "目录页" in output
    assert "[文章 1] 开始" in output
    assert "[文章 1] 已保存" in output
    assert "正文字符=4" in output
    assert "图片=2" in output
    assert "指标=6/6" in output
    assert output.count("checkpoint 已保存") == len(repo.saved)
    assert "private-mid" not in output
    assert "private-token" not in output
    assert "https://" not in output


def test_history_runner_logs_sanitized_failure_type():
    repo = RepositoryStub()
    client = ClientStub({0: ([_item("1")], 1)})
    stream = StringIO()

    result = run_account_history(
        repo,
        AccountStub(),
        client,
        lambda _item: (_ for _ in ()).throw(RuntimeError("cookie=private-token")),
        progress_logger=ProgressLogger(stream=stream),
    )

    output = stream.getvalue()
    assert result.completed is False
    assert "RuntimeError" in output
    assert "private-token" not in output
    assert "cookie=" not in output


def test_empty_or_repeated_page_never_marks_account_completed():
    repo = RepositoryStub()
    client = ClientStub({0: ([_item("1")], 2), 1: ([], 2)})

    result = run_account_history(repo, AccountStub(), client, lambda item: None, page_size=1)

    assert result.completed is False
    assert repo.checkpoint.status == "failed"


@pytest.fixture
def persistent_repo(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'history.db'}")
    init_db(session_factory)
    return Repository(session_factory)


def test_earlier_failure_survives_later_pause_and_prevents_false_completion(persistent_repo):
    repo = persistent_repo
    account = AccountStub()
    task_name = f"crawl-history:{account.canonical_name}"
    client = ClientStub({
        0: ([_item("1")], 3),
        1: ([_item("2")], 3),
        2: ([_item("3")], 3),
    })

    def fail_then_pause(item):
        if item.mid == "1":
            raise RuntimeError("private-cookie=must-not-store")
        raise SessionUnavailable("expired")

    first = run_account_history(repo, account, client, fail_then_pause, page_size=1)
    paused = repo.get_crawl_checkpoint(task_name)
    assert first.failed == 1
    assert first.completed is False
    assert paused.page == 1
    assert paused.status == "paused"

    resumed_items = []
    resumed = run_account_history(
        repo, account, client, lambda item: resumed_items.append(item.mid), page_size=1,
    )

    assert resumed_items == ["2", "3"]
    assert resumed.failed == 0  # Per-run count, not the persisted unresolved total.
    assert resumed.completed is False
    assert repo.get_crawl_checkpoint(task_name).status == "failed"
    with repo.session_factory() as session:
        failure = session.query(FailedCrawlTask).filter_by(task_name=task_name).one()
        assert failure.status == "failed"
        assert failure.last_error == "RuntimeError"

    retried_items = []
    retried = run_account_history(
        repo, account, client, lambda item: retried_items.append(item.mid), page_size=1,
    )

    assert retried_items == ["1"]
    assert retried.completed is True
    assert repo.get_crawl_checkpoint(task_name).status == "completed"
    with repo.session_factory() as session:
        assert session.query(FailedCrawlTask).filter_by(task_name=task_name).one().status == "success"


@pytest.mark.parametrize("failure_task, status", [
    ("metrics:target-biz", "failed"),
    ("crawl-history:another-account", "failed"),
    ("crawl-history:target-account", "success"),
])
def test_completion_ignores_metrics_other_tasks_and_resolved_failures(persistent_repo, failure_task, status):
    repo = persistent_repo
    account = AccountStub()
    task_name = f"crawl-history:{account.canonical_name}"
    repo.record_failed_task(failure_task, account.canonical_name, "article:1:1", "missing_metrics")
    if status == "success":
        repo.mark_failed_task_success(failure_task, "article:1:1")
    repo.save_crawl_checkpoint(task_name, account.canonical_name, 1, ["1:1"], status="paused")
    client = ClientStub({1: ([_item("2")], 2)})

    result = run_account_history(repo, account, client, lambda _item: None, page_size=1)

    assert result.completed is True
    assert repo.get_crawl_checkpoint(task_name).status == "completed"
    with repo.session_factory() as session:
        assert session.query(FailedCrawlTask).filter_by(task_name=failure_task).one().status == status


def test_retry_exhausted_body_failure_still_blocks_completion(persistent_repo):
    repo = persistent_repo
    account = AccountStub()
    task_name = f"crawl-history:{account.canonical_name}"
    for _ in range(4):
        repo.record_failed_task(task_name, account.canonical_name, "article:1:1", "RuntimeError")
    assert repo.list_retryable_failed_tasks() == []
    repo.save_crawl_checkpoint(task_name, account.canonical_name, 1, [], status="paused")
    client = ClientStub({1: ([_item("2")], 2)})

    result = run_account_history(repo, account, client, lambda _item: None, page_size=1)

    assert result.failed == 0
    assert result.completed is False
    assert repo.get_crawl_checkpoint(task_name).status == "failed"


def test_failed_failure_lookup_never_marks_history_completed_or_leaks_details():
    class BrokenLookupRepository(RepositoryStub):
        def session_factory(self):
            raise RuntimeError("cookie=private-token")

    repo = BrokenLookupRepository()
    client = ClientStub({0: ([_item("1")], 1)})
    stream = StringIO()

    result = run_account_history(
        repo, AccountStub(), client, lambda _item: None,
        progress_logger=ProgressLogger(stream=stream),
    )

    assert result.completed is False
    assert repo.checkpoint.status == "failed"
    assert "private-token" not in str(result) + stream.getvalue()
