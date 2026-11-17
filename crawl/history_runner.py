from dataclasses import dataclass

import requests

from crawl.errors import SessionUnavailable
from storage.models import FailedCrawlTask


@dataclass(frozen=True)
class HistoryRunResult:
    processed: int
    failed: int
    completed: bool


def run_account_history(
    repo,
    account,
    client,
    process_item,
    *,
    page_size: int = 5,
    resume: bool = True,
    progress_logger=None,
) -> HistoryRunResult:
    if page_size < 1:
        raise ValueError("page_size must be positive")
    task_name = f"crawl-history:{account.canonical_name}"
    checkpoint = repo.get_crawl_checkpoint(task_name) if resume else None
    if checkpoint and checkpoint.status in {"running", "paused"}:
        current_begin = checkpoint.page
    else:
        current_begin = 0
    processed_keys = list(checkpoint.processed_urls) if checkpoint else []
    processed_set = set(processed_keys)
    processed = 0
    failed = 0

    def save_checkpoint(page: int, status: str = "running"):
        checkpoint_result = repo.save_crawl_checkpoint(
            task_name,
            account.canonical_name,
            page,
            processed_keys,
            status=status,
        )
        if progress_logger is not None:
            progress_logger.checkpoint_saved(
                account.canonical_name,
                page=page,
                processed=len(processed_keys),
                status=status,
            )
        return checkpoint_result

    total_count: int | None = None
    seen_pages: set[tuple[tuple[str, int], ...]] = set()
    seen_catalog_keys: set[tuple[str, int]] = set()
    try:
        while True:
            page_begin = current_begin
            page_items, page_total_count = client.fetch_publish_page(current_begin, page_size)
            if progress_logger is not None:
                progress_logger.catalog_page_fetched(
                    account.canonical_name,
                    begin=current_begin,
                    count=len(page_items),
                    total=page_total_count,
                    page_size=page_size,
                )
            if total_count is None:
                total_count = page_total_count
            elif page_total_count != total_count:
                raise RuntimeError("catalog total count changed")
            page_keys = tuple((item.mid, item.idx) for item in page_items)
            if current_begin < total_count and not page_keys:
                raise RuntimeError("catalog page empty before completion")
            if page_keys and page_keys in seen_pages:
                raise RuntimeError("catalog page repeated")
            if set(page_keys) & seen_catalog_keys:
                raise RuntimeError("catalog article repeated across pages")
            seen_pages.add(page_keys)
            seen_catalog_keys.update(page_keys)
            for item in page_items:
                item_key = _item_key(item)
                if item_key in processed_set:
                    continue
                article_number = len(processed_keys) + 1
                if progress_logger is not None:
                    progress_logger.history_article_started(
                        account.canonical_name,
                        current=article_number,
                        total=page_total_count,
                        title=item.title,
                    )
                try:
                    saved_article = process_item(item)
                    processed_keys.append(item_key)
                    processed_set.add(item_key)
                    repo.mark_failed_task_success(task_name, _failure_reference(item))
                    save_checkpoint(current_begin)
                    processed += 1
                    if progress_logger is not None:
                        progress_logger.history_article_saved(
                            account.canonical_name,
                            current=article_number,
                            total=page_total_count,
                            title=item.title,
                            content_chars=len(getattr(saved_article, "content_text", "") or ""),
                            image_count=int(getattr(saved_article, "image_count", 0) or 0),
                            metrics_count=_metrics_count(getattr(saved_article, "latest_metrics", None)),
                        )
                except SessionUnavailable:
                    save_checkpoint(page_begin, status="paused")
                    return HistoryRunResult(processed=processed, failed=failed, completed=False)
                except Exception as exc:
                    if progress_logger is not None:
                        progress_logger.history_article_failed(
                            account.canonical_name,
                            current=article_number,
                            total=page_total_count,
                            title=item.title,
                            error_type=type(exc).__name__,
                        )
                    repo.record_failed_task(task_name, account.canonical_name, _failure_reference(item), type(exc).__name__)
                    failed += 1
                    save_checkpoint(page_begin, status="failed")
            current_begin += page_size
            if current_begin >= total_count:
                completed = failed == 0 and not _has_unresolved_failures(repo, task_name)
                status = "completed" if completed else "failed"
                next_begin = total_count if status == "completed" else page_begin
                save_checkpoint(next_begin, status=status)
                return HistoryRunResult(processed=processed, failed=failed, completed=completed)
    except SessionUnavailable:
        save_checkpoint(current_begin, status="paused")
        return HistoryRunResult(processed=processed, failed=failed, completed=False)
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        status = "paused" if status_code in {401, 403, 429} else "failed"
        save_checkpoint(current_begin, status=status)
        return HistoryRunResult(processed=processed, failed=failed, completed=False)
    except Exception:
        save_checkpoint(current_begin, status="failed")
        return HistoryRunResult(processed=processed, failed=failed + 1, completed=False)


def _has_unresolved_failures(repo, task_name: str) -> bool:
    session_factory = getattr(repo, "session_factory", None)
    if not callable(session_factory):
        return False  # Legacy stubs have no persistent failure store.
    # Retry eligibility is not resolution: exhausted failures still block completion.
    with session_factory() as session:
        return session.query(FailedCrawlTask.id).filter_by(
            task_name=task_name, status="failed",
        ).first() is not None


def _metrics_count(snapshot) -> int:
    if snapshot is None:
        return 0
    fields = (
        "read_count",
        "like_count",
        "watching_count",
        "favorite_count",
        "share_count",
        "comment_count",
    )
    return sum(getattr(snapshot, field, None) is not None for field in fields)


def _item_key(item) -> str:
    return f"{item.mid}:{item.idx}"


def _failure_reference(item) -> str:
    return f"article:{item.mid}:{item.idx}"
