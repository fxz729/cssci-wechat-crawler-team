import json
from types import SimpleNamespace

from crawl import roster_client_runner as runner


def test_load_manifest_accepts_utf8_bom(tmp_path, monkeypatch):
    roster = SimpleNamespace(
        targets=[object()],
        source_sha256="source-sha",
        count=1,
    )
    entry = {
        "ordinal": 1,
        "journal_name": "journal",
        "account_name": "account",
        "biz": "biz",
        "profile_url": "https://example.invalid/profile",
        "base": str(tmp_path / "001"),
    }
    expected = {"expected_count": 1, "journals": [entry]}
    monkeypatch.setattr(runner, "load_fixed_roster", lambda *args, **kwargs: roster)
    monkeypatch.setattr(runner, "build_rollout_manifest", lambda *args, **kwargs: expected)
    (tmp_path / "run_manifest.json").write_text(
        json.dumps(expected, ensure_ascii=False), encoding="utf-8-sig"
    )

    actual = runner._load_or_create_manifest(tmp_path, tmp_path / "links.md", 1)

    assert actual["journals"][0]["journal_name"] == "journal"
    assert actual["source_sha256"] == "source-sha"
