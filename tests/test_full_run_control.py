import json
from pathlib import Path

import pytest


def test_run_control_reset_and_request_are_atomic_and_typed(tmp_path):
    from utils.run_control import is_stop_requested, request_stop, reset_stop

    path = tmp_path / "run_control.json"
    reset_stop(path)
    assert is_stop_requested(path) is False
    request_stop(path)
    assert is_stop_requested(path) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["stop_requested"] is True
    assert payload["requested_at"]
    assert not path.with_suffix(".json.tmp").exists()


def test_run_control_rejects_malformed_payload_instead_of_continuing(tmp_path):
    from utils.run_control import is_stop_requested

    path = tmp_path / "run_control.json"
    path.write_text('{"stop_requested":"true"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="run_control_invalid"):
        is_stop_requested(path)


def test_full_run_scripts_exist_and_use_owned_control_file_only():
    root = Path(__file__).parents[1]
    expected = [
        "check_full_run.ps1",
        "start_full_run.ps1",
        "show_full_run_status.ps1",
        "stop_full_run.ps1",
    ]
    for name in expected:
        path = root / "scripts" / name
        assert path.exists(), name
        assert path.read_bytes().startswith(b"\xef\xbb\xbf"), name
        text = path.read_text(encoding="utf-8-sig")
        assert "run_control.json" in text or name == "check_full_run.ps1"
        for unsafe in ("Stop-Process", "taskkill", "TerminateProcess", "kill -9"):
            assert unsafe not in text


def test_stop_script_requests_marker_without_killing_process():
    root = Path(__file__).parents[1]
    text = (root / "scripts" / "stop_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "utils.run_control" in text
    assert "request" in text
    assert "Stop-Process" not in text


def test_status_script_prioritizes_active_journal_and_reports_running_article(tmp_path):
    import subprocess

    root = Path(__file__).parents[1]
    run_dir = tmp_path / "run"
    first = run_dir / "journals" / "001_A"
    second = run_dir / "journals" / "002_B"
    second.mkdir(parents=True)
    manifest = {
        "expected_count": 2,
        "journals": [
            {"ordinal": 1, "account_name": "A", "base": str(first), "status": "paused"},
            {"ordinal": 2, "account_name": "B", "base": str(second), "status": "running"},
        ],
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (second / "capture_state.json").write_text(
        json.dumps(
            {
                "jobs": {
                    "0": {"status": "captured", "title": "完成文章"},
                    "1": {"status": "running", "title": "当前文章", "attempt": 2},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(root / "scripts" / "show_full_run_status.ps1"), "-RunDir", str(run_dir)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["current_journal"]["ordinal"] == 2
    assert payload["current_article"]["title"] == "当前文章"
    assert payload["current_article"]["attempt"] == 2


def test_stop_script_writes_only_the_owned_stop_request(tmp_path):
    import subprocess
    from utils.run_control import is_stop_requested

    root = Path(__file__).parents[1]
    run_dir = tmp_path / "run"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(root / "scripts" / "stop_full_run.ps1"), "-RunDir", str(run_dir)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert is_stop_requested(run_dir / "run_control.json") is True
    assert "温和停止" in result.stdout
    assert list(run_dir.iterdir()) == [run_dir / "run_control.json"]
