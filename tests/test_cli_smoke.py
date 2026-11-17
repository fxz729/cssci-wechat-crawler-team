import subprocess
import sys
from pathlib import Path


def test_cli_help_lists_core_commands():
    project_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, str(project_root / "main.py"), "--help"],
        cwd=project_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "import-journals" in result.stdout
    assert "discover-accounts" in result.stdout
    assert "stats" in result.stdout
