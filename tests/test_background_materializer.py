import json
from pathlib import Path
from types import SimpleNamespace

from crawl.capture_cache import write_capture_entry


BIZ = "biz-background"
SN = "c" * 32


def _catalog(items):
    return {
        "schema_version": 2,
        "account": "测试刊",
        "scope_observed": True,
        "active_tab": "全部",
        "terminal_visible": True,
        "loading": False,
        "items": items,
    }


def _events(title="文章一", mid="101", comment="6"):
    article = {
        "schema_version": 1, "event_id": f"a-{mid}", "captured_at": "2026-09-07T10:00:00+00:00",
        "source": "fiddler_classic", "endpoint": "/s", "kind": "article",
        "biz": BIZ, "mid": mid, "idx": 1, "sn": SN,
        "article": {
            "title": title, "author": "作者", "account_name": "测试刊", "digest": "摘要",
            "publish_time": "2026-09-07T12:00:00+08:00",
            "url": f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={mid}&idx=1&sn={SN}",
            "cover_url": "", "content_html": "<p>正文</p>",
        },
    }
    values = {
        "$html.cgiData.read_num": "10",
        "$html.appmsg_bar_data.old_like_count": "0",
        "$html.appmsg_bar_data.like_count": "1",
        "$html.appmsg_bar_data.share_count": "2",
        "$html.appmsg_bar_data.collect_count": "3",
        "$html.appmsg_bar_data.comment_count": comment,
    }
    metric = {
        "schema_version": 1, "event_id": f"m-{mid}", "captured_at": "2026-09-07T10:00:01+00:00",
        "source": "fiddler_classic", "endpoint": "/s", "kind": "metrics",
        "biz": BIZ, "mid": mid, "idx": 1, "sn": SN,
        "metric_format": "client_html_bootstrap_v1", "http_status": 200,
        "process": "wechatappex:101", "session_id": 7, "business_ret": 0,
        "html_identity": {"biz": BIZ, "mid": mid, "idx": "1", "sn": SN},
        "bootstrap_fields": {path: [{"raw_value": value}] for path, value in values.items()},
    }
    return article, metric


def test_background_only_missing_cache_writes_refill_report_and_returns_two(tmp_path, monkeypatch):
    from crawl import background_materializer as worker

    base = tmp_path / "journal"; base.mkdir()
    catalog = _catalog([{"title": "文章一", "runtime": "r1"}])
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(worker, "catalog_snapshot_ready", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_open_repository", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no cache: no database")))
    monkeypatch.setattr(worker, "run_bundle", lambda **k: (_ for _ in ()).throw(AssertionError("no cache: no media")))

    code = worker.run_background(base=base, biz=BIZ, account="测试刊", media_workers=2)

    assert code == 2
    queue = json.loads((base / "background_refill_queue.json").read_text(encoding="utf-8"))
    report = json.loads((base / "background_report.json").read_text(encoding="utf-8"))
    assert queue["indices"] == [0]
    assert report["status"] == "needs_capture"
    assert not (base / "bundle").exists()


def test_background_only_materializes_complete_cache_then_media_csv_and_audit(tmp_path, monkeypatch):
    from crawl import background_materializer as worker

    base = tmp_path / "journal"; base.mkdir()
    item = {"title": "文章一", "runtime": "r1"}
    catalog = _catalog([item])
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    article, metric = _events()
    write_capture_entry(base, 0, item, article, metric, biz=BIZ)

    calls = []
    target = SimpleNamespace(account_name="测试刊", profile_url="https://mp.weixin.qq.com/mp/profile_ext?__biz=" + BIZ)
    monkeypatch.setattr(worker, "catalog_snapshot_ready", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_open_repository", lambda *a, **k: (object(), object(), target))
    monkeypatch.setattr(worker, "persist_capture", lambda repo, account_obj, target_obj, a, m, bundle: calls.append((a["mid"], m["metrics"]["comment_count"])) or {"content_text":"正文","metrics":m["metrics"]})
    monkeypatch.setattr(worker, "materialized_entry_complete", lambda *a, **k: False)
    monkeypatch.setattr(worker, "run_bundle", lambda **k: calls.append("media") or 0)
    monkeypatch.setattr(worker, "write_journal_csv_bundle", lambda bundle: calls.append("csv") or {"article_count": 1})
    monkeypatch.setattr(worker, "audit_journal", lambda *a, **k: {"ok": True, "audit_path": str(base / "audit_report.json")})

    code = worker.run_background(base=base, biz=BIZ, account="测试刊", media_workers=2)

    assert code == 0
    assert calls == [("101", 6), "media", "csv"]
    state = json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    identity = json.loads((base / "catalog_identity.json").read_text(encoding="utf-8"))
    report = json.loads((base / "background_report.json").read_text(encoding="utf-8"))
    assert state["jobs"]["0"]["status"] == "captured"
    assert identity["items"][0]["sn"] == SN
    assert report["status"] == "completed"
    assert report["audit_ok"] is True


def test_background_only_partial_metric_cache_is_materialized_but_queued(tmp_path, monkeypatch):
    from crawl import background_materializer as worker

    base = tmp_path / "journal"; base.mkdir()
    item = {"title": "文章一", "runtime": "r1"}
    catalog = _catalog([item])
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    article, metric = _events(comment="")
    write_capture_entry(base, 0, item, article, metric, biz=BIZ)

    target = SimpleNamespace(account_name="测试刊", profile_url="https://mp.weixin.qq.com/mp/profile_ext?__biz=" + BIZ)
    monkeypatch.setattr(worker, "catalog_snapshot_ready", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_open_repository", lambda *a, **k: (object(), object(), target))
    monkeypatch.setattr(worker, "persist_capture", lambda *a, **k: {"content_text":"正文"})
    monkeypatch.setattr(worker, "materialized_entry_complete", lambda *a, **k: False)
    monkeypatch.setattr(worker, "run_bundle", lambda **k: 0)
    monkeypatch.setattr(worker, "write_journal_csv_bundle", lambda bundle: {})
    monkeypatch.setattr(worker, "audit_journal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("incomplete cache must not be audited as accepted")))

    assert worker.run_background(base=base, biz=BIZ, account="测试刊") == 2
    queue = json.loads((base / "background_refill_queue.json").read_text(encoding="utf-8"))
    assert queue["items"][0]["missing_metric_fields"] == ["comment_count"]


def test_background_module_has_no_desktop_or_capture_control_dependencies():
    import crawl.background_materializer as worker
    source = Path(worker.__file__).read_text(encoding="utf-8-sig").lower()
    for forbidden in ("desktop_wechat", "profile_switch", "set_capture_enabled", "set_capture_target", "nav("):
        assert forbidden not in source


def test_catalog_snapshot_ready_accepts_one_terminal_scan(tmp_path):
    from crawl.background_materializer import catalog_snapshot_ready

    base = tmp_path / "journal"
    relative = "catalog_scan/test/pass_01/catalog_snapshot.json"
    evidence = _catalog([
        {"title": "同名文章", "displayed_date": "", "runtime": "r1"},
        {"title": "同名文章", "displayed_date": "", "runtime": "r2"},
    ])
    path = base / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    catalog = dict(evidence)
    catalog["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": 2,
        "evidence_path": relative,
    }

    assert catalog_snapshot_ready(catalog, "测试刊", base)


def test_background_only_materializes_available_entries_when_some_files_are_missing(tmp_path, monkeypatch):
    from crawl import background_materializer as worker

    base = tmp_path / "journal"
    base.mkdir()
    present = {"title": "文章一", "runtime": "r1"}
    missing = {"title": "文章二", "runtime": "r2"}
    catalog = _catalog([present, missing])
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    article, metric = _events()
    write_capture_entry(base, 0, present, article, metric, biz=BIZ)

    calls = []
    target = SimpleNamespace(account_name="测试刊", profile_url="https://mp.weixin.qq.com/mp/profile_ext?__biz=" + BIZ)
    monkeypatch.setattr(worker, "catalog_snapshot_ready", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_open_repository", lambda *a, **k: (object(), object(), target))
    monkeypatch.setattr(worker, "persist_capture", lambda *a, **k: calls.append(a[3]["mid"]) or {"content_text": "正文"})
    monkeypatch.setattr(worker, "materialized_entry_complete", lambda *a, **k: False)
    monkeypatch.setattr(worker, "run_bundle", lambda **k: calls.append("media") or 0)
    monkeypatch.setattr(worker, "write_journal_csv_bundle", lambda bundle: calls.append("csv") or {"article_count": 1})

    assert worker.run_background(base=base, biz=BIZ, account="测试刊") == 2
    assert calls == ["101", "media", "csv"]
    report = json.loads((base / "background_report.json").read_text(encoding="utf-8"))
    queue = json.loads((base / "background_refill_queue.json").read_text(encoding="utf-8"))
    assert report["status"] == "needs_capture"
    assert report["remaining_indices"] == [1]
    assert queue["indices"] == [1]
