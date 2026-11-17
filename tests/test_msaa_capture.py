import csv
import gzip
import json
from pathlib import Path

import pytest

from crawl.msaa_capture import (
    _read_jsonl,
    fill_missing_identity,
    fill_missing_metrics,
    import_rows,
    run_capture,
    row_to_events,
)


BIZ = "MzUzNzUwMzQ0OA=="
SIX = ("read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count")


def _capture_inbox(tmp_path: Path) -> Path:
    article = {
        "schema_version": 1,
        "event_id": "article-1",
        "captured_at": "2026-09-10T14:28:01Z",
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "article",
        "biz": BIZ,
        "mid": "2247496001",
        "idx": 1,
        "sn": "a" * 32,
        "article": {
            "title": "郭芯雨｜俄罗斯学界中国抗日战争研究的新进展与新趋向",
            "author": "郭芯雨",
            "account_name": "苏区研究",
            "digest": "摘要",
            "publish_time": "1777709778",
            "url": f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=2247496001&idx=1&sn={'a' * 32}",
            "cover_url": "https://mmbiz.qpic.cn/cover.jpg",
            "content_html": "<p>正文</p>",
        },
    }
    metric = {
        "schema_version": 1,
        "event_id": "metric-1",
        "captured_at": "2026-09-10T14:28:02Z",
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "metrics",
        "biz": BIZ,
        "mid": "2247496001",
        "idx": 1,
        "sn": "a" * 32,
        "metrics": {
            "read_count": 4333,
            "like_count": 34,
            "watching_count": 19,
            "share_count": 180,
            "favorite_count": 41,
            "comment_count": 0,
        },
    }
    path = tmp_path / "capture_inbox.jsonl"
    path.write_text(
        json.dumps(article, ensure_ascii=False) + "\n" + json.dumps(metric, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def test_missing_identity_and_metrics_are_backfilled_from_capture_inbox(tmp_path):
    inbox = _capture_inbox(tmp_path)
    rows = [{
        "title": "郭芯雨｜俄罗斯学界中国抗日战争研究的新进展与新趋向",
        "content_html": "<p>正文</p>",
        "metrics": None,
        "metric_event_id": "metric-1",
    }]

    assert fill_missing_identity(rows, inbox, BIZ) == 1
    assert fill_missing_metrics(rows, inbox, BIZ) == 1
    assert rows[0]["mid"] == "2247496001"
    assert rows[0]["sn"] == "a" * 32
    assert all(rows[0]["metrics"][field] is not None for field in SIX)


def test_missing_identity_falls_back_to_article_url(tmp_path):
    row = {
        "title": "URL 身份回填测试",
        "url": (
            f"https://mp.weixin.qq.com/s?__biz={BIZ}"
            f"&mid=2247496001&idx=1&sn={'a' * 32}"
        ),
        "metrics": {
            "read_count": 1,
            "like_count": 2,
            "watching_count": 3,
            "share_count": 4,
            "favorite_count": 5,
            "comment_count": 0,
        },
    }

    assert fill_missing_identity([row], tmp_path / "missing.jsonl", BIZ) == 1
    assert row["biz"] == BIZ
    assert row["mid"] == "2247496001"
    assert row["idx"] == 1
    assert row["sn"] == "a" * 32


def test_row_to_events_preserves_identity_and_six_metrics():
    row = {
        "captured_at": "2026-09-10T14:28:01Z",
        "event_id": "article-1",
        "metric_event_id": "metric-1",
        "biz": BIZ,
        "mid": "2247496001",
        "idx": 1,
        "sn": "a" * 32,
        "title": "郭芯雨｜俄罗斯学界中国抗日战争研究的新进展与新趋向",
        "author": "郭芯雨",
        "account_name": "苏区研究",
        "digest": "摘要",
        "publish_time": "1777709778",
        "url": f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=2247496001&idx=1&sn={'a' * 32}",
        "cover_url": "https://mmbiz.qpic.cn/cover.jpg",
        "content_html": "<p>正文</p>",
        "metrics": {
            "read_count": 4333,
            "like_count": 34,
            "watching_count": 19,
            "share_count": 180,
            "favorite_count": 41,
            "comment_count": 0,
        },
    }

    article_event, metric_event = row_to_events(row, account_name="苏区研究", expected_biz=BIZ)
    assert article_event["article"]["content_html"] == "<p>正文</p>"
    assert metric_event["metrics"] == row["metrics"]
    assert all(metric_event["field_meta"][field]["status"] == "observed" for field in SIX)


def test_import_rows_with_pilot_data_when_available(tmp_path):
    root = Path(__file__).parents[1]
    articles = root / "output" / "v12_validation" / "manual_ui_capture" / "articles.jsonl"
    inbox = root / "output" / "pilot_20260906" / "capture_inbox.jsonl"
    if not articles.is_file() or not inbox.is_file():
        pytest.skip("pilot capture data is not available in this checkout")
    rows = _read_jsonl(articles)[:2]
    for row in rows:
        row.setdefault("biz", BIZ)
    fill_missing_identity(rows, inbox, BIZ)
    fill_missing_metrics(rows, inbox, BIZ)
    rows = [row for row in rows if row.get("mid") and row.get("sn")]
    if len(rows) != 2:
        pytest.skip("pilot capture data no longer matches the archived inbox")

    summary = import_rows(
        rows,
        base=tmp_path,
        journal_links=root / "data" / "sources" / "CSSCI来源刊公众号主页链接.md",
        account_name="苏区研究",
        expected_biz=BIZ,
        media_workers=0,
    )

    assert summary["imported"] == 2
    assert summary["csv"]["article_count"] == 2
    assert (tmp_path / "bundle" / "文章数据.csv").is_file()


def test_run_capture_writes_live_csv(tmp_path, monkeypatch):
    output_dir = tmp_path / "msaa"
    output_dir.mkdir()
    articles = output_dir / "articles.jsonl"
    row = {
        "captured_at": "2026-09-10T14:28:01Z",
        "event_id": "article-1",
        "biz": BIZ,
        "mid": "2247496001",
        "idx": 1,
        "sn": "a" * 32,
        "title": "实时 CSV 测试",
        "author": "作者",
        "account_name": "苏区研究",
        "digest": "摘要",
        "publish_time": "1777709778",
        "url": (
            f"https://mp.weixin.qq.com/s?__biz={BIZ}"
            f"&mid=2247496001&idx=1&sn={'a' * 32}"
        ),
        "cover_url": "https://example.com/cover.jpg",
        "content_html": "<p>正文</p>",
        "metrics": {
            "read_count": 1,
            "like_count": 2,
            "watching_count": 3,
            "share_count": 4,
            "favorite_count": 5,
            "comment_count": 0,
        },
        "metric_event_id": "metric-1",
    }

    class Result:
        returncode = 0

    def fake_run(command, cwd, **kwargs):
        articles.write_text(
            json.dumps(row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return Result()

    monkeypatch.setattr("crawl.msaa_capture.subprocess.run", fake_run)

    code = run_capture(
        profile_hwnd=1,
        biz=BIZ,
        capture_inbox=tmp_path / "capture_inbox.jsonl",
        output_dir=output_dir,
        journal_name="苏区研究",
    )

    assert code == 0
    live_csv = output_dir / "文章数据.live.csv"
    assert live_csv.is_file()
    with live_csv.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["journal_name"] == "苏区研究"
    assert rows[0]["read_count"] == "1"


def test_fill_missing_helpers_skip_inbox_scan_when_rows_complete(tmp_path):
    # A directory path would raise if either helper tried to open it as JSONL.
    inbox_dir = tmp_path / "capture_inbox.jsonl"
    inbox_dir.mkdir()
    rows = [{
        "mid": "2247496001",
        "metrics": {
            "read_count": 1,
            "like_count": 2,
            "watching_count": 3,
            "share_count": 4,
            "favorite_count": 5,
            "comment_count": 0,
        },
    }]

    assert fill_missing_identity(rows, inbox_dir, BIZ) == 0
    assert fill_missing_metrics(rows, inbox_dir, BIZ) == 0


def test_run_capture_refresh_article_is_opt_in(tmp_path, monkeypatch):
    captured = {}

    class Result:
        returncode = 0

    def fake_run(command, cwd, **kwargs):
        captured["command"] = command
        return Result()

    monkeypatch.setattr("crawl.msaa_capture.subprocess.run", fake_run)
    common = {
        "profile_hwnd": 1,
        "biz": BIZ,
        "capture_inbox": tmp_path / "capture_inbox.jsonl",
        "output_dir": tmp_path / "msaa",
    }

    assert run_capture(**common) == 0
    assert "-RefreshArticle" not in captured["command"]

    assert run_capture(**common, refresh_article=True) == 0
    assert "-RefreshArticle" in captured["command"]

    assert run_capture(**common, start_from_top=True) == 0
    assert "-StartFromTop" in captured["command"]


def test_missing_identity_and_metrics_are_backfilled_from_gzip_inbox(tmp_path):
    inbox = _capture_inbox(tmp_path)
    gzip_path = tmp_path / "capture_inbox.jsonl.gz"
    with gzip.open(gzip_path, "wb") as stream:
        stream.write(inbox.read_bytes())
    rows = [{
        "title": "郭芯雨｜俄罗斯学界中国抗日战争研究的新进展与新趋向",
        "content_html": "<p>正文</p>",
        "metrics": None,
        "metric_event_id": "metric-1",
    }]

    assert fill_missing_identity(rows, gzip_path, BIZ) == 1
    assert fill_missing_metrics(rows, gzip_path, BIZ) == 1
    assert rows[0]["mid"] == "2247496001"
    assert all(rows[0]["metrics"][field] is not None for field in SIX)
