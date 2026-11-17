"""Shared, pure contracts for verified WeChat client catalog snapshots."""
from __future__ import annotations

from collections import Counter
import unicodedata


def normalize_catalog_text(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def catalog_sequence(items) -> tuple[tuple[str, str], ...]:
    if not isinstance(items, list) or not items:
        raise ValueError("empty_catalog")
    titles = []
    dates = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_catalog_item")
        title = normalize_catalog_text(item.get("title"))
        if not title:
            raise ValueError("invalid_catalog_item")
        titles.append(title)
        dates.append(normalize_catalog_text(item.get("displayed_date")))
    counts = Counter(titles)
    sequence = []
    for title, displayed_date in zip(titles, dates):
        if counts[title] == 1:
            sequence.append((title, ""))
        else:
            if not displayed_date:
                raise ValueError("catalog_item_identity_ambiguous")
            sequence.append((title, displayed_date))
    if len(set(sequence)) != len(sequence):
        raise ValueError("catalog_item_identity_ambiguous")
    return tuple(sequence)


def consensus_metadata_valid(consensus, item_count: int) -> bool:
    if not isinstance(consensus, dict) or type(item_count) is not int or item_count < 1:
        return False
    required = consensus.get("required_matches")
    attempted = consensus.get("attempted_passes")
    matching = consensus.get("matching_passes")
    evidence_paths = consensus.get("evidence_paths")
    return (
        required == 2
        and type(attempted) is int and attempted >= required
        and isinstance(matching, list) and len(matching) >= required
        and all(type(value) is int and 1 <= value <= attempted for value in matching)
        and len(set(matching)) == len(matching)
        and matching == sorted(matching)
        and consensus.get("item_count") == item_count
        and isinstance(evidence_paths, list) and len(evidence_paths) == len(matching)
        and all(isinstance(path, str) and path.strip() for path in evidence_paths)
    )


def catalog_runtime_membership(items) -> Counter:
    """Preserve duplicate cards by including their live UIA runtime identity."""
    if not isinstance(items, list) or not items:
        raise ValueError("empty_catalog")
    membership = Counter()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_catalog_item")
        title = normalize_catalog_text(item.get("title"))
        runtime = normalize_catalog_text(item.get("runtime"))
        if not title or not runtime:
            raise ValueError("invalid_catalog_item")
        membership[(title, normalize_catalog_text(item.get("displayed_date")), runtime)] += 1
    return membership


def catalog_scan_metadata_valid(scan, item_count: int) -> bool:
    if not isinstance(scan, dict) or type(item_count) is not int or item_count < 1:
        return False
    evidence_path = scan.get("evidence_path")
    return (
        type(scan.get("attempted_passes")) is int
        and scan["attempted_passes"] == 1
        and type(scan.get("accepted_pass")) is int
        and scan["accepted_pass"] == 1
        and type(scan.get("item_count")) is int
        and scan["item_count"] == item_count
        and isinstance(evidence_path, str)
        and bool(evidence_path.strip())
    )


def catalog_evidence_refs(catalog, item_count: int):
    """Return accepted scan evidence while retaining legacy consensus support."""
    if not isinstance(catalog, dict):
        return None
    scan = catalog.get("catalog_scan")
    if catalog_scan_metadata_valid(scan, item_count):
        return [(scan["accepted_pass"], scan["evidence_path"])]
    consensus = catalog.get("catalog_consensus")
    if consensus_metadata_valid(consensus, item_count):
        return list(zip(consensus["matching_passes"], consensus["evidence_paths"]))
    return None
