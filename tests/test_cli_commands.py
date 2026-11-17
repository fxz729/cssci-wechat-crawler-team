import os
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from main import (
    _crawl_journal_with_credentials,
    build_parser,
    cmd_audit_articles,
    cmd_backfill_metrics,
    cmd_crawl_history,
    cmd_crawl_status,
    cmd_retry_failed,
    cmd_sync_target_accounts,
    cmd_validate_sessions,
)
from metrics.headers_parser import JournalCredentials
from crawl.history_runner import SessionUnavailable
from crawl.session_preflight import SessionPreflightStatus, SessionSelection
from metrics.profile_ext import ProfileArticleItem
from metrics.providers.base import MetricsSnapshot
from metrics.providers.wechat_backend import BackendArticleItem
from storage.database import create_session_factory, init_db
from storage.models import FailedCrawlTask
from storage.repository import Repository
from utils.progress_logger import ProgressLogger


def test_sync_targets_defaults_to_the_fixed_126_roster_contract():
    parser = build_parser()
    args = parser.parse_args(["sync-target-accounts"])
    assert args.expected_count == 126


def test_client_bundle_audit_command_parses_bundle_path():
    parser = build_parser()
    args = parser.parse_args(["audit-client-bundle", "--base", "output/run"])
    assert args.base == "output/run"
    assert callable(args.func)


def test_client_history_and_health_commands_parse_without_private_credentials():
    parser = build_parser()
    health = parser.parse_args(["check-wechat-capture", "--hwnd", "331714"])
    client = parser.parse_args([
        "crawl-client-history", "--base", "output/run", "--pilot-dir", "output/pilot",
        "--biz", "target-biz", "--account", "测试期刊", "--hwnd", "331714",
        "--indices", "1,2", "--force", "--stop-control", "output/run/run_control.json",
    ])
    assert health.hwnd == 331714
    assert callable(health.func)
    assert client.indices == "1,2"
    assert client.force is True
    assert client.stop_control == "output/run/run_control.json"
    assert callable(client.func)

    client_with_logs = parser.parse_args([
        "crawl-client-history", "--biz", "target-biz", "--account", "测试期刊",
        "--hwnd", "331714", "--log-dir", "output/logs", "--event-log", "output/events.jsonl",
    ])
    assert client_with_logs.log_dir == "output/logs"
    assert client_with_logs.event_log == "output/events.jsonl"


def test_new_history_commands_parse_private_session_file_arguments():
    parser = build_parser()

    validate = parser.parse_args([
        "validate-sessions",
        "--sessions-file", "config/private.txt",
        "--fakeid", "target-biz",
    ])
    crawl = parser.parse_args([
        "crawl-history",
        "--sessions-file", "config/private.txt",
        "--journal-links", "links.md",
        "--max-accounts", "1",
        "--resume",
    ])

    assert validate.sessions_file == "config/private.txt"
    assert callable(validate.func)
    assert crawl.sessions_file == "config/private.txt"
    assert crawl.journal_links == "links.md"
    assert crawl.max_accounts == 1
    assert crawl.resume is True
    assert callable(crawl.func)


def test_crawl_history_auto_falls_back_to_browser_with_same_checkpoint(tmp_path, monkeypatch):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    links = tmp_path / "links.md"
    links.write_text(
        """# CSSCI 来源刊公众号主页链接

| 序号 | 学科 | 类别 | 期刊原名 | 搜索到的公众号作者 | 公众号主页链接 | 样本文章标题 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 测试 | cssci | 测试期刊 | 测试期刊 | https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=target-biz&scene=124#wechat_redirect | 样本 |
""",
        encoding="utf-8",
    )
    credential = SimpleNamespace(session_id="session-1", fakeid="target-biz")
    items = [
        BackendArticleItem(
            title=f"标题 {mid}",
            author="作者",
            digest="摘要",
            cover_url="",
            link=f"https://mp.weixin.qq.com/s/{mid}",
            mid=mid,
            idx=1,
            publish_time=None,
        )
        for mid in ("123", "456")
    ]
    selections = iter([
        SessionSelection(credential, "requests"),
        SessionSelection(credential, "browser"),
    ])
    transports = []

    class ClientStub:
        def __init__(self, _credential, _fakeid, **kwargs):
            self.transport = "browser" if "headless" in kwargs else "requests"
            transports.append(self.transport)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            return None

        def fetch_publish_page(self, begin, count):
            assert (begin, count) == (0, 5)
            return items, 2

    processed = []

    def process(_repo, _account, client, item):
        processed.append((client.transport, item.mid))
        if client.transport == "requests" and item.mid == "456":
            raise SessionUnavailable("expired")
        return SimpleNamespace(
            content_text="正文详情",
            image_count=2,
            latest_metrics=SimpleNamespace(
                read_count=1,
                like_count=2,
                watching_count=3,
                favorite_count=4,
                share_count=5,
                comment_count=0,
            ),
        )

    monkeypatch.setattr("main.load_session_credentials", lambda _path: (credential,))
    monkeypatch.setattr("main.select_usable_session", lambda *_args, **_kwargs: next(selections))
    monkeypatch.setattr("main.WechatRequestsClient", ClientStub)
    monkeypatch.setattr("main.WechatBrowserClient", ClientStub)
    monkeypatch.setattr("main._process_history_item", process)
    monkeypatch.setattr("main.time.sleep", lambda _seconds: None)

    stream = StringIO()
    exit_code = cmd_crawl_history(Namespace(
        sessions_file=str(tmp_path / "sessions.txt"),
        journal_links=str(links),
        max_accounts=1,
        account_names=[],
        resume=False,
        transport="auto",
        headed=False,
    ), repo, progress_logger=ProgressLogger(stream=stream))

    checkpoint = repo.get_crawl_checkpoint("crawl-history:测试期刊")
    output = stream.getvalue()
    assert exit_code == 0
    assert transports == ["requests", "browser"]
    assert processed == [
        ("requests", "123"),
        ("requests", "456"),
        ("browser", "456"),
    ]
    assert checkpoint.status == "completed"
    assert checkpoint.processed_urls == ["123:1", "456:1"]
    assert "[账号 1/1]" in output
    assert output.count("会话预检") >= 2
    assert "transport=requests" in output
    assert "transport=browser" in output
    assert output.count("requests -> browser") == 1
    assert output.count("账号汇总") == 1
    assert output.count("历史抓取汇总") == 1
    assert "正文字符=4" in output
    assert "图片=2" in output
    assert "指标=6/6" in output
    assert "target-biz" not in output
    assert "https://" not in output


def test_validate_sessions_only_prints_session_identifier_and_status(tmp_path, monkeypatch, capsys):
    session_file = tmp_path / "sessions.txt"
    session_file.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("main.load_session_credentials", lambda path: [type("Credential", (), {"session_id": "desktop-01"})()])
    monkeypatch.setattr(
        "main.validate_session_credential",
        lambda credential, fakeid: type("Result", (), {
            "session_id": credential.session_id,
            "status": SessionPreflightStatus.USABLE,
            "detail": "ok",
        })(),
    )

    exit_code = cmd_validate_sessions(Namespace(
        sessions_file=str(session_file),
        fakeid="target-biz",
        transport="requests",
        headed=False,
    ), None)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "desktop-01" in output
    assert "usable" in output
    assert "placeholder" not in output


    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["CSSCI_SKIP_LOCAL_ENV"] = "1"
    result = subprocess.run(
        [sys.executable, str(project_root / "main.py"), "check-auth", "--provider", "wechat-authorized"],
        cwd=project_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 1
    assert "WECHAT_COOKIE" in result.stdout
    assert "<not-configured>" in result.stdout


def test_crawl_status_and_retry_failed_report_counts_without_revealing_failure_reference(tmp_path, monkeypatch, capsys):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    repo.record_failed_task("crawl-history:test", "测试期刊", "article:private-reference", "RuntimeError")

    forwarded = {}
    monkeypatch.setattr("main.cmd_crawl_history", lambda args, _repo: forwarded.update(vars(args)) or 0)
    status_code = cmd_crawl_status(Namespace(), repo)
    retry_code = cmd_retry_failed(Namespace(
        sessions_file="private-sessions.txt",
        journal_links="links.md",
        max_accounts=1,
        max_attempts=3,
    ), repo)

    output = capsys.readouterr().out
    assert status_code == 0
    assert retry_code == 0
    assert "可恢复失败 1" in output
    assert "private-reference" not in output
    assert forwarded == {
        "sessions_file": "private-sessions.txt",
        "journal_links": "links.md",
        "max_accounts": 1,
        "account_names": ["测试期刊"],
        "resume": True,
        "transport": "auto",
        "headed": False,
    }


def test_audit_articles_reports_biz_mismatches_without_flagging_paper_authors(tmp_path, capsys):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    repo.update_account_biz(account.id, "official-biz")
    batch = repo.upsert_publish_batch(account.id, "other-biz", "mid", datetime(2025, 4, 11, tzinfo=timezone.utc), "source")
    repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="other-biz",
        mid="mid",
        idx=1,
        sn="sn",
        title="不相关标题",
        author="月亮与稿便士",
        publish_time=datetime(2025, 4, 11, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )
    official_batch = repo.upsert_publish_batch(account.id, "official-biz", "official-mid", datetime(2025, 4, 12, tzinfo=timezone.utc), "source")
    repo.upsert_article(
        account_id=account.id,
        publish_batch_id=official_batch.id,
        biz="official-biz",
        mid="official-mid",
        idx=1,
        sn="sn",
        title="官方文章",
        author="刘振天 周子怡",
        publish_time=datetime(2025, 4, 12, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )

    exit_code = cmd_audit_articles(Namespace(account=None, limit=10), repo)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "疑似不匹配文章：1" in output
    assert "北京大学教育评论" in output
    assert "月亮与稿便士" in output
    assert "官方文章" not in output


def test_backfill_metrics_writes_authorized_snapshot_for_matching_article(tmp_path, monkeypatch):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    published_at = datetime(2025, 4, 11, tzinfo=timezone.utc)
    batch = repo.upsert_publish_batch(account.id, "biz", "2247488874", published_at, "source")
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="2247488874",
        idx=1,
        sn="sn",
        title="标题",
        author="北京大学教育评论",
        publish_time=published_at,
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )

    class FakeProvider:
        is_configured = True
        status_message = "ok"
        provider = "wechat-authorized"
        provider_priority = 100

        @classmethod
        def from_env(cls):
            return cls()

        def fetch(self, item):
            assert item.id == article.id
            return MetricsSnapshot(
                provider="wechat-authorized",
                provider_priority=100,
                read_count=3269,
                like_count=32,
                watching_count=6,
                favorite_count=12,
                share_count=355,
                comment_count=0,
                is_authorized_source=True,
            )

    monkeypatch.setattr("main.WechatAuthorizedProvider", FakeProvider)

    exit_code = cmd_backfill_metrics(Namespace(
        provider="wechat-authorized",
        account="北京大学教育评论",
        since="2016-01-01",
        until="2026-12-31",
        sample_size=1,
        request_interval=0,
        export_dir=str(tmp_path / "output"),
        no_export=True,
    ), repo)

    metric = repo.get_latest_metrics(article.id)
    assert exit_code == 0
    assert metric.read_count == 3269
    assert metric.like_count == 32
    assert metric.watching_count == 6
    assert metric.favorite_count == 12
    assert metric.share_count == 355
    assert metric.comment_count == 0


def test_backfill_metrics_skips_empty_authorized_snapshot(tmp_path, monkeypatch, capsys):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "北京大学教育评论", "北京大学教育评论", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "北京大学教育评论", "北京大学教育评论", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("北京大学教育评论")
    published_at = datetime(2025, 4, 11, tzinfo=timezone.utc)
    batch = repo.upsert_publish_batch(account.id, "biz", "2247488874", published_at, "source")
    article = repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="2247488874",
        idx=1,
        sn="sn",
        title="标题",
        author="北京大学教育评论",
        publish_time=published_at,
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="canonical",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )

    class FakeProvider:
        is_configured = True
        status_message = "ok"

        @classmethod
        def from_env(cls):
            return cls()

        def fetch(self, item):
            assert item.id == article.id
            return MetricsSnapshot(
                provider="wechat-authorized",
                provider_priority=100,
                raw_payload={"appmsgext": {"advertisement_info": []}},
                is_authorized_source=True,
            )

    monkeypatch.setattr("main.WechatAuthorizedProvider", FakeProvider)

    exit_code = cmd_backfill_metrics(Namespace(
        provider="wechat-authorized",
        account="北京大学教育评论",
        since="2016-01-01",
        until="2026-12-31",
        sample_size=1,
        request_interval=0,
        export_dir=str(tmp_path / "output"),
        no_export=True,
    ), repo)

    assert exit_code == 1
    assert repo.get_latest_metrics(article.id) is None
    assert "跳过空指标" in capsys.readouterr().out


def test_backfill_metrics_samples_across_time_and_continues_after_failure(tmp_path, monkeypatch, capsys):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "政治学", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("测试期刊")
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index in range(7):
        mid = str(index + 1)
        published_at = start + timedelta(days=index * 30)
        batch = repo.upsert_publish_batch(account.id, "biz", mid, published_at, f"source-{mid}")
        repo.upsert_article(
            account_id=account.id,
            publish_batch_id=batch.id,
            biz="biz",
            mid=mid,
            idx=1,
            sn=f"sn-{mid}",
            title=f"标题 {mid}",
            author="测试期刊",
            publish_time=published_at,
            digest="摘要",
            content_text="正文",
            image_urls=[],
            source_url=f"source-{mid}",
            canonical_url=f"https://mp.weixin.qq.com/s?__biz=biz&mid={mid}&idx=1&sn=sn-{mid}",
            raw_html_path="raw.html",
            raw_text_path="raw.txt",
        )

    calls = []

    class FakeProvider:
        is_configured = True
        status_message = "ok"

        @classmethod
        def from_env(cls):
            return cls()

        def fetch(self, article):
            calls.append(article.mid)
            if article.mid == "4":
                raise ValueError("模拟单篇解析失败")
            return MetricsSnapshot(
                provider="wechat-authorized",
                provider_priority=100,
                read_count=int(article.mid) * 100,
                like_count=int(article.mid) * 10,
                watching_count=int(article.mid),
                favorite_count=8,
                share_count=5,
                comment_count=0,
                collect_num=8,
                raw_payload={"appmsgext": {"_album_id": "album-1", "_album_name": "合集", "_collect_num": 8}},
                is_authorized_source=True,
            )

    sleeps = []
    monkeypatch.setattr("main.WechatAuthorizedProvider", FakeProvider)
    monkeypatch.setattr("main.time.sleep", sleeps.append)

    exit_code = cmd_backfill_metrics(Namespace(
        provider="wechat-authorized",
        account="测试期刊",
        since="2025-01-01",
        until="2025-12-31",
        sample_size=5,
        full=False,
        request_interval=0.25,
        export_dir=str(tmp_path / "output"),
        no_export=True,
    ), repo)

    assert exit_code == 1
    assert calls == ["1", "3", "4", "5", "7"]
    assert sleeps == [0.25, 0.25, 0.25, 0.25]
    with session_factory() as session:
        failed = session.query(FailedCrawlTask).filter_by(
            task_name="backfill-metrics:wechat-authorized",
            url="https://mp.weixin.qq.com/s?__biz=biz&mid=4&idx=1&sn=sn-4",
        ).one()
        assert failed.status == "failed"
    rows = repo.list_articles_for_export(account_name="测试期刊")
    successful = next(row for row in rows if row["mid"] == "3")
    assert successful["collect_num"] == 8
    assert successful["favorite_count"] == 8
    assert successful["album_id"] == "album-1"
    output = capsys.readouterr().out
    assert "成功 4" in output
    assert "失败 1" in output
    assert "覆盖率 80.0%" in output
    assert "耗时" in output


def test_backfill_metrics_parser_accepts_sampling_interval_and_export_options():
    args = build_parser().parse_args([
        "backfill-metrics",
        "--provider", "wechat-authorized",
        "--account", "测试期刊",
        "--sample-size", "5",
        "--full",
        "--request-interval", "2.5",
        "--export-dir", "output",
        "--no-export",
    ])

    assert args.sample_size == 5
    assert args.full is True
    assert args.request_interval == 2.5
    assert args.export_dir == "output"
    assert args.no_export is True


def test_backfill_metrics_requires_successful_sample_before_full_run(tmp_path, monkeypatch, capsys):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "政治学", "cssci", "测试期刊", "测试期刊", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "测试期刊", "测试期刊", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("测试期刊")
    batch = repo.upsert_publish_batch(account.id, "biz", "1", datetime(2025, 1, 1, tzinfo=timezone.utc), "source")
    repo.upsert_article(
        account_id=account.id,
        publish_batch_id=batch.id,
        biz="biz",
        mid="1",
        idx=1,
        sn="sn",
        title="标题",
        author="测试期刊",
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        digest="摘要",
        content_text="正文",
        image_urls=[],
        source_url="source",
        canonical_url="https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=sn",
        raw_html_path="raw.html",
        raw_text_path="raw.txt",
    )

    class Provider:
        is_configured = True
        status_message = "ok"

        @classmethod
        def from_env(cls):
            return cls()

        def fetch(self, article):
            raise AssertionError("full run must be blocked before any request")

    monkeypatch.setattr("main.WechatAuthorizedProvider", Provider)

    exit_code = cmd_backfill_metrics(Namespace(
        provider="wechat-authorized",
        account="测试期刊",
        since="2025-01-01",
        until="2025-12-31",
        sample_size=5,
        full=True,
        request_interval=0,
        export_dir=str(tmp_path / "output"),
        no_export=True,
    ), repo)

    assert exit_code == 1
    assert "请先完成并通过跨时间样本验证" in capsys.readouterr().out


def test_profile_ext_crawl_persists_parsed_digest_collect_album_and_comment_metrics(tmp_path, monkeypatch):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    seed = repo.upsert_journal_seed("2025-2026", "政治学", "cssci", "当代世界社会主义问题", "当代世界社会主义问题", [], "catalog.md", 1)
    repo.upsert_account_candidate(seed.id, "当代世界社会主义问题", "当代世界社会主义问题", score=0.99)
    repo.auto_accept_high_score_candidates()
    account = repo.get_account_by_name("当代世界社会主义问题")
    published_at = datetime(2026, 6, 18, 15, 1, tzinfo=timezone.utc)
    item = ProfileArticleItem(
        biz="biz",
        mid="2247487024",
        idx=1,
        sn="sn",
        title="标题",
        digest="",
        cover_url="cover",
        publish_time=published_at,
        copyright_stat=100,
        audio_fileid=0,
        duration=0,
        is_multi=False,
        content_url="https://mp.weixin.qq.com/s?__biz=biz&mid=2247487024&idx=1&sn=sn&key=full-key&exportkey=full-export",
    )

    class ResponseStub:
        url = "https://mp.weixin.qq.com/s?__biz=biz&mid=2247487024&idx=1&sn=sn"
        text = """
        <html><head><meta name="description" content="解析摘要" /></head><body>
        <a id="js_name">当代世界社会主义问题</a>
        <div id="js_content"><p>正文内容</p></div>
        <script>var req_id = 'req123'; var comment_id = 'comment123';</script>
        </body></html>
        """

        def raise_for_status(self):
            return None

    class SessionStub:
        def get(self, url, *args, **kwargs):
            assert "key=full-key" in url
            assert "exportkey=full-export" in url
            return ResponseStub()

    class ProviderStub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fetch(self, article):
            assert article.comment_id == "comment123"
            return MetricsSnapshot(
                provider="wechat-authorized",
                provider_priority=100,
                read_count=3146,
                like_count=23,
                watching_count=35,
                share_count=132,
                comment_count=1,
                collect_num=8,
                raw_payload={"appmsgext": {"_album_id": "album-123", "_album_name": "专题合集", "_collect_num": 8}},
                is_authorized_source=True,
            )

    monkeypatch.setattr("main.fetch_article_page", lambda session, creds, offset: [item] if offset == 0 else [])
    monkeypatch.setattr("main.requests.Session", SessionStub)
    monkeypatch.setattr("main.WechatAuthorizedProvider", ProviderStub)
    monkeypatch.chdir(tmp_path)

    creds = JournalCredentials(
        biz="biz",
        uin="uin",
        key="key",
        pass_ticket="ticket",
        appmsg_token="token",
        cookie="cookie",
        user_agent="agent",
        sample_article_mid="",
        sample_article_idx=0,
        sample_article_sn="",
        has_tts_sample=False,
    )

    stored = _crawl_journal_with_credentials(creds, account, pages=1, repo=repo, sleep_page=0, sleep_article=0)

    rows = repo.list_articles_for_export(account_name="当代世界社会主义问题")
    assert stored == 1
    assert rows[0]["digest"] == "解析摘要"
    assert rows[0]["collect_num"] == 8
    assert rows[0]["album_id"] == "album-123"
    assert rows[0]["album_name"] == "专题合集"
    assert rows[0]["comment_count"] == 1


def test_import_from_headers_parser_builds():
    parser = build_parser()
    args = parser.parse_args([
        "import-from-headers",
        "--headers-file", "data/sources/所有期刊_Headers.txt",
        "--journal-links", "data/sources/CSSCI来源刊公众号主页链接.md",
        "--pages", "5",
        "--sleep-page", "2.5",
        "--sleep-article", "1.5",
        "--max-journals", "3",
    ])
    assert args.command == "import-from-headers"
    assert args.headers_file == "data/sources/所有期刊_Headers.txt"
    assert args.journal_links == "data/sources/CSSCI来源刊公众号主页链接.md"
    assert args.pages == 5
    assert args.sleep_page == 2.5
    assert args.sleep_article == 1.5
    assert args.max_journals == 3
    assert callable(args.func)


def test_sync_target_accounts_uses_profile_link_list_as_the_active_scope(tmp_path):
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'crawler.db'}")
    init_db(session_factory)
    repo = Repository(session_factory)
    stale_seed = repo.upsert_journal_seed("2025-2026", "教育学", "cssci", "旧目标", "旧目标", [], "catalog.md", 1)
    repo.upsert_account_candidate(stale_seed.id, "旧目标", "旧目标", score=1.0)
    repo.auto_accept_high_score_candidates(threshold=1.0)
    links = tmp_path / "links.md"
    links.write_text(
        """# CSSCI 来源刊公众号主页链接

| 序号 | 学科 | 类别 | 期刊原名 | 搜索到的公众号作者 | 公众号主页链接 | 样本文章标题 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 法学 | cssci | 北方法学 | 北方法学 | https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=target-biz&scene=124#wechat_redirect | 样本 |
""",
        encoding="utf-8",
    )

    exit_code = cmd_sync_target_accounts(Namespace(journal_links=str(links)), repo)

    target = repo.get_account_by_name("北方法学")
    assert exit_code == 0
    assert target.status == "active"
    assert target.biz == "target-biz"
    assert target.profile_url.endswith("__biz=target-biz&scene=124#wechat_redirect")
    assert repo.get_account_by_name("旧目标").status == "inactive"
    assert [account.canonical_name for account in repo.list_accounts()] == ["北方法学"]


def test_client_roster_command_parses_rollout_arguments():
    args = build_parser().parse_args([
        "crawl-client-roster", "--hwnd", "331714", "--run-dir", "output/rollout",
        "--start", "2", "--max-journals", "3", "--max-steps", "40",
    ])
    assert args.hwnd == 331714
    assert args.start == 2
    assert args.max_journals == 3
    assert args.max_steps == 40
    assert callable(args.func)


def test_client_roster_can_discover_window_after_reboot():
    args = build_parser().parse_args(["crawl-client-roster"])
    assert args.hwnd == 0
    assert callable(args.func)


def test_export_client_csv_cli_uses_bundle_without_database(monkeypatch, tmp_path):
    import main
    import export.journal_csv
    calls = []
    monkeypatch.setattr(export.journal_csv, "write_journal_csv_bundle", lambda path: calls.append(Path(path)) or {"article_count":1})
    monkeypatch.setattr(main, "create_session_factory", lambda *a: (_ for _ in ()).throw(AssertionError("no database init")))
    assert main.main(["export-client-csv", "--bundle", str(tmp_path)]) == 0
    assert calls == [tmp_path]


def test_refill_client_journal_cli_parses_offline_mode():
    args = build_parser().parse_args([
        "refill-client-journal",
        "--base", "output/journal",
        "--pilot-dir", "output/pilot",
        "--biz", "biz-a",
        "--account", "A",
        "--offline-only",
        "--media-workers", "3",
    ])
    assert args.offline_only is True
    assert args.media_workers == 3
    assert callable(args.func)


def test_refill_client_journal_cli_forwards_without_database_init(monkeypatch, tmp_path):
    import main
    import metrics.refill_queue

    observed = []
    monkeypatch.setattr(metrics.refill_queue, "main", lambda argv: observed.append(argv) or 2)
    monkeypatch.setattr(main, "create_session_factory", lambda *a: (_ for _ in ()).throw(AssertionError("no database init")))
    result = main.main([
        "refill-client-journal",
        "--base", str(tmp_path / "journal"),
        "--pilot-dir", str(tmp_path / "pilot"),
        "--biz", "biz-a",
        "--account", "A",
        "--offline-only",
    ])
    assert result == 2
    assert "--offline-only" in observed[0]
    assert "--force" not in observed[0]


def test_client_cli_accepts_capture_only_and_roster_phase():
    parser = build_parser()
    article_args = parser.parse_args([
        "crawl-client-history", "--biz", "biz-test", "--account", "测试刊", "--hwnd", "7",
        "--capture-only",
    ])
    roster_args = parser.parse_args(["crawl-client-roster", "--phase", "capture-first"])
    assert article_args.capture_only is True
    assert roster_args.phase == "capture-first"


def test_strict_client_audit_cli_infers_unique_biz_and_uses_rollout_audit(tmp_path, monkeypatch, capsys):
    import json
    from main import build_parser, cmd_audit_client_bundle
    import export.rollout_audit

    base = tmp_path / "journal"
    folder = base / "bundle" / "articles" / "123_1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({"biz": "biz-one"}), encoding="utf-8")
    observed = []
    monkeypatch.setattr(export.rollout_audit, "audit_journal", lambda *a, **kw: observed.append((a, kw)) or {
        "ok": True, "checks": {"catalog": True}, "counts": {"record_count": 1},
        "errors": [], "audit_path": str(base / "audit_report.json"),
    })
    args = build_parser().parse_args(["audit-client-bundle", "--base", str(base), "--baseline-base", str(tmp_path / "baseline")])

    assert cmd_audit_client_bundle(args) == 0
    assert observed[0][1]["expected_biz"] == "biz-one"
    assert observed[0][1]["baseline_base"] == (tmp_path / "baseline").resolve()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["counts"]["record_count"] == 1


def test_strict_client_audit_cli_rejects_missing_or_multiple_biz(tmp_path, monkeypatch, capsys):
    import json
    from main import build_parser, cmd_audit_client_bundle
    import export.rollout_audit

    called = []
    monkeypatch.setattr(export.rollout_audit, "audit_journal", lambda *a, **kw: called.append(1))
    missing = tmp_path / "missing"
    args = build_parser().parse_args(["audit-client-bundle", "--base", str(missing)])
    assert cmd_audit_client_bundle(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "client_bundle_biz_not_found"

    base = tmp_path / "multiple"
    for name, biz in (("1_1", "biz-a"), ("2_1", "biz-b")):
        folder = base / "bundle" / "articles" / name
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps({"biz": biz}), encoding="utf-8")
    args = build_parser().parse_args(["audit-client-bundle", "--base", str(base)])
    assert cmd_audit_client_bundle(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "client_bundle_biz_ambiguous"
    assert called == []


def test_strict_client_audit_cli_explicit_biz_and_failure_return_code(tmp_path, monkeypatch, capsys):
    import json
    from main import build_parser, cmd_audit_client_bundle
    import export.rollout_audit

    monkeypatch.setattr(export.rollout_audit, "audit_journal", lambda *a, **kw: {
        "ok": False, "checks": {"evidence": False}, "counts": {},
        "errors": [{"code": "bad"}], "audit_path": None,
    })
    args = build_parser().parse_args(["audit-client-bundle", "--base", str(tmp_path), "--biz", "biz-explicit"])
    assert cmd_audit_client_bundle(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"code": "bad"}]
