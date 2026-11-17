import os
import subprocess
import sys

import pytest

from utils.runtime_lock import runtime_lock


def test_other_process_cannot_acquire_live_lock_and_can_after_release(tmp_path):
    path = tmp_path / "capture.lock"
    code = "from utils.runtime_lock import runtime_lock; import sys;\nwith runtime_lock(sys.argv[1]): print('acquired')"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with runtime_lock(path):
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert result.returncode != 0
        assert "capture_already_running" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == 0
    assert "acquired" in result.stdout


def test_exception_releases_lock(tmp_path):
    path = tmp_path / "capture.lock"
    with pytest.raises(ValueError):
        with runtime_lock(path):
            raise ValueError("test")
    with runtime_lock(path):
        pass
