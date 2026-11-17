"""Offline materialization stage for sanitized capture-first caches."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from capture.reader import METRIC_FIELDS
from crawl.bundle_media import run_bundle
from crawl.capture_cache import build_capture_summary, read_capture_entry
from crawl.capture_materialization import persist_capture
from crawl.catalog_contract import (catalog_evidence_refs, catalog_runtime_membership, catalog_scan_metadata_valid,
                                    catalog_sequence, normalize_catalog_text)
from export.journal_csv import write_journal_csv_bundle
from export.rollout_audit import audit_journal
from sources.journal_catalog import parse_profile_links_file
from storage.database import create_session_factory, init_db
from storage.repository import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LINKS = PROJECT_ROOT / "data" / "sources" / "CSSCI来源刊公众号主页链接.md"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_catalog_evidence(base: Path, relative: str) -> dict:
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("invalid_catalog_evidence_path")
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("invalid_catalog_evidence_path")
    path = base / Path(relative)
    if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
        raise ValueError("invalid_catalog_evidence_path")
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_snapshot_ready(catalog: dict, account: str, base: Path) -> bool:
    items = catalog.get("items") if isinstance(catalog, dict) else None
    evidence_refs = catalog_evidence_refs(catalog, len(items)) if isinstance(items, list) else None
    if not (catalog.get("schema_version") == 2 and catalog.get("scope_observed") is True
            and catalog.get("account") == account and catalog.get("active_tab") == "全部"
            and catalog.get("terminal_visible") is True and not catalog.get("loading")
            and not catalog.get("load_error") and isinstance(items, list) and items
            and evidence_refs):
        return False
    try:
        single_scan = catalog_scan_metadata_valid(catalog.get("catalog_scan"), len(items))
        final = catalog_runtime_membership(items) if single_scan else catalog_sequence(items)
        observed = None
        for pass_number, relative in evidence_refs:
            parts = Path(relative).parts
            if (len(parts) < 4 or parts[0] != "catalog_scan"
                    or parts[-2] != f"pass_{pass_number:02d}" or parts[-1] != "catalog_snapshot.json"):
                return False
            evidence = _safe_catalog_evidence(base, relative)
            if (evidence.get("schema_version") != 2 or evidence.get("scope_observed") is not True
                    or evidence.get("account") != account or evidence.get("active_tab") != "全部"
                    or evidence.get("terminal_visible") is not True or evidence.get("loading")
                    or evidence.get("load_error")):
                return False
            sequence = (catalog_runtime_membership(evidence.get("items")) if single_scan
                        else catalog_sequence(evidence.get("items")))
            if observed is None:
                observed = sequence
            elif observed != sequence:
                return False
        return observed is not None and (observed == final if single_scan else set(observed) == set(final))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _open_repository(base: Path, biz: str, account: str, journal_links: Path = DEFAULT_LINKS):
    bundle = base / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    factory = create_session_factory("sqlite:///" + (bundle / "journal.db").as_posix())
    init_db(factory)
    repo = Repository(factory)
    targets = [target for target in parse_profile_links_file(str(journal_links)) if target.biz == biz]
    if len(targets) != 1 or targets[0].account_name != account:
        raise RuntimeError("target_not_in_source_or_ambiguous")
    target = targets[0]
    repo.sync_target_accounts([target])
    account_obj = next(row for row in repo.list_accounts(limit=None) if row.biz == biz)
    return repo, account_obj, target


def materialized_entry_complete(state: dict, index: int, bundle: Path, *, biz: str, item: dict) -> bool:
    job = (state.get("jobs") or {}).get(str(index), {})
    if job.get("status") != "captured":
        return False
    mid, idx, sn = str(job.get("mid") or ""), job.get("idx"), str(job.get("sn") or "")
    if not mid.isdigit() or type(idx) is not int or idx < 1 or not sn:
        return False
    try:
        record = json.loads((bundle / "articles" / f"{mid}_{idx}" / "metadata.json").read_text(encoding="utf-8"))
        values = record.get("metrics") or {}
        return (record.get("biz"), record.get("mid"), record.get("idx"), record.get("sn")) == (biz, mid, idx, sn) \
            and normalize_catalog_text(record.get("title")) == normalize_catalog_text(item.get("title")) \
            and all(type(values.get(field)) is int and values[field] >= 0 for field in METRIC_FIELDS)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def run_background(
    *, base: str | Path, biz: str, account: str, media_workers: int = 4,
    journal_links: str | Path = DEFAULT_LINKS, baseline_base: str | Path | None = None,
) -> int:
    if not 1 <= media_workers <= 8:
        raise ValueError("invalid_media_workers")
    base = Path(base).resolve()
    catalog_path = base / "catalog_verified_snapshot.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _write_json(base / "background_report.json", {"schema_version": 1, "status": "invalid_catalog"})
        return 1
    if not catalog_snapshot_ready(catalog, account, base):
        _write_json(base / "background_report.json", {"schema_version": 1, "status": "invalid_catalog"})
        return 1
    summary = build_capture_summary(base, catalog, biz=biz)
    queue = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "biz": biz, "catalog_count": summary["catalog_count"],
        "indices": summary["recapture_indices"],
        "items": [row for row in summary["items"] if row["status"] != "complete"],
    }
    _write_json(base / "background_refill_queue.json", queue)
    cacheable_indices = {
        row["index"] for row in summary["items"]
        if row["status"] in {"complete", "partial"}
    }
    if not cacheable_indices:
        _write_json(base / "background_report.json", {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_capture", "capture_summary": summary,
            "remaining_indices": queue["indices"],
        })
        return 2
    bundle = base / "bundle"
    state_path = base / "capture_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"jobs": {}}
    identity_path = base / "catalog_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.exists() else {
        "schema_version": 1, "source_catalog": "catalog_verified_snapshot.json",
        "account": account,
        "items": [dict(item, status="unresolved", biz="", mid="", idx=None, sn="", url="") for item in catalog["items"]],
    }
    try:
        repo, account_obj, target = _open_repository(base, biz, account, Path(journal_links).resolve())
        for index, item in enumerate(catalog["items"]):
            if index not in cacheable_indices:
                continue
            entry = read_capture_entry(base, index, item, biz=biz)
            article = entry["article_event"]
            metric = entry["metric_event"]
            status = "captured" if entry["complete"] else "partial"
            if not materialized_entry_complete(state, index, bundle, biz=biz, item=item):
                persist_capture(repo, account_obj, target, article, metric, bundle)
            state.setdefault("jobs", {})[str(index)] = {
                "status": status, "title": item["title"], "mid": article["mid"],
                "idx": article["idx"], "sn": article["sn"],
                "article_event_id": article.get("event_id", ""),
                "metric_event_id": metric.get("event_id") if metric else None,
                "capture_origin": entry.get("cache_origin", "live_client"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            identity["items"][index] = {
                **item, "status": status, "biz": article["biz"], "mid": article["mid"],
                "idx": article["idx"], "sn": article["sn"],
                "url": (article.get("article") or {}).get("url", ""),
            }
            _write_json(state_path, state)
            _write_json(identity_path, identity)
        media_code = int(run_bundle(workers=media_workers, bundle_root=bundle) or 0)
        write_journal_csv_bundle(bundle)
        if not summary["complete"]:
            _write_json(base / "background_report.json", {
                "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "needs_capture", "capture_summary": summary,
                "media_return_code": media_code, "remaining_indices": queue["indices"],
            })
            return 2
        baseline = Path(baseline_base).resolve() if baseline_base else None
        audit = audit_journal(base, expected_biz=biz, baseline_base=baseline)
        accepted = media_code == 0 and audit.get("ok") is True
        _write_json(base / "background_report.json", {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed" if accepted else "failed",
            "capture_summary": summary, "media_return_code": media_code,
            "audit_ok": audit.get("ok") is True, "audit_path": audit.get("audit_path"),
        })
        return 0 if accepted else 1
    except Exception as exc:
        _write_json(base / "background_report.json", {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed", "reason": type(exc).__name__,
        })
        return 1
