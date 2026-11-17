import gzip
import json
from types import SimpleNamespace

import pytest

from crawl import msaa_roster_runner as runner
from crawl import roster_client_runner


def test_run_resets_stop_control(tmp_path, monkeypatch):
    control = tmp_path / "run_control.json"
    control.write_text(
        json.dumps({"schema_version": 1, "stop_requested": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_run_locked", lambda args, run_dir: 0)

    assert runner.run(SimpleNamespace(run_dir=tmp_path, preflight_only=False)) == 0
    assert json.loads(control.read_text(encoding="utf-8"))["stop_requested"] is False


def test_preflight_does_not_reset_stop_control(tmp_path, monkeypatch):
    control = tmp_path / "run_control.json"
    control.write_text(
        json.dumps({"schema_version": 1, "stop_requested": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_run_locked", lambda args, run_dir: 0)

    assert runner.run(SimpleNamespace(run_dir=tmp_path, preflight_only=True)) == 0
    assert json.loads(control.read_text(encoding="utf-8"))["stop_requested"] is True


def test_switch_profile_accepts_success_payload(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        payload = (
            b'{"success":true,"search_hwnd":1}'
            if len(calls) == 1
            else b'{"success":true,"profile_hwnd":123,"mode":"msaa"}'
        )
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.switch_profile_by_name("苏区研究") == 123
    assert len(calls) == 2


def test_switch_profile_retries_transient_failure(monkeypatch):
    calls = []

    def fake_switch(account_name, *, wait_seconds=30):
        calls.append(account_name)
        if len(calls) == 1:
            raise RuntimeError("profile_search_failed")
        return 321

    monkeypatch.setattr(runner, "switch_profile_by_name", fake_switch)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    assert runner._switch_profile_with_retry("学校党建与思想教育", wait_seconds=1) == 321
    assert calls == ["学校党建与思想教育", "学校党建与思想教育"]


def test_switch_profile_rejects_name_mismatch(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        payload = (
            b'{"success":true,"search_hwnd":1}'
            if len(calls) == 1
            else b'{"success":false,"error_code":"profile_name_timeout"}'
        )
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="profile_name_timeout"):
        runner.switch_profile_by_name("党的文献")


def test_switch_profile_search_timeout_is_explicit(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout=b'{"success":true,"search_hwnd":1}')
        raise runner.subprocess.TimeoutExpired("powershell", 1)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="profile_search_timeout"):
        runner.switch_profile_by_name("党的文献")


def test_switch_profile_open_timeout_is_explicit(monkeypatch):
    def timeout(*args, **kwargs):
        raise runner.subprocess.TimeoutExpired("powershell", 1)

    monkeypatch.setattr(runner.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="search_window_open_timeout"):
        runner.switch_profile_by_name("党的文献")


def test_profile_search_failure_is_retryable_not_permanent_skip(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    entry = {
        "ordinal": 21,
        "journal_name": "科学与无神论",
        "account_name": "科学与无神论",
        "biz": "biz-a",
        "base": str(run_dir / "journals" / "021_科学与无神论"),
        "status": "pending",
    }
    manifest = {"journals": [entry]}
    monkeypatch.setattr(
        runner,
        "_load_or_create_manifest",
        lambda *args, **kwargs: manifest,
    )
    monkeypatch.setattr(
        runner,
        "_capture_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("profile_search_failed")
        ),
    )
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=1,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=True,
    )

    assert runner._run_locked(args, run_dir) == 1
    assert entry["status"] == "msaa_failed"
    assert entry["reason"] == "profile_search_failed"


def test_entry_completed_requires_marker_or_completed_state(tmp_path):
    base = tmp_path / "journal"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "文章数据.csv").write_text("biz,mid\n", encoding="utf-8")
    entry = {"base": str(base)}

    assert runner._entry_completed(entry) is False

    msaa = base / "msaa"
    msaa.mkdir()
    (msaa / "state.json").write_text(
        json.dumps({"completed": False}),
        encoding="utf-8",
    )
    assert runner._entry_completed(entry) is False

    (msaa / "state.json").write_text(
        json.dumps({"completed": True}),
        encoding="utf-8",
    )
    assert runner._entry_completed(entry) is True

    runner._write_completion_marker(
        entry,
        {"imported": 2, "csv": {"article_count": 2}},
    )
    (msaa / "state.json").write_text(
        json.dumps({"completed": True, "skipped_titles": ["待补采"]}),
        encoding="utf-8",
    )
    assert runner._entry_completed(entry) is True

    (msaa / "state.json").write_text(
        json.dumps({"completed": False}),
        encoding="utf-8",
    )
    runner._write_completion_marker(
        entry,
        {"imported": 2, "csv": {"article_count": 2}},
    )
    assert runner._entry_completed(entry) is True


def test_run_entry_writes_marker_only_on_zero_return_code(tmp_path, monkeypatch):
    base = tmp_path / "journal"
    msaa = base / "msaa"
    msaa.mkdir(parents=True)
    control = tmp_path / "pilot" / "capture_control.json"
    control.parent.mkdir()
    control.write_text("{}", encoding="utf-8")
    entry = {
        "base": str(base),
        "biz": "biz-a",
        "account_name": "测试刊",
        "journal_name": "测试刊",
        "profile_url": "https://example.com/profile",
    }
    args = SimpleNamespace(
        seed_dir=tmp_path / "seed",
        seed_biz="biz-a",
        capture_inbox=tmp_path / "pilot" / "capture_inbox.jsonl",
        journal_links=tmp_path / "links.md",
        media_workers=0,
        profile_wait_seconds=1,
        delay_seconds=1,
        wait_seconds=1,
        scroll_delta=-120,
        max_idle_scrolls=1,
    )
    monkeypatch.setattr(runner, "switch_profile_by_name", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "_import_entry",
        lambda entry, args, **kwargs: {"imported": 1, "csv": {"article_count": 1}},
    )

    captured_capture = {}

    def fake_capture(**kwargs):
        captured_capture.update(kwargs)
        return 0

    monkeypatch.setattr(runner, "run_capture", fake_capture)
    summary = runner._run_entry(entry, args)
    assert summary["completion_marker"]
    assert runner._entry_completed(entry) is True
    assert captured_capture["refresh_article"] is False

    runner._completion_marker_path(entry).unlink()
    monkeypatch.setattr(runner, "run_capture", lambda **kwargs: 1)
    summary = runner._run_entry(entry, args)
    assert summary["completion_marker"] == ""
    assert runner._entry_completed(entry) is False


def test_run_locked_does_not_skip_partial_csv(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_部分刊"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "文章数据.csv").write_text("biz,mid\n", encoding="utf-8")
    entry = {
        "ordinal": 1,
        "journal_name": "部分刊",
        "account_name": "部分刊",
        "biz": "biz-a",
        "base": str(base),
    }
    manifest = {"journals": [entry]}
    monkeypatch.setattr(
        runner,
        "_load_or_create_manifest",
        lambda *args, **kwargs: manifest,
    )
    called = []

    class FakeProcess:
        def poll(self):
            return 0

    class FakeLog:
        def close(self):
            pass

    fake_pending = {
        "entry": entry,
        "process": FakeProcess(),
        "log_handle": FakeLog(),
        "captured": {"return_code": 0, "profile_hwnd": 1},
    }
    monkeypatch.setattr(
        runner,
        "_capture_entry",
        lambda entry, args: (
            called.append(entry["ordinal"])
            or {
                "return_code": 0,
                "profile_hwnd": 1,
                "import_inbox": str(tmp_path / "inbox.jsonl"),
                "archive": "",
            }
        ),
    )
    monkeypatch.setattr(
        runner,
        "_start_import_process",
        lambda entry, args, captured: fake_pending,
    )
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=1,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=True,
    )

    assert runner._run_locked(args, run_dir) == 0
    assert called == [1]
    runner._write_completion_marker(
        entry,
        {"imported": 1, "csv": {"article_count": 1}},
    )
    assert runner._run_locked(args, run_dir) == 0
    assert called == [1]


def test_run_locked_continues_after_partial_capture(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    first = {
        "ordinal": 1,
        "journal_name": "第一刊",
        "account_name": "第一刊",
        "biz": "biz-a",
        "base": str(run_dir / "journals" / "001_第一刊"),
    }
    second = {
        "ordinal": 2,
        "journal_name": "第二刊",
        "account_name": "第二刊",
        "biz": "biz-b",
        "base": str(run_dir / "journals" / "002_第二刊"),
    }
    manifest = {"journals": [first, second]}
    monkeypatch.setattr(
        runner,
        "_load_or_create_manifest",
        lambda *args, **kwargs: manifest,
    )
    called = []
    monkeypatch.setattr(
        runner,
        "_capture_entry",
        lambda entry, args: (
            called.append(entry["ordinal"])
            or {
                "return_code": 1,
                "profile_hwnd": 1,
                "import_inbox": "",
                "archive": "",
            }
        ),
    )
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=2,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=True,
    )

    assert runner._run_locked(args, run_dir) == 1
    assert called == [1, 2]
    assert first["status"] == "msaa_partial"
    assert second["status"] == "msaa_partial"



def test_stalled_capture_is_added_to_independent_backfill_queue(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "006_学校党建与思想教育"
    (base / "msaa").mkdir(parents=True)
    (base / "msaa" / "state.json").write_text(
        json.dumps({"stop_reason": "capture_stalled_no_articles:5"}),
        encoding="utf-8",
    )
    entry = {
        "ordinal": 6,
        "journal_name": "学校党建与思想教育",
        "account_name": "学校党建与思想教育",
        "biz": "MzIyNzc5NTk3MQ==",
        "base": str(base),
        "status": "pending",
    }
    manifest = {"journals": [entry]}
    monkeypatch.setattr(
        runner,
        "_load_or_create_manifest",
        lambda *args, **kwargs: manifest,
    )
    monkeypatch.setattr(
        runner,
        "_capture_entry",
        lambda *args, **kwargs: {
            "return_code": 1,
            "profile_hwnd": 1,
            "import_inbox": "",
            "archive": "",
        },
    )
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=1,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=True,
    )

    assert runner._run_locked(args, run_dir) == 0
    assert entry["status"] == "needs_backfill"
    queue = (run_dir / "journal_backfill_queue.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(queue[0])
    assert record["ordinal"] == 6
    assert record["journal_name"] == "学校党建与思想教育"
    assert record["reason"] == "requires_independent_backfill"


def test_stop_does_not_overwrite_completed_journal(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first_base = run_dir / "journals" / "001_第一刊"
    (first_base / "bundle").mkdir(parents=True)
    (first_base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\n", encoding="utf-8-sig"
    )
    first = {
        "ordinal": 1,
        "journal_name": "第一刊",
        "account_name": "第一刊",
        "biz": "biz-a",
        "base": str(first_base),
        "status": "completed",
    }
    runner._write_completion_marker(first, {"imported": 1})
    second = {
        "ordinal": 2,
        "journal_name": "第二刊",
        "account_name": "第二刊",
        "biz": "biz-b",
        "base": str(run_dir / "journals" / "002_第二刊"),
        "status": "pending",
    }
    manifest = {"journals": [first, second]}
    monkeypatch.setattr(
        runner,
        "_load_or_create_manifest",
        lambda *args, **kwargs: manifest,
    )
    monkeypatch.setattr(
        runner,
        "_capture_entry",
        lambda *args, **kwargs: pytest.fail("capture must not start while stopping"),
    )
    (run_dir / "run_control.json").write_text(
        json.dumps({"schema_version": 1, "stop_requested": True}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=2,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=True,
    )

    assert runner._run_locked(args, run_dir) == 130
    assert first["status"] == "completed"
    assert second["status"] == "paused"
    assert second["reason"] == "stop_requested"



def test_archive_capture_inbox_compresses_and_resets_source(tmp_path):
    inbox = tmp_path / "capture_inbox.jsonl"
    content = b'{"kind":"article","mid":"1"}\n'
    inbox.write_bytes(content)

    archive = runner._archive_capture_inbox(inbox, ordinal=3, biz="MjM5Nzk1NTc4MA==")

    assert archive is not None
    assert archive.is_file()
    assert archive.parent.name == "archive"
    with gzip.open(archive, "rb") as stream:
        assert stream.read() == content
    assert inbox.read_bytes() == b""


def test_archive_capture_inbox_skips_missing_or_empty(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert runner._archive_capture_inbox(missing, ordinal=1, biz="biz") is None

    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert runner._archive_capture_inbox(empty, ordinal=1, biz="biz") is None
    assert empty.read_bytes() == b""


def test_archive_capture_inbox_falls_back_to_plain_snapshot(tmp_path, monkeypatch):
    inbox = tmp_path / "capture_inbox.jsonl"
    inbox.write_bytes(b"data\n")

    def boom(*args, **kwargs):
        raise OSError("gzip unavailable")

    monkeypatch.setattr(runner.gzip, "open", boom)
    archive = runner._archive_capture_inbox(inbox, ordinal=1, biz="biz")
    assert archive is not None
    assert archive.suffix == ".jsonl"
    assert archive.read_bytes() == b"data\n"
    assert inbox.read_bytes() == b""


def test_archive_capture_inbox_keeps_source_when_compressed_and_plain_copy_fail(tmp_path, monkeypatch):
    inbox = tmp_path / "capture_inbox.jsonl"
    inbox.write_bytes(b"data\n")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner.gzip, "open", boom)
    monkeypatch.setattr(runner.shutil, "copy2", boom)
    assert runner._archive_capture_inbox(inbox, ordinal=1, biz="biz") is None
    assert inbox.read_bytes() == b"data\n"


def test_capture_entry_archives_inbox_after_run(tmp_path, monkeypatch):
    base = tmp_path / "journal"
    (base / "msaa").mkdir(parents=True)
    control = tmp_path / "pilot" / "capture_control.json"
    control.parent.mkdir()
    control.write_text("{}", encoding="utf-8")
    inbox = control.parent / "capture_inbox.jsonl"
    inbox.write_bytes(b'{"kind":"article"}\n')
    entry = {
        "ordinal": 3,
        "journal_name": "测试刊",
        "account_name": "测试刊",
        "biz": "biz-a",
        "base": str(base),
        "profile_url": "https://example.com/profile",
    }
    args = SimpleNamespace(
        seed_dir=tmp_path / "seed",
        seed_biz="biz-a",
        capture_inbox=inbox,
        profile_wait_seconds=1,
        delay_seconds=1,
        wait_seconds=1,
        scroll_delta=-120,
        max_idle_scrolls=1,
        media_workers=0,
    )
    monkeypatch.setattr(runner, "switch_profile_by_name", lambda *a, **k: 123)
    monkeypatch.setattr(runner, "set_capture_target", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_capture_enabled", lambda *a, **k: None)
    monkeypatch.setattr(runner, "run_capture", lambda **kwargs: 0)

    captured = runner._capture_entry(entry, args)

    assert captured["return_code"] == 0
    assert captured["archive"].endswith(".jsonl.gz")
    assert captured["import_inbox"] == captured["archive"]
    assert inbox.read_bytes() == b""


def test_start_import_process_uses_low_priority_and_worker_count(tmp_path, monkeypatch):
    base = tmp_path / "journal"
    (base / "msaa").mkdir(parents=True)
    entry = {
        "ordinal": 1,
        "journal_name": "测试刊",
        "account_name": "测试刊",
        "biz": "biz-a",
        "base": str(base),
    }
    args = SimpleNamespace(journal_links=tmp_path / "links.md", media_workers=4)
    captured = {"return_code": 0, "import_inbox": str(tmp_path / "inbox.jsonl.gz")}
    calls = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    pending = runner._start_import_process(entry, args, captured)

    assert pending["process"] is not None
    assert "--media-workers" in calls["command"]
    assert "4" in calls["command"]
    assert calls["kwargs"]["cwd"] == runner.PROJECT_ROOT
    if runner.os.name == "nt":
        assert calls["kwargs"]["creationflags"] != 0


def test_msaa_runner_defaults_to_four_media_workers(monkeypatch):
    captured = {}
    monkeypatch.setattr(runner, "run", lambda args: captured.update(vars(args)) or 0)

    assert runner.main([]) == 0
    assert captured["media_workers"] == 4


def test_finish_import_writes_completion_marker(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = tmp_path / "journal"
    (base / "msaa").mkdir(parents=True)
    entry = {
        "ordinal": 1,
        "journal_name": "测试刊",
        "account_name": "测试刊",
        "biz": "biz-a",
        "base": str(base),
    }
    manifest = {"journals": [entry]}
    progress = run_dir / "progress.jsonl"
    (base / "msaa_import_report.json").write_text(
        json.dumps({"imported": 2, "csv": {"article_count": 2}}),
        encoding="utf-8",
    )

    class FakeProcess:
        def poll(self):
            return 0

    class FakeLog:
        def close(self):
            pass

    pending = {
        "entry": entry,
        "process": FakeProcess(),
        "log_handle": FakeLog(),
        "captured": {"return_code": 0, "profile_hwnd": 1},
    }

    assert runner._finish_import(
        pending, run_dir=run_dir, manifest=manifest, progress=progress,
    ) is True
    assert entry["status"] == "completed"
    assert runner._entry_completed(entry) is True


def test_run_locked_overlaps_import_with_next_capture(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    entries = [
        {
            "ordinal": ordinal,
            "journal_name": f"刊{ordinal}",
            "account_name": f"刊{ordinal}",
            "biz": f"biz-{ordinal}",
            "base": str(run_dir / "journals" / f"{ordinal:03d}"),
        }
        for ordinal in (1, 2)
    ]
    manifest = {"journals": entries}
    monkeypatch.setattr(
        runner, "_load_or_create_manifest", lambda *a, **k: manifest,
    )
    state = {"captures": [], "finished": set()}

    class FakeProcess:
        def __init__(self, ordinal):
            self.ordinal = ordinal

        def poll(self):
            if self.ordinal == 1:
                return 0 if 1 in state["finished"] else None
            return 0 if len(state["captures"]) >= 2 else None

    class FakeLog:
        def close(self):
            pass

    def fake_capture(entry, args):
        state["captures"].append(entry["ordinal"])
        if entry["ordinal"] == 2:
            state["finished"].add(1)
        return {
            "return_code": 0,
            "profile_hwnd": 1,
            "import_inbox": str(tmp_path / "inbox.jsonl"),
            "archive": "",
        }

    def fake_start(entry, args, captured):
        return {
            "entry": entry,
            "process": FakeProcess(entry["ordinal"]),
            "log_handle": FakeLog(),
            "captured": captured,
        }

    monkeypatch.setattr(runner, "_capture_entry", fake_capture)
    monkeypatch.setattr(runner, "_start_import_process", fake_start)
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=2,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=False,
    )

    assert runner._run_locked(args, run_dir) == 0
    assert state["captures"] == [1, 2]
    assert entries[0]["status"] == "completed"
    assert entries[1]["status"] == "completed"



def test_run_locked_continues_when_background_import_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    entries = [
        {
            "ordinal": ordinal,
            "journal_name": f"刊{ordinal}",
            "account_name": f"刊{ordinal}",
            "biz": f"biz-{ordinal}",
            "base": str(run_dir / "journals" / f"{ordinal:03d}"),
        }
        for ordinal in (1, 2)
    ]
    manifest = {"journals": entries}
    monkeypatch.setattr(
        runner, "_load_or_create_manifest", lambda *a, **k: manifest,
    )
    state = {"captures": []}

    class FakeProcess:
        def poll(self):
            return 1

    class FakeLog:
        def close(self):
            pass

    def fake_capture(entry, args):
        state["captures"].append(entry["ordinal"])
        return {
            "return_code": 0,
            "profile_hwnd": 1,
            "import_inbox": str(tmp_path / "inbox.jsonl"),
            "archive": "",
        }

    def fake_start(entry, args, captured):
        return {
            "entry": entry,
            "process": FakeProcess(),
            "log_handle": FakeLog(),
            "captured": captured,
        }

    monkeypatch.setattr(runner, "_capture_entry", fake_capture)
    monkeypatch.setattr(runner, "_start_import_process", fake_start)
    args = SimpleNamespace(
        journal_links=tmp_path / "links.md",
        expected_count=2,
        start=1,
        max_journals=0,
        preflight_only=False,
        import_only=False,
        skip_completed=False,
    )

    assert runner._run_locked(args, run_dir) == 1
    assert state["captures"] == [1, 2]
    assert entries[0]["status"] == "msaa_failed"
    assert entries[1]["status"] == "msaa_failed"

def test_load_manifest_rebases_moved_absolute_paths(tmp_path, monkeypatch):
    run_dir = tmp_path / "moved" / "output" / "rollout" / "full_126"
    run_dir.mkdir(parents=True)
    links = tmp_path / "links.md"
    links.write_text("", encoding="utf-8")
    old_base = r"D:\old\cssci-wechat-crawler-team\output\rollout\full_126\journals\001_Test"
    manifest = {
        "expected_count": 1,
        "source_sha256": "same",
        "journals": [{
            "ordinal": 1, "journal_name": "Test", "account_name": "Test",
            "biz": "biz", "profile_url": "url", "base": old_base,
            "pilot_dir": r"D:\old\cssci-wechat-crawler-team\output\rollout\full_126\capture",
            "status": "pending",
        }],
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = SimpleNamespace(
        ordinal=1, journal_name="Test", account_name="Test", biz="biz",
        profile_url="url", source_line=1,
        source_file=str(links),
    )
    roster = SimpleNamespace(targets=[target], source_sha256="same")
    monkeypatch.setattr(roster_client_runner, "load_fixed_roster", lambda *_args, **_kwargs: roster)

    result = runner._load_or_create_manifest(run_dir, links, 1)

    assert result["journals"][0]["base"] == str(run_dir / "journals" / "001_Test")
    assert result["journals"][0]["pilot_dir"] == str(run_dir / "capture")
    saved = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert saved["journals"][0]["base"] == str(run_dir / "journals" / "001_Test")
