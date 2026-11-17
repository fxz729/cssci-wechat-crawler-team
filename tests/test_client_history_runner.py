import json
from pathlib import Path

import pytest

from crawl.client_history_runner import (
    Tail,
    _metric_complete,
    _parse_indices,
    _recover_interrupted_jobs,
    dump,
)


def test_metric_complete_keeps_explicit_zero_values():
    metric = {"metrics": {
        "read_count": 1,
        "like_count": 0,
        "watching_count": 2,
        "share_count": 0,
        "favorite_count": 3,
        "comment_count": 0,
    }}
    assert _metric_complete(metric) is True
    metric["metrics"]["favorite_count"] = None
    assert _metric_complete(metric) is False


def test_parse_indices_deduplicates_and_rejects_out_of_range():
    assert _parse_indices("2,1,2", 4) == [2, 1]
    with pytest.raises(ValueError, match="index_out_of_range"):
        _parse_indices("4", 4)


def test_tail_uses_the_configured_biz(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {
        "schema_version": 1,
        "kind": "metrics",
        "captured_at": "2026-09-06T00:00:00+00:00",
        "endpoint": "/s",
        "source": "fiddler_classic",
        "biz": "custom-biz",
        "mid": "123",
        "idx": 1,
        "sn": "a" * 32,
        "metric_format": "client_html_bootstrap_v1",
        "http_status": 200,
        "process": "wechatappex:1234",
        "session_id": 1,
        "business_ret": 0,
        "html_identity": {"biz": "custom-biz", "mid": "123", "idx": "1", "sn": "a" * 32},
        "bootstrap_fields": {
            "$html.cgiData.read_num": [{"raw_value": "1"}],
            "$html.appmsg_bar_data.old_like_count": [{"raw_value": "0"}],
            "$html.appmsg_bar_data.like_count": [{"raw_value": "2"}],
            "$html.appmsg_bar_data.share_count": [{"raw_value": "0"}],
            "$html.appmsg_bar_data.collect_count": [{"raw_value": "3"}],
            "$html.appmsg_bar_data.comment_count": [{"raw_value": "0"}],
        },
    }
    path.write_text("", encoding="utf-8")
    tail = Tail(path, "custom-biz")
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert tail.poll()[0]["biz"] == "custom-biz"


def test_dump_retries_a_transient_windows_replace_error(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    original_replace = Path.replace
    attempts = {"count": 0}

    def flaky_replace(self, destination):
        if self.name == "state.json.tmp" and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError("temporarily locked")
        return original_replace(self, destination)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr("crawl.client_history_runner.time.sleep", lambda _seconds: None)
    dump(target, {"ok": True})
    assert target.exists()
    assert attempts["count"] == 1


def test_recover_interrupted_jobs_marks_running_entries_paused():
    state = {
        "jobs": {
            "0": {"status": "captured", "title": "\u5df2\u5b8c\u6210"},
            "1": {"status": "running", "title": "\u4e2d\u65ad\u65f6\u6b63\u5728\u5904\u7406"},
            "2": {"status": "paused", "title": "\u539f\u672c\u5df2\u6682\u505c"},
        }
    }

    changed = _recover_interrupted_jobs(state)

    assert changed == 1
    assert state["jobs"]["0"]["status"] == "captured"
    assert state["jobs"]["1"]["status"] == "paused"
    assert state["jobs"]["1"]["reason"] == "interrupted_before_resume"
    assert state["jobs"]["2"]["status"] == "paused"


def test_existing_empty_metadata_is_not_completed_cache(tmp_path):
    from crawl.client_history_runner import _state_complete
    folder = tmp_path / "articles" / "123_1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text("{}", encoding="utf-8")
    state = {"jobs":{"0":{"status":"captured","mid":"123","idx":1}}}
    assert not _state_complete(state, 0, tmp_path)


def test_nav_replaces_invalid_powershell_bytes(monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner

    calls = []
    monkeypatch.setattr(
        runner,
        "DesktopWeChatDriver",
        lambda _hwnd: SimpleNamespace(focus=lambda: None),
    )

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.nav("profile", hwnd=1, account="A") == {"ok": True}
    assert calls[0][1]["encoding"] == "utf-8"
    assert calls[0][1]["errors"] == "replace"


def test_nav_rejects_replacement_character_in_success_output(monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner

    monkeypatch.setattr(
        runner,
        "DesktopWeChatDriver",
        lambda _hwnd: SimpleNamespace(focus=lambda: None),
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ok": "\ufffd"}',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="navigation_profile_invalid_output"):
        runner.nav("profile", hwnd=1, account="A")


def test_nav_recognizes_ascii_error_reason_around_replacement_characters(monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner

    monkeypatch.setattr(
        runner,
        "DesktopWeChatDriver",
        lambda _hwnd: SimpleNamespace(focus=lambda: None),
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="\ufffd document_navigation_timeout \ufffd",
        ),
    )

    with pytest.raises(RuntimeError, match="navigation_open_document_navigation_timeout"):
        runner.nav("open", hwnd=1, account="A")


def test_identity_cache_rejects_same_length_reordered_catalog(tmp_path):
    from crawl.client_history_runner import _load_or_create_identity
    dump(tmp_path / "catalog_identity.json", {"items":[{"title":"Old"}]})
    with pytest.raises(RuntimeError, match="catalog_identity_changed"):
        _load_or_create_identity(tmp_path, {"items":[{"title":"New"}]})


def test_default_client_limit_is_unbounded(monkeypatch, tmp_path):
    from crawl import client_history_runner as runner
    observed = []
    monkeypatch.setattr(runner, "_run", lambda args: observed.append(args.limit) or 0, raising=False)
    assert runner.main(["--base", str(tmp_path / "base"), "--pilot-dir", str(tmp_path / "pilot")]) == 0
    assert observed == [0]


def test_client_lock_blocks_second_runner_before_state_changes(tmp_path, monkeypatch):
    from utils.runtime_lock import runtime_lock
    from crawl import client_history_runner as runner
    pilot = tmp_path / "pilot"
    monkeypatch.setattr(runner, "_run", lambda args: pytest.fail("run should not start"), raising=False)
    with runtime_lock(pilot / "capture.lock"):
        with pytest.raises(RuntimeError, match="capture_already_running"):
            runner.main(["--base", str(tmp_path / "base"), "--pilot-dir", str(pilot)])


def test_event_pairing_includes_biz_and_sn():
    from crawl.client_history_runner import _article_key
    event = {"biz":"a", "mid":"123", "idx":1, "sn":"one"}
    assert _article_key(event) != _article_key(dict(event, sn="two"))
    assert _article_key(event) != _article_key(dict(event, biz="b"))


def test_keyboard_interrupt_exports_saved_csv_and_disables_capture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner
    base = tmp_path / "base"
    folder = base / "bundle" / "articles" / "123_1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text("{}", encoding="utf-8")
    _prepare_minimal_client_run(base, runner)
    monkeypatch.setattr(runner, "create_session_factory", lambda *a: None)
    monkeypatch.setattr(runner, "init_db", lambda *a: None)
    account = SimpleNamespace(biz="a")
    repo = SimpleNamespace(sync_target_accounts=lambda *a: None, list_accounts=lambda **k: [account])
    monkeypatch.setattr(runner, "Repository", lambda *a: repo)
    monkeypatch.setattr(runner, "parse_profile_links_file", lambda *a: [account])
    controls, exports = [], []
    monkeypatch.setattr(runner, "set_capture_enabled", lambda p, enabled, **k: controls.append(enabled))
    monkeypatch.setattr(runner, "nav", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(runner, "write_journal_csv_bundle", lambda bundle: exports.append(bundle), raising=False)
    assert runner.main(["--base",str(base),"--pilot-dir",str(tmp_path / "pilot"),"--biz","a","--account","A"]) == 130
    assert controls[-1] is False
    assert exports == [base / "bundle"]
    state=json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "paused"



def _prepare_minimal_client_run(base, runner):
    items = [{"title": "T", "runtime": "1"}]
    evidence_paths = []
    for pass_number in (1, 2):
        relative = f"catalog_scan/test/pass_{pass_number:02d}/catalog_snapshot.json"
        evidence_paths.append(relative)
        runner.dump(
            base / relative,
            {
                "schema_version": 2,
                "scope_observed": True,
                "account": "A",
                "active_tab": "全部",
                "terminal_visible": True,
                "items": items,
            },
        )
    runner.dump(
        base / "catalog_verified_snapshot.json",
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
                "item_count": 1,
                "evidence_paths": evidence_paths,
            },
        },
    )


def _stub_client_repository(monkeypatch, runner):
    from types import SimpleNamespace

    account = SimpleNamespace(biz="a")
    repo = SimpleNamespace(sync_target_accounts=lambda *a: None, list_accounts=lambda **k: [account])
    monkeypatch.setattr(runner, "create_session_factory", lambda *a: None)
    monkeypatch.setattr(runner, "Repository", lambda *a: repo)
    monkeypatch.setattr(runner, "parse_profile_links_file", lambda *a: [account])
    return account


def test_initialization_failure_marks_run_failed_instead_of_leaving_running(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner

    base = tmp_path / "base"
    _prepare_minimal_client_run(base, runner)
    monkeypatch.setattr(runner, "create_session_factory", lambda *a: None)
    monkeypatch.setattr(runner, "init_db", lambda *a: (_ for _ in ()).throw(RuntimeError("db_init_failed")))

    with pytest.raises(RuntimeError, match="db_init_failed"):
        runner.main(["--base", str(base), "--pilot-dir", str(tmp_path / "pilot"), "--biz", "a", "--account", "A"])

    state = json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "failed"
    assert state["run"]["reason"] == "db_init_failed"
    assert "completed_at" not in state["run"]


def test_csv_cleanup_failure_does_not_mask_capture_failure(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner

    base = tmp_path / "base"
    folder = base / "bundle" / "articles" / "123_1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text("{}", encoding="utf-8")
    _prepare_minimal_client_run(base, runner)
    _stub_client_repository(monkeypatch, runner)
    monkeypatch.setattr(runner, "init_db", lambda *a: None)
    controls = []
    monkeypatch.setattr(runner, "set_capture_enabled", lambda p, enabled, **k: controls.append(enabled))
    monkeypatch.setattr(runner, "nav", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture_failure")))
    monkeypatch.setattr(
        runner,
        "write_journal_csv_bundle",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("csv_failure")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="capture_failure"):
        runner.main(["--base", str(base), "--pilot-dir", str(tmp_path / "pilot"), "--biz", "a", "--account", "A"])

    assert controls[-1] is False
    state = json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "failed"
    assert state["run"]["reason"] == "capture_failure"
    assert state["run"]["cleanup_errors"] == ["csv_failure"]


def test_incomplete_normal_exit_has_no_completed_timestamp(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner

    base = tmp_path / "base"
    _prepare_minimal_client_run(base, runner)
    _stub_client_repository(monkeypatch, runner)
    monkeypatch.setattr(runner, "init_db", lambda *a: None)
    monkeypatch.setattr(runner, "_parse_indices", lambda *a: [])
    monkeypatch.setattr(runner, "nav", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)

    assert runner.main(["--base", str(base), "--pilot-dir", str(tmp_path / "pilot"), "--biz", "a", "--account", "A"]) == 1
    state = json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "paused"
    assert "completed_at" not in state["run"]


def test_stop_control_pauses_before_client_navigation(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner
    from utils.run_control import request_stop

    base = tmp_path / "base"
    _prepare_minimal_client_run(base, runner)
    _stub_client_repository(monkeypatch, runner)
    monkeypatch.setattr(runner, "init_db", lambda *a: None)
    controls = []
    monkeypatch.setattr(runner, "set_capture_enabled", lambda p, enabled, **k: controls.append(enabled))
    monkeypatch.setattr(runner, "nav", lambda *a, **k: pytest.fail("navigation must not start after stop request"))
    control = tmp_path / "run_control.json"
    request_stop(control)

    assert runner.main([
        "--base", str(base),
        "--pilot-dir", str(tmp_path / "pilot"),
        "--biz", "a",
        "--account", "A",
        "--stop-control", str(control),
    ]) == 130
    state = json.loads((base / "capture_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "paused"
    assert state["run"]["reason"] == "stop_requested"
    assert controls == []



def test_client_runner_rejects_catalog_without_two_pass_consensus(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner

    base = tmp_path / "base"
    runner.dump(
        base / "catalog_verified_snapshot.json",
        {
            "schema_version": 2,
            "scope_observed": True,
            "account": "A",
            "active_tab": "全部",
            "terminal_visible": True,
            "items": [{"title": "T", "runtime": "1"}],
        },
    )
    monkeypatch.setattr(
        runner,
        "set_capture_enabled",
        lambda *a, **k: pytest.fail("capture must not be enabled for a single-pass catalog"),
    )

    with pytest.raises(RuntimeError, match="catalog_not_ready"):
        runner.main([
            "--base", str(base),
            "--pilot-dir", str(tmp_path / "pilot"),
            "--biz", "a",
            "--account", "A",
        ])


def _capture_only_article_event(biz="biz-test", title="缓存文章"):
    sn = "b" * 32
    return {
        "schema_version": 1, "event_id": "a1", "captured_at": "2026-09-07T09:00:00+00:00",
        "source": "fiddler_classic", "endpoint": "/s", "kind": "article",
        "biz": biz, "mid": "987", "idx": 1, "sn": sn,
        "article": {
            "title": title, "author": "作者", "account_name": "测试刊", "digest": "摘要",
            "publish_time": "2026-09-07T12:00:00+08:00",
            "url": f"https://mp.weixin.qq.com/s?__biz={biz}&mid=987&idx=1&sn={sn}",
            "cover_url": "", "content_html": "<p>正文</p>",
        },
    }


def _capture_only_metric_event(biz="biz-test"):
    sn = "b" * 32
    values = {
        "$html.cgiData.read_num": "10",
        "$html.appmsg_bar_data.old_like_count": "0",
        "$html.appmsg_bar_data.like_count": "1",
        "$html.appmsg_bar_data.share_count": "2",
        "$html.appmsg_bar_data.collect_count": "3",
        "$html.appmsg_bar_data.comment_count": "4",
    }
    return {
        "schema_version": 1, "event_id": "m1", "captured_at": "2026-09-07T09:00:01+00:00",
        "source": "fiddler_classic", "endpoint": "/s", "kind": "metrics",
        "biz": biz, "mid": "987", "idx": 1, "sn": sn,
        "metric_format": "client_html_bootstrap_v1", "http_status": 200,
        "process": "wechatappex:100", "session_id": 5, "business_ret": 0,
        "html_identity": {"biz": biz, "mid": "987", "idx": "1", "sn": sn},
        "bootstrap_fields": {path: [{"raw_value": value}] for path, value in values.items()},
    }


def test_capture_only_writes_cache_without_database_bundle_or_csv(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner

    base = tmp_path / "journal"
    pilot = tmp_path / "pilot"
    base.mkdir(); pilot.mkdir()
    catalog = {
        "schema_version": 2, "account": "测试刊", "scope_observed": True,
        "active_tab": "全部", "terminal_visible": True, "loading": False,
        "items": [{"title": "缓存文章", "runtime": "runtime-1"}],
    }
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    article = _capture_only_article_event()
    metric = _capture_only_metric_event()

    monkeypatch.setattr(runner, "_catalog_ready", lambda *a, **k: True)
    monkeypatch.setattr(runner, "create_session_factory", lambda *a, **k: (_ for _ in ()).throw(AssertionError("database forbidden")))
    monkeypatch.setattr(runner, "init_db", lambda *a, **k: (_ for _ in ()).throw(AssertionError("database forbidden")))
    monkeypatch.setattr(runner, "write_journal_csv_bundle", lambda *a, **k: (_ for _ in ()).throw(AssertionError("csv forbidden")))
    monkeypatch.setattr(runner, "Tail", lambda *a, **k: object())
    monkeypatch.setattr(runner, "wait_events", lambda *a, **k: (article, metric, [article, metric]))
    monkeypatch.setattr(runner, "nav", lambda *a, **k: {})
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    args = SimpleNamespace(
        base=str(base), pilot_dir=str(pilot), biz="biz-test", account="测试刊", hwnd=7,
        indices=None, limit=0, wait_seconds=1.0, attempts=1, sleep_seconds=0.0,
        repair_existing=False, force=False, log_dir=str(base / "logs"),
        event_log=str(base / "progress.jsonl"), stop_control=None, capture_only=True,
    )
    assert runner._run(args) == 0
    assert not (base / "bundle").exists()
    assert not (base / "capture_state.json").exists()
    state = json.loads((base / "capture_cache_state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["0"]["status"] == "cached"
    summary = json.loads((base / "capture_cache_summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is True



def test_capture_only_rebinds_verified_cache_runtime_without_reopening_article(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner
    from crawl.capture_cache import read_capture_entry, write_capture_entry

    base = tmp_path / "journal"
    pilot = tmp_path / "pilot"
    base.mkdir(); pilot.mkdir()
    old_item = {"title": "缓存文章", "runtime": "runtime-old"}
    new_item = {"title": "缓存文章", "runtime": "runtime-new"}
    catalog = {
        "schema_version": 2, "account": "测试刊", "scope_observed": True,
        "active_tab": "全部", "terminal_visible": True, "loading": False,
        "items": [new_item],
    }
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    write_capture_entry(
        base, 0, old_item, _capture_only_article_event(), _capture_only_metric_event(), biz="biz-test"
    )
    nav_actions = []

    monkeypatch.setattr(runner, "_catalog_ready", lambda *a, **k: True)
    monkeypatch.setattr(runner, "Tail", lambda *a, **k: object())
    monkeypatch.setattr(runner, "wait_events", lambda *a, **k: (_ for _ in ()).throw(AssertionError("capture forbidden")))
    monkeypatch.setattr(runner, "nav", lambda action, **kwargs: nav_actions.append(action) or {})
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)

    args = SimpleNamespace(
        base=str(base), pilot_dir=str(pilot), biz="biz-test", account="测试刊", hwnd=7,
        indices=None, limit=0, wait_seconds=1.0, attempts=1, sleep_seconds=0.0,
        repair_existing=False, force=False, log_dir=str(base / "logs"),
        event_log=str(base / "progress.jsonl"), stop_control=None, capture_only=True,
    )
    assert runner._run(args) == 0
    assert "open" not in nav_actions and "reload" not in nav_actions
    assert read_capture_entry(base, 0, new_item, biz="biz-test")["complete"] is True


def test_capture_only_runs_one_independent_retry_for_failed_indices(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawl import client_history_runner as runner

    base = tmp_path / "journal"
    pilot = tmp_path / "pilot"
    base.mkdir(); pilot.mkdir()
    catalog = {
        "schema_version": 2, "account": "测试刊", "scope_observed": True,
        "active_tab": "全部", "terminal_visible": True, "loading": False,
        "items": [{"title": "缓存文章", "runtime": "runtime-1"}],
    }
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    responses = iter([
        RuntimeError("temporary_capture_failure"),
        (_capture_only_article_event(), _capture_only_metric_event(), [
            _capture_only_article_event(), _capture_only_metric_event(),
        ]),
    ])

    def wait_once_then_succeed(*args, **kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runner, "_catalog_ready", lambda *a, **k: True)
    monkeypatch.setattr(runner, "Tail", lambda *a, **k: object())
    monkeypatch.setattr(runner, "wait_events", wait_once_then_succeed)
    monkeypatch.setattr(runner, "nav", lambda *a, **k: {})
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    args = SimpleNamespace(
        base=str(base), pilot_dir=str(pilot), biz="biz-test", account="测试刊", hwnd=7,
        indices=None, limit=0, wait_seconds=1.0, attempts=1, sleep_seconds=0.0,
        repair_existing=False, force=False, log_dir=str(base / "logs"),
        event_log=str(base / "progress.jsonl"), stop_control=None, capture_only=True,
    )
    assert runner._run(args) == 0
    summary = json.loads((base / "capture_cache_summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is True
    state = json.loads((base / "capture_cache_state.json").read_text(encoding="utf-8"))
    assert state["run"]["status"] == "cache_ready"


def test_prior_complete_picture_events_preserve_original_capture_time(tmp_path):
    from crawl.client_history_runner import _prior_complete_events
    article = _capture_only_article_event(title="图片消息")
    metric = _capture_only_metric_event()
    path = tmp_path / "capture_inbox.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in (article, metric)) + "\n", encoding="utf-8")
    pair = _prior_complete_events(path, "biz-test", "图片消息")
    assert pair[0]["captured_at"] == article["captured_at"]
    assert pair[1]["metrics"]["favorite_count"] == 3


def test_prior_picture_seed_is_used_only_after_live_wait_returns_no_article(tmp_path, monkeypatch):
    from crawl import client_history_runner as runner
    article = _capture_only_article_event(title="图片消息")
    metric = _capture_only_metric_event()
    monkeypatch.setattr(runner, "wait_events", lambda *a, **k: (None, None, []))
    monkeypatch.setattr(runner, "_prior_complete_events", lambda *a, **k: (article, metric))
    result = runner._wait_or_seed_picture(object(), {"title":"图片消息","card_class":"double-img-card-item"}, biz="biz-test", seconds=1, inbox=tmp_path / "x")
    assert result[0] == article
    assert result[1] == metric
    assert result[3] == "prior_local_event"


def test_catalog_ready_accepts_one_terminal_scan(tmp_path):
    from crawl.client_history_runner import _catalog_ready

    items = [
        {"title": "同名文章", "displayed_date": "", "runtime": "1"},
        {"title": "同名文章", "displayed_date": "", "runtime": "2"},
    ]
    relative = "catalog_scan/test/pass_01/catalog_snapshot.json"
    evidence = {
        "schema_version": 2,
        "scope_observed": True,
        "account": "A",
        "active_tab": "全部",
        "terminal_visible": True,
        "items": items,
    }
    dump(tmp_path / relative, evidence)
    catalog = dict(evidence)
    catalog["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": 2,
        "evidence_path": relative,
    }

    assert _catalog_ready(catalog, "A", tmp_path)
