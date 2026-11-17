"""MSAA/Fiddler client capture backend and bundle importer.

The UI capture script writes one JSON object per article. This module converts
those rows into the same per-journal bundle contract used by the existing
client-history pipeline, so the existing CSV, media and audit readers keep
working without moving any historical output.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import subprocess
import unicodedata
from urllib.parse import parse_qs, urlsplit

from capture.reader import METRIC_FIELDS
from crawl.bundle_media import run_bundle
from crawl.client_history_runner import persist
from crawl.live_csv import LiveArticleCsvWriter
from export.journal_csv import write_journal_csv_bundle
from sources.journal_catalog import parse_profile_links_file
from storage.database import create_session_factory, init_db
from storage.repository import Repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "run_wechat_msaa_capture.ps1"
DEFAULT_CAPTURE_INBOX = PROJECT_ROOT / "output" / "pilot_20260906" / "capture_inbox.jsonl"
DEFAULT_JOURNAL_LINKS = PROJECT_ROOT / "data" / "sources" / "CSSCI来源刊公众号主页链接.md"
METRIC_RAW_FIELDS = {
    "read_count": "read_num",
    "like_count": "old_like_count",
    "watching_count": "like_count",
    "share_count": "share_num",
    "favorite_count": "collect_num",
    "comment_count": "comment_count",
}


def normalize_title(value) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not Path(path).exists():
        return rows
    for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            value["_line"] = number
            rows.append(value)
    return rows


def _article_identity_from_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(str(url or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com"
            or parsed.path != "/s"):
        raise ValueError("invalid_article_url")
    values = []
    for key in ("__biz", "mid", "idx", "sn"):
        if len(query.get(key, [])) != 1:
            raise ValueError("invalid_article_identity")
        values.append(query[key][0])
    biz, mid, idx, sn = values
    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", biz):
        raise ValueError("invalid_article_biz")
    if not mid.isdigit() or not idx.isdigit() or not 1 <= int(idx) <= 100:
        raise ValueError("invalid_article_identity")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", sn):
        raise ValueError("invalid_article_sn")
    return biz, mid, int(idx), sn


def _event_time(row: dict) -> str:
    value = str(row.get("captured_at") or "").strip()
    if not value:
        return datetime.now(timezone.utc).isoformat()
    return value


def _integer_metric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    text = str(value or "").strip()
    return int(text) if re.fullmatch(r"\d+", text) else None


def _open_inbox(path: Path):
    path = Path(path)
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace")
    return path.open(encoding="utf-8-sig")


def _capture_inbox_index(path: Path, biz: str) -> dict[str, dict]:
    """Index article + metric events in a capture inbox by normalized title."""
    index: dict[str, dict] = {}
    if not Path(path).exists():
        return index
    with _open_inbox(Path(path)) as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("biz") != biz:
                continue
            kind = record.get("kind")
            mid = str(record.get("mid") or "")
            if not mid:
                continue
            entry = index.setdefault(mid, {"article": None, "metrics": None})
            if kind == "article":
                article = record.get("article") or {}
                entry["article"] = record
                entry["title"] = normalize_title(article.get("title"))
            elif kind == "metrics":
                entry["metrics"] = record
    by_title: dict[str, dict] = {}
    for entry in index.values():
        article = entry.get("article") or {}
        title = normalize_title((article.get("article") or {}).get("title"))
        if title and title not in by_title:
            by_title[title] = entry
    return by_title


def _metrics_from_record(record: dict) -> dict:
    values = record.get("metrics") or {}
    if values:
        return {field: _integer_metric(values.get(field)) for field in METRIC_FIELDS}
    bootstrap = record.get("bootstrap_fields") or {}
    def pick(name):
        for prefix in (
            "$html.cgiDataNew.user_info.appmsg_bar_data.",
            "$html.appmsg_bar_data.",
        ):
            value = bootstrap.get(prefix + name)
            if isinstance(value, list) and value:
                return value[0].get("value")
        return None
    return {
        "read_count": _integer_metric(pick("read_num")),
        "like_count": _integer_metric(pick("old_like_count")),
        "watching_count": _integer_metric(pick("like_count")),
        "share_count": _integer_metric(pick("share_count")),
        "favorite_count": _integer_metric(pick("collect_count")),
        "comment_count": _integer_metric(pick("comment_count")),
    }


def fill_missing_metrics(rows: list[dict], capture_inbox: Path, biz: str) -> int:
    """Recover metrics that exist in Fiddler but are null in the JSONL row."""
    if all(
        all((row.get("metrics") or {}).get(field) is not None for field in METRIC_FIELDS)
        for row in rows
    ):
        return 0
    by_event: dict[str, tuple[dict, dict]] = {}
    by_mid: dict[str, list[tuple[dict, dict]]] = {}
    if not Path(capture_inbox).exists():
        return 0
    with _open_inbox(Path(capture_inbox)) as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("biz") != biz or record.get("kind") != "metrics":
                continue
            values = _metrics_from_record(record)
            event_id = str(record.get("event_id") or "")
            if event_id:
                by_event[event_id] = (record, values)
            mid = str(record.get("mid") or "")
            if mid:
                by_mid.setdefault(mid, []).append((record, values))
    filled = 0
    for row in rows:
        current = row.get("metrics") or {}
        if all(current.get(field) is not None for field in METRIC_FIELDS):
            continue
        candidates = []
        event_id = str(row.get("metric_event_id") or "")
        if event_id and event_id in by_event:
            candidates = [by_event[event_id]]
        elif str(row.get("mid") or "") in by_mid:
            candidates = by_mid[str(row["mid"])]
        if not candidates:
            continue
        record, values = candidates[-1]
        row["metrics"] = values
        row["metric_event_id"] = str(record.get("event_id") or row.get("metric_event_id") or "")
        filled += 1
    return filled


def fill_missing_identity(rows: list[dict], capture_inbox: Path, biz: str) -> int:
    """Backfill rows written before top-level identity existed.

    Some early pilot rows only contain title/content/metrics. The capture inbox
    still has the real article and metric events, so identity is recovered by
    exact normalized title and the target biz.
    """
    if all(str(row.get("mid") or "") for row in rows):
        return 0
    by_title = _capture_inbox_index(Path(capture_inbox), biz)
    filled = 0
    for row in rows:
        if str(row.get("mid") or ""):
            continue
        entry = by_title.get(normalize_title(row.get("title")))
        if entry:
            article_event = entry.get("article") or {}
            article = article_event.get("article") or {}
            row.update(
                biz=str(article_event.get("biz") or biz),
                mid=str(article_event.get("mid") or ""),
                idx=article_event.get("idx", row.get("idx")),
                sn=str(article_event.get("sn") or ""),
                url=str(article.get("url") or row.get("url") or ""),
                title=str(article.get("title") or row.get("title") or ""),
                author=str(article.get("author") or row.get("author") or ""),
                account_name=str(article.get("account_name") or row.get("account_name") or ""),
                digest=str(article.get("digest") or row.get("digest") or ""),
                publish_time=str(article.get("publish_time") or row.get("publish_time") or ""),
                cover_url=str(article.get("cover_url") or row.get("cover_url") or ""),
                content_html=str(article.get("content_html") or row.get("content_html") or ""),
            )
            filled += 1
            continue
        try:
            url_biz, url_mid, url_idx, url_sn = _article_identity_from_url(
                str(row.get("url") or "")
            )
        except ValueError:
            continue
        if url_biz != biz:
            continue
        row.update(biz=url_biz, mid=url_mid, idx=url_idx, sn=url_sn)
        filled += 1
    return filled


def row_to_events(row: dict, *, account_name: str, expected_biz: str) -> tuple[dict, dict]:
    biz, mid, idx, sn = (
        str(row.get("biz") or expected_biz),
        str(row.get("mid") or ""),
        int(row.get("idx") or 1),
        str(row.get("sn") or ""),
    )
    url = str(row.get("url") or "")
    if not mid or not sn:
        raise ValueError("missing_article_identity")
    url_biz, url_mid, url_idx, url_sn = _article_identity_from_url(url)
    if (url_biz, url_mid, url_idx, url_sn) != (biz, mid, idx, sn):
        raise ValueError("article_identity_mismatch")
    captured_at = _event_time(row)
    article_event = {
        "schema_version": 1,
        "event_id": str(row.get("event_id") or f"msaa-article-{mid}-{idx}"),
        "captured_at": captured_at,
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "article",
        "biz": biz,
        "mid": mid,
        "idx": idx,
        "sn": sn,
        "article": {
            "title": str(row.get("title") or ""),
            "author": str(row.get("author") or ""),
            "account_name": str(row.get("account_name") or account_name),
            "digest": str(row.get("digest") or ""),
            "publish_time": str(row.get("publish_time") or ""),
            "url": url,
            "cover_url": str(row.get("cover_url") or ""),
            "content_html": str(row.get("content_html") or ""),
        },
    }
    metrics = {
        field: _integer_metric((row.get("metrics") or {}).get(field))
        for field in METRIC_FIELDS
    }
    metric_event = {
        "schema_version": 1,
        "event_id": str(row.get("metric_event_id") or f"msaa-metrics-{mid}-{idx}"),
        "captured_at": captured_at,
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "metrics",
        "biz": biz,
        "mid": mid,
        "idx": idx,
        "sn": sn,
        "metrics": metrics,
        "field_meta": {
            field: {
                "status": "observed" if metrics[field] is not None else "not_observed",
                "precision": "exact" if metrics[field] is not None else "unknown",
                "raw_field": METRIC_RAW_FIELDS[field],
                "raw_value": str(metrics[field]) if metrics[field] is not None else None,
                "source": "fiddler_classic",
                "captured_at": captured_at,
            }
            for field in METRIC_FIELDS
        },
    }
    return article_event, metric_event


def find_target(journal_links: Path, biz: str):
    for target in parse_profile_links_file(str(journal_links)):
        if target.biz == biz:
            return target
    raise ValueError("target_not_in_roster")


def import_rows(
    rows: list[dict],
    *,
    base: Path,
    journal_links: Path,
    account_name: str,
    expected_biz: str,
    media_workers: int = 0,
) -> dict:
    base = Path(base).resolve()
    bundle = base / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    target = find_target(Path(journal_links), expected_biz)
    factory = create_session_factory("sqlite:///" + (bundle / "journal.db").as_posix())
    init_db(factory)
    repo = Repository(factory)
    repo.sync_target_accounts([target])
    account = next(item for item in repo.list_accounts(limit=None) if item.biz == expected_biz)
    imported = 0
    skipped = 0
    for row in rows:
        if str(row.get("biz") or expected_biz) != expected_biz:
            skipped += 1
            continue
        try:
            article_event, metric_event = row_to_events(
                row, account_name=account_name, expected_biz=expected_biz,
            )
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        persist(repo, account, target, article_event, metric_event, bundle)
        imported += 1
    csv_result = write_journal_csv_bundle(bundle)
    media_result = None
    if media_workers > 0:
        media_result = {"return_code": run_bundle(workers=media_workers, bundle_root=bundle)}
        write_journal_csv_bundle(bundle)
    summary = {
        "base": str(base),
        "bundle": str(bundle),
        "imported": imported,
        "skipped": skipped,
        "csv": csv_result,
        "media": media_result,
    }
    (base / "msaa_import_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return summary


def run_capture(
    *,
    profile_hwnd: int,
    biz: str,
    capture_inbox: Path,
    output_dir: Path,
    delay_seconds: int = 2,
    wait_seconds: int = 60,
    scroll_delta: int = -120,
    max_idle_scrolls: int = 40,
    script: Path = DEFAULT_CAPTURE_SCRIPT,
    journal_name: str = "",
    profile_url: str = "",
    refresh_article: bool = False,
    start_from_top: bool = False,
) -> int:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    live_writer = None
    try:
        live_writer = LiveArticleCsvWriter(
            output_dir / "articles.jsonl",
            output_dir / "文章数据.live.csv",
            journal_name=journal_name,
            profile_url=profile_url,
        )
        live_writer.start()
    except Exception as exc:  # noqa: BLE001 - live preview must not stop capture
        if live_writer is not None:
            try:
                live_writer.stop()
            except Exception:
                pass
        live_writer = None
        print(f"[live_csv] disabled: {type(exc).__name__}:{exc}", flush=True)
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(Path(script).resolve()),
        "-ProfileWindowHandle", str(int(profile_hwnd)),
        "-Biz", str(biz),
        "-CaptureInbox", str(Path(capture_inbox).resolve()),
        "-OutputDir", str(output_dir),
        "-DelaySeconds", str(int(delay_seconds)),
        "-WaitSeconds", str(int(wait_seconds)),
        "-ScrollDelta", str(int(scroll_delta)),
        "-MaxIdleScrolls", str(int(max_idle_scrolls)),
    ]
    if journal_name:
        command.extend(["-Account", str(journal_name)])
    if refresh_article:
        command.append("-RefreshArticle")
    if start_from_top:
        command.append("-StartFromTop")
    try:
        return int(subprocess.run(command, cwd=PROJECT_ROOT).returncode)
    finally:
        if live_writer is not None:
            live_writer.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--journal-links", type=Path, default=DEFAULT_JOURNAL_LINKS)
    parser.add_argument("--biz", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--capture-inbox", type=Path, default=DEFAULT_CAPTURE_INBOX)
    parser.add_argument("--media-workers", type=int, default=0)
    args = parser.parse_args(argv)
    rows = _read_jsonl(args.articles)
    filled = fill_missing_identity(rows, args.capture_inbox, args.biz)
    filled_metrics = fill_missing_metrics(rows, args.capture_inbox, args.biz)
    summary = import_rows(
        rows,
        base=args.base,
        journal_links=args.journal_links,
        account_name=args.account,
        expected_biz=args.biz,
        media_workers=args.media_workers,
    )
    summary["filled_missing_identity"] = filled
    summary["filled_missing_metrics"] = filled_metrics
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["imported"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
