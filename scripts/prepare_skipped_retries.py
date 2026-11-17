#!/usr/bin/env python
"""Queue per-article skipped titles for independent retry without reopening journals."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


try:
    csv.field_size_limit(100 * 1024 * 1024)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_atomic(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8-sig",
    )
    os.replace(temporary, path)


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        next(reader, None)
        return sum(1 for _ in reader)


def _pending_backfill_keys(run_dir: Path) -> set[tuple[str, str]]:
    queue_path = Path(run_dir) / "journal_backfill_queue.jsonl"
    if not queue_path.is_file():
        return set()
    done: set[str] = set()
    progress_path = Path(run_dir) / "backfill_progress.json"
    if progress_path.is_file():
        try:
            value = read_json(progress_path)
        except (OSError, ValueError):
            value = {}
        done = {str(item) for item in (value.get("done") or []) if item}
    pending = set()
    for record in _read_retry_records(queue_path):
        ordinal = str(record.get("ordinal") or "")
        biz = str(record.get("biz") or "")
        if f"{ordinal}:{biz}" in done:
            continue
        pending.add((ordinal, biz))
    return pending


def finalize_completed_csv_entries(manifest: dict, run_dir: Path, *, dry_run: bool = False) -> list[dict]:
    finalized = []
    pending_backfill = _pending_backfill_keys(run_dir)
    for entry in manifest.get("journals", []):
        base = Path(entry["base"])
        state_path = base / "msaa" / "state.json"
        csv_path = base / "bundle" / "文章数据.csv"
        marker_path = base / "bundle" / "complete.json"
        if not state_path.is_file() or not csv_path.is_file():
            continue
        state = read_json(state_path)
        if state.get("completed") is not True:
            continue
        if (
            str(entry.get("ordinal") or ""),
            str(entry.get("biz") or ""),
        ) in pending_backfill:
            continue
        skipped = [str(item) for item in (state.get("skipped_titles") or []) if item]
        marker = None
        if marker_path.is_file():
            try:
                marker = read_json(marker_path)
            except (OSError, ValueError):
                marker = None
        marker_valid = bool(
            isinstance(marker, dict)
            and marker.get("completed_with_skips") == bool(skipped)
            and marker.get("skipped_count") == len(skipped)
            and isinstance(marker.get("article_count"), int)
        )
        if marker_valid:
            article_count = marker["article_count"]
            marker_repaired = False
        else:
            report_path = base / "msaa_import_report.json"
            report = read_json(report_path) if report_path.is_file() else {}
            article_count = (report.get("csv") or {}).get("article_count")
            if not isinstance(article_count, int):
                article_count = count_csv_rows(csv_path)
            marker = {
                "schema_version": 1,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "imported": report.get("imported", article_count),
                "article_count": article_count,
                "csv": "文章数据.csv",
                "completed_with_skips": bool(skipped),
                "skipped_count": len(skipped),
            }
            marker_repaired = True
        desired_status = "completed_with_skips" if skipped else "completed"
        desired_reason = "skipped_articles_queued" if skipped else ""
        status_reconciled = (
            entry.get("status") != desired_status or entry.get("reason") != desired_reason
        )
        if not marker_repaired and not status_reconciled:
            continue
        finalized.append({
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "article_count": article_count,
            "skipped_count": len(skipped),
            "state_path": str(state_path),
            "marker_path": str(marker_path),
            "status": desired_status,
            "marker_repaired": marker_repaired,
            "status_reconciled": status_reconciled,
        })
        if dry_run:
            continue
        if marker_repaired:
            write_json_atomic(marker_path, marker)
        entry["status"] = desired_status
        entry["reason"] = desired_reason
        if marker.get("completed_at"):
            entry["completed_at"] = marker["completed_at"]
        if skipped:
            entry["skipped_retry_count"] = len(skipped)
    return finalized


def reconcile_cached_import_markers(manifest: dict, *, dry_run: bool = False) -> list[dict]:
    changed = []
    for entry in manifest.get("journals", []):
        marker_path = Path(entry["base"]) / "bundle" / "complete.json"
        if not marker_path.is_file():
            continue
        marker = read_json(marker_path)
        if marker.get("source") != "cached_msaa_import":
            continue
        verified_tail = marker.get("verified_tail") is True
        status = "completed" if verified_tail else "completed_with_skips"
        reason = "cached_import_verified" if verified_tail else "cached_import_unverified_tail"
        if (entry.get("status") == status and entry.get("reason") == reason
                and entry.get("cached_import") is True):
            continue
        changed.append({
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "status": status,
            "reason": reason,
            "marker_path": str(marker_path),
        })
        if not dry_run:
            entry["status"] = status
            entry["reason"] = reason
            entry["cached_import"] = True
    return changed


def latest_backup(path: Path, pattern: str) -> Path | None:
    candidates = [candidate for candidate in path.parent.glob(pattern) if candidate.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)


def skipped_titles(state: dict) -> list[str]:
    return [str(item) for item in (state.get("skipped_titles") or []) if item]


def append_retry_queue(queue_path: Path, records: list[dict], *, dry_run: bool = False) -> int:
    existing: set[tuple[int, str]] = set()
    if queue_path.is_file():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            try:
                existing.add((int(record.get("ordinal") or 0), str(record.get("title") or "")))
            except (TypeError, ValueError):
                continue
    additions = []
    for record in records:
        key = (int(record.get("ordinal") or 0), str(record.get("title") or ""))
        if key not in existing:
            existing.add(key)
            additions.append(record)
    if additions and not dry_run:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with queue_path.open("a", encoding="utf-8", newline="\n") as stream:
            for record in additions:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(additions)


def normalize_title(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()


def _read_retry_records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _resolve_retry_entry(manifest: dict, record: dict) -> dict | None:
    journals = [entry for entry in manifest.get("journals", []) if isinstance(entry, dict)]
    ordinal = str(record.get("ordinal") or "").strip()
    biz = str(record.get("biz") or "").strip()
    journal_name = str(record.get("journal_name") or "").strip()
    if ordinal:
        for entry in journals:
            if str(entry.get("ordinal") or "").strip() == ordinal:
                return entry
    if biz:
        for entry in journals:
            if str(entry.get("biz") or "").strip() == biz:
                return entry
    if journal_name:
        for entry in journals:
            names = {
                str(entry.get("journal_name") or "").strip(),
                str(entry.get("account_name") or "").strip(),
            }
            if journal_name in names:
                return entry
    return None


def _append_backfill_queue(path: Path, record: dict, *, dry_run: bool = False) -> bool:
    key = (str(record.get("ordinal") or ""), str(record.get("biz") or ""))
    if path.is_file():
        for existing in _read_retry_records(path):
            existing_key = (
                str(existing.get("ordinal") or ""),
                str(existing.get("biz") or ""),
            )
            if existing_key == key:
                return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def prepare_article_retry_journals(
    manifest: dict,
    run_dir: Path,
    *,
    dry_run: bool = False,
) -> list[dict]:
    run_dir = Path(run_dir)
    sources = (
        (run_dir / "skipped_retry_queue.jsonl", "article_retry_queue"),
        (run_dir / "metrics_retry_queue.jsonl", "metrics_retry_queue"),
    )
    groups: dict[tuple[str, str], dict] = {}
    for queue_path, reason in sources:
        for record in _read_retry_records(queue_path):
            title = normalize_title(record.get("title"))
            if not title:
                continue
            entry = _resolve_retry_entry(manifest, record)
            if entry is None:
                continue
            key = (
                str(entry.get("ordinal") or ""),
                str(entry.get("biz") or ""),
            )
            group = groups.setdefault(key, {"entry": entry, "titles": set(), "reasons": set()})
            group["titles"].add(title)
            group["reasons"].add(reason)
    changed = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for group in groups.values():
        entry = group["entry"]
        titles = group["titles"]
        state_path = Path(entry["base"]) / "msaa" / "state.json"
        removed_skipped = 0
        removed_processed = 0
        if state_path.is_file():
            state = read_json(state_path)
            skipped = [
                str(item) for item in (state.get("skipped_titles") or [])
                if str(item) and normalize_title(item) not in titles
            ]
            processed = [
                str(item) for item in (state.get("processed_titles") or [])
                if str(item) and normalize_title(item) not in titles
            ]
            original_skipped = [str(item) for item in (state.get("skipped_titles") or []) if str(item)]
            original_processed = [str(item) for item in (state.get("processed_titles") or []) if str(item)]
            removed_skipped = len(original_skipped) - len(skipped)
            removed_processed = len(original_processed) - len(processed)
            if (removed_skipped or removed_processed) and not dry_run:
                backup_path = state_path.with_name(
                    f"{state_path.name}.retry_queue_backup_{stamp}"
                )
                if not backup_path.exists():
                    shutil.copy2(state_path, backup_path)
                state["skipped_titles"] = skipped
                state["processed_titles"] = processed
                state["processed_count"] = len(processed)
                if normalize_title(state.get("last_processed_title")) in titles:
                    state["last_processed_title"] = ""
                write_json_atomic(state_path, state)
        record = {
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "biz": str(entry.get("biz") or ""),
            "base": str(entry.get("base") or ""),
            "reason": (
                "article_retry_queue"
                if "article_retry_queue" in group["reasons"]
                else "metrics_retry_queue"
            ),
            "reasons": sorted(group["reasons"]),
            "titles": sorted(titles),
        }
        appended = _append_backfill_queue(
            run_dir / "journal_backfill_queue.jsonl",
            record,
            dry_run=dry_run,
        )
        changed.append({
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "removed_skipped": removed_skipped,
            "removed_processed": removed_processed,
            "queued": appended,
            "reasons": sorted(group["reasons"]),
        })
    return changed


def prepare_skipped_retries(manifest: dict, run_dir: Path, *, dry_run: bool = False) -> list[dict]:
    queue_path = run_dir / "skipped_retry_queue.jsonl"
    changed = []
    for entry in manifest.get("journals", []):
        base = Path(entry["base"])
        state_path = base / "msaa" / "state.json"
        csv_path = base / "bundle" / "文章数据.csv"
        marker_path = base / "bundle" / "complete.json"
        if not state_path.is_file() or not csv_path.is_file():
            continue

        state = read_json(state_path)
        skipped = skipped_titles(state)
        restored_from = ""
        if not skipped:
            state_backup = latest_backup(state_path, "state.json.retry_backup_*")
            if state_backup is not None:
                backup_state = read_json(state_backup)
                backup_skipped = skipped_titles(backup_state)
                current_titles = [str(item) for item in (state.get("processed_titles") or []) if item]
                backup_titles = [str(item) for item in (backup_state.get("processed_titles") or []) if item]
                current_last = str(state.get("last_processed_title") or "")
                # 只恢复旧版 prepare 留下的“清空 skipped + 断点指向首个 skipped”形态。
                # 绝不覆盖比备份更新的 state，否则会丢失本次已采文章。
                looks_like_old_mutation = (
                    len(current_titles) <= len(backup_titles)
                    and (not current_last or current_last in backup_skipped)
                )
                if backup_skipped and looks_like_old_mutation:
                    skipped = backup_skipped
                    state = backup_state
                    restored_from = str(state_backup)
                    if not dry_run:
                        shutil.copy2(state_backup, state_path)
        if not skipped:
            continue
        if state.get("completed") is not True:
            if restored_from:
                changed.append({
                    "ordinal": int(entry.get("ordinal") or 0),
                    "journal_name": str(entry.get("journal_name") or ""),
                    "skipped": len(skipped),
                    "queued": 0,
                    "restored_from": restored_from,
                    "state_path": str(state_path),
                    "queue_path": str(queue_path),
                    "restored_only": True,
                })
                if not dry_run:
                    entry["status"] = "pending_skip_retry"
                    entry["reason"] = "partial_capture_resume"
            continue

        if not marker_path.is_file():
            marker_backup = latest_backup(marker_path, "complete.json.retry_backup_*")
            if marker_backup is not None and not dry_run:
                shutil.copy2(marker_backup, marker_path)

        queued_at = datetime.now(timezone.utc).isoformat()
        records = [{
            "queued_at": queued_at,
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "title": title,
            "biz": str(entry.get("biz") or ""),
            "base": str(base),
            "state_path": str(state_path),
        } for title in skipped]
        queued = append_retry_queue(queue_path, records, dry_run=dry_run)
        changed.append({
            "ordinal": int(entry.get("ordinal") or 0),
            "journal_name": str(entry.get("journal_name") or ""),
            "skipped": len(skipped),
            "queued": queued,
            "restored_from": restored_from,
            "state_path": str(state_path),
            "queue_path": str(queue_path),
        })
        if dry_run:
            continue
        entry["status"] = "completed_with_skips"
        entry["reason"] = "skipped_articles_queued"
        entry["skipped_retry_count"] = len(skipped)
        entry["skipped_retry_queue"] = str(queue_path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "run_manifest.json"
    manifest = read_json(manifest_path)
    article_retry = prepare_article_retry_journals(manifest, run_dir, dry_run=args.dry_run)
    cached = reconcile_cached_import_markers(manifest, dry_run=args.dry_run)
    changed = prepare_skipped_retries(manifest, run_dir, dry_run=args.dry_run)
    finalized = finalize_completed_csv_entries(manifest, run_dir, dry_run=args.dry_run)

    if (cached or changed or finalized) and not args.dry_run:
        write_json_atomic(manifest_path, manifest)

    print(json.dumps({
        "dry_run": bool(args.dry_run),
        "changed_count": len(changed),
        "cached_import_count": len(cached),
        "finalized_count": len(finalized),
        "article_retry_count": len(article_retry),
        "article_retries": article_retry,
        "cached_imports": cached,
        "finalized": finalized,
        "journals": changed,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
