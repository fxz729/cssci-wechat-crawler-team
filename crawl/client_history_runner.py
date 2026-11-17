"""Resumable one-journal client capture runner.

UI Automation is used only to open/return to the already logged-in WeChat
article window. Article fields and metrics are accepted only from validated
Fiddler events in ``capture_inbox.jsonl``.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata

from capture.reader import METRIC_FIELDS, load_events, normalize_event, to_wire_event
from capture.setup import set_capture_enabled
from crawl.client_capture import build_capture_metadata
from crawl.catalog_contract import (catalog_evidence_refs, catalog_runtime_membership,
                                    catalog_scan_metadata_valid, catalog_sequence)
from crawl.capture_cache import (
    build_capture_summary,
    read_capture_entry,
    rebind_capture_entry_runtime,
    write_capture_entry,
)
from crawl.desktop_wechat import DesktopWeChatDriver
from crawl.article_persistence import persist_history_article
from crawl.pilot import _html, _time
from sources.journal_catalog import parse_profile_links_file
from storage.database import create_session_factory, init_db
from storage.models import Article, ArticleMetric
from storage.repository import Repository
from utils.progress_logger import ProgressLogger, setup_logging
from utils.run_control import is_stop_requested
from utils.runtime_lock import runtime_lock
from export.journal_csv import write_journal_csv_bundle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE = PROJECT_ROOT / "output" / "journal_validation_20260906"
DEFAULT_PILOT = PROJECT_ROOT / "output" / "pilot_20260906"
DEFAULT_BIZ = "Mzk3NTE3NTU0Ng=="
DEFAULT_ACCOUNT = "当代世界社会主义问题"
DEFAULT_HWND = 331714


def key(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    for attempt in range(5):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


class StopRequested(RuntimeError):
    pass


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    return text if re.fullmatch(r"[a-z][a-z0-9_]*", text) else type(exc).__name__


def _metric_complete(metric: dict | None) -> bool:
    return bool(metric) and all(metric.get("metrics", {}).get(field) is not None for field in METRIC_FIELDS)


def _recover_interrupted_jobs(state: dict) -> int:
    changed = 0
    for job in (state.get("jobs") or {}).values():
        if not isinstance(job, dict) or job.get("status") != "running":
            continue
        job["status"] = "paused"
        job["reason"] = "interrupted_before_resume"
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        changed += 1
    return changed


def _article_key(event: dict) -> tuple[str, str, int, str]:
    return (str(event.get("biz") or ""), str(event.get("mid") or ""),
            int(event.get("idx") or 0), str(event.get("sn") or ""))


class Tail:
    """Read only newly appended, validated Fiddler events."""

    def __init__(self, path: Path, biz: str = DEFAULT_BIZ):
        self.path = path
        self.biz = biz
        self.offset = path.stat().st_size if path.exists() else 0

    def poll(self) -> list[dict]:
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:
            self.offset = 0
        found: list[dict] = []
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line or not line.endswith(b"\n"):
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise RuntimeError("oversized_capture_event")
                self.offset = stream.tell()
                try:
                    found.append(normalize_event(json.loads(line.decode("utf-8-sig")), {self.biz}))
                except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                    continue
        return found


def wait_events(tail: Tail, item: dict, *, biz: str, seconds: float = 60.0, metric_grace: float = 20.0):
    """Wait for the article and its same-identity HTML metric event.

    The old harness used a fixed 18-second window. Older articles and a cold
    WeChat network process can exceed that even though the request succeeds.
    """
    end = time.monotonic() + seconds
    article: dict | None = None
    metrics: list[dict] = []
    observed: list[dict] = []
    metric_end: float | None = None
    while time.monotonic() < end:
        for event in tail.poll():
            if event.get("biz") != biz:
                continue
            observed.append(event)
            if event.get("kind") == "article" and key((event.get("article") or {}).get("title")) == key(item.get("title")):
                article = event
                metric_end = time.monotonic() + metric_grace
            elif event.get("kind") == "metrics" and event.get("metric_format") == "client_html_bootstrap_v1":
                metrics.append(event)
        if article:
            matching = [event for event in metrics if _article_key(event) == _article_key(article)]
            if matching:
                return article, matching[-1], observed
            if metric_end is not None and time.monotonic() >= metric_end:
                return article, None, observed
        time.sleep(0.2)
    return article, None, observed




def _prior_complete_events(inbox: Path, biz: str, title: str) -> tuple[dict, dict] | None:
    if not inbox.exists():
        return None
    events, _issues = load_events(inbox, {biz})
    articles = [
        event for event in events
        if event.get("kind") == "article"
        and key((event.get("article") or {}).get("title")) == key(title)
    ]
    metrics: dict[tuple[str, str, int, str], list[dict]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "metrics" and event.get("metric_format") == "client_html_bootstrap_v1":
            metrics[_article_key(event)].append(event)
    for article in sorted(articles, key=lambda event: str(event.get("captured_at") or ""), reverse=True):
        candidates = [event for event in metrics.get(_article_key(article), []) if _metric_complete(event)]
        if candidates:
            return article, max(candidates, key=lambda event: str(event.get("captured_at") or ""))
    return None


def _wait_or_seed_picture(tail: Tail, item: dict, *, biz: str, seconds: float, inbox: Path):
    article, metric, observed = wait_events(tail, item, biz=biz, seconds=seconds)
    if article is not None or not re.search(r"double-img-card-item|picture", str(item.get("card_class") or ""), re.I):
        return article, metric, observed, "live_client"
    prior = _prior_complete_events(inbox, biz, item.get("title", ""))
    if prior is None:
        return article, metric, observed, "live_client"
    return prior[0], prior[1], observed, "prior_local_event"

def nav(action: str, *, hwnd: int, account: str, item: dict | None = None, profile_url: str | None = None) -> dict:
    DesktopWeChatDriver(hwnd).focus()
    command = [
        "powershell",
        "-NoProfile",
        "-File",
        str(PROJECT_ROOT / "crawl" / "client_navigation.ps1"),
        "-Action",
        action,
        "-WindowHandle",
        str(hwnd),
        "-Account",
        account,
    ]
    if item is not None:
        command += ["-Title", str(item["title"]), "-RuntimeId", str(item["runtime"])]
    if profile_url is not None:
        command += ["-ProfileUrl", str(profile_url)]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    if result.returncode:
        known = (
            "foreground_changed",
            "document_identity_changed",
            "article_title_missing_or_ambiguous",
            "article_card_not_invokable",
            "document_navigation_timeout",
            "profile_tab_not_found",
            "reload_control_not_verified",
        )
        reason = next((value for value in known if value in result.stderr), "failed")
        raise RuntimeError(f"navigation_{action}_{reason}")
    if "\ufffd" in result.stdout:
        raise RuntimeError(f"navigation_{action}_invalid_output")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"navigation_{action}_invalid_output") from exc


def _article_wrapper(article_event: dict, metric: dict | None, account_name: str) -> str:
    """Build a parser-compatible local page from validated public fields only."""
    article = article_event["article"]
    stamp = _time(article.get("publish_time"))
    metadata = "".join(
        f'<meta name="{name}" content="{_html_escape(article.get(value, ""))}">'
        for name, value in (("author", "author"), ("description", "digest"))
    )
    metadata += f'<meta property="og:title" content="{_html_escape(article.get("title", ""))}">'
    metadata += f'<meta property="og:image" content="{_html_escape(article.get("cover_url", ""))}">'
    public_vars = "".join(
        f"var {name}={json.dumps(str(value or ""), ensure_ascii=False)};"
        for name, value in (
            ("biz", article_event.get("biz")),
            ("mid", article_event.get("mid")),
            ("idx", article_event.get("idx")),
            ("sn", article_event.get("sn")),
        )
    )
    if stamp:
        public_vars += f"var ct=\"{int(stamp.timestamp())}\";"
    if metric and metric.get("comment_id"):
        public_vars += f"var comment_id=\"{_html_escape(metric['comment_id'])}\";"
    author = article.get("author") or ""
    author_node = f'<span id="js_author_name">{_html_escape(author)}</span>' if author else ""
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        + metadata
        + "</head><body>"
        + f'<h1 id="activity-name">{_html_escape(article.get("title", ""))}</h1>'
        + f'<span id="js_name">{_html_escape(account_name)}</span>'
        + author_node
        + str(article.get("content_html") or "")
        + f"<script>{public_vars}</script></body></html>"
    )


def _html_escape(value) -> str:
    from html import escape

    return escape(str(value or ""), quote=True)


def _db_row(stored: Article) -> dict:
    row = {column.name: getattr(stored, column.name) for column in Article.__table__.columns}
    row["media_assets"] = json.loads(stored.media_assets_json or "[]")
    row["image_urls"] = json.loads(stored.image_urls_json or "[]")
    row["field_provenance"] = json.loads(stored.field_provenance_json or "{}")
    return row


def persist(
    repo: Repository,
    account,
    target,
    article_event: dict,
    metric: dict | None,
    bundle: Path,
) -> dict:
    article = article_event["article"]
    html = _article_wrapper(article_event, metric, target.account_name)
    item = type("CapturedItem", (), {
        "mid": article_event["mid"],
        "idx": article_event["idx"],
        "title": article.get("title", ""),
        "author": article.get("author", ""),
        "digest": article.get("digest", ""),
        "cover_url": article.get("cover_url", ""),
        "link": article.get("url", ""),
        "publish_time": _time(article.get("publish_time")),
    })()
    client = type("CapturedClient", (), {"fetch_article_page": lambda _self, _item: (item.link, html)})()
    saved = persist_history_article(repo, account, client, item, bundle / "captured_content", with_metrics=False)

    folder = bundle / "articles" / f"{article_event['mid']}_{article_event['idx']}"
    folder.mkdir(parents=True, exist_ok=True)
    local_html = folder / "article.html"
    local_text = folder / "article.txt"
    local_fragment = folder / "content_fragment.html"
    local_html.write_text(html, encoding="utf-8")
    local_fragment.write_text(str(article.get("content_html") or ""), encoding="utf-8")

    with repo.session_factory() as db:
        stored = db.query(Article).filter_by(id=saved.id).one()
        base_row = _db_row(stored)
    record = build_capture_metadata(
        article_event,
        metric,
        base_row,
        journal_name=target.account_name,
        profile_url=target.profile_url,
        local_html=local_html.relative_to(bundle).as_posix(),
        local_text=local_text.relative_to(bundle).as_posix(),
        local_article_dir=folder.relative_to(bundle).as_posix(),
    )
    local_text.write_text(str(record.get("content_text") or ""), encoding="utf-8")

    # Update the canonical DB row with fields from the actual client event,
    # rather than the synthetic wrapper's fallback values.
    with repo.session_factory() as db:
        stored = db.query(Article).filter_by(id=saved.id).one()
        stored.sn = record["sn"]
        stored.title = record["title"]
        stored.author = record["author"]
        stored.digest = record["digest"]
        stored.cover_image_url = record["cover_url"]
        stored.source_url = record["article_url"]
        stored.canonical_url = record["article_url"]
        stored.content_html = record["content_fragment_html"] or stored.content_html
        stored.comment_id = record["comment_id"]
        stored.profile_ext_source = "fiddler_classic:/s"
        stored.field_provenance_json = json.dumps(record["field_provenance"], ensure_ascii=False)
        db.commit()

    evidence = {
        "schema_version": 1,
        "article_event": to_wire_event(article_event),
        "metric_event": to_wire_event(metric) if metric else None,
        "metric_fields": record["metric_fields"],
    }
    record["capture_evidence"] = (folder / "capture_evidence.json").relative_to(bundle).as_posix()
    dump(folder / "capture_evidence.json", evidence)
    dump(folder / "metadata.json", record)

    if metric:
        with repo.session_factory() as db:
            prior = any(
                json.loads(snapshot.raw_payload_json or "{}").get("event_id") == metric.get("event_id")
                for snapshot in db.query(ArticleMetric).filter_by(article_id=saved.id).all()
            )
        if not prior:
            snapshot = repo.insert_metrics_snapshot(
                saved.id,
                provider="wechat-client-http",
                provider_priority=120,
                raw_payload=metric,
                **metric["metrics"],
            )
            with repo.session_factory() as db:
                db.query(ArticleMetric).filter_by(id=snapshot.id).update({"captured_at": _time(metric.get("captured_at"))})
                db.commit()
    return record


def _load_or_create_identity(base: Path, catalog: dict) -> dict:
    path = base / "catalog_identity.json"
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            old = [key(item.get("title")) for item in value["items"]]
            new = [key(item.get("title")) for item in catalog["items"]]
            if old == new[:len(old)] and value.get("account", catalog.get("account")) == catalog.get("account"):
                value["items"].extend(dict(item, status="unresolved", biz="", mid="", idx=None, sn="", url="")
                                      for item in catalog["items"][len(old):])
                return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        raise RuntimeError("catalog_identity_changed")
    return {
        "schema_version": 1,
        "source_catalog": "catalog_verified_snapshot.json",
        "account": catalog.get("account", ""),
        "items": [dict(item, status="unresolved", biz="", mid="", idx=None, sn="", url="") for item in catalog["items"]],
    }


def _update_identity(identity: dict, index: int, event: dict, status: str) -> None:
    item = identity["items"][index]
    article = event.get("article") or {}
    item.update(
        status=status,
        biz=event.get("biz", ""),
        mid=event.get("mid", ""),
        idx=event.get("idx"),
        sn=event.get("sn", ""),
        url=article.get("url", ""),
        captured_at=event.get("captured_at", ""),
    )


def _rebuild_identity_from_events(identity: dict, events: list[dict], catalog: dict) -> None:
    by_title: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "article":
            by_title[key((event.get("article") or {}).get("title"))].append(event)
    for index, item in enumerate(catalog["items"]):
        candidates = by_title.get(key(item.get("title")), [])
        if candidates:
            _update_identity(identity, index, max(candidates, key=lambda e: str(e.get("captured_at", ""))), "captured")


def _state_complete(state: dict, index: int, bundle: Path, *, biz: str | None = None, item: dict | None = None) -> bool:
    job = state.get("jobs", {}).get(str(index), {})
    if job.get("status") != "captured":
        return False
    mid, idx = job.get("mid"), job.get("idx")
    if not str(mid or "").isdigit() or type(idx) is not int or idx < 1:
        return False
    folder = bundle / "articles" / f"{mid}_{idx}"
    try:
        if not folder.resolve().is_relative_to(bundle.resolve()):
            return False
        record = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        evidence = json.loads((folder / "capture_evidence.json").read_text(encoding="utf-8"))
        article = normalize_event(evidence["article_event"], {biz or record["biz"]})
        metric = normalize_event(evidence["metric_event"], {biz or record["biz"]})
        expected = (record["biz"], str(mid), idx, record["sn"])
        for event in (article, metric):
            if (event["biz"], str(event["mid"]), event["idx"], event["sn"]) != expected:
                return False
        if record["mid"] != str(mid) or record["idx"] != idx or not _metric_complete(metric):
            return False
        if record.get("metrics") != metric.get("metrics"):
            return False
        if not record.get("content_text") or not record.get("content_fragment_html"):
            return False
        for filename in ("article.html", "article.txt", "content_fragment.html"):
            if (folder / filename).stat().st_size == 0:
                return False
        if (folder / "article.txt").read_text(encoding="utf-8") != record["content_text"]:
            return False
        if (folder / "content_fragment.html").read_text(encoding="utf-8") != record["content_fragment_html"]:
            return False
        if item is not None and key(item.get("title")) != key(record.get("title")):
            return False
        return (job.get("article_event_id") == article.get("event_id")
                and job.get("metric_event_id") == metric.get("event_id"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _parse_indices(raw: str | None, total: int) -> list[int]:
    if not raw:
        return list(range(total))
    result = []
    for value in raw.split(","):
        number = int(value.strip())
        if not 0 <= number < total:
            raise ValueError("index_out_of_range")
        result.append(number)
    return list(dict.fromkeys(result))


def _repair_existing(repo, account, target, bundle: Path, inbox: Path, catalog: dict, state: dict, identity: dict, base: Path) -> int:
    if not inbox.exists():
        return 0
    events, issues = load_events(inbox, {target.biz})
    articles: dict[str, dict] = {}
    metrics: dict[tuple[str, str, int, str], list[dict]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "article":
            articles[key((event.get("article") or {}).get("title"))] = event
        elif event.get("kind") == "metrics" and event.get("metric_format") == "client_html_bootstrap_v1":
            metrics[_article_key(event)].append(event)
    repaired = 0
    for index, item in enumerate(catalog["items"]):
        article_event = articles.get(key(item.get("title")))
        if not article_event:
            continue
        candidates = metrics.get(_article_key(article_event), [])
        metric = max(candidates, key=lambda e: str(e.get("captured_at", ""))) if candidates else None
        record = persist(repo, account, target, article_event, metric, bundle)
        status = "captured" if _metric_complete(metric) else "partial"
        state.setdefault("jobs", {})[str(index)] = {
            "status": status,
            "title": item["title"],
            "mid": article_event["mid"],
            "idx": article_event["idx"],
            "article_event_id": article_event.get("event_id", ""),
            "metric_event_id": metric.get("event_id") if metric else None,
            "started_at": article_event.get("captured_at", ""),
        }
        _update_identity(identity, index, article_event, status)
        repaired += 1
    dump(base / "capture_state.json", state)
    dump(base / "catalog_identity.json", identity)
    print(json.dumps({"repair_count": repaired, "invalid_events": len(issues)}, ensure_ascii=False), flush=True)
    return repaired


def main(argv: list[str] | None = None, *, _capture_lock_held: bool = False) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=str(BASE))
    parser.add_argument("--pilot-dir", default=str(DEFAULT_PILOT))
    parser.add_argument("--biz", default=DEFAULT_BIZ)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--hwnd", type=int, default=DEFAULT_HWND)
    parser.add_argument("--indices")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--repair-existing", action="store_true")
    parser.add_argument("--force", action="store_true", help="重新捕获指定索引，即使已有完成状态")
    parser.add_argument("--capture-only", action="store_true", help="只缓存脱敏客户端响应，不建库、不导出CSV")
    parser.add_argument("--log-dir")
    parser.add_argument("--event-log")
    parser.add_argument("--stop-control")
    args = parser.parse_args(argv)
    if args.attempts < 1 or args.attempts > 4:
        raise ValueError("attempts_must_be_1_to_4")

    if args.limit < 0 or args.wait_seconds <= 0 or args.sleep_seconds < 0:
        raise ValueError("invalid_capture_bounds")
    if args.capture_only and args.repair_existing:
        raise ValueError("capture_only_repair_existing_conflict")
    with ExitStack() as locks:
        if not _capture_lock_held:
            locks.enter_context(runtime_lock(Path(args.pilot_dir) / "capture.lock"))
        locks.enter_context(runtime_lock(Path(args.base) / "run.lock"))
        return _run(args)


def _safe_catalog_evidence(base: Path, relative: str) -> dict:
    if not isinstance(relative, str) or not relative.strip() or "\\" in relative or ":" in relative:
        raise ValueError("unsafe_catalog_evidence")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_catalog_evidence")
    target = base / path
    for component in (target, *target.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("unsafe_catalog_evidence")
    if not target.resolve().is_relative_to(base.resolve()) or not target.is_file():
        raise ValueError("missing_catalog_evidence")
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid_catalog_evidence")
    return value


def _catalog_ready(catalog: dict, account: str, base: Path | None = None) -> bool:
    items = catalog.get("items")
    evidence_refs = catalog_evidence_refs(catalog, len(items)) if isinstance(items, list) else None
    if not (bool(account) and catalog.get("schema_version") == 2 and catalog.get("scope_observed") is True
            and catalog.get("account") == account and catalog.get("active_tab") == "全部"
            and catalog.get("terminal_visible") is True and not catalog.get("loading")
            and not catalog.get("load_error") and isinstance(items, list) and items
            and evidence_refs):
        return False
    try:
        single_scan = catalog_scan_metadata_valid(catalog.get("catalog_scan"), len(items))
        final_sequence = catalog_runtime_membership(items) if single_scan else catalog_sequence(items)
        if base is None:
            return all(isinstance(relative, str) and relative.strip() for _, relative in evidence_refs)
        observed_sequence = None
        for pass_number, relative in evidence_refs:
            parts = Path(relative).parts if isinstance(relative, str) else ()
            if (len(parts) < 4 or parts[0] != "catalog_scan"
                    or parts[-2] != f"pass_{pass_number:02d}"
                    or parts[-1] != "catalog_snapshot.json"):
                return False
            evidence = _safe_catalog_evidence(base, relative)
            if (evidence.get("schema_version") != 2 or evidence.get("scope_observed") is not True
                    or evidence.get("account") != account or evidence.get("active_tab") != "全部"
                    or evidence.get("terminal_visible") is not True or evidence.get("loading")
                    or evidence.get("load_error")):
                return False
            sequence = (catalog_runtime_membership(evidence.get("items")) if single_scan
                        else catalog_sequence(evidence.get("items")))
            if observed_sequence is None:
                observed_sequence = sequence
            elif sequence != observed_sequence:
                return False
        return observed_sequence is not None and (observed_sequence == final_sequence if single_scan
                                                  else set(observed_sequence) == set(final_sequence))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return False


def _run(args) -> int:
    base = Path(args.base).resolve()
    pilot_dir = Path(args.pilot_dir).resolve()
    capture_only = bool(getattr(args, "capture_only", False))
    bundle = base / "bundle"
    if not capture_only:
        bundle.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((base / "catalog_verified_snapshot.json").read_text(encoding="utf-8"))
    if not _catalog_ready(catalog, args.account, base):
        raise RuntimeError("catalog_not_ready")
    state_path = base / ("capture_cache_state.json" if capture_only else "capture_state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"jobs": {}}
    runtime_rebound = 0
    if capture_only:
        for index, item in enumerate(catalog["items"]):
            try:
                runtime_rebound += int(rebind_capture_entry_runtime(base, index, item, biz=args.biz))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
    recovered = _recover_interrupted_jobs(state)
    if recovered:
        dump(state_path, state)
    identity = None if capture_only else _load_or_create_identity(base, catalog)
    log_dir = Path(args.log_dir).resolve() if args.log_dir else base / "logs"
    setup_logging(log_dir)
    progress = ProgressLogger(event_path=Path(args.event_log).resolve() if args.event_log else base / "progress_events.jsonl")
    mode = "capture-only" if capture_only else "materialize"
    progress.info(f"客户端采集启动 | mode={mode} | catalog={len(catalog['items'])} | recovered={recovered} | runtime_rebound={runtime_rebound}")
    state["run"] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "recovered_jobs": recovered,
        "runtime_rebound": runtime_rebound,
    }
    dump(state_path, state)

    current_index: int | None = None
    try:
        repo = account = target = None
        if not capture_only:
            factory = create_session_factory("sqlite:///" + (bundle / "journal.db").as_posix())
            init_db(factory)
            repo = Repository(factory)
            targets = [target for target in parse_profile_links_file(str(PROJECT_ROOT / "data/sources/CSSCI来源刊公众号主页链接.md")) if target.biz == args.biz]
            if len(targets) != 1:
                raise RuntimeError("target_not_in_source_or_ambiguous")
            target = targets[0]
            repo.sync_target_accounts([target])
            account = next(account for account in repo.list_accounts(limit=None) if account.biz == args.biz)

        inbox = pilot_dir / "capture_inbox.jsonl"
        if args.repair_existing:
            _repair_existing(repo, account, target, bundle, inbox, catalog, state, identity, base)

        indices = _parse_indices(args.indices, len(catalog["items"]))
        if args.repair_existing and not args.indices:
            # Repair mode is intentionally non-interactive; it does not open the client.
            state["run"] = {**state.get("run", {}), "status": "repaired", "completed_at": datetime.now(timezone.utc).isoformat()}
            dump(state_path, state)
            progress.info("已有事件修复结束")
            if any((bundle / "articles").glob("*/metadata.json")):
                write_journal_csv_bundle(bundle)
            return 0
    except StopRequested:
        if current_index is not None and state.get("jobs", {}).get(str(current_index), {}).get("status") == "running":
            item = catalog["items"][current_index]
            job = state.setdefault("jobs", {}).setdefault(str(current_index), {"title": item["title"]})
            job.update({
                "status": "paused",
                "title": item["title"],
                "reason": "stop_requested",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        state["run"] = {
            **state.get("run", {}),
            "status": "paused",
            "reason": "stop_requested",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["run"].pop("completed_at", None)
        dump(state_path, state)
        progress.info("运行已按请求暂停 | reason=stop_requested")
        return 130
    except KeyboardInterrupt:
        state["run"] = {
            **state.get("run", {}),
            "status": "paused",
            "reason": "interrupted_by_user",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["run"].pop("completed_at", None)
        dump(state_path, state)
        return 130
    except Exception as exc:
        state["run"] = {
            **state.get("run", {}),
            "status": "failed",
            "reason": _safe_error(exc),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["run"].pop("completed_at", None)
        dump(state_path, state)
        progress.error(f"客户端初始化异常 | reason={_safe_error(exc)}")
        raise

    stop_control = Path(args.stop_control).resolve() if args.stop_control else None
    if stop_control is not None and is_stop_requested(stop_control):
        state["run"] = {
            **state.get("run", {}),
            "status": "paused",
            "reason": "stop_requested",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["run"].pop("completed_at", None)
        dump(state_path, state)
        return 130

    tail = Tail(inbox, args.biz)
    capture_started = time.monotonic()
    set_capture_enabled(str(pilot_dir), True, minutes=60)
    processed = 0
    try:
        if stop_control is not None and is_stop_requested(stop_control):
            raise StopRequested("stop_requested")
        nav("profile", hwnd=args.hwnd, account=args.account)
        for index in indices:
            current_index = index
            if stop_control is not None and is_stop_requested(stop_control):
                raise StopRequested("stop_requested")
            if args.limit and processed >= args.limit:
                break
            item = catalog["items"][index]
            already_complete = False
            if not args.force:
                if capture_only:
                    try:
                        already_complete = read_capture_entry(base, index, item, biz=args.biz)["complete"]
                    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                        already_complete = False
                else:
                    already_complete = _state_complete(state, index, bundle, biz=args.biz, item=item)
            if already_complete:
                processed += 1
                continue
            last_error = "no_verified_article_response"
            completed = False
            for attempt in range(1, args.attempts + 1):
                if stop_control is not None and is_stop_requested(stop_control):
                    raise StopRequested("stop_requested")
                state.setdefault("jobs", {})[str(index)] = {
                    "status": "running",
                    "title": item["title"],
                    "attempt": attempt,
                    "started_at": state.get("jobs", {}).get(str(index), {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                dump(state_path, state)
                progress.history_article_started(
                    args.account, current=index + 1, total=len(catalog["items"]), title=item["title"]
                )
                if time.monotonic() - capture_started > 45 * 60:
                    set_capture_enabled(str(pilot_dir), True, minutes=60)
                    capture_started = time.monotonic()
                opened = False
                try:
                    nav("profile", hwnd=args.hwnd, account=args.account)
                    nav("open", hwnd=args.hwnd, account=args.account, item=item)
                    opened = True
                    nav("reload", hwnd=args.hwnd, account=args.account, item=item)
                    article_event, metric, observed, capture_origin = _wait_or_seed_picture(
                        tail, item, biz=args.biz, seconds=args.wait_seconds, inbox=inbox
                    )
                    with (base / "network_events.jsonl").open("a", encoding="utf-8") as stream:
                        for event in observed:
                            stream.write(json.dumps(to_wire_event(event), ensure_ascii=False) + "\n")
                        stream.flush()
                        try:
                            os.fsync(stream.fileno())
                        except OSError:
                            pass
                    if article_event is None:
                        raise RuntimeError("no_verified_article_response")
                    if capture_only:
                        cached = write_capture_entry(base, index, item, article_event, metric, biz=args.biz, cache_origin=capture_origin)
                        status = "cached" if cached["complete"] else "partial"
                        metrics = metric.get("metrics", {}) if metric else {}
                        content_chars = len((article_event.get("article") or {}).get("content_html") or "")
                        image_count = 0
                    else:
                        record = persist(repo, account, target, article_event, metric, bundle)
                        status = "captured" if _metric_complete(metric) else "partial"
                        metrics = record["metrics"]
                        content_chars = len(record.get("content_text") or "")
                        image_count = len(record.get("image_urls") or [])
                    state.setdefault("jobs", {})[str(index)] = {
                        "status": status,
                        "title": item["title"],
                        "mid": article_event["mid"],
                        "idx": article_event["idx"],
                        "sn": article_event["sn"],
                        "article_event_id": article_event.get("event_id", ""),
                        "metric_event_id": metric.get("event_id") if metric else None,
                        "capture_origin": capture_origin,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "attempt": attempt,
                    }
                    if not capture_only:
                        _update_identity(identity, index, article_event, status)
                    state["jobs"][str(index)]["updated_at"] = datetime.now(timezone.utc).isoformat()
                    if status in {"cached", "captured"}:
                        state["jobs"][str(index)]["completed_at"] = datetime.now(timezone.utc).isoformat()
                    dump(state_path, state)
                    if not capture_only:
                        dump(base / "catalog_identity.json", identity)
                    progress.history_article_saved(
                        args.account,
                        current=index + 1,
                        total=len(catalog["items"]),
                        title=item["title"],
                        content_chars=content_chars,
                        image_count=image_count,
                        metrics_count=sum(value is not None for value in metrics.values()),
                    )
                    print(json.dumps({
                        "index": index,
                        "of": len(catalog["items"]),
                        "status": status,
                        "title": item["title"],
                        "mid": article_event["mid"],
                        "metrics": metrics,
                        "attempt": attempt,
                        "capture_origin": capture_origin,
                    }, ensure_ascii=False), flush=True)
                    if capture_only and status == "partial" and attempt < args.attempts:
                        last_error = "missing_metrics"
                        continue
                    completed = True
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    last_error = _safe_error(exc)
                    progress.history_article_failed(
                        args.account, current=index + 1, total=len(catalog["items"]), title=item["title"], error_type=type(exc).__name__
                    )
                    if attempt == args.attempts:
                        state.setdefault("jobs", {})[str(index)] = {
                            "status": "paused",
                            "title": item["title"],
                            "error_type": type(exc).__name__,
                            "reason": last_error,
                            "attempts": attempt,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                        }
                        state["jobs"][str(index)]["updated_at"] = datetime.now(timezone.utc).isoformat()
                        dump(state_path, state)
                        progress.checkpoint_saved(args.account, page=index, processed=processed, status="paused")
                        print(json.dumps({"index": index, "status": "paused", "reason": last_error}, ensure_ascii=False), flush=True)
                finally:
                    if opened:
                        try:
                            nav("return", hwnd=args.hwnd, account=args.account, item=item)
                        except Exception:
                            try:
                                nav("profile", hwnd=args.hwnd, account=args.account)
                            except Exception:
                                pass
                if not completed:
                    time.sleep(1.0)
            processed += 1
            if not capture_only and processed % 10 == 0 and any((bundle / "articles").glob("*/metadata.json")):
                write_journal_csv_bundle(bundle)
                progress.info(f"CSV已更新 | processed={processed}")
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
    except KeyboardInterrupt:
        if current_index is not None and state.get("jobs", {}).get(str(current_index), {}).get("status") == "running":
            item = catalog["items"][current_index]
            job = state.setdefault("jobs", {}).setdefault(str(current_index), {"title": item["title"]})
            job.update({
                "status": "paused",
                "title": item["title"],
                "reason": "interrupted_by_user",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            progress.info(f"运行已暂停 | index={current_index} | reason=interrupted_by_user")
            dump(state_path, state)
        state["run"] = {
            **state.get("run", {}),
            "status": "paused",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        dump(state_path, state)
        return 130
    except Exception as exc:
        state["run"] = {
            **state.get("run", {}),
            "status": "failed",
            "reason": _safe_error(exc),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        dump(state_path, state)
        progress.error(f"客户端采集异常 | reason={_safe_error(exc)}")
        raise
    finally:
        cleanup_errors = []
        try:
            set_capture_enabled(str(pilot_dir), False)
        except Exception as exc:
            cleanup_errors.append(_safe_error(exc))
        if not capture_only and any((bundle / "articles").glob("*/metadata.json")):
            try:
                write_journal_csv_bundle(bundle)
            except Exception as exc:
                cleanup_errors.append(_safe_error(exc))
        if cleanup_errors:
            state["run"] = {
                **state.get("run", {}),
                "cleanup_errors": cleanup_errors,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if state["run"].get("status") not in {"failed", "paused"}:
                state["run"]["status"] = "failed"
                state["run"]["reason"] = cleanup_errors[0]
                state["run"].pop("completed_at", None)
                dump(state_path, state)
                raise RuntimeError(cleanup_errors[0])
            dump(state_path, state)

    if capture_only:
        summary = build_capture_summary(base, catalog, biz=args.biz)
        dump(base / "capture_cache_summary.json", summary)
        retry_indices = summary["recapture_indices"]
        if not args.indices and not args.limit and retry_indices:
            progress.info(f"独立补采启动 | count={len(retry_indices)}")
            retry_args = argparse.Namespace(**vars(args))
            retry_args.indices = ",".join(str(index) for index in retry_indices)
            return _run(retry_args)
        complete = summary["complete"]
        traversed = summary["missing_count"] == 0
        run_status = "cache_ready" if complete else "cache_partial" if traversed else "paused"
    else:
        complete = bool(catalog["items"]) and all(
            _state_complete(state, index, bundle, biz=args.biz, item=item)
            for index, item in enumerate(catalog["items"])
        )
        traversed = complete
        run_status = "completed" if complete else "paused"
    state["run"] = {
        **state.get("run", {}),
        "status": run_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if complete:
        state["run"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        state["run"].pop("completed_at", None)
    dump(state_path, state)
    progress.info(f"客户端采集结束 | mode={mode} | status={run_status}")

    counts = defaultdict(int)
    for job in state.get("jobs", {}).values():
        counts[job.get("status", "unknown")] += 1
    if not capture_only:
        dump(base / "catalog_identity.json", identity)
    print(json.dumps({"state_counts": dict(counts), "catalog_count": len(catalog["items"])}, ensure_ascii=False), flush=True)
    return 0 if traversed else 1


if __name__ == "__main__":
    raise SystemExit(main())
