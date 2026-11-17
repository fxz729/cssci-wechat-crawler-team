import csv
import json
from pathlib import Path

from crawl.live_csv import LIVE_HEADERS, LiveArticleCsvWriter, build_live_row


def _row(mid: str, title: str = "测试标题", **overrides) -> dict:
    row = {
        "captured_at": "2026-09-10T14:28:01Z",
        "event_id": f"article-{mid}",
        "biz": "biz-a",
        "mid": mid,
        "idx": 1,
        "sn": "a" * 32,
        "title": title,
        "author": "作者甲",
        "account_name": "测试刊",
        "digest": "测试摘要",
        "publish_time": "1777709778",
        "url": (
            f"https://mp.weixin.qq.com/s?__biz=biz-a&mid={mid}"
            f"&idx=1&sn={'a' * 32}"
        ),
        "cover_url": "https://example.com/cover.jpg",
        "content_html": (
            "<div><p>第一段</p><p>第二段</p>"
            "<img src='https://example.com/a.png'/>"
            "<video></video><mpvoice></mpvoice></div>"
        ),
        "metrics": {
            "read_count": 4333,
            "like_count": 34,
            "watching_count": 19,
            "share_count": 180,
            "favorite_count": 41,
            "comment_count": 0,
        },
        "metric_event_id": f"metric-{mid}",
    }
    row.update(overrides)
    return row


def _read_live(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def test_live_csv_appends_rows_and_dedupes(tmp_path):
    articles = tmp_path / "articles.jsonl"
    articles.write_text(
        json.dumps(_row("1"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    writer = LiveArticleCsvWriter(
        articles,
        tmp_path / "文章数据.live.csv",
        journal_name="测试刊",
    )
    writer.sync_once()
    writer.sync_once()
    writer.stop()

    headers, rows = _read_live(tmp_path / "文章数据.live.csv")
    assert headers == LIVE_HEADERS
    assert len(rows) == 1
    assert rows[0]["live_key"] == f"biz-a|1|1|{'a' * 32}"
    assert rows[0]["journal_name"] == "测试刊"
    assert rows[0]["read_count"] == "4333"
    assert rows[0]["comment_count"] == "0"
    assert rows[0]["image_count"] == "1"
    assert rows[0]["video_count"] == "1"
    assert rows[0]["audio_count"] == "1"
    assert "第一段" in rows[0]["content_text_preview"]
    assert rows[0]["live_content_is_preview"] == "true"


def test_live_csv_resumes_without_duplicate_rows(tmp_path):
    articles = tmp_path / "articles.jsonl"
    csv_path = tmp_path / "文章数据.live.csv"
    articles.write_text(
        json.dumps(_row("1"), ensure_ascii=False) + "\n"
        + json.dumps(_row("2"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    first = LiveArticleCsvWriter(articles, csv_path, journal_name="测试刊")
    first.sync_once()
    first.stop()

    with articles.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_row("3"), ensure_ascii=False) + "\n")
    second = LiveArticleCsvWriter(articles, csv_path, journal_name="测试刊")
    second.sync_once()
    second.stop()

    _, rows = _read_live(csv_path)
    assert [row["mid"] for row in rows] == ["1", "2", "3"]


def test_live_csv_waits_for_complete_jsonl_line(tmp_path):
    articles = tmp_path / "articles.jsonl"
    csv_path = tmp_path / "文章数据.live.csv"
    first = json.dumps(_row("1"), ensure_ascii=False)
    second = json.dumps(_row("2"), ensure_ascii=False)
    articles.write_text(first + "\n" + second[:12], encoding="utf-8")

    writer = LiveArticleCsvWriter(articles, csv_path, journal_name="测试刊")
    writer.sync_once()
    _, rows = _read_live(csv_path)
    assert [row["mid"] for row in rows] == ["1"]

    with articles.open("a", encoding="utf-8") as stream:
        stream.write(second[12:] + "\n")
    writer.sync_once()
    writer.stop()

    _, rows = _read_live(csv_path)
    assert [row["mid"] for row in rows] == ["1", "2"]


def test_live_csv_reads_large_html_on_resume(tmp_path):
    long_text = "长" * 200000
    articles = tmp_path / "articles.jsonl"
    csv_path = tmp_path / "文章数据.live.csv"
    articles.write_text(
        json.dumps(
            _row("1", content_html=f"<p>{long_text}</p>"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    first = LiveArticleCsvWriter(articles, csv_path, journal_name="测试刊")
    first.sync_once()
    first.stop()

    second = LiveArticleCsvWriter(articles, csv_path, journal_name="测试刊")
    second.sync_once()
    assert second.written == 0
    second.stop()

    _, rows = _read_live(csv_path)
    assert len(rows) == 1
    assert long_text in rows[0]["content_html"]


def test_build_live_row_marks_partial_metrics_without_zero_fill():
    row = _row("1", metrics={
        "read_count": 1,
        "like_count": None,
        "watching_count": None,
        "share_count": None,
        "favorite_count": None,
        "comment_count": None,
    })
    live = build_live_row(row, journal_name="测试刊", line_number=7)
    assert live["metrics_status"] == "partial"
    assert live["read_count"] == 1
    assert live["like_count"] == ""
    assert live["live_row_number"] == 7
