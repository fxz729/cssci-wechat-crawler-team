"""Append-only live CSV preview for the MSAA capture worker.

The capture worker appends one JSON object per article to ``articles.jsonl``.
This module tails that file while the worker runs and appends one row per
article to ``文章数据.live.csv``. The live CSV is intentionally a preview:
the canonical per-journal CSV is still produced by
``import_rows/write_journal_csv_bundle`` after the journal finishes.

JSONL remains the source of truth. A live CSV write failure must never stop
the capture worker; at worst the row is retried on the next poll or backfilled
on the next run.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import threading
import unicodedata

from capture.reader import METRIC_FIELDS
from export.journal_csv import EXTRA_HEADERS
from export.realtime_exporter import default_export_headers


SHANGHAI_TZ = timezone(timedelta(hours=8))
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


LIVE_HEADERS = _dedupe(
    list(default_export_headers([]))
    + list(EXTRA_HEADERS)
    + [
        "journal_name",
        "article_url",
        "content_text_preview",
        "article_key",
        "live_key",
        "live_row_number",
        "live_content_is_preview",
    ]
)


def _normalize_title(value) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")),
    ).strip()


def article_key(row: dict) -> str:
    """Return a stable dedupe key for one captured article row."""
    biz = str(row.get("biz") or "").strip()
    mid = str(row.get("mid") or "").strip()
    idx = str(row.get("idx") or "").strip()
    sn = str(row.get("sn") or "").strip()
    if biz and mid and sn:
        return f"{biz}|{mid}|{idx}|{sn}"
    event_id = str(row.get("event_id") or "").strip()
    if event_id:
        return "event:" + event_id
    fallback = _normalize_title(row.get("title")) or str(row.get("captured_at") or "")
    return "fallback:" + hashlib.sha1(fallback.encode("utf-8")).hexdigest()


def _cell(value):
    return "" if value is None else value


def _json_cell(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _publish_time_info(value) -> tuple[str, datetime | None]:
    text = str(value or "").strip()
    if not text:
        return "", None
    if re.fullmatch(r"\d{10}", text):
        try:
            moment = datetime.fromtimestamp(int(text), tz=SHANGHAI_TZ)
        except (OverflowError, OSError, ValueError):
            return text, None
        return moment.isoformat(), moment
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text, None
    if moment.tzinfo is not None:
        moment = moment.astimezone(SHANGHAI_TZ)
    return moment.isoformat(), moment


class _LiveHtmlSummary(HTMLParser):
    """Lightweight HTML text/count extractor used only for the live preview."""

    _BLOCK_TAGS = frozenset({
        "article", "blockquote", "br", "div", "figcaption", "figure",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.image_count = 0
        self.video_count = 0
        self.audio_count = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if tag == "img":
            self.image_count += 1
        elif tag in {"video", "mpvideo", "iframe"}:
            self.video_count += 1
        elif tag in {"audio", "mpvoice"}:
            self.audio_count += 1
        if tag in self._BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs) -> None:
        if tag.lower() not in {"script", "style"}:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._skip:
                self._skip -= 1
            return
        if tag in self._BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n[ \t]*\n+", "\n", text)
        return text.strip()


def build_live_row(
    row: dict,
    *,
    journal_name: str = "",
    profile_url: str = "",
    line_number: int = 0,
) -> dict:
    """Map one capture JSONL row to the live CSV column contract."""
    content_html = str(row.get("content_html") or "")
    summary = _LiveHtmlSummary()
    try:
        summary.feed(content_html)
        summary.close()
    except Exception:
        pass
    content_text = summary.text()
    publish_time, published = _publish_time_info(row.get("publish_time"))
    metrics = row.get("metrics") or {}
    metric_values = {name: _cell(metrics.get(name)) for name in METRIC_FIELDS}
    complete_metrics = all(metrics.get(name) is not None for name in METRIC_FIELDS)
    biz = str(row.get("biz") or "")
    mid = str(row.get("mid") or "")
    idx = row.get("idx")
    sn = str(row.get("sn") or "")
    title = str(row.get("title") or "")
    digest = str(row.get("digest") or "")
    url = str(row.get("url") or "")

    result = {name: "" for name in LIVE_HEADERS}
    result.update({
        "journal_name": journal_name,
        "account_name": str(row.get("account_name") or journal_name or ""),
        "author": str(row.get("author") or ""),
        "publish_time": publish_time,
        "title": title,
        "title_length": len(title),
        "digest": digest,
        "digest_length": len(digest),
        "content": content_text,
        "content_text": content_text,
        "content_text_preview": content_text,
        "content_html": content_html,
        "word_count": len(re.sub(r"\s", "", content_text)),
        "image_count": summary.image_count,
        "video_count": summary.video_count,
        "audio_count": summary.audio_count,
        "read_count": metric_values["read_count"],
        "watching_count": metric_values["watching_count"],
        "like_count": metric_values["like_count"],
        "favorite_count": metric_values["favorite_count"],
        "share_count": metric_values["share_count"],
        "comment_count": metric_values["comment_count"],
        "collect_num": metric_values["favorite_count"],
        "biz": biz,
        "mid": mid,
        "idx": "" if idx is None else idx,
        "sn": sn,
        "batch_key": f"{biz}:{mid}",
        "source_url": url,
        "canonical_url": url,
        "article_url": url,
        "profile_url": profile_url,
        "cover_image_url": str(row.get("cover_url") or ""),
        "quarter": (published.month - 1) // 3 + 1 if published else "",
        "is_weekday": published.weekday() < 5 if published else "",
        "metrics_provider": "fiddler_classic",
        "metrics_captured_at": str(row.get("captured_at") or ""),
        "metrics_status": "complete" if complete_metrics else "partial",
        "metric_field_status": _json_cell({
            name: ("observed" if metrics.get(name) is not None else "not_observed")
            for name in METRIC_FIELDS
        }),
        "metric_fields": _json_cell(metric_values),
        "crawl_status": "captured_live",
        "article_event_id": str(row.get("event_id") or ""),
        "metric_event_id": str(row.get("metric_event_id") or ""),
        "media_manifest_status": "pending_live",
        "field_provenance": _json_cell({
            "content_html": "fiddler_classic",
            "metrics": "fiddler_classic",
            "live_text": "local_html_parser_preview",
        }),
        "article_key": f"{mid}_{idx}" if mid and idx is not None else "",
        "live_key": article_key(row),
        "live_row_number": int(line_number or 0),
        "live_content_is_preview": "true",
    })
    return result


class LiveArticleCsvWriter:
    """Tail ``articles.jsonl`` and append rows to ``文章数据.live.csv``."""

    def __init__(
        self,
        articles_path: Path,
        csv_path: Path,
        *,
        journal_name: str = "",
        profile_url: str = "",
        poll_interval: float = 0.5,
    ):
        self.articles_path = Path(articles_path)
        self.csv_path = Path(csv_path)
        self.journal_name = str(journal_name or "")
        self.profile_url = str(profile_url or "")
        self.poll_interval = max(0.1, float(poll_interval))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stream = None
        self._writer = None
        self._offset = 0
        self._jsonl_line_number = 0
        self._seen: set[str] = set()
        self._logged_errors: set[str] = set()
        self.written = 0
        self.failed = 0

    def start(self) -> None:
        self._stop_event.clear()
        self.sync_once()
        self._thread = threading.Thread(
            target=self._run,
            name="live-csv-writer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        if thread is not None and thread.is_alive():
            self._log_error("live_csv_thread_join_timeout")
            return
        try:
            self.sync_once()
        except Exception as exc:  # noqa: BLE001 - best-effort preview only
            self._log_error(f"live_csv_final_sync_failed:{exc}")
        self._close()

    def sync_once(self) -> None:
        with self._lock:
            if self._writer is None or self._stream is None:
                self._prepare_locked()
            self._sync_locked()

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001 - best-effort preview only
                self._log_error(f"live_csv_sync_failed:{exc}")

    def _prepare_locked(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if self.csv_path.exists() and self.csv_path.stat().st_size > 0:
            self._repair_trailing_partial_line()
            self._load_existing_locked()
        self._open_writer_locked()

    def _repair_trailing_partial_line(self) -> None:
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            return
        with self.csv_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return
            position = size
            last_newline = -1
            while position > 0:
                chunk_size = min(65536, position)
                position -= chunk_size
                stream.seek(position)
                chunk = stream.read(chunk_size)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    last_newline = position + index
                    break
        with self.csv_path.open("r+b") as stream:
            stream.truncate(last_newline + 1 if last_newline >= 0 else 0)

    def _load_existing_locked(self) -> None:
        with self.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = list(reader.fieldnames or [])
            if headers != LIVE_HEADERS:
                raise ValueError("live_csv_header_mismatch")
            for row in reader:
                key = str(row.get("live_key") or "")
                if key:
                    self._seen.add(key)

    def _open_writer_locked(self) -> None:
        new_file = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        self._stream = self.csv_path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._stream,
            fieldnames=LIVE_HEADERS,
            extrasaction="ignore",
        )
        if new_file:
            self._stream.write("\ufeff")
            self._writer.writeheader()
            self._stream.flush()

    def _close_stream_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except OSError:
                pass
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = None
        self._writer = None

    def _close(self) -> None:
        with self._lock:
            self._close_stream_locked()

    def _sync_locked(self) -> None:
        if self._writer is None or self._stream is None:
            return
        if not self.articles_path.is_file():
            return
        if self.articles_path.stat().st_size < self._offset:
            # The source was truncated by a fresh run. Re-read from the start
            # and rely on the live_key index to avoid duplicate rows.
            self._offset = 0
            self._jsonl_line_number = 0
        try:
            with self.articles_path.open("rb") as stream:
                stream.seek(self._offset)
                while True:
                    line_start = self._offset
                    raw = stream.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break
                    line_end = line_start + len(raw)
                    line_number = self._jsonl_line_number + 1
                    text = raw.decode("utf-8-sig", errors="replace").strip()
                    if not text:
                        self._offset = line_end
                        self._jsonl_line_number = line_number
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        self._offset = line_end
                        self._jsonl_line_number = line_number
                        continue
                    if not isinstance(record, dict):
                        self._offset = line_end
                        self._jsonl_line_number = line_number
                        continue
                    key = article_key(record)
                    if not key or key in self._seen:
                        self._offset = line_end
                        self._jsonl_line_number = line_number
                        continue
                    live_row = build_live_row(
                        record,
                        journal_name=self.journal_name,
                        profile_url=self.profile_url,
                        line_number=line_number,
                    )
                    try:
                        self._writer.writerow(live_row)
                        self._stream.flush()
                    except OSError as exc:
                        self.failed += 1
                        self._log_error(
                            f"live_csv_write_failed:{type(exc).__name__}:{exc}"
                        )
                        self._close_stream_locked()
                        try:
                            self._open_writer_locked()
                        except OSError as reopen_exc:
                            self._log_error(
                                f"live_csv_reopen_failed:"
                                f"{type(reopen_exc).__name__}:{reopen_exc}"
                            )
                        break
                    except csv.Error as exc:
                        self.failed += 1
                        self._log_error(f"live_csv_row_failed:{exc}")
                        self._offset = line_end
                        self._jsonl_line_number = line_number
                        continue
                    self._seen.add(key)
                    self._offset = line_end
                    self._jsonl_line_number = line_number
                    self.written += 1
        except OSError as exc:
            self._log_error(f"live_csv_read_failed:{type(exc).__name__}:{exc}")

    def _log_error(self, message: str) -> None:
        if message in self._logged_errors:
            return
        self._logged_errors.add(message)
        print(f"[live_csv] {message}", file=sys.stderr, flush=True)
