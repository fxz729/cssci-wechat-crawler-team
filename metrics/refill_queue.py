"""Independent refill queue for one frozen client-captured journal.

This module never rescans the WeChat catalog.  It selects only articles whose
saved evidence does not pass the same completeness check used by the primary
client runner, then refreshes media, CSV files, and the strict audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from capture.reader import METRIC_FIELDS
from capture.setup import set_capture_target, set_diagnostic_enabled
from crawl.bundle_media import run_bundle
from crawl.client_history_runner import _catalog_ready, _state_complete, dump, main as run_client_history
from export.journal_csv import write_journal_csv_bundle
from export.rollout_audit import audit_journal


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("invalid_refill_input")
    return value


def _missing_metric_fields(bundle: Path, job: dict) -> list[str]:
    mid, idx = job.get("mid"), job.get("idx")
    if not str(mid or "").isdigit() or type(idx) is not int or idx < 1:
        return list(METRIC_FIELDS)
    path = bundle / "articles" / f"{mid}_{idx}" / "metadata.json"
    try:
        record = _read_json(path)
        metrics = record.get("metrics") or {}
        if not isinstance(metrics, dict):
            return list(METRIC_FIELDS)
        return [field for field in METRIC_FIELDS if metrics.get(field) is None]
    except (OSError, ValueError, TypeError, RuntimeError):
        return list(METRIC_FIELDS)


def build_refill_queue(base: str | Path, *, biz: str) -> dict:
    base = Path(base).resolve()
    catalog = _read_json(base / "catalog_verified_snapshot.json")
    if not _catalog_ready(catalog, str(catalog.get("account") or ""), base):
        raise RuntimeError("catalog_scope_unverified")
    state_path = base / "capture_state.json"
    state = _read_json(state_path) if state_path.exists() else {"jobs": {}}
    jobs = state.get("jobs") or {}
    if not isinstance(jobs, dict):
        raise RuntimeError("capture_state_invalid")
    bundle = base / "bundle"
    items = []
    for index, catalog_item in enumerate(catalog["items"]):
        if _state_complete(state, index, bundle, biz=biz, item=catalog_item):
            continue
        job = jobs.get(str(index)) or {}
        missing = _missing_metric_fields(bundle, job)
        if job.get("status") == "partial" and missing:
            reason = "missing_metrics"
        else:
            reason = str(job.get("reason") or "missing_or_invalid_capture")
        items.append(
            {
                "index": index,
                "title": str(catalog_item.get("title") or ""),
                "job_status": str(job.get("status") or "missing"),
                "reason": reason,
                "missing_metric_fields": missing,
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": str(base),
        "biz": biz,
        "catalog_count": len(catalog["items"]),
        "indices": [item["index"] for item in items],
        "items": items,
    }


def run_refill(
    *,
    base: str | Path,
    pilot_dir: str | Path,
    biz: str,
    account: str,
    hwnd: int,
    wait_seconds: float = 60.0,
    attempts: int = 2,
    sleep_seconds: float = 1.0,
    media_workers: int = 4,
    offline_only: bool = False,
    baseline_base: str | Path | None = None,
) -> int:
    base = Path(base).resolve()
    pilot_dir = Path(pilot_dir).resolve()
    queue = build_refill_queue(base, biz=biz)
    dump(base / "refill_queue.json", queue)
    capture_code = 0
    if queue["indices"]:
        if offline_only:
            dump(
                base / "refill_report.json",
                {
                    "schema_version": 1,
                    "status": "client_capture_required",
                    "remaining_indices": queue["indices"],
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return 2
        argv = [
            "--base", str(base),
            "--pilot-dir", str(pilot_dir),
            "--biz", biz,
            "--account", account,
            "--hwnd", str(hwnd),
            "--indices", ",".join(str(index) for index in queue["indices"]),
            "--wait-seconds", str(wait_seconds),
            "--attempts", str(attempts),
            "--sleep-seconds", str(sleep_seconds),
        ]
        set_capture_target(pilot_dir, biz)
        set_diagnostic_enabled(pilot_dir, True, minutes=60, after_session_id=0)
        try:
            capture_code = int(run_client_history(argv) or 0)
        finally:
            set_diagnostic_enabled(pilot_dir, False)

    queue_after = build_refill_queue(base, biz=biz)
    dump(base / "refill_queue.json", queue_after)
    media_code = int(run_bundle(workers=media_workers, bundle_root=base / "bundle") or 0)
    if any((base / "bundle" / "articles").glob("*/metadata.json")):
        write_journal_csv_bundle(base / "bundle")
    baseline = Path(baseline_base).resolve() if baseline_base else None
    audit = audit_journal(base, expected_biz=biz, baseline_base=baseline)
    accepted = capture_code == 0 and not queue_after["indices"] and media_code == 0 and audit.get("ok") is True
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if accepted else "needs_refill",
        "capture_return_code": capture_code,
        "remaining_indices": queue_after["indices"],
        "media_return_code": media_code,
        "audit_ok": audit.get("ok") is True,
        "audit_path": audit.get("audit_path"),
    }
    dump(base / "refill_report.json", report)
    return 0 if accepted else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--pilot-dir", required=True)
    parser.add_argument("--biz", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--hwnd", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--media-workers", type=int, default=4)
    parser.add_argument("--offline-only", action="store_true")
    parser.add_argument("--baseline-base")
    args = parser.parse_args(argv)
    if args.attempts < 1 or args.attempts > 4 or not 1 <= args.media_workers <= 8:
        raise ValueError("invalid_refill_bounds")
    return run_refill(**vars(args))


if __name__ == "__main__":
    raise SystemExit(main())
