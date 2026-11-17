import json
from pathlib import Path

import pytest

from crawl.roster_client_runner import build_rollout_manifest, safe_slug
from sources.journal_catalog import ProfileLinkTarget


def target(name, biz):
    return ProfileLinkTarget(
        discipline="test", tier="cssci", journal_name=name, account_name=name,
        profile_url=f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}",
        biz=biz, source_file="links.md", source_line=1,
    )


def test_safe_slug_is_stable_and_does_not_include_url_or_credentials():
    value = safe_slug("测试期刊 / 2026")
    assert value == "测试期刊_2026"
    assert "http" not in value.lower()
    assert "token" not in value.lower()


def test_build_rollout_manifest_is_one_entry_per_fixed_target(tmp_path):
    manifest = build_rollout_manifest(
        [target("甲刊", "biz-a"), target("乙刊", "biz-b")],
        tmp_path / "rollout",
    )

    assert [entry["ordinal"] for entry in manifest["journals"]] == [1, 2]
    assert [entry["journal_name"] for entry in manifest["journals"]] == ["甲刊", "乙刊"]
    assert len({entry["base"] for entry in manifest["journals"]}) == 2
    assert all(entry["status"] == "pending" for entry in manifest["journals"])
    assert all(Path(entry["base"]).parent == tmp_path / "rollout" / "journals" for entry in manifest["journals"])


def test_scan_summary_reads_terminal_from_last_navigation_frame(tmp_path):
    from crawl.roster_client_runner import _scan_terminal_from_frames
    frames = tmp_path / "frames.jsonl"
    frames.write_text(
        '{"step": 1, "markers": ["正在加载..."]}\n'
        '{"step": 2, "markers": ["已无更多消息"]}\n',
        encoding="utf-8",
    )

    assert _scan_terminal_from_frames(frames) is True


def test_media_runs_project_module_without_copying_output_code(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner
    import crawl.bundle_media
    observed = []
    monkeypatch.setattr(crawl.bundle_media, "run_bundle", lambda **kw: observed.append(kw) or 0)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not execute output code")))
    assert runner._run_media_bundle({"base":str(tmp_path)}, 2) == 0
    assert observed == [{"workers":2,"bundle_root":tmp_path / "bundle"}]
    assert not (tmp_path / "download_bundle.py").exists()


def test_switch_profile_replaces_invalid_powershell_bytes(monkeypatch):
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"success":true,"browser_hwnd":123,"document":"Expected","active_tab":"全部"}',
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    entry = {"account_name": "Expected"}
    assert runner._switch_profile(entry, 0) == 123
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"


def test_switch_profile_rejects_replacement_character_in_success_output(monkeypatch):
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"success":true,"browser_hwnd":123,"document":"Expected","active_tab":"全部","noise":"\ufffd"}',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="profile_switch_invalid_output"):
        runner._switch_profile({"account_name": "Expected"}, 0)


def test_switch_profile_rejects_wrong_document_even_on_exit_zero(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from crawl import roster_client_runner as runner
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='{"success":true,"browser_hwnd":123,"document":"Wrong"}', stderr=""))
    with pytest.raises(RuntimeError, match="profile_switch_invalid_output"):
        runner._switch_profile({"account_name":"Expected"}, 0)


def test_switch_profile_rejects_nonpositive_hwnd(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from crawl import roster_client_runner as runner
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='{"success":true,"browser_hwnd":0,"document":"Expected"}', stderr=""))
    with pytest.raises(RuntimeError, match="profile_switch_invalid_output"):
        runner._switch_profile({"account_name":"Expected"}, 0)


def test_saved_manifest_cannot_redirect_output_or_replace_target(tmp_path, monkeypatch):
    import json
    import pytest
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner
    targets = [target("A", "biz-a")]
    roster = SimpleNamespace(targets=targets, source_sha256="abc", source_file="links.md")
    monkeypatch.setattr(runner, "load_fixed_roster", lambda *a, **k: roster)
    manifest = build_rollout_manifest(targets, tmp_path)
    manifest["journals"][0]["base"] = str(tmp_path.parent / "outside")
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="rollout_manifest_changed"):
        runner._load_or_create_manifest(tmp_path, Path("links.md"), 1)


def test_saved_manifest_revalidates_roster_file(tmp_path, monkeypatch):
    import json
    import pytest
    from crawl import roster_client_runner as runner
    (tmp_path / "run_manifest.json").write_text(json.dumps({"journals":[]}), encoding="utf-8")
    monkeypatch.setattr(runner, "load_fixed_roster", lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid_roster")))
    with pytest.raises(ValueError, match="invalid_roster"):
        runner._load_or_create_manifest(tmp_path, Path("links.md"), 1)


def test_catalog_without_observed_all_scope_is_rejected():
    import pytest
    from crawl.roster_client_runner import _validate_catalog
    with pytest.raises(RuntimeError, match="catalog_scope_unverified"):
        _validate_catalog({"active_tab":"全部","terminal_visible":True,"items":[{"title":"x"}]}, "A")


def test_catalog_wrong_account_rejected():
    import pytest
    from crawl.roster_client_runner import _validate_catalog
    with pytest.raises(RuntimeError, match="catalog_scope_unverified"):
        _validate_catalog({"schema_version":2,"scope_observed":True,"account":"B",
                           "active_tab":"全部","terminal_visible":True,"items":[{"title":"x","runtime":"1"}]}, "A")


def test_refresh_runtime_preserves_frozen_order_and_requires_full_coverage():
    import pytest
    from crawl.roster_client_runner import _refresh_catalog_runtime
    old = {"items":[{"title":"First","runtime":"stale1"},{"title":"Second","runtime":"stale2"}]}
    current = {"items":[{"title":"Second","runtime":"new2"},{"title":"First","runtime":"new1"}]}
    result = _refresh_catalog_runtime(old, current)
    assert [x["title"] for x in result["items"]] == ["First","Second"]
    assert [x["runtime"] for x in result["items"]] == ["new1","new2"]
    with pytest.raises(RuntimeError, match="catalog_membership_changed"):
        _refresh_catalog_runtime(old, {"items":current["items"][:1]})


def test_refresh_ambiguous_title_is_rejected_not_guessed():
    import pytest
    from crawl.roster_client_runner import _refresh_catalog_runtime
    duplicate = {"items":[{"title":"Same","runtime":"1"},{"title":"Same","runtime":"2"}]}
    with pytest.raises(RuntimeError, match="catalog_title_ambiguous"):
        _refresh_catalog_runtime(duplicate, duplicate)


def test_refresh_duplicate_titles_uses_stable_distinct_card_classes():
    from crawl.roster_client_runner import _refresh_catalog_runtime

    old = {"items": [
        {"title": "Same", "displayed_date": "", "card_class": "image-card", "runtime": "old-image"},
        {"title": "Same", "displayed_date": "", "card_class": "video-card", "runtime": "old-video"},
    ]}
    current = {"items": [
        {"title": "Same", "displayed_date": "", "card_class": "video-card", "runtime": "new-video"},
        {"title": "Same", "displayed_date": "", "card_class": "image-card", "runtime": "new-image"},
    ]}

    result = _refresh_catalog_runtime(old, current)

    assert [item["runtime"] for item in result["items"]] == ["new-image", "new-video"]


def test_preflight_no_browser_stops_without_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from crawl import roster_client_runner as runner
    calls=[]
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **kw: calls.append((command, kw)) or SimpleNamespace(
        returncode=0, stdout='{"read_only":true,"windows":[]}', stderr=""))
    with pytest.raises(RuntimeError, match="search_window_required"):
        runner._preflight(SimpleNamespace(pilot_dir=str(tmp_path)))
    assert calls[0][0][-1] == "-InspectOnly"
    assert calls[0][1]["errors"] == "replace"


def test_preflight_rejects_replacement_character_in_success_output(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"read_only":true,"windows":[],"noise":"\ufffd"}',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="search_window_inspection_failed"):
        runner._preflight(SimpleNamespace(pilot_dir=str(tmp_path)))


def test_preflight_selects_only_the_unique_search_input_window(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner
    import crawl.client_capture_health as health_module

    active = tmp_path / "CustomRules.js"
    active.write_text("rules", encoding="utf-8")
    (tmp_path / "capture_activation.json").write_text(
        json.dumps({"active_path": str(active)}), encoding="utf-8"
    )
    payload = {
        "read_only": True,
        "windows": [
            {"browser_hwnd": 111, "document": "某公众号", "search_input": False},
            {"browser_hwnd": 222, "document": "微信搜一搜", "search_input": True},
        ],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    observed = []
    monkeypatch.setattr(
        health_module,
        "check_capture_health",
        lambda **kw: observed.append(kw) or {"ok": True},
    )

    assert runner._preflight(SimpleNamespace(pilot_dir=str(tmp_path))) == 222
    assert observed[0]["hwnd"] == 222


def test_preflight_rejects_ambiguous_search_input_windows(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import pytest
    from crawl import roster_client_runner as runner

    payload = {
        "read_only": True,
        "windows": [
            {"browser_hwnd": 111, "document": "微信搜一搜", "search_input": True},
            {"browser_hwnd": 222, "document": "微信搜一搜", "search_input": True},
        ],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    with pytest.raises(RuntimeError, match="ambiguous_search_window"):
        runner._preflight(SimpleNamespace(pilot_dir=str(tmp_path)))


def test_preflight_does_not_treat_truthy_non_boolean_as_search_input(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import pytest
    from crawl import roster_client_runner as runner

    payload = {
        "read_only": True,
        "windows": [
            {"browser_hwnd": 111, "document": "某公众号", "search_input": "true"},
        ],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    with pytest.raises(RuntimeError, match="search_window_required"):
        runner._preflight(SimpleNamespace(pilot_dir=str(tmp_path)))


def test_media_success_still_requires_csv_and_audit_gate(monkeypatch, tmp_path):
    from crawl import roster_client_runner as runner
    import export.rollout_audit
    called=[]
    monkeypatch.setattr(runner, "write_journal_csv_bundle", lambda bundle: called.append(bundle), raising=False)
    monkeypatch.setattr(export.rollout_audit, "audit_journal", lambda *a, **kw: {"ok":False,"checks":{"csv":False}})
    report=runner._audit_entry({"base":str(tmp_path),"biz":"x"})
    assert not report["ok"]
    assert called == [tmp_path / "bundle"]



def _rollout_args(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        run_dir=str(tmp_path / "run"),
        journal_links=str(tmp_path / "links.md"),
        expected_count=1,
        start=1,
        max_journals=1,
        pilot_dir=str(tmp_path / "pilot"),
        hwnd=0,
        max_steps=1,
        wait_seconds=1.0,
        attempts=1,
        sleep_seconds=0.0,
        media_workers=1,
    )


def _rollout_manifest(tmp_path, *, status="pending", completed_at=None):
    base = tmp_path / "run" / "journals" / "001_A"
    entry = {
        "ordinal": 1,
        "journal_name": "A",
        "account_name": "A",
        "biz": "biz-a",
        "profile_url": "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=biz-a",
        "source_line": 1,
        "base": str(base),
        "pilot_dir": str(tmp_path / "pilot"),
        "status": status,
    }
    if completed_at is not None:
        entry["completed_at"] = completed_at
    return {"schema_version": 1, "expected_count": 1, "journals": [entry]}, entry


def test_rerun_clears_stale_completed_at_before_catalog_failure(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path, status="completed", completed_at="old")
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "_audit_entry", lambda *a, **k: {"ok": False, "audit_path": "audit.json"})
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("catalog_failed")))

    assert runner._run(_rollout_args(tmp_path)) == 1
    assert entry["status"] == "paused"
    assert entry["reason"] == "catalog_failed"
    assert "completed_at" not in entry
    assert entry["prior_completion_claim"]["completed_at"] == "old"


def test_capture_target_failure_is_checkpointed_as_paused(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(
        runner,
        "set_capture_target",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture_target_failed")),
    )

    assert runner._run(_rollout_args(tmp_path)) == 1
    assert entry["status"] == "paused"
    assert entry["reason"] == "capture_target_failed"


def test_completed_entry_audit_exception_is_not_left_completed_or_uncaught(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path, status="completed", completed_at="old")
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "_audit_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit_export_failed")))
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("catalog_failed")))

    assert runner._run(_rollout_args(tmp_path)) == 1
    assert entry["status"] == "paused"
    assert entry["reason"] == "catalog_failed"
    assert "completed_at" not in entry


def test_post_capture_audit_failure_requires_verification_not_plain_pause(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    catalog = {
        "schema_version": 2,
        "scope_observed": True,
        "account": "A",
        "active_tab": "全部",
        "terminal_visible": True,
        "items": [{"title": "T", "runtime": "1"}],
    }
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: catalog)

    def fake_journal(*args, **kwargs):
        base = Path(entry["base"])
        base.mkdir(parents=True, exist_ok=True)
        (base / "capture_state.json").write_text(
            json.dumps({"jobs": {"0": {"status": "captured"}}}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(runner, "run_journal", fake_journal)
    monkeypatch.setattr(runner, "_run_media_bundle", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "_audit_entry", lambda *a, **k: {"ok": False, "audit_path": "audit.json"})

    assert runner._run(_rollout_args(tmp_path)) == 1
    assert entry["status"] == "needs_verification"
    assert entry["reason"] == "audit_failed"
    assert "completed_at" not in entry


def test_roster_stops_before_next_journal_without_switching_target(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "is_stop_requested", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: pytest.fail("must not switch after stop request"))

    assert runner._run(_rollout_args(tmp_path)) == 130
    assert entry["status"] == "pending"


def test_roster_passes_stop_control_to_client_and_preserves_stop_exit(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    catalog = {
        "schema_version": 2,
        "scope_observed": True,
        "account": "A",
        "active_tab": "全部",
        "terminal_visible": True,
        "items": [{"title": "T", "runtime": "1"}],
    }
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "is_stop_requested", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: catalog)
    observed = []

    def fake_journal(argv, **kwargs):
        observed.append(argv)
        base = Path(entry["base"])
        base.mkdir(parents=True, exist_ok=True)
        (base / "capture_state.json").write_text(
            json.dumps({"jobs": {"0": {"status": "paused", "reason": "stop_requested"}}}),
            encoding="utf-8",
        )
        return 130

    monkeypatch.setattr(runner, "run_journal", fake_journal)

    assert runner._run(_rollout_args(tmp_path)) == 130
    argv = observed[0]
    assert argv[argv.index("--stop-control") + 1] == str(Path(_rollout_args(tmp_path).run_dir).resolve() / "run_control.json")
    assert entry["status"] == "paused"
    assert entry["reason"] == "stop_requested"


def test_roster_main_resets_old_stop_request_only_after_locks_are_held(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner
    from utils.run_control import is_stop_requested, request_stop

    run_dir = tmp_path / "run"
    pilot = tmp_path / "pilot"
    control = run_dir / "run_control.json"
    request_stop(control)
    observed = []
    monkeypatch.setattr(runner, "_run", lambda args: observed.append(is_stop_requested(control)) or 0)

    assert runner.main(["--run-dir", str(run_dir), "--pilot-dir", str(pilot)]) == 0
    assert observed == [False]


def test_refresh_runtime_allows_same_title_on_distinct_display_dates():
    from crawl.roster_client_runner import _refresh_catalog_runtime

    old = {
        "items": [
            {"title": "同名文章", "displayed_date": "1月1日", "runtime": "old-1"},
            {"title": "同名文章", "displayed_date": "1月2日", "runtime": "old-2"},
        ]
    }
    current = {
        "items": [
            {"title": "同名文章", "displayed_date": "1月2日", "runtime": "new-2"},
            {"title": "同名文章", "displayed_date": "1月1日", "runtime": "new-1"},
        ]
    }

    result = _refresh_catalog_runtime(old, current)
    assert [item["runtime"] for item in result["items"]] == ["new-1", "new-2"]


def _catalog_snapshot(*titles):
    return {
        "schema_version": 2,
        "scope_observed": True,
        "account": "A",
        "active_tab": "全部",
        "terminal_visible": True,
        "items": [
            {"title": title, "displayed_date": f"{index + 1}月1日", "runtime": f"r-{index}"}
            for index, title in enumerate(titles)
        ],
    }


def test_catalog_consensus_requires_two_identical_independent_sequences():
    from crawl.roster_client_runner import _select_catalog_consensus

    first = _catalog_snapshot("一", "二", "三")
    second = _catalog_snapshot("一", "二")
    third = _catalog_snapshot("一", "二", "三")
    third["items"][0]["runtime"] = "fresh-runtime"

    result = _select_catalog_consensus([first, second, third])
    assert len(result["items"]) == 3
    assert result["items"][0]["runtime"] == "fresh-runtime"
    assert result["catalog_consensus"]["matching_passes"] == [1, 3]
    assert result["catalog_consensus"]["attempted_passes"] == 3


def test_catalog_consensus_rejects_three_different_sequences():
    from crawl.roster_client_runner import _select_catalog_consensus

    with pytest.raises(RuntimeError, match="catalog_consensus_unverified"):
        _select_catalog_consensus([
            _catalog_snapshot("一", "二", "三"),
            _catalog_snapshot("一", "二"),
            _catalog_snapshot("一", "三"),
        ])


def test_catalog_consensus_does_not_collapse_duplicate_identity_rows():
    from crawl.roster_client_runner import _select_catalog_consensus

    duplicate = _catalog_snapshot("同名", "同名")
    duplicate["items"][1]["displayed_date"] = duplicate["items"][0]["displayed_date"]
    with pytest.raises(RuntimeError, match="catalog_item_identity_ambiguous"):
        _select_catalog_consensus([duplicate, duplicate])


def test_discover_catalog_runs_one_natural_top_to_terminal_scan(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    entry = {"base": str(tmp_path / "journal"), "account_name": "A"}
    calls = []
    switches = []
    monkeypatch.setattr(runner, "_switch_profile", lambda entry, hwnd: switches.append(hwnd) or 123)
    monkeypatch.setattr(runner, "DesktopWeChatDriver", lambda hwnd: SimpleNamespace(focus=lambda: None))

    run_kwargs = []

    def fake_run(command, **kwargs):
        calls.append(command)
        run_kwargs.append(kwargs)
        out = Path(command[command.index("-OutputDir") + 1])
        snapshot = _catalog_snapshot("一", "二", "三")
        (out / "catalog_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._discover_catalog(entry, 0, 10)

    assert switches == [0]
    assert len(calls) == 1
    assert run_kwargs[0]["errors"] == "replace"
    command = calls[0]
    assert command[command.index("-ExpectedFirstTitle") + 1] == ""
    assert Path(command[command.index("-OutputDir") + 1]).name == "pass_01"
    assert result["catalog_scan"] == {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": 3,
        "evidence_path": f"catalog_scan/{Path(command[command.index('-OutputDir') + 1]).parent.name}/pass_01/catalog_snapshot.json",
    }
    assert "catalog_consensus" not in result
    frozen = json.loads((Path(entry["base"]) / "catalog_verified_snapshot.json").read_text(encoding="utf-8"))
    assert frozen["catalog_scan"] == result["catalog_scan"]


def test_discover_catalog_resumes_verified_frames_after_interruption(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    entry = {"base": str(tmp_path / "journal"), "account_name": "A"}
    calls = []
    focused = []
    monkeypatch.setattr(runner, "_switch_profile", lambda entry, hwnd: 123)
    monkeypatch.setattr(
        runner,
        "DesktopWeChatDriver",
        lambda hwnd: SimpleNamespace(focus=lambda: focused.append(hwnd)),
    )

    frame = {
        "schema_version": 2,
        "scope_observed": True,
        "at": "2026-09-09T00:00:00+00:00",
        "step": 0,
        "account": "A",
        "active_tab": "全部",
        "document_runtime": "42,1",
        "items": [{"title": "一", "displayed_date": "", "runtime": "r-1", "card_class": "article-item"}],
        "terminal_visible": False,
        "terminal_label": "",
        "loading": False,
        "load_error": False,
        "reached_tail": False,
        "full_history_complete": False,
        "status": "scanning",
        "source": "wechat_uia_catalog_navigation_only",
    }

    def fake_run(command, **kwargs):
        calls.append(command)
        out = Path(command[command.index("-OutputDir") + 1])
        if len(calls) == 1:
            (out / "catalog_frames.jsonl").write_text(
                json.dumps(frame, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=1, stderr="foreground_changed")
        resume = Path(command[command.index("-ResumeFramesPath") + 1])
        supplied_hash = command[command.index("-ResumeFramesSha256") + 1]
        assert resume == Path(calls[0][calls[0].index("-OutputDir") + 1]) / "catalog_frames.jsonl"
        assert supplied_hash == hashlib.sha256(resume.read_bytes()).hexdigest()
        snapshot = _catalog_snapshot("一", "二", "三")
        (out / "catalog_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._discover_catalog(entry, 0, 10)

    assert len(calls) == 2
    assert focused == [123, 123]
    assert result["catalog_resume"] == {
        "segments": 2,
        "interrupted_segments": 1,
        "same_document_runtime": True,
    }
    assert Path(result["catalog_scan"]["evidence_path"]).parts[-2:] == (
        "resume_02",
        "catalog_snapshot.json",
    )


def test_discover_catalog_reuses_ready_single_scan_on_live_window(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    base = tmp_path / "journal"
    entry = {"base": str(base), "account_name": "A", "browser_hwnd": 123}
    evidence = _catalog_snapshot("同名", "同名")
    for item in evidence["items"]:
        item["displayed_date"] = ""
    relative = "catalog_scan/test/pass_01/catalog_snapshot.json"
    evidence_path = base / relative
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    snapshot = dict(evidence)
    snapshot["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": 2,
        "evidence_path": relative,
    }
    (base / "catalog_verified_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    focused = []
    monkeypatch.setattr(runner, "DesktopWeChatDriver", lambda hwnd: SimpleNamespace(focus=lambda: focused.append(hwnd)))
    monkeypatch.setattr(runner, "_switch_profile", lambda *a, **k: pytest.fail("ready catalog must not switch profile"))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("ready catalog must not rescan"))

    result = runner._discover_catalog(entry, 0, 10)

    assert result == snapshot
    assert focused == [123]


def test_discover_catalog_does_not_freeze_nonterminal_single_scan(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from crawl import roster_client_runner as runner

    entry = {"base": str(tmp_path / "journal"), "account_name": "A"}
    monkeypatch.setattr(runner, "_switch_profile", lambda entry, hwnd: 123)
    monkeypatch.setattr(runner, "DesktopWeChatDriver", lambda hwnd: SimpleNamespace(focus=lambda: None))

    def fake_run(command, **kwargs):
        out = Path(command[command.index("-OutputDir") + 1])
        snapshot = _catalog_snapshot("一", "二", "三")
        snapshot["terminal_visible"] = False
        (out / "catalog_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="catalog_scope_unverified"):
        runner._discover_catalog(entry, 0, 10)
    assert not (Path(entry["base"]) / "catalog_verified_snapshot.json").exists()


def test_final_catalog_validation_accepts_single_scan_metadata():
    from crawl.roster_client_runner import _validate_catalog

    snapshot = _catalog_snapshot("一", "二")
    with pytest.raises(RuntimeError, match="catalog_evidence_unverified"):
        _validate_catalog(snapshot, "A", require_evidence=True)
    snapshot["catalog_scan"] = {
        "attempted_passes": 1,
        "accepted_pass": 1,
        "item_count": 2,
        "evidence_path": "catalog_scan/test/pass_01/catalog_snapshot.json",
    }
    _validate_catalog(snapshot, "A", require_evidence=True)


def test_refresh_runtime_uses_unique_title_fallback_across_date_schema_change():
    from crawl.roster_client_runner import _refresh_catalog_runtime

    old = {"items": [{"title": "唯一文章", "displayed_date": "2026年1月1日", "runtime": "old"}]}
    current = {"items": [{"title": "唯一文章", "displayed_date": "", "runtime": "new"}]}
    result = _refresh_catalog_runtime(old, current)
    assert result["items"][0]["runtime"] == "new"


def test_refresh_runtime_rejects_duplicate_title_when_dates_cannot_disambiguate():
    from crawl.roster_client_runner import _refresh_catalog_runtime

    old = {"items": [
        {"title": "同名", "displayed_date": "1月1日", "runtime": "old-1"},
        {"title": "同名", "displayed_date": "1月2日", "runtime": "old-2"},
    ]}
    current = {"items": [
        {"title": "同名", "displayed_date": "", "runtime": "new-1"},
        {"title": "同名", "displayed_date": "", "runtime": "new-2"},
    ]}
    with pytest.raises(RuntimeError, match="catalog_title_ambiguous"):
        _refresh_catalog_runtime(old, current)


def test_background_phase_never_runs_client_preflight_or_capture_controls(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    args = _rollout_args(tmp_path)
    args.phase = "background-only"
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: pytest.fail("background must not preflight client"))
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: pytest.fail("background must not change capture target"))
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: pytest.fail("background must not inspect client catalog"))
    monkeypatch.setattr(runner, "run_journal", lambda *a, **k: pytest.fail("background must not navigate client"))
    observed = []
    monkeypatch.setattr(runner, "run_background", lambda **kw: observed.append(kw) or 0, raising=False)

    assert runner._run(args) == 0
    assert observed[0]["base"] == entry["base"]
    assert entry["status"] == "completed"
    assert entry["phase"] == "background-only"


def test_capture_first_passes_capture_only_and_records_cache_ready(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, entry = _rollout_manifest(tmp_path)
    args = _rollout_args(tmp_path)
    args.phase = "capture-first"
    catalog = {"items": [{"title": "T", "runtime": "r1"}]}
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "is_stop_requested", lambda *a, **k: False)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda *a, **k: catalog)
    observed = []

    def fake_journal(argv, **kwargs):
        observed.append(argv)
        base = Path(entry["base"]); base.mkdir(parents=True, exist_ok=True)
        (base / "capture_cache_summary.json").write_text(json.dumps({
            "catalog_count": 1, "complete_count": 1, "partial_count": 0,
            "missing_count": 0, "complete": True, "recapture_indices": [],
        }), encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "run_journal", fake_journal)
    assert runner._run(args) == 0
    assert "--capture-only" in observed[0]
    assert entry["status"] == "cache_ready"
    assert entry["cache_complete_count"] == 1
    assert "completed_at" not in entry


def test_capture_first_continues_after_partial_cache_and_returns_two(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, first = _rollout_manifest(tmp_path)
    second = dict(first, ordinal=2, journal_name="B", account_name="B", biz="biz-b",
                  base=str(tmp_path / "run" / "journals" / "002_B"), status="pending")
    manifest["expected_count"] = 2
    manifest["journals"] = [first, second]
    args = _rollout_args(tmp_path)
    args.phase = "capture-first"; args.max_journals = 0
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "is_stop_requested", lambda *a, **k: False)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda entry, *a, **k: {"items": [{"title": entry["account_name"], "runtime": "r"}]})

    def fake_journal(argv, **kwargs):
        base = Path(argv[argv.index("--base") + 1]); base.mkdir(parents=True, exist_ok=True)
        partial = base.name == "001_A"
        (base / "capture_cache_summary.json").write_text(json.dumps({
            "catalog_count": 1, "complete_count": 0 if partial else 1,
            "partial_count": 1 if partial else 0, "missing_count": 0,
            "complete": not partial, "recapture_indices": [0] if partial else [],
        }), encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "run_journal", fake_journal)
    assert runner._run(args) == 2
    assert [entry["status"] for entry in manifest["journals"]] == ["cache_partial", "cache_ready"]


def test_capture_first_continues_after_missing_cache_and_returns_two(tmp_path, monkeypatch):
    from crawl import roster_client_runner as runner

    manifest, first = _rollout_manifest(tmp_path)
    second = dict(first, ordinal=2, journal_name="B", account_name="B", biz="biz-b",
                  base=str(tmp_path / "run" / "journals" / "002_B"), status="pending")
    manifest["expected_count"] = 2
    manifest["journals"] = [first, second]
    args = _rollout_args(tmp_path)
    args.phase = "capture-first"; args.max_journals = 0
    monkeypatch.setattr(runner, "_load_or_create_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(runner, "_preflight", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "is_stop_requested", lambda *a, **k: False)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_discover_catalog", lambda entry, *a, **k: {"items": [{"title": entry["account_name"], "runtime": "r"}]})

    def fake_journal(argv, **kwargs):
        base = Path(argv[argv.index("--base") + 1]); base.mkdir(parents=True, exist_ok=True)
        missing = base.name == "001_A"
        (base / "capture_cache_summary.json").write_text(json.dumps({
            "catalog_count": 1, "complete_count": 0 if missing else 1,
            "partial_count": 0, "missing_count": 1 if missing else 0,
            "complete": not missing, "recapture_indices": [0] if missing else [],
        }), encoding="utf-8")
        return 1 if missing else 0

    monkeypatch.setattr(runner, "run_journal", fake_journal)

    assert runner._run(args) == 2
    assert [entry["status"] for entry in manifest["journals"]] == ["cache_partial", "cache_ready"]
    assert first["cache_missing_count"] == 1
    assert second["cache_complete_count"] == 1


def test_production_catalog_step_defaults_cover_long_histories():
    project = Path(__file__).resolve().parents[1]
    assert 'parser.add_argument("--max-steps", type=int, default=2000)' in (
        project / "crawl" / "roster_client_runner.py"
    ).read_text(encoding="utf-8")
    assert 'client_roster_parser.add_argument("--max-steps", type=int, default=2000)' in (
        project / "main.py"
    ).read_text(encoding="utf-8")
    for relative in (
        Path("crawl/client_catalog.ps1"),
        Path("scripts/start_capture_first.ps1"),
        Path("scripts/start_full_run.ps1"),
    ):
        source = (project / relative).read_text(encoding="utf-8-sig")
        assert "[int]$MaxSteps = 2000" in source, relative.as_posix()
