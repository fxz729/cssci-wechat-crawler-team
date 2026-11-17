import pytest


def test_unique_titles_ignore_transient_display_date_differences():
    from crawl.catalog_contract import catalog_sequence

    with_date = [
        {"title": "文章一", "displayed_date": "2026年1月1日"},
        {"title": "文章二", "displayed_date": "2026年1月2日"},
    ]
    without_date = [
        {"title": "文章一", "displayed_date": ""},
        {"title": "文章二"},
    ]
    assert catalog_sequence(with_date) == catalog_sequence(without_date)


def test_duplicate_titles_require_distinct_nonempty_dates():
    from crawl.catalog_contract import catalog_sequence

    valid = [
        {"title": "同名文章", "displayed_date": "1月1日"},
        {"title": "同名文章", "displayed_date": "1月2日"},
    ]
    assert len(set(catalog_sequence(valid))) == 2
    with pytest.raises(ValueError, match="catalog_item_identity_ambiguous"):
        catalog_sequence([
            {"title": "同名文章", "displayed_date": ""},
            {"title": "同名文章", "displayed_date": "1月2日"},
        ])
    with pytest.raises(ValueError, match="catalog_item_identity_ambiguous"):
        catalog_sequence([
            {"title": "同名文章", "displayed_date": "1月1日"},
            {"title": "同名文章", "displayed_date": "1月1日"},
        ])


def test_consensus_metadata_contract_requires_paths_for_each_matching_pass():
    from crawl.catalog_contract import consensus_metadata_valid

    valid = {
        "required_matches": 2,
        "attempted_passes": 3,
        "matching_passes": [1, 3],
        "item_count": 10,
        "evidence_paths": ["a", "b"],
    }
    assert consensus_metadata_valid(valid, 10)
    assert not consensus_metadata_valid(dict(valid, matching_passes=[1]), 10)
    assert not consensus_metadata_valid(dict(valid, evidence_paths=["a"]), 10)
    assert not consensus_metadata_valid(dict(valid, item_count=9), 10)


def test_catalog_evidence_refs_accept_single_scan_and_legacy_consensus():
    from crawl.catalog_contract import catalog_evidence_refs

    single = {
        "catalog_scan": {
            "attempted_passes": 1,
            "accepted_pass": 1,
            "item_count": 10,
            "evidence_path": "catalog_scan/run/pass_01/catalog_snapshot.json",
        }
    }
    assert catalog_evidence_refs(single, 10) == [(1, "catalog_scan/run/pass_01/catalog_snapshot.json")]

    legacy = {
        "catalog_consensus": {
            "required_matches": 2,
            "attempted_passes": 2,
            "matching_passes": [1, 2],
            "item_count": 10,
            "evidence_paths": ["one", "two"],
        }
    }
    assert catalog_evidence_refs(legacy, 10) == [(1, "one"), (2, "two")]


def test_catalog_evidence_refs_rejects_malformed_single_scan():
    from crawl.catalog_contract import catalog_evidence_refs

    invalid = {
        "catalog_scan": {
            "attempted_passes": 2,
            "accepted_pass": 1,
            "item_count": 10,
            "evidence_path": "catalog_scan/run/pass_01/catalog_snapshot.json",
        }
    }
    assert catalog_evidence_refs(invalid, 10) is None


def test_catalog_scan_metadata_rejects_boolean_pass_numbers():
    from crawl.catalog_contract import catalog_scan_metadata_valid

    scan = {
        "attempted_passes": True,
        "accepted_pass": True,
        "item_count": True,
        "evidence_path": "catalog_scan/run/pass_01/catalog_snapshot.json",
    }
    assert not catalog_scan_metadata_valid(scan, 1)


def test_catalog_runtime_membership_keeps_duplicate_titles_without_dates():
    from crawl.catalog_contract import catalog_runtime_membership

    items = [
        {"title": "同名文章", "displayed_date": "", "runtime": "r1"},
        {"title": "同名文章", "displayed_date": "", "runtime": "r2"},
    ]
    membership = catalog_runtime_membership(items)
    assert sum(membership.values()) == 2
    assert len(membership) == 2
