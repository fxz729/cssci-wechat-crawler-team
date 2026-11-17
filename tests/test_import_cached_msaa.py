import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load_module():
    path = ROOT / "scripts" / "import_cached_msaa.py"
    spec = importlib.util.spec_from_file_location("import_cached_msaa", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_select_cached_entries_respects_min_rows_and_active_status(tmp_path):
    module = _load_module()
    base = tmp_path / "journals" / "011_管理科学学报"
    (base / "msaa").mkdir(parents=True)
    (base / "msaa" / "articles.jsonl").write_text(
        "\n".join(json.dumps({"index": i}) for i in range(20)) + "\n",
        encoding="utf-8",
    )
    (base / "msaa" / "state.json").write_text(json.dumps({"completed": True}), encoding="utf-8")
    manifest = {"journals": [
        {"ordinal": 11, "status": "msaa_partial", "base": str(base), "journal_name": "管理科学学报"},
        {"ordinal": 12, "status": "msaa_running", "base": str(base), "journal_name": "运行中"},
    ]}
    selected = module.select_cached_entries(manifest, min_rows=20)
    assert [entry["ordinal"] for entry in selected] == [11]


def test_select_cached_entries_skips_unverified_partial_without_opt_in(tmp_path):
    module = _load_module()
    base = tmp_path / "journals" / "007_公共管理评论"
    (base / "msaa").mkdir(parents=True)
    (base / "msaa" / "articles.jsonl").write_text(
        "\n".join(json.dumps({"index": i}) for i in range(20)) + "\n",
        encoding="utf-8",
    )
    (base / "msaa" / "state.json").write_text(json.dumps({"completed": False}), encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 7,
        "status": "msaa_partial",
        "base": str(base),
        "journal_name": "公共管理评论",
    }]}

    assert module.select_cached_entries(manifest, min_rows=20) == []
    selected = module.select_cached_entries(manifest, min_rows=20, allow_unverified=True)
    assert [entry["ordinal"] for entry in selected] == [7]


def test_mark_cached_import_writes_marker_and_backfill(tmp_path):
    module = _load_module()
    base = tmp_path / "journals" / "012_系统工程"
    (base / "msaa").mkdir(parents=True)
    (base / "msaa" / "state.json").write_text(
        json.dumps({"completed": False, "skipped_titles": ["缺一篇"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    entry = {
        "ordinal": 12,
        "journal_name": "系统工程",
        "biz": "biz-12",
        "base": str(base),
    }
    marked = module.mark_cached_import(
        entry,
        summary={"imported": 233, "csv": {"article_count": 233}},
        run_dir=tmp_path,
    )
    assert marked is True
    marker = json.loads((base / "bundle" / "complete.json").read_text(encoding="utf-8"))
    assert marker["source"] == "cached_msaa_import"
    assert marker["verified_tail"] is False
    queue = (tmp_path / "journal_backfill_queue.jsonl").read_text(encoding="utf-8")
    assert "cached_import_unverified_tail" in queue
