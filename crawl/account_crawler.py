from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from crawl.article_fetcher import parse_fetched_article, persist_article_files


@dataclass(frozen=True)
class CrawlWindow:
    since: date | datetime
    until: date | datetime

    def contains(self, value) -> bool:
        if value is None:
            return True
        current = value.date() if hasattr(value, "date") else value
        since = self.since.date() if hasattr(self.since, "date") else self.since
        until = self.until.date() if hasattr(self.until, "date") else self.until
        return since <= current <= until


def crawl_account_from_results(
    repo,
    account,
    results: list[dict],
    fetch_html: Callable[[str], tuple[str, str] | None],
    data_dir: Path,
    since: date | datetime,
    until: date | datetime,
    task_name: str | None = None,
    start_page: int = 1,
    realtime_exporter=None,
    progress_logger=None,
) -> int:
    window = CrawlWindow(since=since, until=until)
    task_name = task_name or f"crawl:{account.canonical_name}"
    checkpoint = repo.get_crawl_checkpoint(task_name)
    processed_urls = set(checkpoint.processed_urls if checkpoint else [])
    stored = 0
    total = len(results)
    if progress_logger:
        progress_logger.account_started(account.canonical_name, total_results=total)

    for offset, result in enumerate(results, start=1):
        url = result["url"]
        if url in processed_urls:
            if progress_logger:
                progress_logger.article_skipped(account.canonical_name, url, "checkpoint")
            continue
        try:
            fetched = fetch_html(url)
            if not fetched:
                raise RuntimeError("empty article html")
            final_url, html = fetched
            parsed_article, url_parts = parse_fetched_article(html, final_url)
            if parsed_article is None or not url_parts.biz or not url_parts.mid or url_parts.idx is None:
                raise RuntimeError("invalid article html or url parts")
            if not _is_official_article(account, parsed_article):
                processed_urls.add(url)
                repo.save_crawl_checkpoint(task_name, account.canonical_name, start_page, list(processed_urls), status="running")
                if progress_logger:
                    progress_logger.article_skipped(account.canonical_name, url, f"non-official author={parsed_article.author or parsed_article.account_name}")
                continue
            if not window.contains(parsed_article.publish_time):
                processed_urls.add(url)
                repo.save_crawl_checkpoint(task_name, account.canonical_name, start_page, list(processed_urls), status="running")
                if progress_logger:
                    progress_logger.article_skipped(account.canonical_name, url, "out-of-range")
                continue
            raw_html_path, raw_text_path = persist_article_files(
                data_dir,
                url_parts.biz,
                url_parts.mid,
                url_parts.idx,
                html,
                parsed_article.content_text,
            )
            if hasattr(repo, "update_account_biz"):
                repo.update_account_biz(account.id, url_parts.biz)
            batch = repo.upsert_publish_batch(
                account_id=account.id,
                biz=url_parts.biz,
                mid=url_parts.mid,
                publish_time=parsed_article.publish_time,
                source_url=url,
            )
            repo.upsert_article(
                account_id=account.id,
                publish_batch_id=batch.id,
                biz=url_parts.biz,
                mid=url_parts.mid,
                idx=url_parts.idx,
                sn=url_parts.sn or "",
                title=parsed_article.title,
                author=parsed_article.author,
                publish_time=parsed_article.publish_time,
                digest=parsed_article.digest or result.get("summary", ""),
                content_text=parsed_article.content_text,
                image_urls=parsed_article.image_urls,
                source_url=url,
                canonical_url=url_parts.canonical_url,
                raw_html_path=raw_html_path,
                raw_text_path=raw_text_path,
                content_html=parsed_article.content_html,
                cover_image_url=parsed_article.cover_image_url,
            )
            stored += 1
            processed_urls.add(url)
            repo.mark_failed_task_success(task_name, url)
            repo.save_crawl_checkpoint(task_name, account.canonical_name, start_page, list(processed_urls), status="running")
            if progress_logger:
                progress_logger.article_saved(account.canonical_name, parsed_article.title, stored=stored, total=total)
            if realtime_exporter:
                paths = realtime_exporter.update(account_name=account.canonical_name)
                if progress_logger:
                    progress_logger.export_updated(account.canonical_name, paths)
        except Exception as exc:
            failed = repo.record_failed_task(task_name, account.canonical_name, url, str(exc))
            if progress_logger:
                progress_logger.article_failed(account.canonical_name, url, str(exc), failed.retry_count)
            repo.save_crawl_checkpoint(task_name, account.canonical_name, start_page, list(processed_urls), status="running")
            continue

    repo.save_crawl_checkpoint(task_name, account.canonical_name, start_page, list(processed_urls), status="completed")
    return stored


def _is_official_article(account, parsed_article) -> bool:
    expected_names = {_normalize_name(account.canonical_name)}
    aliases = getattr(account, "account_aliases_json", "") or ""
    if aliases:
        import json
        try:
            expected_names.update(_normalize_name(alias) for alias in json.loads(aliases))
        except Exception:
            pass
    names = {_normalize_name(parsed_article.account_name), _normalize_name(parsed_article.author)}
    return bool(expected_names & names)


def _normalize_name(name: str | None) -> str:
    return (name or "").strip().replace(" ", "").replace("（", "(").replace("）", ")")
