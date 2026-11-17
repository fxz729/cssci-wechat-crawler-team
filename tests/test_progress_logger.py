import json
from io import StringIO

from utils.progress_logger import ProgressLogger, setup_logging


def test_progress_logger_writes_realtime_status_lines():
    stream = StringIO()
    logger = ProgressLogger(stream=stream)

    logger.account_started("北京大学教育评论", total_results=10)
    logger.article_saved("北京大学教育评论", title="文章标题", stored=1, total=10)
    logger.article_failed("北京大学教育评论", url="https://weixin.sogou.com/link", error="timeout", retry_count=1)

    output = stream.getvalue()
    assert "开始抓取" in output
    assert "北京大学教育评论" in output
    assert "[1/10]" in output
    assert "文章标题" in output
    assert "失败" in output
    assert "retry=1" in output


def test_history_progress_events_write_immediately_to_explicit_stream():
    stream = StringIO()
    logger = ProgressLogger(stream=stream)

    logger.history_account_started("测试期刊", current=1, total=2)
    logger.session_preflight_started("测试期刊", candidate=1, total=2, transport="auto")
    logger.transport_selected("测试期刊", transport="requests")
    logger.catalog_page_fetched("测试期刊", begin=10, count=2, total=20, page_size=5)
    logger.history_article_started("测试期刊", current=1, total=20, title="标题\n伪造日志")
    logger.history_article_saved(
        "测试期刊",
        current=1,
        total=20,
        title="标题",
        content_chars=1234,
        image_count=3,
        metrics_count=6,
    )
    logger.checkpoint_saved("测试期刊", page=0, processed=1, status="running")
    logger.transport_fallback("测试期刊", source="requests", target="browser")
    logger.history_account_finished("测试期刊", processed=1, failed=0, completed=True)
    logger.history_finished(completed=1, total=2)

    output = stream.getvalue()
    assert "[账号 1/2]" in output
    assert "会话预检" in output
    assert "transport=requests" in output
    assert "目录页" in output
    assert "page=3" in output
    assert "[文章 1]" in output
    assert "目录总量=20" in output
    assert "正文字符=1234" in output
    assert "图片=3" in output
    assert "指标=6/6" in output
    assert "checkpoint" in output
    assert "requests -> browser" in output
    assert "伪造日志" in output
    assert "标题\n伪造日志" not in output
    assert output.endswith("\n")


def test_production_progress_event_is_flushed_to_daily_log_file(tmp_path):
    setup_logging(tmp_path)
    logger = ProgressLogger()

    logger.history_finished(completed=1, total=1)

    log_files = list(tmp_path.glob("crawler_*.log"))
    assert len(log_files) == 1
    assert "历史抓取汇总 | completed=1/1" in log_files[0].read_text(encoding="utf-8")


def test_history_failure_event_only_logs_sanitized_error_type():
    stream = StringIO()
    logger = ProgressLogger(stream=stream)

    logger.history_article_failed(
        "测试期刊",
        current=2,
        total=20,
        title="失败文章 https://example.invalid/?token=secret cookie=private fakeid=target",
        error_type="RuntimeError",
    )

    output = stream.getvalue()
    assert "RuntimeError" in output
    assert "secret" not in output
    assert "private" not in output
    assert "target" not in output
    assert "cookie=" not in output.lower()
    assert "token=" not in output.lower()
    assert "fakeid=" not in output.lower()
    assert "https://" not in output


def test_generic_log_boundary_redacts_sensitive_values_and_urls():
    stream = StringIO()
    logger = ProgressLogger(stream=stream)

    logger.info(
        "账号暂停 | account=公告 credential=secret-one cookie: secret-two "
        "fakeid: secret-three https://example.invalid/private"
    )

    output = stream.getvalue().lower()
    assert "secret-one" not in output
    assert "secret-two" not in output
    assert "secret-three" not in output
    assert "credential=" not in output
    assert "cookie:" not in output
    assert "fakeid:" not in output
    assert "https://" not in output


def test_progress_logger_writes_structured_realtime_event_file(tmp_path):
    stream = StringIO()
    event_path = tmp_path / "progress_events.jsonl"
    logger = ProgressLogger(stream=stream, event_path=event_path)

    logger.checkpoint_saved("\u6d4b\u8bd5\u671f\u520a", page=3, processed=12, status="running")

    event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["level"] == "INFO"
    assert event["message"].startswith("checkpoint")
    assert event["message"].endswith("status=running")
    assert stream.getvalue().endswith("\n")
