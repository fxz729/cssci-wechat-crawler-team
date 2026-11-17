import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_module():
    path = ROOT / "scripts" / "prepare_skipped_retries.py"
    spec = importlib.util.spec_from_file_location("prepare_skipped_retries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconcile_cached_import_marker_marks_unverified_tail_completed_with_skips(tmp_path):
    module = load_module()
    base = tmp_path / "journals" / "011_测试刊"
    (base / "bundle").mkdir(parents=True)
    marker = base / "bundle" / "complete.json"
    marker.write_text(json.dumps({
        "source": "cached_msaa_import",
        "verified_tail": False,
        "article_count": 401,
    }, ensure_ascii=False), encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 11,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "msaa_partial",
    }]}

    changed = module.reconcile_cached_import_markers(manifest)

    assert changed[0]["status"] == "completed_with_skips"
    assert manifest["journals"][0]["reason"] == "cached_import_unverified_tail"
    assert manifest["journals"][0]["cached_import"] is True


def test_finalize_completed_csv_entry_uses_real_chinese_csv_name(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    csv_path = base / "bundle" / "文章数据.csv"
    csv_path.write_text("biz,mid\nA,1\nA,2\n", encoding="utf-8-sig")
    (base / "msaa" / "state.json").write_text(
        json.dumps({"completed": True, "skipped_titles": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = {"journals": [{
        "ordinal": 1,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "import_running",
    }]}

    finalized = module.finalize_completed_csv_entries(manifest, run_dir)

    marker = base / "bundle" / "complete.json"
    assert finalized[0]["article_count"] == 2
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8-sig"))["csv"] == "文章数据.csv"
    assert manifest["journals"][0]["status"] == "completed"



def test_finalize_reconciles_valid_marker_with_stale_manifest_status(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "008_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\n", encoding="utf-8-sig"
    )
    (base / "msaa" / "state.json").write_text(
        json.dumps({
            "completed": True,
            "skipped_titles": ["需要补采"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    marker_path = base / "bundle" / "complete.json"
    marker_path.write_text(json.dumps({
        "schema_version": 1,
        "completed_at": "2026-01-01T00:00:00+00:00",
        "article_count": 1,
        "completed_with_skips": True,
        "skipped_count": 1,
    }, ensure_ascii=False), encoding="utf-8")
    marker_mtime = marker_path.stat().st_mtime_ns
    manifest = {"journals": [{
        "ordinal": 8,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "paused",
        "reason": "stop_requested",
    }]}

    finalized = module.finalize_completed_csv_entries(manifest, run_dir)

    assert finalized[0]["status_reconciled"] is True
    assert finalized[0]["marker_repaired"] is False
    assert manifest["journals"][0]["status"] == "completed_with_skips"
    assert manifest["journals"][0]["reason"] == "skipped_articles_queued"
    assert marker_path.stat().st_mtime_ns == marker_mtime



def test_prepare_metrics_retry_queues_journal_when_title_absent_from_state(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "007_公共管理评论"
    (base / "msaa").mkdir(parents=True)
    (base / "bundle").mkdir(parents=True)
    state_path = base / "msaa" / "state.json"
    state_path.write_text(json.dumps({
        "completed": False,
        "processed_titles": ["当前断点文章"],
        "processed_count": 1,
        "last_processed_title": "当前断点文章",
        "skipped_titles": [],
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "metrics_retry_queue.jsonl").write_text(json.dumps({
        "ordinal": None,
        "journal_name": "公共管理评论",
        "biz": "biz-7",
        "title": "历史导入但指标缺失文章",
        "reason": "metrics_incomplete_preexisting",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 7,
        "journal_name": "公共管理评论",
        "biz": "biz-7",
        "base": str(base),
        "status": "paused",
    }]}

    changed = module.prepare_article_retry_journals(manifest, run_dir, dry_run=False)

    assert changed[0]["ordinal"] == 7
    assert changed[0]["removed_processed"] == 0
    assert changed[0]["queued"] is True
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert state["processed_titles"] == ["当前断点文章"]
    record = json.loads((run_dir / "journal_backfill_queue.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["reason"] == "metrics_retry_queue"


def test_finalize_waits_for_pending_backfill_queue(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "004_苏区研究"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nbiz-4,1\n", encoding="utf-8-sig"
    )
    (base / "msaa" / "state.json").write_text(
        json.dumps({"completed": True, "skipped_titles": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = {"journals": [{
        "ordinal": 4,
        "journal_name": "苏区研究",
        "biz": "biz-4",
        "base": str(base),
        "status": "completed_with_skips",
        "reason": "skipped_articles_queued",
    }]}
    (run_dir / "journal_backfill_queue.jsonl").write_text(json.dumps({
        "ordinal": 4,
        "journal_name": "苏区研究",
        "biz": "biz-4",
        "reason": "article_retry_queue",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    assert module.finalize_completed_csv_entries(manifest, run_dir) == []
    assert manifest["journals"][0]["status"] == "completed_with_skips"

    (run_dir / "backfill_progress.json").write_text(
        json.dumps({"done": ["4:biz-4"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    finalized = module.finalize_completed_csv_entries(manifest, run_dir)
    assert finalized[0]["ordinal"] == 4
    assert manifest["journals"][0]["status"] == "completed"


def test_prepare_article_retry_queue_clears_skipped_title_and_queues_journal(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "004_苏区研究"
    (base / "msaa").mkdir(parents=True)
    (base / "bundle").mkdir(parents=True)
    state_path = base / "msaa" / "state.json"
    state_path.write_text(json.dumps({
        "completed": True,
        "processed_titles": ["已采文章"],
        "processed_count": 1,
        "last_processed_title": "已采文章",
        "skipped_titles": ["缺失文章"],
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "skipped_retry_queue.jsonl").write_text(json.dumps({
        "ordinal": 4,
        "journal_name": "苏区研究",
        "biz": "biz-4",
        "title": "缺失文章",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 4,
        "journal_name": "苏区研究",
        "biz": "biz-4",
        "base": str(base),
        "status": "completed_with_skips",
    }]}

    changed = module.prepare_article_retry_journals(manifest, run_dir, dry_run=False)

    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert state["skipped_titles"] == []
    assert state["completed"] is True
    assert changed[0]["removed_skipped"] == 1
    queue = (run_dir / "journal_backfill_queue.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(queue[0])
    assert record["ordinal"] == 4
    assert record["reason"] == "article_retry_queue"


def test_prepare_metrics_retry_queue_removes_processed_title_and_queues(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "007_公共管理评论"
    (base / "msaa").mkdir(parents=True)
    (base / "bundle").mkdir(parents=True)
    state_path = base / "msaa" / "state.json"
    state_path.write_text(json.dumps({
        "completed": True,
        "processed_titles": ["指标缺失文章", "正常文章"],
        "processed_count": 2,
        "last_processed_title": "正常文章",
        "skipped_titles": [],
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "metrics_retry_queue.jsonl").write_text(json.dumps({
        "ordinal": None,
        "journal_name": "公共管理评论",
        "biz": "biz-7",
        "title": "指标缺失文章",
        "reason": "metrics_incomplete_after_retries",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 7,
        "journal_name": "公共管理评论",
        "biz": "biz-7",
        "base": str(base),
        "status": "paused",
    }]}

    changed = module.prepare_article_retry_journals(manifest, run_dir, dry_run=False)

    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert state["processed_titles"] == ["正常文章"]
    assert state["processed_count"] == 1
    assert changed[0]["removed_processed"] == 1
    queue = (run_dir / "journal_backfill_queue.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(queue[0])
    assert record["ordinal"] == 7
    assert record["reason"] == "metrics_retry_queue"


def test_prepare_queues_skipped_articles_without_reopening_journal(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\n", encoding="utf-8-sig"
    )
    marker = base / "bundle" / "complete.json"
    marker.write_text('{"schema_version": 1}', encoding="utf-8")
    state_path = base / "msaa" / "state.json"
    state_path.write_text(
        json.dumps({
            "completed": True,
            "processed_titles": ["已经采集"],
            "processed_count": 1,
            "last_processed_title": "已经采集",
            "skipped_titles": ["需要补采"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = {"journals": [{
        "ordinal": 1,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "pending_skip_retry",
    }]}

    changed = module.prepare_skipped_retries(manifest, run_dir, dry_run=False)
    finalized = module.finalize_completed_csv_entries(manifest, run_dir, dry_run=False)

    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    queued = [json.loads(line) for line in (run_dir / "skipped_retry_queue.jsonl").read_text(encoding="utf-8").splitlines()]
    assert state["completed"] is True
    assert state["skipped_titles"] == ["需要补采"]
    assert state["last_processed_title"] == "已经采集"
    assert queued[0]["title"] == "需要补采"
    assert changed[0]["queued"] == 1
    assert finalized[0]["skipped_count"] == 1
    assert marker.is_file()
    assert manifest["journals"][0]["status"] == "completed_with_skips"


def test_prepare_restores_retry_backup_instead_of_rescanning(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "002_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\n", encoding="utf-8-sig"
    )
    state_path = base / "msaa" / "state.json"
    state_path.write_text(
        json.dumps({"completed": False, "skipped_titles": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    backup_state = {
        "completed": True,
        "processed_titles": ["已经采集"],
        "processed_count": 1,
        "last_processed_title": "已经采集",
        "skipped_titles": ["需要补采"],
    }
    state_backup = base / "msaa" / "state.json.retry_backup_20260101T000000Z"
    state_backup.write_text(json.dumps(backup_state, ensure_ascii=False), encoding="utf-8")
    manifest = {"journals": [{
        "ordinal": 2,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "pending_skip_retry",
    }]}

    module.prepare_skipped_retries(manifest, run_dir, dry_run=False)
    module.finalize_completed_csv_entries(manifest, run_dir, dry_run=False)

    restored = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert restored["completed"] is True
    assert restored["skipped_titles"] == ["需要补采"]
    assert (run_dir / "skipped_retry_queue.jsonl").is_file()
    assert manifest["journals"][0]["status"] == "completed_with_skips"


def test_prepare_restores_partial_backup_for_resume(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "003_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\n", encoding="utf-8-sig"
    )
    state_path = base / "msaa" / "state.json"
    state_path.write_text(
        json.dumps({
            "completed": False,
            "skipped_titles": [],
            "last_processed_title": "需要补采",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    backup_state = {
        "completed": False,
        "processed_titles": ["已经采集"],
        "processed_count": 1,
        "last_processed_title": "已经采集",
        "skipped_titles": ["需要补采"],
    }
    (base / "msaa" / "state.json.retry_backup_20260101T000000Z").write_text(
        json.dumps(backup_state, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {"journals": [{
        "ordinal": 3,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "pending_skip_retry",
    }]}

    changed = module.prepare_skipped_retries(manifest, run_dir, dry_run=False)

    restored = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert restored["completed"] is False
    assert restored["skipped_titles"] == ["需要补采"]
    assert restored["last_processed_title"] == "已经采集"
    assert changed[0]["restored_from"].endswith("state.json.retry_backup_20260101T000000Z")
    assert manifest["journals"][0]["status"] == "pending_skip_retry"


def test_prepare_never_overwrites_state_newer_than_backup(tmp_path):
    module = load_module()
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "004_测试刊"
    (base / "bundle").mkdir(parents=True)
    (base / "msaa").mkdir(parents=True)
    (base / "bundle" / "文章数据.csv").write_text(
        "biz,mid\nA,1\nB,2\n", encoding="utf-8-sig"
    )
    state_path = base / "msaa" / "state.json"
    current = {
        "completed": False,
        "processed_titles": ["新文章一", "新文章二"],
        "last_processed_title": "新文章二",
        "skipped_titles": [],
    }
    state_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    backup = {
        "completed": False,
        "processed_titles": ["旧文章"],
        "last_processed_title": "旧文章",
        "skipped_titles": ["历史漏采"],
    }
    (base / "msaa" / "state.json.retry_backup_20260101T000000Z").write_text(
        json.dumps(backup, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {"journals": [{
        "ordinal": 4,
        "journal_name": "测试刊",
        "base": str(base),
        "status": "pending_skip_retry",
    }]}

    module.prepare_skipped_retries(manifest, run_dir, dry_run=False)

    preserved = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert preserved["processed_titles"] == ["新文章一", "新文章二"]
    assert preserved["last_processed_title"] == "新文章二"
    assert preserved["skipped_titles"] == []
