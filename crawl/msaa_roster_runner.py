"""One-click MSAA roster runner.

Flow per journal:
    name search -> open official-account profile -> find H5 profile window
    -> MSAA/Fiddler per-article capture -> existing bundle/CSV import.

The existing run manifest, per-journal base directories and historical bundle
files are reused. The runner never deletes or moves old output.
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from capture.setup import set_capture_enabled, set_capture_target
from crawl.msaa_capture import (
    DEFAULT_CAPTURE_INBOX,
    DEFAULT_JOURNAL_LINKS,
    fill_missing_identity,
    fill_missing_metrics,
    import_rows,
    normalize_title,
    run_capture,
    _read_jsonl,
)
from crawl.roster_client_runner import _load_or_create_manifest
from utils.run_control import is_stop_requested, reset_stop
from utils.runtime_lock import runtime_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_SWITCH = PROJECT_ROOT / "crawl" / "profile_switch.ps1"
OPEN_SEARCH_WINDOW = PROJECT_ROOT / "crawl" / "open_search_window.ps1"
SEED_BIZ = "MzUzNzUwMzQ0OA=="
DEFAULT_SEED_DIR = PROJECT_ROOT / "output" / "v12_validation" / "manual_ui_capture"
DEFAULT_RUN_DIR = PROJECT_ROOT / "output" / "rollout" / "full_126"


def _atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _process_name(process_id: int) -> str:
    """Return the executable stem for a PID using only Win32 calls."""
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(process_id))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).stem.lower()
    finally:
        kernel32.CloseHandle(handle)


def find_h5_profile_windows() -> list[int]:
    """Return visible H5SubscriptionProfileWnd handles owned by WeChat."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != "H5SubscriptionProfileWnd":
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if _process_name(process_id.value) == "wechat":
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return handles


def _parse_last_json(text: str) -> dict:
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def switch_profile_by_name(account_name: str, *, wait_seconds: int = 30) -> int:
    """Search by name and return the H5 profile window only after name match."""
    open_command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(OPEN_SEARCH_WINDOW), "-WaitSeconds", str(int(wait_seconds)),
    ]
    try:
        opened = subprocess.run(
            open_command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=max(5, int(wait_seconds) + 5),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("search_window_open_timeout") from None
    open_payload = _parse_last_json(opened.stdout.decode("utf-8-sig", errors="replace"))
    if open_payload.get("success") is not True:
        raise RuntimeError(str(open_payload.get("error_code") or "search_window_open_failed"))
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(PROFILE_SWITCH),
        "-Account", str(account_name), "-ForMsaa", "-WaitSeconds", str(int(wait_seconds)),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=max(5, int(wait_seconds) + 5),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("profile_search_timeout") from None
    payload = _parse_last_json(result.stdout.decode("utf-8-sig", errors="replace"))
    if payload.get("success") is True:
        profile_hwnd = int(payload.get("profile_hwnd") or 0)
        if profile_hwnd > 0:
            return profile_hwnd
        raise RuntimeError("profile_window_missing")
    error = str(payload.get("error_code") or "").strip()
    if error:
        raise RuntimeError(error)
    if result.returncode != 0:
        raise RuntimeError("profile_search_failed")
    raise RuntimeError("profile_search_invalid_response")


def _switch_profile_with_retry(
    account_name: str, *, wait_seconds: int = 30, attempts: int = 3,
) -> int:
    """Retry the search/navigation race without touching an existing article."""
    last_error: RuntimeError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return switch_profile_by_name(account_name, wait_seconds=wait_seconds)
        except RuntimeError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(2)
    if last_error is not None:
        raise last_error
    raise RuntimeError("profile_search_failed")


def _seed_capture_dir(seed_dir: Path, target_dir: Path, biz: str, seed_biz: str) -> list[str]:
    """Copy the pilot JSONL/state without moving or changing the source."""
    if biz != seed_biz or not Path(seed_dir).is_dir():
        return []
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("articles.jsonl", "state.json"):
        source = Path(seed_dir) / name
        target = target_dir / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)
            copied.append(name)
    return copied


def _archive_capture_inbox(capture_inbox: Path, *, ordinal: int, biz: str) -> Path | None:
    """Compress and clear a finished inbox while preserving all raw records."""
    source = Path(capture_inbox)
    if not source.is_file() or source.stat().st_size <= 0:
        return None
    archive_dir = source.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_biz = re.sub(r"[^A-Za-z0-9_-]", "_", str(biz))[:24] or "unknown"
    target = archive_dir / f"capture_inbox_{int(ordinal):03d}_{safe_biz}_{stamp}.jsonl.gz"
    temporary = target.with_name(target.name + ".tmp")
    try:
        with source.open("rb") as src, gzip.open(temporary, "wb", compresslevel=1) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        with gzip.open(temporary, "rb") as check:
            check.read(1)
        os.replace(temporary, target)
        with source.open("wb") as stream:
            stream.truncate(0)
        return target
    except Exception:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        fallback = target.with_suffix(".jsonl")
        try:
            shutil.copy2(source, fallback)
            if fallback.stat().st_size != source.stat().st_size:
                raise OSError("archive_copy_size_mismatch")
            with source.open("wb") as stream:
                stream.truncate(0)
            return fallback
        except Exception:
            try:
                if fallback.exists():
                    fallback.unlink()
            except OSError:
                pass
            return None


def _stop_requested(run_dir: Path) -> bool:
    control = Path(run_dir) / "run_control.json"
    try:
        return bool(is_stop_requested(control))
    except TypeError:
        if not control.exists():
            return False
        try:
            value = json.loads(control.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return False
        return bool(value.get("stop_requested") or value.get("action") in {"stop", "pause"})


def _completion_marker_path(entry: dict) -> Path:
    return Path(entry["base"]) / "bundle" / "complete.json"


def _queue_independent_backfill(entry: dict, run_dir: Path, *, reason: str) -> bool:
    path = Path(run_dir) / "journal_backfill_queue.jsonl"
    key = (str(entry.get("ordinal") or ""), str(entry.get("biz") or ""))
    if path.is_file():
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing_key = (
                    str(existing.get("ordinal") or ""),
                    str(existing.get("biz") or ""),
                )
                if existing_key == key:
                    return False
    record = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "ordinal": int(entry.get("ordinal") or 0),
        "journal_name": str(entry.get("journal_name") or ""),
        "biz": str(entry.get("biz") or ""),
        "base": str(entry.get("base") or ""),
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def _write_completion_marker(entry: dict, summary: dict) -> Path:
    marker = _completion_marker_path(entry)
    _atomic_json(marker, {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "imported": summary.get("imported"),
        "article_count": (summary.get("csv") or {}).get("article_count"),
        "csv": "文章数据.csv",
    })
    return marker


def _entry_completed(entry: dict) -> bool:
    """Return true only for a real completed journal, never a partial CSV."""
    base = Path(entry["base"])
    state_path = base / "msaa" / "state.json"
    csv_path = base / "bundle" / "文章数据.csv"
    state = None
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            state = None
    if _completion_marker_path(entry).is_file():
        return True
    if state is None or not csv_path.is_file():
        return False
    return bool(state.get("completed"))


def _import_entry(entry: dict, args, *, capture_inbox: Path | None = None) -> dict:
    msaa_dir = Path(entry["base"]) / "msaa"
    seeded = _seed_capture_dir(
        Path(args.seed_dir), msaa_dir, entry["biz"], args.seed_biz,
    )
    articles = msaa_dir / "articles.jsonl"
    rows = _read_jsonl(articles)
    if not rows:
        raise RuntimeError("msaa_articles_missing")
    inbox = Path(capture_inbox or args.capture_inbox)
    filled_identity = fill_missing_identity(rows, inbox, entry["biz"])
    filled_metrics = fill_missing_metrics(rows, inbox, entry["biz"])
    summary = import_rows(
        rows,
        base=Path(entry["base"]),
        journal_links=Path(args.journal_links),
        account_name=entry["account_name"],
        expected_biz=entry["biz"],
        media_workers=int(args.media_workers),
    )
    summary.update(
        seeded_files=seeded,
        filled_missing_identity=filled_identity,
        filled_missing_metrics=filled_metrics,
    )
    return summary


def _capture_entry(entry: dict, args) -> dict:
    """Run UI capture for one journal and archive its Fiddler inbox."""
    msaa_dir = Path(entry["base"]) / "msaa"
    _seed_capture_dir(Path(args.seed_dir), msaa_dir, entry["biz"], args.seed_biz)
    profile_hwnd = _switch_profile_with_retry(
        entry["account_name"], wait_seconds=int(args.profile_wait_seconds),
    )
    pilot_dir = Path(args.capture_inbox).parent
    control = pilot_dir / "capture_control.json"
    if not control.exists():
        raise RuntimeError("capture_control_missing")
    set_capture_target(pilot_dir, entry["biz"])
    set_capture_enabled(pilot_dir, True, minutes=60)
    return_code = run_capture(
        profile_hwnd=profile_hwnd,
        biz=entry["biz"],
        capture_inbox=Path(args.capture_inbox),
        output_dir=msaa_dir,
        delay_seconds=int(args.delay_seconds),
        wait_seconds=int(args.wait_seconds),
        scroll_delta=int(args.scroll_delta),
        max_idle_scrolls=int(args.max_idle_scrolls),
        journal_name=entry.get("journal_name") or entry.get("account_name") or "",
        profile_url=entry.get("profile_url") or "",
        refresh_article=bool(getattr(args, "refresh_article", False)),
        start_from_top=bool(getattr(args, "start_from_top", False)),
    )
    archive = _archive_capture_inbox(
        Path(args.capture_inbox),
        ordinal=int(entry.get("ordinal") or 0),
        biz=entry["biz"],
    )
    return {
        "return_code": return_code,
        "profile_hwnd": profile_hwnd,
        "import_inbox": str(archive or Path(args.capture_inbox)),
        "archive": str(archive) if archive else "",
    }


def _run_entry(entry: dict, args) -> dict:
    """Capture and import synchronously (kept for import checks and tests)."""
    captured = _capture_entry(entry, args)
    summary = _import_entry(
        entry, args, capture_inbox=Path(captured["import_inbox"]),
    )
    summary["capture_return_code"] = captured["return_code"]
    summary["profile_hwnd"] = captured["profile_hwnd"]
    if captured["return_code"] == 0:
        marker = _write_completion_marker(entry, summary)
        summary["completion_marker"] = str(marker)
    else:
        summary["completion_marker"] = ""
    return summary


def _start_import_process(entry: dict, args, captured: dict) -> dict:
    msaa_dir = Path(entry["base"]) / "msaa"
    msaa_dir.mkdir(parents=True, exist_ok=True)
    log_path = msaa_dir / "import.log"
    command = [
        sys.executable, "-X", "utf8", "-B", "-m", "crawl.msaa_capture",
        "--base", str(Path(entry["base"]).resolve()),
        "--journal-links", str(Path(args.journal_links).resolve()),
        "--biz", str(entry["biz"]),
        "--account", str(entry["account_name"]),
        "--articles", str((msaa_dir / "articles.jsonl").resolve()),
        "--capture-inbox", str(Path(captured["import_inbox"]).resolve()),
    ]
    media_workers = int(getattr(args, "media_workers", 0) or 0)
    if media_workers > 0:
        command.extend(["--media-workers", str(media_workers)])
    creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if os.name == "nt" else 0
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except Exception:
        log_handle.close()
        raise
    return {
        "entry": entry,
        "process": process,
        "log_path": log_path,
        "log_handle": log_handle,
        "captured": captured,
    }


def _finish_import(pending: dict, *, run_dir: Path, manifest: dict, progress: Path) -> bool:
    process = pending["process"]
    return_code = process.poll()
    if return_code is None:
        return False
    try:
        pending["log_handle"].close()
    except OSError:
        pass
    entry = pending["entry"]
    captured = pending.get("captured") or {}
    base = Path(entry["base"])
    summary: dict = {}
    report = base / "msaa_import_report.json"
    if report.is_file():
        try:
            value = json.loads(report.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                summary = value
        except (OSError, ValueError):
            summary = {}
    summary["capture_return_code"] = captured.get("return_code")
    summary["profile_hwnd"] = captured.get("profile_hwnd")
    if return_code == 0:
        marker = _write_completion_marker(entry, summary)
        summary["completion_marker"] = str(marker)
        entry["status"] = "completed"
        entry["completed_at"] = datetime.now(timezone.utc).isoformat()
        record_status = "completed"
    else:
        summary["completion_marker"] = ""
        entry["status"] = "msaa_failed"
        entry["reason"] = f"import_return_code_{return_code}"
        record_status = "msaa_failed"
    entry["msaa"] = summary
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "ordinal": entry["ordinal"],
        "journal_name": entry["journal_name"],
        "status": record_status,
        "imported": summary.get("imported"),
    }
    if record_status != "completed":
        record["reason"] = entry["reason"]
    with progress.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    _atomic_json(run_dir / "run_manifest.json", manifest)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return True


def run(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    with runtime_lock(run_dir / "msaa_run.lock"):
        if not args.preflight_only:
            reset_stop(run_dir / "run_control.json")
        return _run_locked(args, run_dir)


def _run_locked(args, run_dir: Path) -> int:
    manifest = _load_or_create_manifest(
        run_dir, Path(args.journal_links).resolve(), int(args.expected_count),
    )
    entries = manifest["journals"][int(args.start) - 1:]
    if int(args.max_journals) > 0:
        entries = entries[:int(args.max_journals)]
    progress = run_dir / "msaa_progress.jsonl"
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.preflight_only:
        results = []
        for entry in entries:
            try:
                profile_hwnd = switch_profile_by_name(
                    entry["account_name"], wait_seconds=int(args.profile_wait_seconds),
                )
                record = {
                    "ordinal": entry["ordinal"],
                    "journal_name": entry["journal_name"],
                    "status": "ok",
                    "profile_hwnd": profile_hwnd,
                }
            except Exception as exc:
                record = {
                    "ordinal": entry["ordinal"],
                    "journal_name": entry["journal_name"],
                    "status": "failed",
                    "reason": str(exc) or type(exc).__name__,
                }
            results.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if record["status"] != "ok":
                break
        _atomic_json(run_dir / "msaa_preflight.json", {"results": results})
        return 0 if all(record["status"] == "ok" for record in results) else 1
    pending_import = None

    def settle_pending(*, wait: bool = False) -> bool:
        nonlocal pending_import
        if pending_import is None:
            return False
        if wait:
            while pending_import["process"].poll() is None:
                time.sleep(2)
        entry = pending_import["entry"]
        if _finish_import(
            pending_import, run_dir=run_dir, manifest=manifest, progress=progress,
        ):
            failed = entry.get("status") == "msaa_failed"
            pending_import = None
            return failed
        return False

    failed_ordinals: list = []
    for entry in entries:
        if settle_pending():
            failed_ordinals.append(entry.get("ordinal"))
        if args.skip_completed and not getattr(args, "backfill", False) and (
            _entry_completed(entry) or entry.get("status") in {"needs_backfill"}
        ):
            continue
        if _stop_requested(run_dir):
            settle_pending(wait=True)
            if not _entry_completed(entry):
                entry["status"] = "paused"
                entry["reason"] = "stop_requested"
            _atomic_json(run_dir / "run_manifest.json", manifest)
            return 130
        entry["status"] = "msaa_running"
        entry["msaa_started_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(run_dir / "run_manifest.json", manifest)
        try:
            if args.import_only:
                summary = _import_entry(entry, args)
                marker = _write_completion_marker(entry, summary)
                summary["completion_marker"] = str(marker)
                entry["status"] = "completed"
                entry["completed_at"] = datetime.now(timezone.utc).isoformat()
                entry["msaa"] = summary
                record = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "ordinal": entry["ordinal"],
                    "journal_name": entry["journal_name"],
                    "status": "completed",
                    "imported": summary.get("imported"),
                }
            else:
                captured = _capture_entry(entry, args)
                if captured["return_code"] != 0:
                    state_stop_reason = ""
                    try:
                        state_value = json.loads(
                            (Path(entry["base"]) / "msaa" / "state.json").read_text(encoding="utf-8-sig")
                        )
                        if isinstance(state_value, dict):
                            state_stop_reason = str(state_value.get("stop_reason") or "")
                    except (OSError, ValueError):
                        state_stop_reason = ""
                    if state_stop_reason.startswith("capture_stalled_no_articles:"):
                        entry["status"] = "needs_backfill"
                        entry["reason"] = "requires_independent_backfill"
                        _queue_independent_backfill(
                            entry,
                            run_dir,
                            reason="requires_independent_backfill",
                        )
                    else:
                        entry["status"] = "msaa_partial"
                        entry["reason"] = f"capture_return_code_{captured['return_code']}"
                    entry["msaa"] = captured
                    record = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "ordinal": entry["ordinal"],
                        "journal_name": entry["journal_name"],
                        "status": entry["status"],
                        "reason": entry["reason"],
                        "archive": captured.get("archive"),
                    }
                else:
                    if pending_import is not None and settle_pending(wait=True):
                        return 1
                    pending_import = _start_import_process(entry, args, captured)
                    entry["status"] = "import_running"
                    entry["msaa"] = captured
                    record = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "ordinal": entry["ordinal"],
                        "journal_name": entry["journal_name"],
                        "status": "import_running",
                        "archive": captured.get("archive"),
                    }
        except Exception as exc:
            reason = str(exc) if str(exc) else type(exc).__name__
            entry["status"] = "msaa_failed"
            entry["reason"] = reason
            record = {
                "time": datetime.now(timezone.utc).isoformat(),
                "ordinal": entry["ordinal"],
                "journal_name": entry["journal_name"],
                "status": "msaa_failed",
                "reason": reason,
            }
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        _atomic_json(run_dir / "run_manifest.json", manifest)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if entry.get("status") in {"msaa_partial", "msaa_failed"}:
            failed_ordinals.append(entry.get("ordinal"))
            settle_pending(wait=True)
            continue
    if settle_pending(wait=True):
        failed_ordinals.append(None)
    return 1 if failed_ordinals else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--journal-links", type=Path, default=DEFAULT_JOURNAL_LINKS)
    parser.add_argument("--expected-count", type=int, default=126)
    parser.add_argument("--start", type=int, default=3)
    parser.add_argument("--max-journals", type=int, default=0)
    parser.add_argument("--capture-inbox", type=Path, default=DEFAULT_CAPTURE_INBOX)
    parser.add_argument("--seed-dir", type=Path, default=DEFAULT_SEED_DIR)
    parser.add_argument("--seed-biz", default=SEED_BIZ)
    parser.add_argument("--profile-wait-seconds", type=int, default=30)
    parser.add_argument("--delay-seconds", type=int, default=2)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--scroll-delta", type=int, default=-120)
    parser.add_argument("--max-idle-scrolls", type=int, default=40)
    parser.add_argument("--media-workers", type=int, default=4)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--skip-completed", action="store_true")
    parser.add_argument("--refresh-article", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start-from-top", action="store_true")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
