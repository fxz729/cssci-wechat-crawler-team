import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config.env_loader import load_local_env
from config.session_credentials import load_session_credentials
from crawl.session_preflight import select_usable_session, validate_browser_session_credential, validate_session_credential
from crawl.article_fetcher import parse_fetched_article, persist_article_files
from crawl.article_persistence import persist_history_article
from crawl.account_crawler import crawl_account_from_results
from crawl.history_runner import SessionUnavailable, run_account_history
from crawl.wechat_browser import WechatBrowserClient
from crawl.wechat_requests import WechatRequestsClient
from crawl.wechat_urls import is_official_article_url
from discovery.candidate_ranker import rank_candidate
from discovery.query_builder import build_discovery_queries
from discovery.sogou_search import SogouSearchClient
from export.csv_exporter import export_articles_csv
from export.json_exporter import export_articles_json
from export.markdown_exporter import export_articles_markdown
from export.realtime_exporter import RealtimeExporter, clean_export_row, default_export_headers
from metrics.headers_parser import parse_headers_file
from metrics.profile_ext import fetch_article_page
from metrics.providers.base import MetricsSnapshot
from metrics.providers.wechat_authorized import WechatAuthorizedProvider
from metrics.providers.wechat_backend import WechatBackendClient
from parsers.article_parser import parse_article_html
from sources.journal_catalog import parse_catalog_file, parse_profile_links_file
from storage.database import create_session_factory, init_db
from storage.repository import Repository
from utils.progress_logger import ProgressLogger, setup_logging


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "cssci_wechat.db"
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "sources" / "CSSCI来源刊目录（2025-2026）.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CSSCI 微信公众号文章爬虫")
    parser.add_argument("--db", default=f"sqlite:///{DEFAULT_DB_PATH}", help="SQLAlchemy database URL")
    sub = parser.add_subparsers(dest="command")

    import_parser = sub.add_parser("import-journals", help="导入 CSSCI 期刊目录")
    import_parser.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH), help="CSSCI 目录文件，默认使用项目内 data/sources 目录")
    import_parser.set_defaults(func=cmd_import_journals)

    discover_parser = sub.add_parser("discover-accounts", help="发现期刊公众号候选")
    discover_parser.add_argument("--journal", default=None)
    discover_parser.add_argument("--all-disciplines", action="store_true")
    discover_parser.add_argument("--limit", type=int, default=5)
    discover_parser.add_argument("--auto-accept-threshold", type=float, default=0.92)
    discover_parser.set_defaults(func=cmd_discover_accounts)

    sync_targets_parser = sub.add_parser("sync-target-accounts", help="同步已验证公众号为当前爬取范围")
    sync_targets_parser.add_argument("--journal-links", default="data/sources/CSSCI来源刊公众号主页链接.md")
    sync_targets_parser.add_argument("--expected-count", type=int, default=126, help="固定名单期望条数；测试或子集运行可显式调整")
    sync_targets_parser.set_defaults(func=cmd_sync_target_accounts)

    validate_sessions_parser = sub.add_parser("validate-sessions", help="验证私有微信会话")
    validate_sessions_parser.add_argument("--sessions-file", required=True)
    validate_sessions_parser.add_argument("--fakeid", required=True)
    validate_sessions_parser.add_argument("--transport", choices=["auto", "requests", "browser"], default="auto")
    validate_sessions_parser.add_argument("--headed", action="store_true")
    validate_sessions_parser.set_defaults(func=cmd_validate_sessions)

    history_parser = sub.add_parser("crawl-history", help="按后台目录顺序抓取历史文章")
    history_parser.add_argument("--sessions-file", required=True)
    history_parser.add_argument("--journal-links", default="data/sources/CSSCI来源刊公众号主页链接.md")
    history_parser.add_argument("--max-accounts", type=int, default=1)
    history_parser.add_argument("--resume", action="store_true")
    history_parser.add_argument("--transport", choices=["auto", "requests", "browser"], default="auto")
    history_parser.add_argument("--headed", action="store_true")
    history_parser.set_defaults(func=cmd_crawl_history)

    status_parser = sub.add_parser("crawl-status", help="查看历史抓取状态")
    status_parser.set_defaults(func=cmd_crawl_status)

    retry_parser = sub.add_parser("retry-failed", help="按 checkpoint 恢复失败任务")
    retry_parser.add_argument("--sessions-file", required=True)
    retry_parser.add_argument("--journal-links", default="data/sources/CSSCI来源刊公众号主页链接.md")
    retry_parser.add_argument("--max-accounts", type=int, default=1)
    retry_parser.add_argument("--max-attempts", type=int, default=3)
    retry_parser.add_argument("--transport", choices=["auto", "requests", "browser"], default="auto")
    retry_parser.add_argument("--headed", action="store_true")
    retry_parser.set_defaults(func=cmd_retry_failed)

    review_parser = sub.add_parser("review-candidates", help="复核公众号候选")
    review_parser.add_argument("--status", default="uncertain")
    review_parser.set_defaults(func=cmd_review_candidates)

    crawl_account_parser = sub.add_parser("crawl-account", help="抓取单个确认公众号")
    crawl_account_parser.add_argument("--account", required=True)
    crawl_account_parser.add_argument("--since", default="2016-01-01")
    crawl_account_parser.add_argument("--until", default="2026-12-31")
    crawl_account_parser.add_argument("--pages", type=int, default=3)
    crawl_account_parser.add_argument("--max-articles", type=int, default=50)
    crawl_account_parser.add_argument("--export-dir", default="output")
    crawl_account_parser.add_argument("--no-realtime-export", action="store_true")
    crawl_account_parser.set_defaults(func=cmd_crawl_account)

    crawl_batch_parser = sub.add_parser("crawl-batch", help="批量抓取确认公众号")
    crawl_batch_parser.add_argument("--all-disciplines", action="store_true")
    crawl_batch_parser.add_argument("--since", default="2016-01-01")
    crawl_batch_parser.add_argument("--until", default="2026-12-31")
    crawl_batch_parser.add_argument("--max-accounts", type=int, default=20)
    crawl_batch_parser.add_argument("--pages", type=int, default=5)
    crawl_batch_parser.set_defaults(func=cmd_crawl_batch)

    metrics_parser = sub.add_parser("backfill-metrics", help="回填文章指标")
    metrics_parser.add_argument("--provider", choices=["public-html", "manual-import", "wechat-authorized"], default="public-html")
    metrics_parser.add_argument("--account", required=True)
    metrics_parser.add_argument("--since", default="2016-01-01")
    metrics_parser.add_argument("--until", default="2026-12-31")
    metrics_parser.add_argument("--sample-size", type=int, default=5, help="按时间轴等距抽取文章数")
    metrics_parser.add_argument("--full", action="store_true", help="在已通过样本验证后回填全部文章")
    metrics_parser.add_argument("--request-interval", type=float, default=2.5, help="相邻文章请求间隔秒数")
    metrics_parser.add_argument("--export-dir", default="output")
    metrics_parser.add_argument("--no-export", action="store_true")
    metrics_parser.set_defaults(func=cmd_backfill_metrics)

    backend_parser = sub.add_parser("import-backend-publish", help="从公众号后台导入官方发布列表")
    backend_parser.add_argument("--account", required=True)
    backend_parser.add_argument("--begin", type=int, default=0)
    backend_parser.add_argument("--count", type=int, default=5)
    backend_parser.set_defaults(func=cmd_import_backend_publish)

    export_parser = sub.add_parser("export", help="导出数据")
    export_parser.add_argument("--format", choices=["csv", "json", "markdown"], default="csv")
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--account", default=None)
    export_parser.add_argument("--download-images", action="store_true")
    export_parser.set_defaults(func=cmd_export)

    audit_parser = sub.add_parser("audit-articles", help="审计疑似非目标公众号文章")
    audit_parser.add_argument("--account", default=None)
    audit_parser.add_argument("--limit", type=int, default=50)
    audit_parser.set_defaults(func=cmd_audit_articles)

    check_auth_parser = sub.add_parser("check-auth", help="检查授权指标配置")
    check_auth_parser.add_argument("--provider", choices=["wechat-authorized"], default="wechat-authorized")
    check_auth_parser.set_defaults(func=cmd_check_auth)

    profile_import_parser = sub.add_parser("import-profile-ext", help="从 profile_ext 导入单个公众号文章")
    profile_import_parser.add_argument("--account", required=True)
    profile_import_parser.add_argument("--biz", default=None)
    profile_import_parser.add_argument("--headers-file", default=None)
    profile_import_parser.add_argument("--pages", type=int, default=0)
    profile_import_parser.add_argument("--sleep-page", type=float, default=2.5)
    profile_import_parser.add_argument("--sleep-article", type=float, default=1.5)
    profile_import_parser.set_defaults(func=cmd_import_profile_ext)

    batch_import_parser = sub.add_parser("import-from-headers", help="从 headers 文件批量导入所有期刊")
    batch_import_parser.add_argument("--headers-file", default="data/sources/所有期刊_Headers.txt")
    batch_import_parser.add_argument("--journal-links", default="data/sources/CSSCI来源刊公众号主页链接.md")
    batch_import_parser.add_argument("--pages", type=int, default=0)
    batch_import_parser.add_argument("--sleep-page", type=float, default=2.5)
    batch_import_parser.add_argument("--sleep-article", type=float, default=1.5)
    batch_import_parser.add_argument("--max-journals", type=int, default=0)
    batch_import_parser.set_defaults(func=cmd_import_from_headers)

    daemon_parser = sub.add_parser("daemon", help="后台调度任务")
    daemon_parser.add_argument("--dry-run", action="store_true")
    daemon_parser.set_defaults(func=cmd_daemon)

    stats_parser = sub.add_parser("stats", help="查看采集统计")
    stats_parser.set_defaults(func=cmd_stats)

    client_history_parser = sub.add_parser("crawl-client-history", help="使用已登录微信客户端按目录采集历史文章")
    client_history_parser.add_argument("--base", default=str(PROJECT_ROOT / "output" / "journal_validation_20260906"))
    client_history_parser.add_argument("--pilot-dir", default=str(PROJECT_ROOT / "output" / "pilot_20260906"))
    client_history_parser.add_argument("--biz", required=True)
    client_history_parser.add_argument("--account", required=True)
    client_history_parser.add_argument("--hwnd", type=int, required=True)
    client_history_parser.add_argument("--indices", default=None)
    client_history_parser.add_argument("--limit", type=int, default=0)
    client_history_parser.add_argument("--wait-seconds", type=float, default=60.0)
    client_history_parser.add_argument("--attempts", type=int, default=2)
    client_history_parser.add_argument("--sleep-seconds", type=float, default=1.0)
    client_history_parser.add_argument("--repair-existing", action="store_true")
    client_history_parser.add_argument("--force", action="store_true")
    client_history_parser.add_argument("--capture-only", action="store_true")
    client_history_parser.add_argument("--log-dir", default=None)
    client_history_parser.add_argument("--event-log", default=None)
    client_history_parser.add_argument("--stop-control", default=None)
    client_history_parser.set_defaults(func=cmd_crawl_client_history)

    client_roster_parser = sub.add_parser("crawl-client-roster", help="按固定名单顺序轮转微信客户端历史采集")
    client_roster_parser.add_argument("--run-dir", default=str(PROJECT_ROOT / "output" / "rollout" / "client_20260906"))
    client_roster_parser.add_argument("--journal-links", default=str(PROJECT_ROOT / "data/sources/CSSCI来源刊公众号主页链接.md"))
    client_roster_parser.add_argument("--expected-count", type=int, default=126)
    client_roster_parser.add_argument("--hwnd", type=int, default=0, help="可选旧窗口句柄；重启后默认自动发现")
    client_roster_parser.add_argument("--pilot-dir", default=str(PROJECT_ROOT / "output" / "pilot_20260906"))
    client_roster_parser.add_argument("--max-journals", type=int, default=0)
    client_roster_parser.add_argument("--start", type=int, default=1)
    client_roster_parser.add_argument("--max-steps", type=int, default=2000)
    client_roster_parser.add_argument("--wait-seconds", type=float, default=60.0)
    client_roster_parser.add_argument("--attempts", type=int, default=2)
    client_roster_parser.add_argument("--sleep-seconds", type=float, default=1.0)
    client_roster_parser.add_argument("--media-workers", type=int, default=4)
    client_roster_parser.add_argument("--phase", choices=["full", "capture-first", "background-only"], default="full")
    client_roster_parser.set_defaults(func=cmd_crawl_client_roster)

    refill_parser = sub.add_parser("refill-client-journal", help="按冻结目录独立补采缺失文章、指标与媒体")
    refill_parser.add_argument("--base", required=True)
    refill_parser.add_argument("--pilot-dir", default=str(PROJECT_ROOT / "output" / "pilot_20260906"))
    refill_parser.add_argument("--biz", required=True)
    refill_parser.add_argument("--account", required=True)
    refill_parser.add_argument("--hwnd", type=int, default=0)
    refill_parser.add_argument("--wait-seconds", type=float, default=60.0)
    refill_parser.add_argument("--attempts", type=int, default=2)
    refill_parser.add_argument("--sleep-seconds", type=float, default=1.0)
    refill_parser.add_argument("--media-workers", type=int, default=4)
    refill_parser.add_argument("--offline-only", action="store_true")
    refill_parser.add_argument("--baseline-base", default=None)
    refill_parser.set_defaults(func=cmd_refill_client_journal)

    client_csv_parser = sub.add_parser("export-client-csv", help="导出客户端采集数据为CSV及媒体相对路径索引")
    client_csv_parser.add_argument("--bundle", required=True)
    client_csv_parser.set_defaults(func=cmd_export_client_csv)

    audit_client_parser = sub.add_parser("audit-client-bundle", help="严格验收客户端历史采集数据包")
    audit_client_parser.add_argument("--base", default=str(PROJECT_ROOT / "output" / "journal_validation_20260906"))
    audit_client_parser.add_argument("--biz", default=None)
    audit_client_parser.add_argument("--baseline-base", default=None)
    audit_client_parser.set_defaults(func=cmd_audit_client_bundle)

    health_parser = sub.add_parser("check-wechat-capture", help="只读检查微信客户端与 Fiddler 捕获边界")
    health_parser.add_argument("--hwnd", type=int, required=True)
    health_parser.add_argument("--pilot-dir", default=str(PROJECT_ROOT / "output" / "pilot_20260906"))
    health_parser.add_argument("--fiddler-exe", default=str(PROJECT_ROOT.parent / "Fiddler" / "Fiddler.exe"))
    health_parser.add_argument("--active-rules", default=None)
    health_parser.add_argument("--activation-metadata", default=None)
    health_parser.add_argument("--listen-port", type=int, default=8899)
    health_parser.set_defaults(func=cmd_check_wechat_capture)

    pilot_parser = sub.add_parser("pilot", help="固定名单中的一刊小样本及独立指标补采")
    pilot_parser.add_argument("--journal-links", default=str(PROJECT_ROOT / "data/sources/CSSCI来源刊公众号主页链接.md"))
    pilot_parser.add_argument("--account", required=True)
    pilot_parser.add_argument("--inbox", required=True)
    pilot_parser.add_argument("--output-dir", required=True)
    pilot_parser.add_argument("--max-articles", type=int, default=3)
    pilot_parser.add_argument("--uia-observation", action="append", default=[])
    pilot_parser.set_defaults(func=cmd_pilot)

    observe_parser = sub.add_parser("desktop-observe", help="只读获取当前微信文章的公开链接和可见指标")
    observe_parser.add_argument("--hwnd", type=int, required=True)
    observe_parser.add_argument("--output", required=True)
    observe_parser.set_defaults(func=cmd_desktop_observe)

    return parser


def cmd_import_journals(args, repo: Repository) -> int:
    seeds = parse_catalog_file(args.catalog)
    for seed in seeds:
        repo.upsert_journal_seed(
            catalog_version=seed.catalog_version,
            discipline=seed.discipline,
            tier=seed.tier,
            journal_name=seed.journal_name,
            normalized_name=seed.normalized_name,
            aliases=seed.aliases,
            source_file=seed.source_file,
            source_line=seed.source_line,
        )
    print(f"导入期刊种子：{len(seeds)}")
    return 0


def cmd_discover_accounts(args, repo: Repository) -> int:
    seeds = repo.list_journal_seeds(journal_name=args.journal, limit=args.limit if args.journal else None)
    created = 0
    failures = 0
    with SogouSearchClient() as client:
        for seed in seeds:
            queries = build_discovery_queries(seed.journal_name)
            found_for_seed = 0
            for query_text in queries[: args.limit]:
                try:
                    candidates = client.search_article_candidates(query_text, pages=1)
                except Exception as exc:
                    print(f"发现失败：{seed.journal_name} | {query_text} | {exc}")
                    failures += 1
                    continue
                for candidate in candidates:
                    evidence_titles = candidate.evidence_titles or []
                    score = rank_candidate(
                        seed.journal_name,
                        candidate.name,
                        candidate.description,
                        evidence_titles,
                        candidate.is_verified,
                    )
                    repo.upsert_account_candidate(
                        journal_seed_id=seed.id,
                        query_text=query_text,
                        candidate_name=candidate.name,
                        candidate_wechat_id=candidate.wechat_id,
                        candidate_profile_url=candidate.profile_url,
                        candidate_description=candidate.description,
                        is_verified=candidate.is_verified,
                        evidence={"queries": queries, "titles": evidence_titles},
                        score=score,
                    )
                    created += 1
                    found_for_seed += 1
            if found_for_seed == 0:
                print(f"未发现候选：{seed.journal_name}")
    accepted = repo.auto_accept_high_score_candidates(args.auto_accept_threshold)
    print(f"生成候选：{created}，自动确认：{accepted}，失败查询：{failures}")
    return 0


def cmd_sync_target_accounts(args, repo: Repository) -> int:
    expected_count = getattr(args, "expected_count", None)
    if expected_count is not None:
        from sources.fixed_roster import load_fixed_roster
        try:
            targets = list(load_fixed_roster(args.journal_links, expected_count=expected_count).targets)
        except (OSError, ValueError) as exc:
            print(f"固定公众号名单校验失败：{type(exc).__name__}:{exc}")
            return 1
    else:
        targets = parse_profile_links_file(args.journal_links)
    if not targets:
        print(f"未从公众号主页链接文件解析到有效目标：{args.journal_links}")
        return 1
    synced, deactivated = repo.sync_target_accounts(targets)
    print(f"已同步目标公众号：{synced}，已停用范围外账号：{deactivated}")
    return 0


def cmd_validate_sessions(args, _repo: Repository | None) -> int:
    credentials = load_session_credentials(Path(args.sessions_file))
    fakeid = getattr(args, "fakeid", "") or ""
    transport = getattr(args, "transport", "auto")
    headed = bool(getattr(args, "headed", False))
    selection = select_usable_session(
        credentials,
        fakeid,
        transport=transport,
        browser_validator=lambda credential, target: validate_browser_session_credential(
            credential,
            target,
            headless=not headed,
        ),
        requests_validator=validate_session_credential,
    )
    if selection is None:
        print(f"可用会话：0/{len(credentials)}")
        return 1
    print(f"{selection.credential.session_id}: usable ({selection.transport})")
    print(f"可用会话：1/{len(credentials)}")
    return 0


def cmd_pilot(args, repo):
    import json
    from urllib.parse import urlsplit
    from capture.reader import load_events
    from capture.uia import build_uia_event
    from crawl.pilot import run_pilot
    targets = parse_profile_links_file(args.journal_links)
    target = next((t for t in targets if t.account_name == args.account), None)
    if target is None:
        print("目标不在指定名单中")
        return 1
    for observation_path in args.uia_observation:
        path = Path(observation_path)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("observation_too_large")
        observation = json.loads(path.read_text(encoding="utf-8-sig"))
        events, _ = load_events(args.inbox, {target.biz})
        matches = [e for e in events if e["kind"] == "article" and (
            e["endpoint"] == urlsplit(observation["url"]).path and e["endpoint"].startswith("/s/")
            or e["article"]["url"] == observation["url"])]
        if len(matches) != 1:
            print("客户端观测尚无唯一、已核验的正文响应，未导入")
            return 1
        event = build_uia_event(observation, matches[0], confirmed_public_url=observation["url"])
        with Path(args.inbox).open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = run_pilot(repo, args.journal_links, args.inbox, args.output_dir, args.account, args.max_articles)
    print(json.dumps({k: report[k] for k in ("status", "sample_articles", "all_six_metrics_complete_articles", "pending_metrics_articles", "full_history_complete")}, ensure_ascii=False))
    return 0 if report["sample_articles"] else 1


def cmd_desktop_observe(args, _repo=None):
    from crawl.desktop_wechat import DesktopWeChatDriver
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    DesktopWeChatDriver(args.hwnd).observe(path)
    print("客户端公开字段观测已保存：" + str(path))
    return 0


def cmd_export_client_csv(args, _repo=None) -> int:
    import json
    from export.journal_csv import write_journal_csv_bundle
    result = write_journal_csv_bundle(Path(args.bundle))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_crawl_client_roster(args, _repo=None) -> int:
    from crawl.roster_client_runner import main as run_client_roster

    forwarded = []
    for name in ("run_dir", "journal_links", "expected_count", "hwnd", "pilot_dir", "max_journals", "start", "max_steps", "wait_seconds", "attempts", "sleep_seconds", "media_workers", "phase"):
        value = getattr(args, name, None)
        if value is not None:
            forwarded.extend(["--" + name.replace("_", "-"), str(value)])
    return int(run_client_roster(forwarded) or 0)


def cmd_refill_client_journal(args, _repo=None) -> int:
    from metrics.refill_queue import main as run_refill

    forwarded = []
    for name in ("base", "pilot_dir", "biz", "account", "hwnd", "wait_seconds", "attempts", "sleep_seconds", "media_workers", "baseline_base"):
        value = getattr(args, name, None)
        if value is not None:
            forwarded.extend(["--" + name.replace("_", "-"), str(value)])
    if getattr(args, "offline_only", False):
        forwarded.append("--offline-only")
    return int(run_refill(forwarded) or 0)


def cmd_crawl_client_history(args, _repo=None) -> int:
    from crawl.client_history_runner import main as run_client_history

    forwarded = []
    for name in ("base", "pilot_dir", "biz", "account", "hwnd", "indices", "limit", "wait_seconds", "attempts", "sleep_seconds", "log_dir", "event_log", "stop_control"):
        value = getattr(args, name, None)
        if value is not None:
            forwarded.extend(["--" + name.replace("_", "-"), str(value)])
    for name in ("repair_existing", "force", "capture_only"):
        if getattr(args, name, False):
            forwarded.append("--" + name.replace("_", "-"))
    result = run_client_history(forwarded)
    return int(result or 0)


def _infer_client_bundle_biz(base: Path) -> str:
    import json
    import re

    root = Path(base).absolute()
    articles = root / "bundle" / "articles"
    if not articles.is_dir() or articles.is_symlink():
        raise RuntimeError("client_bundle_biz_not_found")
    found = set()
    for folder in articles.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        metadata = folder / "metadata.json"
        if not metadata.is_file() or metadata.is_symlink() or not metadata.resolve().is_relative_to(root.resolve()):
            continue
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
            biz = value.get("biz") if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(biz, str) and re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", biz):
            found.add(biz)
    if not found:
        raise RuntimeError("client_bundle_biz_not_found")
    if len(found) != 1:
        raise RuntimeError("client_bundle_biz_ambiguous")
    return next(iter(found))


def cmd_audit_client_bundle(args, _repo=None) -> int:
    import json
    from export.rollout_audit import audit_journal

    base = Path(args.base).resolve()
    try:
        biz = args.biz or _infer_client_bundle_biz(base)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "base": str(base)}, ensure_ascii=False))
        return 2
    baseline = Path(args.baseline_base).resolve() if args.baseline_base else None
    result = audit_journal(base, expected_biz=biz, baseline_base=baseline)
    print(json.dumps({
        "ok": result.get("ok") is True,
        "checks": result.get("checks", {}),
        "counts": result.get("counts", {}),
        "errors": result.get("errors", []),
        "audit_path": result.get("audit_path"),
    }, ensure_ascii=False))
    return 0 if result.get("ok") is True else 2


def cmd_check_wechat_capture(args, _repo=None) -> int:
    import json
    from crawl.client_capture_health import check_capture_health

    pilot_dir = Path(args.pilot_dir).resolve()
    metadata_path = Path(args.activation_metadata).resolve() if args.activation_metadata else pilot_dir / "capture_activation.json"
    active_rules = Path(args.active_rules).resolve() if args.active_rules else None
    if active_rules is None and metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            active_rules = Path(str(metadata.get("active_path") or "")) if metadata.get("active_path") else None
        except (OSError, ValueError, TypeError):
            active_rules = None
    if active_rules is None:
        active_rules = pilot_dir / "CustomRules.js"
    result = check_capture_health(
        hwnd=args.hwnd,
        fiddler_exe=Path(args.fiddler_exe),
        active_rules=active_rules,
        activation_metadata=metadata_path,
        control_path=pilot_dir / "capture_control.json",
        listen_port=args.listen_port,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_crawl_history(
    args,
    repo: Repository,
    *,
    progress_logger: ProgressLogger | None = None,
) -> int:
    _log = progress_logger or ProgressLogger()
    targets = parse_profile_links_file(args.journal_links)
    if not targets:
        print("未解析到有效公众号目标")
        return 1
    repo.sync_target_accounts(targets)
    credentials = load_session_credentials(Path(args.sessions_file))
    target_order = {target.biz: index for index, target in enumerate(targets)}
    accounts = [account for account in repo.list_accounts(limit=None) if account.biz in target_order]
    accounts.sort(key=lambda account: target_order[account.biz])
    account_names = set(getattr(args, "account_names", []) or [])
    if account_names:
        accounts = [account for account in accounts if account.canonical_name in account_names]
    accounts = accounts[:max(1, args.max_accounts)]
    transport = getattr(args, "transport", "auto")
    headed = bool(getattr(args, "headed", False))
    completed = 0
    for account_index, account in enumerate(accounts, start=1):
        _log.history_account_started(account.canonical_name, current=account_index, total=len(accounts))
        account_completed = False
        total_processed = 0
        total_failed = 0
        remaining = list(credentials)
        forced_browser_credential = None
        while remaining or forced_browser_credential is not None:
            browser_fallback = forced_browser_credential is not None
            candidates = (forced_browser_credential,) if browser_fallback else tuple(remaining)
            selected_transport = "browser" if browser_fallback else transport
            forced_browser_credential = None
            _log.session_preflight_started(
                account.canonical_name,
                candidate=1,
                total=len(candidates),
                transport=selected_transport,
            )
            selection = select_usable_session(
                candidates,
                account.biz,
                transport=selected_transport,
                browser_validator=lambda credential, target: validate_browser_session_credential(
                    credential,
                    target,
                    headless=not headed,
                ),
            )
            if selection is None:
                _log.session_preflight_finished(account.canonical_name, usable=False)
                if browser_fallback:
                    remaining = [candidate for candidate in remaining if candidate is not candidates[0]]
                    continue
                break
            credential = selection.credential
            _log.session_preflight_finished(
                account.canonical_name,
                usable=True,
                transport=selection.transport,
            )
            _log.transport_selected(account.canonical_name, transport=selection.transport)
            if browser_fallback or selection.transport == "browser" or transport != "auto":
                remaining = [candidate for candidate in remaining if candidate is not credential]
            client_factory = WechatBrowserClient if selection.transport == "browser" else WechatRequestsClient
            client_kwargs = {"headless": not headed} if selection.transport == "browser" else {}
            with client_factory(credential, account.biz, **client_kwargs) as client:
                def process_item(item):
                    saved_article = _process_history_item(repo, account, client, item)
                    time.sleep(3)
                    return saved_article

                result = run_account_history(
                    repo,
                    account,
                    client,
                    process_item,
                    resume=args.resume or browser_fallback,
                    progress_logger=_log,
                )
            total_processed += result.processed
            total_failed += result.failed
            if result.completed:
                account_completed = True
                break
            checkpoint = repo.get_crawl_checkpoint(f"crawl-history:{account.canonical_name}")
            if checkpoint is None or checkpoint.status != "paused":
                break
            if transport == "auto" and selection.transport == "requests":
                _log.transport_fallback(account.canonical_name, source="requests", target="browser")
                forced_browser_credential = credential
        completed += account_completed
        _log.history_account_finished(
            account.canonical_name,
            processed=total_processed,
            failed=total_failed,
            completed=account_completed,
        )
        if not account_completed and total_processed == 0 and total_failed == 0:
            _log.info(f"账号暂停 | account={account.canonical_name} | 无可用会话")
    _log.history_finished(completed=completed, total=len(accounts))
    return 0 if accounts and completed == len(accounts) else 1


def _process_history_item(repo: Repository, account, client, item):
    try:
        return persist_history_article(repo, account, client, item, PROJECT_ROOT / "data")
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code in {401, 403, 429}:
            raise SessionUnavailable(f"http_{status_code}") from None
        raise
    except RuntimeError as exc:
        message = str(exc)
        session_markers = (
            "appmsgstat",
            "metrics rejected",
            "comments rejected",
            "指标接口返回错误",
        )
        if any(marker in message for marker in session_markers):
            raise SessionUnavailable("degraded") from None
        raise


def cmd_crawl_status(_args, repo: Repository) -> int:
    stats = repo.get_stats()
    failures = repo.list_retryable_failed_tasks()
    print(
        f"历史抓取状态：账号 {stats['accounts']}，文章 {stats['articles']}，"
        f"指标快照 {stats['metrics']}，可恢复失败 {len(failures)}"
    )
    return 0


def cmd_retry_failed(args, repo: Repository) -> int:
    failures = repo.list_retryable_failed_tasks(max_attempts=args.max_attempts)
    if not failures:
        print("没有可恢复失败任务。")
        return 0
    retry_args = argparse.Namespace(
        sessions_file=args.sessions_file,
        journal_links=args.journal_links,
        max_accounts=args.max_accounts,
        account_names=sorted({failure.account_name for failure in failures if failure.account_name}),
        resume=True,
        transport=getattr(args, "transport", "auto"),
        headed=bool(getattr(args, "headed", False)),
    )
    return cmd_crawl_history(retry_args, repo)


def cmd_review_candidates(args, repo: Repository) -> int:
    candidates = repo.list_candidates(status=args.status)
    if not candidates:
        print("没有待复核候选。")
        return 0
    for candidate in candidates:
        print(f"[{candidate.id}] {candidate.candidate_name} | score={candidate.score:.2f} | {candidate.candidate_description}")
    return 0


def cmd_crawl_account(args, repo: Repository) -> int:
    account = repo.get_account_by_name(args.account)
    if account is None:
        print(f"未找到已确认公众号：{args.account}。请先运行 discover-accounts 并确认候选。")
        return 1
    since = _parse_date(args.since)
    until = _parse_date(args.until, end_of_day=True)
    data_dir = pathlib.Path(__file__).resolve().parent / "data"
    realtime_exporter = None if args.no_realtime_export else RealtimeExporter(
        repo=repo,
        output_dir=pathlib.Path(args.export_dir),
        formats=["csv", "json"],
    )
    progress_logger = ProgressLogger()
    with SogouSearchClient() as client:
        results = client.search_article_results(account.canonical_name, pages=args.pages)
        if args.max_articles:
            results = results[: args.max_articles]
        stored = crawl_account_from_results(
            repo=repo,
            account=account,
            results=[{"url": result.url, "summary": result.summary} for result in results],
            fetch_html=client.fetch_article_html,
            data_dir=data_dir,
            since=since,
            until=until,
            task_name=f"crawl:{account.canonical_name}",
            realtime_exporter=realtime_exporter,
            progress_logger=progress_logger,
        )
    print(f"抓取完成：{account.canonical_name}，入库 {stored} 篇")
    return 0


def cmd_crawl_batch(args, repo: Repository) -> int:
    accounts = repo.list_accounts(limit=args.max_accounts)
    total = 0
    for account in accounts:
        single_args = argparse.Namespace(
            account=account.canonical_name,
            since=args.since,
            until=args.until,
            pages=args.pages,
            max_articles=50,
            export_dir="output",
            no_realtime_export=False,
        )
        total += 1 if cmd_crawl_account(single_args, repo) == 0 else 0
    print(f"批量抓取完成：处理账号 {total}/{len(accounts)}")
    return 0


def cmd_import_backend_publish(args, repo: Repository) -> int:
    account = repo.get_account_by_name(args.account)
    if account is None:
        print(f"未找到已确认公众号：{args.account}")
        return 1
    client = WechatBackendClient.from_env()
    items = client.fetch_publish_items(begin=args.begin, count=args.count)
    stored = 0
    data_dir = pathlib.Path(__file__).resolve().parent / "data"
    with SogouSearchClient() as browser:
        for item in items:
            content_text = ""
            content_html = ""
            image_urls = []
            raw_html_path = ""
            raw_text_path = ""
            canonical_url = item.link
            sn = ""
            try:
                fetched = browser.fetch_article_html(item.link)
                if fetched:
                    final_url, html = fetched
                    parsed_article, url_parts = parse_fetched_article(html, final_url)
                    canonical_url = url_parts.canonical_url if url_parts.canonical_url else final_url
                    sn = url_parts.sn or ""
                    if parsed_article:
                        content_text = parsed_article.content_text
                        content_html = parsed_article.content_html
                        image_urls = parsed_article.image_urls
                        raw_html_path, raw_text_path = persist_article_files(
                            data_dir,
                            client.fakeid,
                            item.mid,
                            item.idx,
                            html,
                            content_text,
                        )
            except Exception as exc:
                print(f"正文抓取失败：{item.mid}_{item.idx} | {exc}")
            batch = repo.upsert_publish_batch(
                account_id=account.id,
                biz=client.fakeid,
                mid=item.mid,
                publish_time=item.publish_time,
                source_url=item.link,
            )
            repo.upsert_article(
                account_id=account.id,
                publish_batch_id=batch.id,
                biz=client.fakeid,
                mid=item.mid,
                idx=item.idx,
                sn=sn,
                title=item.title,
                author=item.author,
                publish_time=item.publish_time,
                digest=item.digest,
                content_text=content_text,
                image_urls=image_urls,
                source_url=item.link,
                canonical_url=canonical_url,
                raw_html_path=raw_html_path,
                raw_text_path=raw_text_path,
                content_html=content_html,
                cover_image_url=item.cover_url,
            )
            stored += 1
    RealtimeExporter(repo=repo, output_dir=pathlib.Path("output"), formats=["csv", "json"]).update(account.canonical_name)
    print(f"后台发布列表导入完成：{account.canonical_name}，{stored} 条")
    return 0


class _ArticleStub:
    """Minimal article stub for WechatAuthorizedProvider.fetch()."""
    __slots__ = ("mid", "idx", "canonical_url", "publish_time", "raw_html_path", "title", "req_id", "sn", "comment_id")

    def __init__(self, mid, idx, canonical_url, publish_time, title, req_id, sn, comment_id=""):
        self.mid = mid
        self.idx = idx
        self.canonical_url = canonical_url
        self.publish_time = publish_time
        self.raw_html_path = ""
        self.title = title
        self.req_id = req_id
        self.sn = sn
        self.comment_id = comment_id


def _crawl_journal_with_credentials(creds, account, pages, repo: Repository, sleep_page: float, sleep_article: float, progress_logger: ProgressLogger | None = None) -> int:
    """Shared implementation for profile_ext crawl. Returns count of stored articles."""
    _log = progress_logger or ProgressLogger()
    MAX_PAGES = 200
    page_limit = MAX_PAGES if pages == 0 else pages
    task_name = f"profile_ext:{account.canonical_name}"

    # Update account biz if needed
    if creds.biz and account.biz != creds.biz:
        repo.update_account_biz(account.id, creds.biz)
        account = repo.get_account_by_name(account.canonical_name)

    # Load checkpoint for skip logic
    checkpoint = repo.get_crawl_checkpoint(task_name)
    processed_urls: list[str] = []
    if checkpoint and checkpoint.processed_urls_json:
        import json as _json
        processed_urls = _json.loads(checkpoint.processed_urls_json)
    processed_set = set(processed_urls)

    session = requests.Session()
    all_items = []
    offset = 0

    for page_num in range(page_limit):
        try:
            items = fetch_article_page(session, creds, offset)
        except Exception as exc:
            _log.error(f"获取第 {page_num + 1} 页失败 | account={account.canonical_name} | error={exc}")
            break
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        repo.save_crawl_checkpoint(task_name, account.canonical_name, page_num + 1, processed_urls)
        if sleep_page > 0:
            time.sleep(sleep_page)

    total = len(all_items)
    stored = 0

    for art_idx, item in enumerate(all_items):
        canonical_url = item.content_url or (
            f"https://mp.weixin.qq.com/s?__biz={item.biz}&mid={item.mid}&idx={item.idx}&sn={item.sn}"
        )
        _log.info(f"[{art_idx + 1}/{total}] 处理文章 | account={account.canonical_name} | title={item.title[:30]}")

        # Upsert publish batch + article metadata from ProfileArticleItem
        batch = repo.upsert_publish_batch(
            account_id=account.id,
            biz=item.biz,
            mid=item.mid,
            publish_time=item.publish_time,
            source_url=canonical_url,
        )
        article = repo.upsert_article(
            account_id=account.id,
            publish_batch_id=batch.id,
            biz=item.biz,
            mid=item.mid,
            idx=item.idx,
            sn=item.sn,
            title=item.title,
            author="",
            publish_time=item.publish_time,
            digest=item.digest,
            content_text="",
            image_urls=[],
            source_url=canonical_url,
            canonical_url=canonical_url,
            raw_html_path="",
            raw_text_path="",
            cover_image_url=item.cover_url,
            copyright_stat=item.copyright_stat,
            profile_ext_source=item.biz,
        )

        # Skip if already processed
        if canonical_url in processed_set:
            continue

        try:
            resp = session.get(
                canonical_url,
                headers={"Cookie": creds.cookie, "User-Agent": creds.user_agent},
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
            html = resp.text
            final_url = resp.url

            parsed = parse_article_html(html, final_url)
            req_id = parsed.req_id if parsed else ""
            comment_id = parsed.comment_id if parsed else ""
            word_count = parsed.word_count if parsed else None
            is_original = parsed.is_original if parsed else None
            audio_count = parsed.audio_count if parsed else 0
            video_count = parsed.video_count if parsed else 0

            article = repo.upsert_article(
                account_id=account.id,
                publish_batch_id=batch.id,
                biz=item.biz,
                mid=item.mid,
                idx=item.idx,
                sn=item.sn,
                title=item.title,
                author=parsed.author if parsed else "",
                publish_time=item.publish_time,
                digest=(parsed.digest if parsed else "") or item.digest,
                content_text=parsed.content_text if parsed else "",
                content_html=parsed.content_html if parsed else "",
                image_urls=parsed.image_urls if parsed else [],
                source_url=canonical_url,
                canonical_url=final_url or canonical_url,
                raw_html_path="",
                raw_text_path="",
                cover_image_url=item.cover_url,
                copyright_stat=item.copyright_stat,
                req_id=req_id,
                comment_id=comment_id,
                word_count=word_count,
                is_original=is_original,
                audio_count=audio_count,
                video_count=video_count,
                profile_ext_source=item.biz,
            )

            stub = _ArticleStub(
                mid=item.mid,
                idx=item.idx,
                canonical_url=canonical_url,
                publish_time=item.publish_time,
                title=item.title,
                req_id=req_id,
                sn=item.sn,
                comment_id=comment_id,
            )
            provider = WechatAuthorizedProvider(
                cookie=creds.cookie,
                appmsg_token=creds.appmsg_token,
                uin=creds.uin,
                key=creds.key,
                pass_ticket=creds.pass_ticket,
                user_agent=creds.user_agent,
                session=session,
            )
            snapshot = provider.fetch(stub)
            appmsgext_payload = snapshot.raw_payload.get("appmsgext", {}) if snapshot.raw_payload else {}
            article = repo.upsert_article(
                account_id=account.id,
                publish_batch_id=batch.id,
                biz=item.biz,
                mid=item.mid,
                idx=item.idx,
                sn=item.sn,
                title=item.title,
                author=parsed.author if parsed else "",
                publish_time=item.publish_time,
                digest=(parsed.digest if parsed else "") or item.digest,
                content_text=parsed.content_text if parsed else "",
                content_html=parsed.content_html if parsed else "",
                image_urls=parsed.image_urls if parsed else [],
                source_url=canonical_url,
                canonical_url=final_url or canonical_url,
                raw_html_path="",
                raw_text_path="",
                cover_image_url=item.cover_url,
                copyright_stat=item.copyright_stat,
                req_id=req_id,
                comment_id=comment_id,
                word_count=word_count,
                is_original=is_original,
                audio_count=audio_count,
                video_count=video_count,
                album_id=str(appmsgext_payload.get("_album_id") or ""),
                album_name=appmsgext_payload.get("_album_name") or "",
                collect_num=snapshot.collect_num,
                profile_ext_source=item.biz,
            )
            repo.insert_metrics_snapshot(
                article_id=article.id,
                provider=snapshot.provider,
                provider_priority=snapshot.provider_priority,
                read_count=snapshot.read_count,
                like_count=snapshot.like_count,
                watching_count=snapshot.watching_count,
                reward_count=snapshot.reward_count,
                share_count=snapshot.share_count,
                favorite_count=snapshot.favorite_count,
                subscriber_count=snapshot.subscriber_count,
                comment_count=snapshot.comment_count,
                raw_payload=snapshot.raw_payload,
                is_authorized_source=snapshot.is_authorized_source,
            )

            processed_urls.append(canonical_url)
            processed_set.add(canonical_url)
            stored += 1
        except Exception as exc:
            _log.error(f"文章处理失败 | url={canonical_url} | error={exc}")
            repo.record_failed_task(task_name, account.canonical_name, canonical_url, str(exc))

        if sleep_article > 0:
            time.sleep(sleep_article)

    repo.save_crawl_checkpoint(task_name, account.canonical_name, page_limit, processed_urls, status="completed")
    RealtimeExporter(repo=repo, output_dir=Path("output"), formats=["csv", "json"]).update(account.canonical_name)
    return stored


def cmd_import_profile_ext(args, repo: Repository) -> int:
    account = repo.get_account_by_name(args.account)
    if account is None:
        print(f"未找到已确认公众号：{args.account}")
        return 1

    if args.headers_file and args.biz:
        creds_map = parse_headers_file(Path(args.headers_file))
        creds = creds_map.get(args.biz)
        if creds is None:
            print(f"headers 文件中未找到 biz={args.biz} 的凭据")
            return 1
    else:
        provider = WechatAuthorizedProvider.from_env()
        if not provider.is_configured:
            print(provider.status_message)
            return 1
        from metrics.headers_parser import JournalCredentials
        creds = JournalCredentials(
            biz=args.biz or "",
            uin=provider.uin or "",
            key=provider.key or "",
            pass_ticket=provider.pass_ticket or "",
            appmsg_token=provider.appmsg_token or "",
            cookie=provider.cookie or "",
            user_agent=provider.user_agent or "",
            sample_article_mid="",
            sample_article_idx=0,
            sample_article_sn="",
            has_tts_sample=False,
        )

    _log = ProgressLogger()
    stored = _crawl_journal_with_credentials(creds, account, args.pages, repo, args.sleep_page, args.sleep_article, _log)
    _log.info(f"import-profile-ext 完成 | account={args.account} | stored={stored}")
    return 0


def cmd_import_from_headers(args, repo: Repository) -> int:
    _log = ProgressLogger()
    creds_map = parse_headers_file(Path(args.headers_file))

    # Build biz -> (account_name, discipline, tier) from journal links markdown
    targets = parse_profile_links_file(args.journal_links)
    biz_to_meta = {target.biz: (target.account_name, target.discipline, target.tier) for target in targets}

    total = len(creds_map)
    limit = args.max_journals if args.max_journals > 0 else total
    processed_journals = 0
    success = 0
    _log.info(f"批量导入开始 | total={min(limit, total)}")

    for biz, creds in list(creds_map.items())[:limit]:
        processed_journals += 1
        meta = biz_to_meta.get(biz)
        account_name = meta[0] if meta else biz
        _log.info(f"[{processed_journals}/{min(limit, total)}] 处理公众号 | account={account_name} | biz={biz}")

        account = repo.get_account_by_name(account_name)
        if account is None:
            # Create a minimal account entry via the seed+candidate path
            discipline = meta[1] if meta else "未知"
            tier = meta[2] if meta else "未知"
            seed = repo.upsert_journal_seed(
                catalog_version="headers-import",
                discipline=discipline,
                tier=tier,
                journal_name=account_name,
                normalized_name=account_name,
                aliases=[],
                source_file=args.headers_file,
                source_line=None,
            )
            repo.upsert_account_candidate(
                journal_seed_id=seed.id,
                query_text="headers-import",
                candidate_name=account_name,
                score=1.0,
            )
            repo.auto_accept_high_score_candidates(threshold=1.0)
            account = repo.get_account_by_name(account_name)
            if account is None:
                _log.error(f"无法创建账号，跳过 | account={account_name}")
                continue

        try:
            stored = _crawl_journal_with_credentials(creds, account, args.pages, repo, args.sleep_page, args.sleep_article, _log)
            _log.info(f"账号完成 | account={account_name} | stored={stored}")
            success += 1
        except Exception as exc:
            _log.error(f"公众号处理失败 | account={account_name} | error={exc}")

    _log.info(f"import-from-headers 完成 | processed={processed_journals} | success={success}")
    return 0


def cmd_backfill_metrics(args, repo: Repository) -> int:
    if args.provider == "wechat-authorized":
        provider = WechatAuthorizedProvider.from_env()
        if not provider.is_configured:
            print(provider.status_message)
            return 1
        request_interval = float(getattr(args, "request_interval", 2.5))
        sample_size = int(getattr(args, "sample_size", 5))
        full = bool(getattr(args, "full", False))
        if request_interval < 0 or sample_size <= 0:
            print("request-interval 不能为负数，sample-size 必须大于 0")
            return 1
        since = _parse_date(args.since)
        until = _parse_date(args.until, end_of_day=True)
        articles = [
            article for article in repo.list_articles(account_name=args.account)
            if article.publish_time is None or (
                (since is None or _as_utc(article.publish_time) >= since)
                and (until is None or _as_utc(article.publish_time) <= until)
            )
        ]
        articles.sort(key=lambda article: (_as_utc(article.publish_time) if article.publish_time else datetime.min.replace(tzinfo=timezone.utc), article.id))
        task_name = "backfill-metrics:wechat-authorized"
        validation_task_name = f"{task_name}:sample:{args.since}:{args.until}:{sample_size}"
        if full:
            validation = repo.get_crawl_checkpoint(validation_task_name)
            if validation is None or validation.status != "success" or validation.account_name != args.account:
                print("请先完成并通过跨时间样本验证，再使用 --full 回填全部文章")
                return 1
        else:
            articles = _sample_articles_across_time(articles, sample_size)

        started_at = time.monotonic()
        stored = 0
        empty = 0
        failed = 0
        request_durations: list[float] = []
        for index, article in enumerate(articles):
            url = article.canonical_url or article.source_url or f"{article.biz}:{article.mid}:{article.idx}"
            request_started_at = time.monotonic()
            try:
                snapshot = provider.fetch(article)
                if _is_empty_metrics_snapshot(snapshot):
                    empty += 1
                    repo.record_failed_task(task_name, args.account or "", url, "微信指标响应为空")
                    print(f"跳过空指标：{article.mid}_{article.idx}")
                else:
                    repo.insert_metrics_snapshot(
                        article_id=article.id,
                        provider=snapshot.provider,
                        provider_priority=snapshot.provider_priority,
                        read_count=snapshot.read_count,
                        like_count=snapshot.like_count,
                        watching_count=snapshot.watching_count,
                        reward_count=snapshot.reward_count,
                        share_count=snapshot.share_count,
                        favorite_count=snapshot.favorite_count,
                        subscriber_count=snapshot.subscriber_count,
                        comment_count=snapshot.comment_count,
                        raw_payload=snapshot.raw_payload,
                        is_authorized_source=snapshot.is_authorized_source,
                    )
                    appmsgext = snapshot.raw_payload.get("appmsgext", {}) if snapshot.raw_payload else {}
                    repo.update_article_metrics_metadata(
                        article.id,
                        collect_num=snapshot.collect_num,
                        album_id=str(appmsgext.get("_album_id") or ""),
                        album_name=str(appmsgext.get("_album_name") or ""),
                    )
                    repo.mark_failed_task_success(task_name, url)
                    stored += 1
            except Exception as exc:
                failed += 1
                error_message = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}"
                repo.record_failed_task(task_name, args.account or "", url, error_message)
                print(f"指标回填失败：{article.mid}_{article.idx} | {error_message}")
            finally:
                request_durations.append(time.monotonic() - request_started_at)
            if index + 1 < len(articles) and request_interval:
                time.sleep(request_interval)

        elapsed = time.monotonic() - started_at
        total = len(articles)
        coverage = stored / total * 100 if total else 100.0
        average_request = sum(request_durations) / len(request_durations) if request_durations else 0.0
        request_range = (
            f"{min(request_durations):.2f}/{average_request:.2f}/{max(request_durations):.2f} 秒"
            if request_durations else "0.00/0.00/0.00 秒"
        )
        print(
            f"指标回填完成：目标 {total}，成功 {stored}，空响应 {empty}，失败 {failed}，"
            f"覆盖率 {coverage:.1f}%，耗时 {elapsed:.1f} 秒，单篇耗时 最小/平均/最大 {request_range}"
        )
        if not full:
            repo.save_crawl_checkpoint(
                validation_task_name,
                args.account,
                0,
                [article.canonical_url or article.source_url for article in articles],
                status="success" if failed == 0 and empty == 0 else "failed",
            )
        if full and failed == 0 and empty == 0 and not getattr(args, "no_export", False):
            written = RealtimeExporter(
                repo=repo,
                output_dir=pathlib.Path(getattr(args, "export_dir", "output")),
                formats=["csv", "json"],
            ).update(args.account)
            print("导出刷新完成：" + "，".join(str(path) for path in written))
        return 0 if failed == 0 and empty == 0 else 1
    elif args.provider == "public-html":
        print("public-html 指标仅在已有 HTML 中做低可信度解析；当前 CLI 不批量重读 HTML。")
        return 0
    else:
        print("manual-import 请通过后续 CSV/Markdown 导入流程回填。")
        return 0


def _cookie_value(cookie: str, name: str) -> str:
    prefix = f"{name}="
    for part in cookie.split(";"):
        candidate = part.strip()
        if candidate.startswith(prefix):
            return candidate[len(prefix):]
    return ""


def _sample_articles_across_time(articles, sample_size: int):
    dated = [article for article in articles if article.publish_time is not None]
    if sample_size <= 0 or sample_size >= len(dated):
        return dated if sample_size else articles
    if sample_size == 1:
        return [dated[len(dated) // 2]]
    last = len(dated) - 1
    indices = [round(index * last / (sample_size - 1)) for index in range(sample_size)]
    return [dated[index] for index in indices]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_empty_metrics_snapshot(snapshot: MetricsSnapshot) -> bool:
    return all(
        getattr(snapshot, field_name) is None
        for field_name in (
            "read_count",
            "like_count",
            "watching_count",
            "reward_count",
            "share_count",
            "favorite_count",
            "subscriber_count",
            "comment_count",
        )
    )


def cmd_export(args, repo: Repository) -> int:
    if getattr(args, "download_images", False):
        output_dir = pathlib.Path(args.output)
        if output_dir.suffix:
            output_dir = output_dir.parent
        written = RealtimeExporter(repo=repo, output_dir=output_dir, formats=["csv", "json"]).update(
            account_name=args.account,
            download_images=True,
        )
        print("导出与图片下载完成：" + "，".join(str(path) for path in written))
        return 0
    output_path = pathlib.Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [clean_export_row(row) for row in repo.list_articles_for_export(account_name=args.account)]
    if args.format == "markdown":
        account_name = args.account or "CSSCI 微信公众号文章"
        output_path.write_text(export_articles_markdown(account_name, rows), encoding="utf-8")
    elif args.format == "csv":
        headers = default_export_headers(rows)
        export_articles_csv(rows, str(output_path), headers)
    else:
        export_articles_json(rows, str(output_path))
    print(f"导出完成：{output_path}（{len(rows)} 条）")
    return 0


def cmd_audit_articles(args, repo: Repository) -> int:
    rows = repo.list_articles_for_export(account_name=args.account)
    mismatches = [row for row in rows if row.get("_account_biz") and row.get("biz") and row.get("_account_biz") != row.get("biz")]
    print(f"疑似不匹配文章：{len(mismatches)}")
    for row in mismatches[: args.limit]:
        print(f"{row.get('account_name')} | {row.get('author')} | {row.get('title')} | {row.get('canonical_url')}")
    return 0


def cmd_check_auth(args, _repo: Repository) -> int:
    provider = WechatAuthorizedProvider.from_env()
    print(provider.status_message)
    print(provider.redacted_config)
    return 0 if provider.is_configured else 1


def cmd_daemon(args, _repo: Repository) -> int:
    from scheduler.runner import start_daemon
    return start_daemon(dry_run=args.dry_run)


def cmd_stats(_args, repo: Repository) -> int:
    stats = repo.get_stats()
    print("=== 采集统计 ===")
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


def cmd_not_implemented(args, _repo: Repository) -> int:
    print(f"命令 {args.command} 已登记，后续任务实现。")
    return 0


def _parse_date(value: str, end_of_day: bool = False) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    load_local_env()
    setup_logging()

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command in {"desktop-observe", "check-wechat-capture", "crawl-client-history", "crawl-client-roster", "refill-client-journal", "export-client-csv", "audit-client-bundle"}:
        return args.func(args)
    database_url = args.db
    if args.command == "pilot" and database_url == f"sqlite:///{DEFAULT_DB_PATH}":
        database_url = "sqlite:///" + str(Path(args.output_dir) / "pilot.db")
    if database_url.startswith("sqlite:///"):
        db_path = pathlib.Path(database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    session_factory = create_session_factory(database_url)
    init_db(session_factory)
    return args.func(args, Repository(session_factory))


if __name__ == "__main__":
    raise SystemExit(main())
