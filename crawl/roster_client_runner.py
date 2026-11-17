"""Sequential, resumable client capture for the validated fixed roster."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import unicodedata

from capture.setup import set_capture_target
from crawl.client_history_runner import _catalog_ready, dump, main as run_journal
from crawl.catalog_contract import catalog_evidence_refs, catalog_sequence
from crawl.client_history_runner import key
from crawl.desktop_wechat import DesktopSafetyError, DesktopWeChatDriver
from sources.fixed_roster import load_fixed_roster
from utils.progress_logger import ProgressLogger, setup_logging
from utils.runtime_lock import runtime_lock
from utils.run_control import is_stop_requested, reset_stop
from export.journal_csv import write_journal_csv_bundle
from crawl.background_materializer import run_background

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LINKS = PROJECT_ROOT / "data" / "sources" / "CSSCI来源刊公众号主页链接.md"
DEFAULT_RUN_DIR = PROJECT_ROOT / "output" / "rollout" / "client_20260906"
PROFILE_SWITCH_SCRIPT = PROJECT_ROOT / "crawl" / "profile_switch.ps1"


def safe_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE)
    return value.strip("._")[:80] or "journal"


def build_rollout_manifest(targets, run_dir: Path) -> dict:
    run_dir = Path(run_dir).resolve()
    journals_dir = run_dir / "journals"
    entries = []
    for ordinal, target in enumerate(targets, start=1):
        name = f"{ordinal:03d}_{safe_slug(target.journal_name)}"
        base = journals_dir / name
        entries.append({
            "ordinal": ordinal,
            "journal_name": target.journal_name,
            "account_name": target.account_name,
            "biz": target.biz,
            "profile_url": target.profile_url,
            "source_line": target.source_line,
            "base": str(base),
            "pilot_dir": str(run_dir / "capture"),
            "status": "pending",
        })
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_file": targets[0].source_file if targets else "",
        "expected_count": len(entries),
        "journals": entries,
    }


def _catalog_from_candidates(account_name: str, candidates: list[dict], terminal_visible: bool) -> dict:
    items = []
    seen = set()
    for candidate in candidates:
        title = str(candidate.get("title") or "").strip()
        runtime = str(candidate.get("runtime") or "").strip()
        if not title or not runtime or (title, runtime) in seen:
            continue
        seen.add((title, runtime))
        items.append({
            "title": title,
            "title_key": str(candidate.get("key") or title),
            "displayed_date": str(candidate.get("date") or ""),
            "runtime": runtime,
            "card_class": str(candidate.get("card_class") or ""),
        })
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account": account_name,
        "active_tab": "全部",
        "terminal_label": "已无更多消息" if terminal_visible else "",
        "terminal_visible": bool(terminal_visible),
        "items": items,
        "source": "wechat_uia_catalog_navigation_only",
    }


def _scan_terminal_from_frames(frames_path: Path) -> bool:
    if not frames_path.exists():
        return False
    last = None
    with frames_path.open('r', encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                last = json.loads(line)
    markers = (last or {}).get('markers') or []
    return any(str(marker).startswith(('没有更多', '已无更多')) for marker in markers)


def _switch_profile(entry: dict, hwnd: int) -> int:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(PROFILE_SWITCH_SCRIPT),
         "-Account", entry["account_name"]],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if result.returncode:
        raise RuntimeError("profile_switch_failed")
    if "\ufffd" in result.stdout:
        raise RuntimeError("profile_switch_invalid_output")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        browser_hwnd = payload["browser_hwnd"]
        if (payload.get("success") is not True or type(browser_hwnd) is not int or browser_hwnd <= 0
                or payload.get("document") != entry["account_name"] or payload.get("active_tab") != "全部"):
            raise ValueError("invalid_profile_result")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise RuntimeError("profile_switch_invalid_output") from None
    entry["browser_hwnd"] = browser_hwnd
    return browser_hwnd


def _validate_catalog(catalog: dict, account: str, *, require_evidence: bool = False) -> None:
    if (catalog.get("schema_version") != 2 or catalog.get("scope_observed") is not True
            or catalog.get("account") != account or catalog.get("active_tab") != "全部"
            or catalog.get("terminal_visible") is not True or not catalog.get("items")):
        raise RuntimeError("catalog_scope_unverified")
    for item in catalog["items"]:
        if not isinstance(item, dict) or not item.get("title") or not item.get("runtime"):
            raise RuntimeError("catalog_item_invalid")
    if require_evidence and catalog_evidence_refs(catalog, len(catalog["items"])) is None:
        raise RuntimeError("catalog_evidence_unverified")


def _select_catalog_consensus(snapshots: list[dict]) -> dict:
    groups: dict[tuple[tuple[str, str], ...], list[int]] = {}
    for index, snapshot in enumerate(snapshots):
        try:
            sequence = catalog_sequence(snapshot.get("items"))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        groups.setdefault(sequence, []).append(index)
    matching = [indices for indices in groups.values() if len(indices) >= 2]
    if not matching:
        raise RuntimeError("catalog_consensus_unverified")
    matching.sort(key=lambda indices: (len(indices), indices[-1]), reverse=True)
    if len(matching) > 1 and len(matching[0]) == len(matching[1]):
        raise RuntimeError("catalog_consensus_unverified")
    accepted = matching[0]
    result = dict(snapshots[accepted[-1]])
    result["catalog_consensus"] = {
        "required_matches": 2,
        "attempted_passes": len(snapshots),
        "matching_passes": [index + 1 for index in accepted],
        "item_count": len(result["items"]),
    }
    return result


def _refresh_catalog_identities(items) -> list[tuple[str, str, str]]:
    if not isinstance(items, list) or not items:
        raise ValueError("empty_catalog")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_catalog_item")
        title = key(item.get("title"))
        if not title:
            raise ValueError("invalid_catalog_item")
        normalized.append((title, key(item.get("displayed_date")), key(item.get("card_class"))))
    counts = Counter(title for title, _date, _card_class in normalized)
    identities = [
        (title, "", "") if counts[title] == 1 else (title, displayed_date, card_class)
        for title, displayed_date, card_class in normalized
    ]
    if any(not displayed_date and not card_class for title, displayed_date, card_class in identities if counts[title] > 1):
        raise ValueError("catalog_item_identity_ambiguous")
    if len(set(identities)) != len(identities):
        raise ValueError("catalog_item_identity_ambiguous")
    return identities


def _refresh_catalog_runtime(frozen: dict, current: dict) -> dict:
    try:
        old_identities = _refresh_catalog_identities(frozen.get("items"))
        current_identities = _refresh_catalog_identities(current.get("items"))
    except ValueError as exc:
        if str(exc) == "catalog_item_identity_ambiguous":
            raise RuntimeError("catalog_title_ambiguous") from None
        raise RuntimeError(str(exc)) from None
    current_by_identity = {identity: index for index, identity in enumerate(current_identities)}
    resolved = []
    for identity in old_identities:
        selected = current_by_identity.get(identity)
        if selected is None:
            raise RuntimeError("catalog_membership_changed")
        resolved.append(selected)
    used = set(resolved)
    # Never renumber persisted jobs. Newly discovered cards are appended in
    # the current scan order after all previously frozen jobs.
    resolved.extend(index for index in range(len(current["items"])) if index not in used)
    return dict(current, items=[dict(current["items"][index]) for index in resolved])


def _validated_resume_frames(path: Path, account: str) -> dict:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise ValueError("resume_frame_invalid")
    document_runtime = None
    seen = {}
    for expected_step, line in enumerate(lines):
        frame = json.loads(line)
        if frame.get("step") != expected_step:
            raise ValueError("resume_steps_not_contiguous")
        if (
            frame.get("schema_version") != 2
            or frame.get("account") != account
            or frame.get("active_tab") != "全部"
            or frame.get("scope_observed") is not True
            or frame.get("reached_tail") is True
            or not isinstance(frame.get("items"), list)
        ):
            raise ValueError("resume_frame_invalid")
        current_runtime = str(frame.get("document_runtime") or "")
        if not current_runtime:
            raise ValueError("resume_frame_invalid")
        if document_runtime is None:
            document_runtime = current_runtime
        elif document_runtime != current_runtime:
            raise ValueError("resume_document_changed")
        for item in frame["items"]:
            runtime = str((item or {}).get("runtime") or "")
            if not runtime:
                raise ValueError("resume_frame_invalid")
            if runtime in seen and seen[runtime] != item:
                raise ValueError("resume_runtime_identity_invalid")
            seen[runtime] = item
    return {
        "frame_count": len(lines),
        "document_runtime": document_runtime,
        "item_count": len(seen),
    }


def _discover_catalog(entry: dict, hwnd: int, max_steps: int) -> dict:
    base = Path(entry["base"])
    snapshot_path = base / "catalog_verified_snapshot.json"
    frozen = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else None
    base.mkdir(parents=True, exist_ok=True)
    cached_hwnd = int(entry.get("browser_hwnd") or 0)
    if frozen and cached_hwnd > 0 and _catalog_ready(frozen, entry["account_name"], base):
        try:
            DesktopWeChatDriver(cached_hwnd).focus()
            return frozen
        except DesktopSafetyError:
            pass
    # A cached runtime id or HWND is not valid evidence after the client window changes.
    hwnd = _switch_profile(entry, hwnd)
    scan_root = base / "catalog_scan" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    script = PROJECT_ROOT / "crawl" / "client_catalog.ps1"
    expected_first_title = str((frozen or {}).get("items", [{}])[0].get("title") or "") if (frozen or {}).get("items") else ""
    resume_path = None
    resume_info = None
    no_progress = 0
    result = None
    output_dir = None
    segment = 0
    for segment in range(1, 9):
        output_dir = scan_root / ("pass_01" if segment == 1 else f"resume_{segment:02d}")
        output_dir.mkdir(parents=True, exist_ok=False)
        command = [
            "powershell", "-NoProfile", "-File", str(script), "-WindowHandle", str(hwnd),
            "-Account", entry["account_name"], "-MaxSteps", str(max_steps), "-OutputDir", str(output_dir),
            "-ExpectedFirstTitle", expected_first_title,
        ]
        if resume_path is not None:
            command.extend([
                "-ResumeFramesPath", str(resume_path),
                "-ResumeFramesSha256", hashlib.sha256(resume_path.read_bytes()).hexdigest(),
            ])
        DesktopWeChatDriver(hwnd).focus()
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=max(300, max_steps * 10),
        )
        if result.returncode == 0:
            break
        candidate = output_dir / "catalog_frames.jsonl"
        try:
            candidate_info = _validated_resume_frames(candidate, entry["account_name"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise RuntimeError("catalog_scan_failed") from None
        if resume_info is not None and candidate_info["frame_count"] <= resume_info["frame_count"]:
            no_progress += 1
        else:
            no_progress = 0
        if resume_info is not None and candidate_info["document_runtime"] != resume_info["document_runtime"]:
            raise RuntimeError("catalog_scan_failed")
        if no_progress >= 3:
            raise RuntimeError("catalog_scan_failed")
        resume_path = candidate
        resume_info = candidate_info
    else:
        raise RuntimeError("catalog_scan_failed")
    snapshot = json.loads((output_dir / "catalog_snapshot.json").read_text(encoding="utf-8-sig"))
    _validate_catalog(snapshot, entry["account_name"])
    snapshot["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": len(snapshot["items"]),
        "evidence_path": (output_dir / "catalog_snapshot.json").relative_to(base).as_posix(),
    }
    if segment > 1:
        snapshot["catalog_resume"] = {
            "segments": segment,
            "interrupted_segments": segment - 1,
            "same_document_runtime": True,
        }
    snapshot.pop("catalog_consensus", None)
    if frozen:
        snapshot = _refresh_catalog_runtime(frozen, snapshot)
    _validate_catalog(snapshot, entry["account_name"], require_evidence=True)
    dump(snapshot_path, snapshot)
    return snapshot


def _run_media_bundle(entry: dict, workers: int) -> int:
    from crawl.bundle_media import run_bundle
    return run_bundle(workers=workers, bundle_root=Path(entry["base"]) / "bundle")


def _rebase_manifest_paths(manifest: dict, expected: dict, run_dir: Path) -> bool:
    """Move absolute manifest paths with the project when a handoff is relocated."""
    entries = manifest.get("journals") or []
    expected_entries = expected.get("journals") or []
    if not entries or len(entries) != len(expected_entries):
        return False
    old_base = next(
        (str(item.get("base") or "") for item in entries
         if Path(str(item.get("base") or "")).is_absolute()),
        "",
    )
    old_path = Path(old_base) if old_base else None
    parts = list(old_path.parts) if old_path else []
    try:
        journal_pos = next(i for i, part in enumerate(parts) if part.lower() == "journals")
    except StopIteration:
        journal_pos = -1
    if journal_pos <= 0:
        old_root = None
    else:
        old_root = Path(*parts[:journal_pos])
    if old_root is None or old_root == run_dir:
        return False
    old_prefix = str(old_root)
    changed = False

    def rebase(value):
        nonlocal changed
        if not isinstance(value, str):
            return value
        if value == old_prefix or value.startswith(old_prefix + os.sep) or value.startswith(old_prefix + "\\"):
            changed = True
            return str(run_dir) + value[len(old_prefix):]
        return value

    for entry, desired in zip(entries, expected_entries):
        for key in ("base", "pilot_dir", "cache_summary", "background_report", "last_audit",
                    "unavailable_csv"):
            if key in entry:
                entry[key] = rebase(entry.get(key))
        msaa = entry.get("msaa")
        if isinstance(msaa, dict):
            for key in ("base", "bundle", "completion_marker"):
                if key in msaa:
                    msaa[key] = rebase(msaa.get(key))
        if entry.get("base") != desired.get("base"):
            entry["base"] = desired.get("base")
            changed = True
        if entry.get("pilot_dir") != desired.get("pilot_dir"):
            entry["pilot_dir"] = desired.get("pilot_dir")
            changed = True
    source_file = str(manifest.get("source_file") or "")
    if source_file and Path(source_file).is_absolute():
        candidate = run_dir.parent.parent.parent / "data" / "sources" / Path(source_file).name
        if candidate.is_file() and str(candidate) != source_file:
            manifest["source_file"] = str(candidate)
            changed = True
    return changed


def _load_or_create_manifest(run_dir: Path, links: Path, expected_count: int) -> dict:
    roster = load_fixed_roster(links, expected_count=expected_count)
    expected = build_rollout_manifest(roster.targets, run_dir)
    manifest_path = run_dir / "run_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        fields = ("ordinal", "journal_name", "account_name", "biz", "profile_url", "base")
        try:
            rebased = _rebase_manifest_paths(manifest, expected, run_dir)
            actual = [tuple(entry[field] for field in fields) for entry in manifest["journals"]]
            desired = [tuple(entry[field] for field in fields) for entry in expected["journals"]]
            if (actual != desired or manifest.get("expected_count") != expected_count
                    or manifest.get("source_sha256", roster.source_sha256) != roster.source_sha256):
                raise ValueError("mismatch")
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("rollout_manifest_changed") from None
        manifest["source_sha256"] = roster.source_sha256
        if rebased:
            dump(manifest_path, manifest)
        return manifest
    expected["source_sha256"] = roster.source_sha256
    dump(manifest_path, expected)
    return expected

def _preflight(args) -> int:
    from crawl.client_capture_health import check_capture_health
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(PROFILE_SWITCH_SCRIPT), "-InspectOnly"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode or "\ufffd" in result.stdout:
        raise RuntimeError("search_window_inspection_failed")
    payload = json.loads(result.stdout)
    windows = payload.get("windows") or []
    search_windows = [
        window for window in windows
        if isinstance(window, dict) and window.get("search_input") is True
    ]
    if not search_windows:
        raise RuntimeError("search_window_required")
    if len(search_windows) > 1:
        raise RuntimeError("ambiguous_search_window")
    hwnd = search_windows[0].get("browser_hwnd")
    if type(hwnd) is not int or hwnd <= 0:
        raise RuntimeError("invalid_browser_window")
    pilot = Path(args.pilot_dir).resolve()
    metadata_path = pilot / "capture_activation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    health = check_capture_health(
        hwnd=hwnd, fiddler_exe=PROJECT_ROOT.parent / "Fiddler" / "Fiddler.exe",
        active_rules=Path(metadata["active_path"]), activation_metadata=metadata_path,
        control_path=pilot / "capture_control.json",
    )
    if not health["ok"]:
        raise RuntimeError("capture_health_failed")
    return hwnd


def _audit_entry(entry: dict) -> dict:
    from export.rollout_audit import audit_journal
    from crawl.client_history_runner import BASE, DEFAULT_BIZ
    write_journal_csv_bundle(Path(entry["base"]) / "bundle")
    baseline = BASE if entry["biz"] == DEFAULT_BIZ and (BASE / "bundle").is_dir() else None
    return audit_journal(Path(entry["base"]), expected_biz=entry["biz"], baseline_base=baseline)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--journal-links", default=str(DEFAULT_LINKS))
    parser.add_argument("--expected-count", type=int, default=126)
    parser.add_argument("--hwnd", type=int, default=0)
    parser.add_argument("--pilot-dir", default=str(PROJECT_ROOT / "output" / "pilot_20260906"))
    parser.add_argument("--max-journals", type=int, default=0)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=2000)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--media-workers", type=int, default=4)
    parser.add_argument("--phase", choices=["full", "capture-first", "background-only"], default="full")
    args = parser.parse_args(argv)
    if args.start < 1 or args.max_journals < 0 or not 1 <= args.media_workers <= 8:
        raise ValueError("invalid_rollout_range")
    with runtime_lock(Path(args.pilot_dir) / "capture.lock"):
        with runtime_lock(Path(args.run_dir) / "run.lock"):
            reset_stop(Path(args.run_dir) / "run_control.json")
            return _run(args)


def _error_reason(exc: Exception) -> str:
    text = str(exc)
    return text if re.fullmatch(r"[a-z][a-z0-9_]*", text) else type(exc).__name__


def _invalidate_completion(entry: dict, reason: str) -> None:
    completed_at = entry.pop("completed_at", None)
    if completed_at and "prior_completion_claim" not in entry:
        entry["prior_completion_claim"] = {
            "completed_at": completed_at,
            "invalidated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }


def _safe_audit_entry(entry: dict) -> dict:
    try:
        return _audit_entry(entry)
    except Exception as exc:
        return {"ok": False, "audit_path": None, "error": _error_reason(exc)}


def _selected_entries(manifest: dict, args) -> list[dict]:
    entries = manifest["journals"][args.start - 1:]
    if args.max_journals:
        entries = entries[:args.max_journals]
    return entries


def _read_cache_summary(entry: dict) -> dict:
    value = json.loads((Path(entry["base"]) / "capture_cache_summary.json").read_text(encoding="utf-8"))
    required = ("catalog_count", "complete_count", "partial_count", "missing_count", "complete", "recapture_indices")
    if not isinstance(value, dict) or any(name not in value for name in required):
        raise RuntimeError("invalid_capture_cache_summary")
    if any(type(value[name]) is not int or value[name] < 0 for name in required[:4]):
        raise RuntimeError("invalid_capture_cache_summary")
    if type(value["complete"]) is not bool or not isinstance(value["recapture_indices"], list):
        raise RuntimeError("invalid_capture_cache_summary")
    if value["complete_count"] + value["partial_count"] + value["missing_count"] != value["catalog_count"]:
        raise RuntimeError("invalid_capture_cache_summary")
    return value


def _run_background_phase(args, run_dir: Path, manifest: dict, progress: ProgressLogger) -> int:
    stop_control = run_dir / "run_control.json"
    needs_capture = False
    failed = False
    entries = _selected_entries(manifest, args)
    progress.info(f"后台物化启动 | selected={len(entries)} | expected={manifest['expected_count']}")
    for entry in entries:
        if is_stop_requested(stop_control):
            progress.info("后台物化已按请求暂停 | reason=stop_requested")
            return 130
        entry["status"] = "background_running"
        entry["phase"] = "background-only"
        entry["background_started_at"] = datetime.now(timezone.utc).isoformat()
        entry.pop("reason", None)
        _invalidate_completion(entry, "background_rerun_started")
        dump(run_dir / "run_manifest.json", manifest)
        try:
            code = int(run_background(
                base=entry["base"], biz=entry["biz"], account=entry["account_name"],
                media_workers=args.media_workers, journal_links=args.journal_links,
            ) or 0)
            entry["background_return_code"] = code
            report_path = Path(entry["base"]) / "background_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            entry["background_report"] = str(report_path)
            entry["last_audit"] = report.get("audit_path")
            if code == 0:
                entry["status"] = "completed"
                entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            elif code == 2:
                entry["status"] = "needs_capture"
                entry["reason"] = "capture_cache_incomplete"
                entry.pop("completed_at", None)
                needs_capture = True
            else:
                entry["status"] = "background_failed"
                entry["reason"] = str(report.get("reason") or "background_materialization_failed")
                entry.pop("completed_at", None)
                failed = True
        except KeyboardInterrupt:
            entry["status"] = "paused"
            entry["reason"] = "interrupted_by_user"
            dump(run_dir / "run_manifest.json", manifest)
            return 130
        except Exception as exc:
            entry["status"] = "background_failed"
            entry["reason"] = _error_reason(exc)
            entry.pop("completed_at", None)
            failed = True
        entry["background_updated_at"] = datetime.now(timezone.utc).isoformat()
        dump(run_dir / "run_manifest.json", manifest)
        progress.info(f"后台期刊结束 | ordinal={entry['ordinal']} | status={entry['status']}")
    progress.info("后台物化结束")
    return 1 if failed else 2 if needs_capture else 0


def _run(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    setup_logging(run_dir / "logs")
    progress = ProgressLogger(event_path=run_dir / "progress_events.jsonl")
    manifest = _load_or_create_manifest(run_dir, Path(args.journal_links).resolve(), args.expected_count)
    phase = getattr(args, "phase", "full")
    if phase == "background-only":
        return _run_background_phase(args, run_dir, manifest, progress)
    try:
        _preflight(args)
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]*", str(exc)) else type(exc).__name__
        progress.error(f"启动检查未通过 | reason={reason} | 请登录微信并打开搜一搜和Fiddler")
        return 1
    entries = _selected_entries(manifest, args)
    stop_control = run_dir / "run_control.json"
    cache_partial = False
    progress.info(f"126刊轮转启动 | phase={phase} | selected={len(entries)} | expected={manifest['expected_count']}")
    for entry in entries:
        if is_stop_requested(stop_control):
            progress.info("轮转已按请求暂停 | reason=stop_requested")
            return 130
        if phase == "capture-first" and entry.get("status") == "cache_ready":
            try:
                summary = _read_cache_summary(entry)
                if summary["complete"]:
                    progress.info(f"跳过已完整缓存期刊 | ordinal={entry['ordinal']}")
                    continue
            except Exception:
                pass
        if phase == "full" and entry.get("status") == "completed":
            audit = _safe_audit_entry(entry)
            entry["last_audit"] = audit.get("audit_path")
            if audit["ok"]:
                progress.info(f"跳过已验收期刊 | ordinal={entry['ordinal']}")
                continue
            entry["status"] = "needs_verification"
            entry["reason"] = "audit_failed"
            if audit.get("error"):
                entry["last_audit_error"] = audit["error"]
            _invalidate_completion(entry, "audit_failed")
            dump(run_dir / "run_manifest.json", manifest)
        entry["status"] = "running"
        entry["phase"] = phase
        entry["started_at"] = datetime.now(timezone.utc).isoformat()
        entry.pop("reason", None)
        _invalidate_completion(entry, "rerun_started")
        dump(run_dir / "run_manifest.json", manifest)
        try:
            set_capture_target(args.pilot_dir, entry["biz"])
            catalog = _discover_catalog(entry, args.hwnd, args.max_steps)
            browser_hwnd = int(entry.get("browser_hwnd") or args.hwnd)
            dump(run_dir / "run_manifest.json", manifest)
            progress.info(f"目录已冻结 | ordinal={entry['ordinal']} | articles={len(catalog['items'])}")
            journal_argv = [
                "--base", entry["base"], "--pilot-dir", args.pilot_dir, "--biz", entry["biz"],
                "--account", entry["account_name"], "--hwnd", str(browser_hwnd),
                "--wait-seconds", str(args.wait_seconds), "--attempts", str(args.attempts),
                "--sleep-seconds", str(args.sleep_seconds), "--log-dir", str(run_dir / "logs"),
                "--event-log", str(run_dir / "progress_events.jsonl"),
                "--stop-control", str(stop_control),
            ]
            if phase == "capture-first":
                journal_argv.append("--capture-only")
            result = run_journal(journal_argv, _capture_lock_held=True)
            entry["return_code"] = int(result or 0)
            entry["article_count"] = len(catalog["items"])
            if result == 130 or is_stop_requested(stop_control):
                entry["status"] = "paused"
                entry["reason"] = "stop_requested"
                dump(run_dir / "run_manifest.json", manifest)
                progress.info(f"期刊暂停 | ordinal={entry['ordinal']} | reason=stop_requested")
                return 130
            if phase == "capture-first":
                summary = None
                if result != 0:
                    try:
                        summary = _read_cache_summary(entry)
                    except Exception:
                        entry["status"] = "paused"
                        entry["reason"] = "capture_cache_incomplete"
                        dump(run_dir / "run_manifest.json", manifest)
                        progress.info(f"缓存期刊暂停 | ordinal={entry['ordinal']} | return_code={result}")
                        return 1
                    if summary["missing_count"] == 0:
                        entry["status"] = "paused"
                        entry["reason"] = "capture_cache_result_inconsistent"
                        dump(run_dir / "run_manifest.json", manifest)
                        progress.info(f"缓存期刊暂停 | ordinal={entry['ordinal']} | return_code={result}")
                        return 1
                if summary is None:
                    summary = _read_cache_summary(entry)
                entry["cache_catalog_count"] = summary["catalog_count"]
                entry["cache_complete_count"] = summary["complete_count"]
                entry["cache_partial_count"] = summary["partial_count"]
                entry["cache_missing_count"] = summary["missing_count"]
                entry["cache_recapture_indices"] = summary["recapture_indices"]
                entry["cache_summary"] = str(Path(entry["base"]) / "capture_cache_summary.json")
                entry["status"] = "cache_ready" if summary["complete"] else "cache_partial"
                if not summary["complete"]:
                    entry["reason"] = "metrics_cache_incomplete"
                    cache_partial = True
                entry.pop("completed_at", None)
                dump(run_dir / "run_manifest.json", manifest)
                progress.info(
                    f"缓存期刊结束 | ordinal={entry['ordinal']} | status={entry['status']} | "
                    f"complete={summary['complete_count']}/{summary['catalog_count']}"
                )
                continue
            state_path = Path(entry["base"]) / "capture_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            jobs = state.get("jobs", {})
            complete = result == 0 and len(jobs) == len(catalog["items"]) and all(job.get("status") == "captured" for job in jobs.values())
            if not complete:
                entry["status"] = "paused"
                dump(run_dir / "run_manifest.json", manifest)
                progress.info(f"期刊结束 | ordinal={entry['ordinal']} | status=paused | articles={len(jobs)}/{len(catalog['items'])}")
                return 1
            if is_stop_requested(stop_control):
                entry["status"] = "paused"
                entry["reason"] = "stop_requested"
                dump(run_dir / "run_manifest.json", manifest)
                return 130
            entry["status"] = "media_running"
            dump(run_dir / "run_manifest.json", manifest)
            progress.info(f"媒体下载启动 | ordinal={entry['ordinal']} | workers={args.media_workers}")
            media_code = _run_media_bundle(entry, args.media_workers)
            entry["media_status"] = "complete" if media_code == 0 else "partial"
            audit = _safe_audit_entry(entry)
            entry["last_audit"] = audit.get("audit_path")
            accepted = media_code == 0 and audit["ok"]
            if accepted:
                entry["status"] = "completed"
                entry.pop("reason", None)
                entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            elif media_code != 0:
                entry["status"] = "paused"
                entry["reason"] = "media_incomplete"
                entry.pop("completed_at", None)
            else:
                entry["status"] = "needs_verification"
                entry["reason"] = "audit_failed"
                entry.pop("completed_at", None)
            if audit.get("error"):
                entry["last_audit_error"] = audit["error"]
            entry["media_return_code"] = media_code
            dump(run_dir / "run_manifest.json", manifest)
            progress.info(f"期刊结束 | ordinal={entry['ordinal']} | status={entry['status']} | articles={len(jobs)}/{len(catalog['items'])} | media={entry['media_status']}")
            if not accepted:
                return 1
        except KeyboardInterrupt:
            entry["status"] = "paused"
            entry["reason"] = "interrupted_by_user"
            dump(run_dir / "run_manifest.json", manifest)
            return 130
        except Exception as exc:
            entry["status"] = "paused"
            entry["reason"] = _error_reason(exc)
            dump(run_dir / "run_manifest.json", manifest)
            progress.error(f"期刊暂停 | ordinal={entry['ordinal']} | reason={entry['reason']}")
            return 1
    progress.info(f"126刊轮转结束 | phase={phase}")
    return 2 if phase == "capture-first" and cache_partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
