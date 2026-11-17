"""Import already-captured MSAA article rows into journal bundles.

This is the no-UI recovery path for journals whose raw articles.jsonl exists
but whose bundle/CSV was not completed before the capture process stopped.
It never deletes or rewrites raw capture state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl.msaa_capture import (  # noqa: E402
    _read_jsonl,
    fill_missing_identity,
    fill_missing_metrics,
    import_rows,
)


SKIP_STATUSES = {"msaa_running", "import_running", "needs_backfill"}
BACKFILL_QUEUE = "journal_backfill_queue.jsonl"
DEFAULT_JOURNAL_LINKS = PROJECT_ROOT / "data" / "sources" / "CSSCI来源刊公众号主页链接.md"
DEFAULT_CAPTURE_INBOX = PROJECT_ROOT / "output" / "pilot_20260906" / "capture_inbox.jsonl"


def _write_json_atomic(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _count_rows(path: Path) -> int:
    if not Path(path).is_file():
        return 0
    with Path(path).open(encoding="utf-8-sig", errors="replace") as stream:
        return sum(1 for line in stream if line.strip())


def select_cached_entries(
    manifest: dict,
    *,
    min_rows: int = 20,
    ordinals: set[int] | None = None,
    force: bool = False,
    allow_unverified: bool = False,
) -> list[dict]:
    selected = []
    for entry in manifest.get("journals", []):
        try:
            ordinal = int(entry.get("ordinal"))
        except (TypeError, ValueError):
            continue
        if ordinals is not None and ordinal not in ordinals:
            continue
        if str(entry.get("status") or "") in SKIP_STATUSES:
            continue
        base = Path(str(entry.get("base") or ""))
        articles = base / "msaa" / "articles.jsonl"
        marker = base / "bundle" / "complete.json"
        if marker.is_file() and not force:
            continue
        if _count_rows(articles) < int(min_rows):
            continue
        state_path = base / "msaa" / "state.json"
        state_completed = False
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8-sig"))
                state_completed = state.get("completed") is True
            except (OSError, ValueError):
                state_completed = False
        # Automatic startup recovery must never mark a partially captured journal
        # complete. Unverified caches require an explicit operator opt-in.
        if not state_completed and not allow_unverified:
            continue
        selected.append(entry)
    return selected


def _append_jsonl_unique(path: Path, record: dict, *, key_fields: tuple[str, ...]) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = tuple(str(record.get(field) or "") for field in key_fields)
    if path.is_file():
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tuple(str(existing.get(field) or "") for field in key_fields) == key:
                    return False
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def mark_cached_import(entry: dict, *, summary: dict, run_dir: Path) -> bool:
    """Mark a cached import complete and queue independent tail verification."""
    base = Path(str(entry["base"]))
    state_path = base / "msaa" / "state.json"
    marker_path = base / "bundle" / "complete.json"
    state = {}
    if state_path.is_file():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                state = value
        except (OSError, ValueError):
            state = {}
    if marker_path.is_file():
        return False
    skipped = [str(item) for item in (state.get("skipped_titles") or []) if item]
    verified_tail = state.get("completed") is True
    _write_json_atomic(marker_path, {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": "cached_msaa_import",
        "verified_tail": bool(verified_tail),
        "imported": summary.get("imported"),
        "article_count": (summary.get("csv") or {}).get("article_count"),
        "csv": "文章数据.csv",
        "skipped_titles": skipped,
    })
    if not verified_tail:
        _append_jsonl_unique(
            Path(run_dir) / BACKFILL_QUEUE,
            {
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "ordinal": int(entry.get("ordinal") or 0),
                "journal_name": str(entry.get("journal_name") or entry.get("account_name") or ""),
                "biz": str(entry.get("biz") or ""),
                "reason": "cached_import_unverified_tail",
                "base": str(base),
                "cached_articles": int(summary.get("imported") or 0),
            },
            key_fields=("ordinal", "biz", "reason"),
        )
    return True


def import_one(
    entry: dict,
    *,
    journal_links: Path,
    capture_inbox: Path,
    media_workers: int = 0,
) -> dict:
    base = Path(str(entry["base"]))
    rows = _read_jsonl(base / "msaa" / "articles.jsonl")
    if not rows:
        raise RuntimeError("cached_articles_empty")
    filled_identity = fill_missing_identity(rows, Path(capture_inbox), str(entry.get("biz") or ""))
    filled_metrics = fill_missing_metrics(rows, Path(capture_inbox), str(entry.get("biz") or ""))
    summary = import_rows(
        rows,
        base=base,
        journal_links=Path(journal_links),
        account_name=str(entry.get("account_name") or entry.get("journal_name") or ""),
        expected_biz=str(entry.get("biz") or ""),
        media_workers=int(media_workers),
    )
    summary["filled_missing_identity"] = filled_identity
    summary["filled_missing_metrics"] = filled_metrics
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=PROJECT_ROOT / "output" / "rollout" / "full_126")
    parser.add_argument("--journal-links", type=Path, default=DEFAULT_JOURNAL_LINKS)
    parser.add_argument("--capture-inbox", type=Path, default=DEFAULT_CAPTURE_INBOX)
    parser.add_argument("--min-rows", type=int, default=20)
    parser.add_argument("--ordinals", default="")
    parser.add_argument("--media-workers", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-unverified", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-mark", action="store_true")
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    ordinals = {
        int(value) for value in str(args.ordinals).split(",") if value.strip().isdigit()
    } or None
    entries = select_cached_entries(
        manifest, min_rows=int(args.min_rows), ordinals=ordinals, force=bool(args.force),
        allow_unverified=bool(args.allow_unverified),
    )
    results = []
    for entry in entries:
        result = {
            "ordinal": entry.get("ordinal"),
            "journal_name": entry.get("journal_name"),
            "status": "dry_run" if args.dry_run else "imported",
        }
        try:
            if not args.dry_run:
                summary = import_one(
                    entry,
                    journal_links=args.journal_links.resolve(),
                    capture_inbox=args.capture_inbox.resolve(),
                    media_workers=int(args.media_workers),
                )
                result.update({
                    "imported": summary.get("imported"),
                    "article_count": (summary.get("csv") or {}).get("article_count"),
                    "filled_missing_identity": summary.get("filled_missing_identity"),
                    "filled_missing_metrics": summary.get("filled_missing_metrics"),
                })
                if not args.no_mark:
                    result["marked"] = mark_cached_import(entry, summary=summary, run_dir=run_dir)
        except Exception as exc:  # noqa: BLE001 - continue other journals
            result.update({"status": "failed", "error": f"{type(exc).__name__}:{exc}"})
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    report_path = run_dir / "cached_import_results.json"
    _write_json_atomic(report_path, {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "selected": len(entries),
        "results": results,
    })
    failures = sum(1 for item in results if item.get("status") == "failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
