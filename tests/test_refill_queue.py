import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _write_minimal_catalog(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    items = [
        {"title": "完整", "runtime": "1"},
        {"title": "缺指标", "runtime": "2"},
        {"title": "暂停", "runtime": "3"},
    ]
    evidence_paths = []
    for pass_number in (1, 2):
        relative = f"catalog_scan/test/pass_{pass_number:02d}/catalog_snapshot.json"
        evidence_paths.append(relative)
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "scope_observed": True,
                    "account": "A",
                    "active_tab": "全部",
                    "terminal_visible": True,
                    "items": items,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    (base / "catalog_verified_snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "scope_observed": True,
                "account": "A",
                "active_tab": "全部",
                "terminal_visible": True,
                "items": items,
                "catalog_consensus": {
                    "required_matches": 2,
                    "attempted_passes": 2,
                    "matching_passes": [1, 2],
                    "item_count": 3,
                    "evidence_paths": evidence_paths,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "capture_state.json").write_text(
        json.dumps(
            {
                "jobs": {
                    "0": {"status": "captured", "mid": "100", "idx": 1},
                    "1": {"status": "partial", "mid": "101", "idx": 1},
                    "2": {"status": "paused", "reason": "no_verified_article_response"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_build_refill_queue_excludes_verified_complete_and_lists_missing_metrics(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    folder = base / "bundle" / "articles" / "101_1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "read_count": 10,
                    "like_count": None,
                    "watching_count": 2,
                    "share_count": 3,
                    "favorite_count": None,
                    "comment_count": 4,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(refill_queue, "_state_complete", lambda state, index, *a, **k: index == 0)

    queue = refill_queue.build_refill_queue(base, biz="biz-a")

    assert queue["indices"] == [1, 2]
    assert queue["items"][0]["reason"] == "missing_metrics"
    assert queue["items"][0]["missing_metric_fields"] == ["like_count", "favorite_count"]
    assert queue["items"][1]["reason"] == "no_verified_article_response"


def test_refill_does_not_rescan_catalog_or_force_completed_articles(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    queues = [
        {"schema_version": 1, "indices": [1, 2], "items": [{"index": 1}, {"index": 2}]},
        {"schema_version": 1, "indices": [], "items": []},
    ]
    monkeypatch.setattr(refill_queue, "build_refill_queue", lambda *a, **k: queues.pop(0))
    calls = []

    def fake_client(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0

    controls = []
    monkeypatch.setattr(
        refill_queue,
        "set_capture_target",
        lambda pilot, biz: controls.append(("target", Path(pilot), biz)),
        raising=False,
    )
    monkeypatch.setattr(
        refill_queue,
        "set_diagnostic_enabled",
        lambda pilot, enabled, **kwargs: controls.append(("diagnostic", Path(pilot), enabled, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(refill_queue, "run_client_history", fake_client)
    monkeypatch.setattr(refill_queue, "run_bundle", lambda **k: 0)
    monkeypatch.setattr(refill_queue, "write_journal_csv_bundle", lambda *a: None)
    monkeypatch.setattr(refill_queue, "audit_journal", lambda *a, **k: {"ok": True, "audit_path": "audit.json"})

    result = refill_queue.run_refill(
        base=base,
        pilot_dir=tmp_path / "pilot",
        biz="biz-a",
        account="A",
        hwnd=123,
        media_workers=2,
    )

    assert result == 0
    argv = calls[0][0]
    assert argv[argv.index("--indices") + 1] == "1,2"
    assert "--force" not in argv
    assert calls[0][1] == {}
    assert controls == [
        ("target", tmp_path / "pilot", "biz-a"),
        ("diagnostic", tmp_path / "pilot", True, {"minutes": 60, "after_session_id": 0}),
        ("diagnostic", tmp_path / "pilot", False, {}),
    ]


def test_refill_disables_diagnostics_when_client_capture_raises(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    monkeypatch.setattr(
        refill_queue,
        "build_refill_queue",
        lambda *a, **k: {"schema_version": 1, "indices": [1], "items": [{"index": 1}]},
    )
    controls = []
    monkeypatch.setattr(refill_queue, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(
        refill_queue,
        "set_diagnostic_enabled",
        lambda pilot, enabled, **kwargs: controls.append((enabled, kwargs)),
    )
    monkeypatch.setattr(
        refill_queue,
        "run_client_history",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture_failed")),
    )

    with pytest.raises(RuntimeError, match="capture_failed"):
        refill_queue.run_refill(
            base=base,
            pilot_dir=tmp_path / "pilot",
            biz="biz-a",
            account="A",
            hwnd=123,
        )

    assert controls == [
        (True, {"minutes": 60, "after_session_id": 0}),
        (False, {}),
    ]


def test_offline_media_refill_never_invokes_client(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    monkeypatch.setattr(
        refill_queue,
        "build_refill_queue",
        lambda *a, **k: {"schema_version": 1, "indices": [], "items": []},
    )
    monkeypatch.setattr(
        refill_queue,
        "run_client_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("client must stay untouched")),
    )
    observed = []
    monkeypatch.setattr(refill_queue, "run_bundle", lambda **k: observed.append(k) or 0)
    monkeypatch.setattr(refill_queue, "write_journal_csv_bundle", lambda bundle: observed.append({"csv": bundle}))
    monkeypatch.setattr(refill_queue, "audit_journal", lambda *a, **k: {"ok": True, "audit_path": "audit.json"})

    assert refill_queue.run_refill(
        base=base,
        pilot_dir=tmp_path / "pilot",
        biz="biz-a",
        account="A",
        hwnd=0,
        media_workers=3,
        offline_only=True,
    ) == 0
    assert observed[0] == {"workers": 3, "bundle_root": base / "bundle"}


def test_offline_only_returns_two_when_article_capture_is_still_needed(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    monkeypatch.setattr(
        refill_queue,
        "build_refill_queue",
        lambda *a, **k: {"schema_version": 1, "indices": [2], "items": [{"index": 2}]},
    )
    monkeypatch.setattr(
        refill_queue,
        "run_client_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("offline mode must not invoke client")),
    )

    assert refill_queue.run_refill(
        base=base,
        pilot_dir=tmp_path / "pilot",
        biz="biz-a",
        account="A",
        hwnd=0,
        offline_only=True,
    ) == 2


def test_refill_requires_media_and_audit_success(tmp_path, monkeypatch):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    monkeypatch.setattr(
        refill_queue,
        "build_refill_queue",
        lambda *a, **k: {"schema_version": 1, "indices": [], "items": []},
    )
    monkeypatch.setattr(refill_queue, "run_bundle", lambda **k: 2)
    monkeypatch.setattr(refill_queue, "write_journal_csv_bundle", lambda *a: None)
    monkeypatch.setattr(refill_queue, "audit_journal", lambda *a, **k: {"ok": False, "audit_path": "audit.json"})

    assert refill_queue.run_refill(
        base=base,
        pilot_dir=tmp_path / "pilot",
        biz="biz-a",
        account="A",
        hwnd=0,
        offline_only=True,
    ) == 1
    report = json.loads((base / "refill_report.json").read_text(encoding="utf-8"))
    assert report["media_return_code"] == 2
    assert report["audit_ok"] is False



def test_refill_queue_rejects_single_pass_catalog(tmp_path):
    from metrics import refill_queue

    base = tmp_path / "journal"
    _write_minimal_catalog(base)
    path = base / "catalog_verified_snapshot.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog.pop("catalog_consensus")
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="catalog_scope_unverified"):
        refill_queue.build_refill_queue(base, biz="biz-a")
