from datetime import datetime, timezone
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import crawl.article_persistence as persistence

from crawl.article_fetcher import persist_article_files
from crawl.article_persistence import persist_history_article
from crawl.history_runner import SessionUnavailable
from crawl.wechat_browser import WechatBrowserClient
from storage.database import create_session_factory, init_db
from storage.repository import Repository


def test_history_article_persistence_exposes_one_transport_independent_api():
    spec = importlib.util.find_spec("crawl.article_persistence")

    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert list(inspect.signature(module.persist_history_article).parameters) == [
        "repo",
        "account",
        "client",
        "item",
        "data_dir",
        "with_metrics",
    ]
    assert inspect.signature(module.persist_history_article).parameters["with_metrics"].default is True


class BrowserPageStub:
    def goto(self, _url, **_kwargs):
        return None

    def evaluate(self, _script, argument):
        if argument["operation"] == "article":
            return {
                "http_status": 200,
                "url": "https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=article-sn",
                "html": """
                    <html><head>
                    <title>浏览器文章</title>
                    <meta name="author" content="测试作者" />
                    <meta name="description" content="测试摘要" />
                    </head><body>
                    <div id="js_content">
                      <p>完整正文</p>
                      <img data-src="https://mmbiz.qpic.cn/test-image?wx_fmt=png" />
                    </div>
                    <script>var req_id = 'request-id'; var comment_id = 'comment-id';</script>
                    </body></html>
                """,
            }
        if argument["operation"] == "metrics":
            return {
                "http_status": 200,
                "ext_payload": {
                    "base_resp": {"ret": 0},
                    "appmsgstat": {
                        "read_num": 101,
                        "old_like_num": 11,
                        "like_num": 12,
                        "collect_num": 13,
                        "share_num": 14,
                    },
                },
                "comment_payload": {"base_resp": {"ret": 0}, "total_count": 15},
            }
        raise AssertionError(argument)


class BrowserContextStub:
    def add_cookies(self, _cookies):
        return None

    def cookies(self):
        return [{"name": "appmsg_token", "value": "fresh-token"}]


class BrowserSessionStub:
    def __init__(self, **_kwargs):
        self.context = BrowserContextStub()
        self.page = BrowserPageStub()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return None

    def new_page(self):
        return self.page


def test_browser_transport_persists_article_content_images_and_six_metrics_to_sqlite(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "测试", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    account = repo.get_account_by_name("测试期刊")
    repo.update_account_biz(account.id, "target-biz")
    account = repo.get_account_by_name("测试期刊")
    credential = SimpleNamespace(
        user_agent="synthetic-agent",
        cookie="appmsg_token=initial-token",
        backend_token="backend-token",
        fingerprint="fingerprint",
    )
    item = SimpleNamespace(
        link="https://mp.weixin.qq.com/s/short",
        mid="123",
        idx=1,
        title="目录标题",
        author="目录作者",
        digest="目录摘要",
        cover_url="https://mmbiz.qpic.cn/cover?wx_fmt=jpg",
        publish_time=None,
    )

    with WechatBrowserClient(
        credential,
        "target-biz",
        browser_session_factory=BrowserSessionStub,
    ) as client:
        article = persist_history_article(repo, account, client, item, tmp_path / "data")

    stored = repo.list_articles(account_name="测试期刊")[0]
    metrics = repo.get_latest_metrics(article.id)
    assert article.content_text == "完整正文"
    assert article.image_count == 1
    assert article.latest_metrics.read_count == 101
    assert article.latest_metrics.comment_count == 15
    assert stored.content_text == "完整正文"
    assert "id=\"js_content\"" in stored.content_html
    assert json.loads(stored.image_urls_json) == ["https://mmbiz.qpic.cn/test-image?wx_fmt=png"]
    assert stored.image_count == 1
    assert stored.crawl_status == "success"
    assert (
        metrics.read_count,
        metrics.like_count,
        metrics.watching_count,
        metrics.favorite_count,
        metrics.share_count,
        metrics.comment_count,
    ) == (101, 11, 12, 13, 14, 15)


class RecordingRepository:
    def __init__(self):
        self.calls = []
        self.article_values = None
        self.batch_values = None

    def upsert_publish_batch(self, *args, **kwargs):
        self.calls.append("batch")
        self.batch_values = args
        return SimpleNamespace(id=1)

    def upsert_article(self, **kwargs):
        self.calls.append("article")
        self.article_values = kwargs
        return SimpleNamespace(id=1, **kwargs)

    def insert_metrics_snapshot(self, **kwargs):
        self.calls.append("metrics")

    def complete_article_with_metrics(self, article_id, snapshot):
        self.calls.append("complete")


class PersistenceClientStub:
    def __init__(self, final_mid="123", snapshot=None):
        self.final_mid = final_mid
        self.snapshot = snapshot or SimpleNamespace(
            provider="wechat-authorized",
            provider_priority=100,
            read_count=1,
            like_count=2,
            watching_count=3,
            reward_count=None,
            share_count=4,
            favorite_count=5,
            subscriber_count=None,
            comment_count=6,
            collect_num=5,
            raw_payload={},
            is_authorized_source=True,
        )

    def fetch_article_page(self, _item):
        return (
            f"https://mp.weixin.qq.com/s?__biz=target-biz&mid={self.final_mid}&idx=1&sn=sn",
            "<div id='js_content'><p>正文</p></div>",
        )

    def fetch_metrics(self, _article):
        return self.snapshot


def _history_item(mid="123"):
    return SimpleNamespace(
        mid=mid,
        idx=1,
        title="目录标题",
        author="目录作者",
        digest="目录摘要",
        cover_url="",
        publish_time=None,
    )


def test_history_persistence_keeps_content_when_metrics_are_empty(tmp_path):
    repo = RecordingRepository()
    client = PersistenceClientStub(snapshot=_snapshot(None))

    article = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), client, _history_item(), tmp_path,
    )

    assert repo.calls == ["batch", "article"]
    assert article.latest_metrics is None
    assert article.content_text == repo.article_values["content_text"]
    assert repo.article_values["crawl_status"] == "success"
    assert Path(repo.article_values["raw_html_path"]).is_file()
    assert Path(repo.article_values["raw_text_path"]).is_file()


def test_history_persistence_rejects_article_identity_mismatch_before_any_write(tmp_path):
    repo = RecordingRepository()

    with pytest.raises(RuntimeError, match="article_identity_mismatch"):
        persist_history_article(
            repo,
            SimpleNamespace(id=1, biz="target-biz"),
            PersistenceClientStub(final_mid="999"),
            _history_item(mid="123"),
            tmp_path,
        )

    assert repo.calls == []
    assert list(tmp_path.rglob("*")) == []


def test_raw_article_files_reject_unsafe_path_identifiers(tmp_path):
    with pytest.raises(ValueError, match="unsafe article identifier"):
        persist_article_files(tmp_path, "target-biz", "../../outside", 1, "html", "text")

    assert list(tmp_path.rglob("*")) == []


METRIC_FIELDS = (
    "read_count", "like_count", "watching_count", "share_count",
    "favorite_count", "comment_count",
)


def _snapshot(value=0, **overrides):
    values = dict(vars(PersistenceClientStub().snapshot))
    values.update(dict.fromkeys(METRIC_FIELDS, value))
    values.update(overrides)
    return SimpleNamespace(**values)


class RetryRecordingRepository(RecordingRepository):
    def __init__(self):
        super().__init__()
        self.failed_tasks = []

    def record_failed_task(self, **kwargs):
        self.failed_tasks.append(kwargs)


def test_history_persistence_writes_content_before_requesting_metrics(tmp_path):
    repo = RecordingRepository()

    class CheckingClient(PersistenceClientStub):
        def fetch_metrics(self, article):
            assert repo.calls == ["batch", "article"]
            assert Path(article.raw_html_path).is_file()
            return super().fetch_metrics(article)

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), CheckingClient(), _history_item(), tmp_path,
    )

    assert repo.calls == ["batch", "article", "complete"]
    assert result.latest_metrics.read_count == 1


def test_history_persistence_content_only_never_requests_metrics(tmp_path):
    repo = RetryRecordingRepository()

    class ContentOnlyClient(PersistenceClientStub):
        def fetch_metrics(self, _article):
            pytest.fail("content-only mode must not request metrics")

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), ContentOnlyClient(),
        _history_item(), tmp_path, with_metrics=False,
    )

    assert repo.calls == ["batch", "article"]
    assert repo.article_values["crawl_status"] == "success"
    assert result.latest_metrics is None
    assert repo.failed_tasks == []


@pytest.mark.parametrize("snapshot", [None, _snapshot(None)])
def test_history_persistence_queues_missing_metrics_separately(tmp_path, snapshot):
    repo = RetryRecordingRepository()
    client = PersistenceClientStub()
    client.snapshot = snapshot

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz", canonical_name="journal"),
        client, _history_item(), tmp_path,
    )

    assert result.latest_metrics is None
    assert repo.calls == ["batch", "article"]
    assert repo.failed_tasks == [{
        "task_name": "metrics:target-biz", "account_name": "journal",
        "url": "https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=sn",
        "error_message": "missing_metrics",
    }]


def test_history_persistence_accepts_six_zero_metrics(tmp_path):
    repo = RetryRecordingRepository()
    snapshot = _snapshot(0)

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"),
        PersistenceClientStub(snapshot=snapshot), _history_item(), tmp_path,
    )

    assert result.latest_metrics is snapshot
    assert repo.calls == ["batch", "article", "complete"]
    assert repo.failed_tasks == []


@pytest.mark.parametrize("missing_field", METRIC_FIELDS)
def test_history_persistence_saves_partial_snapshot_and_queues_refill(tmp_path, missing_field):
    repo = RetryRecordingRepository()
    snapshot = _snapshot(0, **{missing_field: None})

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"),
        PersistenceClientStub(snapshot=snapshot), _history_item(), tmp_path,
    )

    assert result.latest_metrics is snapshot
    assert repo.calls == ["batch", "article", "complete"]
    assert repo.failed_tasks[0]["task_name"] == "metrics:target-biz"
    assert repo.failed_tasks[0]["error_message"] == "missing_metrics"


@pytest.mark.parametrize("error, expected", [
    (RuntimeError("Cookie=private-cookie https://mp.weixin.qq.com/?token=private-token"), "RuntimeError"),
    (SessionUnavailable("http_403"), "http_403"),
    (SessionUnavailable("expired"), "expired"),
    (SessionUnavailable("appmsg_token=private-token"), "SessionUnavailable"),
])
def test_history_persistence_keeps_body_and_redacts_metric_failures(tmp_path, error, expected):
    repo = RetryRecordingRepository()

    class BrokenClient(PersistenceClientStub):
        def fetch_metrics(self, _article):
            raise error

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), BrokenClient(), _history_item(), tmp_path,
    )

    assert result.latest_metrics is None
    assert repo.calls == ["batch", "article"]
    assert repo.failed_tasks[0]["error_message"] == expected
    assert "private-" not in repr(repo.failed_tasks)
    assert repo.article_values["crawl_status"] == "success"


def test_history_persistence_records_http_status_without_request_secrets(tmp_path):
    repo = RetryRecordingRepository()
    response = requests.Response()
    response.status_code = 401

    class BrokenClient(PersistenceClientStub):
        def fetch_metrics(self, _article):
            raise requests.HTTPError("token=private-token", response=response)

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), BrokenClient(), _history_item(), tmp_path,
    )

    assert result.latest_metrics is None
    assert repo.failed_tasks[0]["error_message"] == "http_401"


def test_history_persistence_keeps_body_if_snapshot_write_fails(tmp_path):
    class BrokenMetricsRepository(RetryRecordingRepository):
        def complete_article_with_metrics(self, article_id, snapshot):
            raise RuntimeError("Cookie=private-cookie")

    repo = BrokenMetricsRepository()
    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"),
        PersistenceClientStub(), _history_item(), tmp_path,
    )

    assert repo.article_values["crawl_status"] == "success"
    assert result.latest_metrics is None
    assert repo.failed_tasks[0]["error_message"] == "RuntimeError"


def _patch_parsed(monkeypatch, **changes):
    parsed, parts = persistence.parse_fetched_article(
        "<div id='js_content'>content</div>",
        "https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=sn",
    )
    parsed = SimpleNamespace(**vars(parsed))
    for name, value in changes.items():
        setattr(parsed, name, value)
    monkeypatch.setattr(persistence, "parse_fetched_article", lambda *_args: (parsed, parts))
    return parsed


@pytest.mark.parametrize("directory_time", [None, datetime(2020, 1, 1, tzinfo=timezone.utc), "absent"])
def test_history_persistence_uses_parsed_publish_time_when_directory_has_none(tmp_path, monkeypatch, directory_time):
    parsed_time = datetime(2019, 1, 1, tzinfo=timezone.utc)
    _patch_parsed(monkeypatch, publish_time=parsed_time)
    item = _history_item()
    if directory_time == "absent":
        del item.publish_time
    else:
        item.publish_time = directory_time
    repo = RecordingRepository()

    persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), PersistenceClientStub(), item, tmp_path,
    )

    expected = parsed_time if directory_time in (None, "absent") else directory_time
    assert repo.article_values["publish_time"] == expected
    assert repo.batch_values[3] == expected


@pytest.mark.parametrize("source", ["html", "catalog", "absent"])
def test_history_persistence_keeps_real_author_and_digest_without_fabrication(tmp_path, monkeypatch, source):
    parsed = _patch_parsed(
        monkeypatch, author="html-author" if source == "html" else "",
        digest="html-digest" if source == "html" else "", account_name="not-an-author",
    )
    item = _history_item()
    item.author = "catalog-author" if source != "absent" else ""
    item.digest = "catalog-digest" if source != "absent" else ""
    repo = RecordingRepository()

    persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), PersistenceClientStub(), item, tmp_path,
    )

    assert repo.article_values["author"] == (parsed.author or item.author)
    assert repo.article_values["digest"] == (parsed.digest or item.digest)
    assert repo.article_values["author"] != parsed.account_name


@pytest.mark.parametrize("assets, provenance", [
    ([{"kind": "video", "id": "video-id"}], {"author": "html:meta-author"}),
    ([], {}),
])
def test_history_persistence_forwards_media_and_provenance(tmp_path, monkeypatch, assets, provenance):
    _patch_parsed(monkeypatch, media_assets=assets, field_provenance=provenance)
    repo = RecordingRepository()

    persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), PersistenceClientStub(), _history_item(), tmp_path,
    )

    assert repo.article_values["media_assets"] == assets
    assert repo.article_values["field_provenance"] == provenance


def test_history_persistence_accepts_old_parsed_stubs_without_new_fields(tmp_path, monkeypatch):
    parsed = _patch_parsed(monkeypatch)
    for field in ("media_assets", "field_provenance"):
        if hasattr(parsed, field):
            delattr(parsed, field)
    repo = RecordingRepository()

    result = persist_history_article(
        repo, SimpleNamespace(id=1, biz="target-biz"), PersistenceClientStub(), _history_item(), tmp_path,
    )

    assert result.id == 1
    assert repo.article_values.get("media_assets") is None
    assert repo.article_values.get("field_provenance") is None


@pytest.mark.parametrize("biz, mid, idx, detail", [
    ("other-biz", "123", 1, "article_biz_mismatch"),
    ("target-biz", "123", 2, "article_identity_mismatch"),
    ("", "123", 1, "unparseable_article"),
])
def test_history_persistence_identity_checks_also_apply_to_content_only(tmp_path, monkeypatch, biz, mid, idx, detail):
    parsed = _patch_parsed(monkeypatch)
    parts = SimpleNamespace(biz=biz, mid=mid, idx=idx)
    monkeypatch.setattr(persistence, "parse_fetched_article", lambda *_args: (parsed, parts))
    repo = RecordingRepository()

    with pytest.raises(RuntimeError, match=detail):
        persist_history_article(
            repo, SimpleNamespace(id=1, biz="target-biz"),
            PersistenceClientStub(), _history_item(), tmp_path, with_metrics=False,
        )

    assert repo.calls == []
    assert list(tmp_path.rglob("*")) == []
