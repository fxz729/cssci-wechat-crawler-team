from dataclasses import dataclass

from metrics.providers.wechat_backend import BackendArticleItem


class BackendCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogResult:
    items: tuple[BackendArticleItem, ...]
    next_begin: int
    total_count: int
    terminal_reason: str
    complete: bool


def collect_backend_catalog(client, *, page_size: int = 5, begin: int = 0) -> CatalogResult:
    if page_size < 1:
        raise ValueError("page_size must be positive")
    items: list[BackendArticleItem] = []
    seen_article_keys: set[tuple[str, int]] = set()
    seen_pages: set[tuple[tuple[str, int], ...]] = set()
    total_count: int | None = None
    current_begin = begin
    while True:
        page_items, page_total_count = client.fetch_publish_page(current_begin, page_size)
        if total_count is None:
            total_count = page_total_count
        elif page_total_count != total_count:
            raise BackendCatalogError("backend catalog incomplete: total count changed")
        page_keys = tuple((item.mid, item.idx) for item in page_items)
        if current_begin < total_count and not page_items:
            raise BackendCatalogError("backend catalog incomplete: empty page before total count")
        if page_keys and page_keys in seen_pages:
            raise BackendCatalogError("backend catalog incomplete: repeated page")
        seen_pages.add(page_keys)
        overlap = set(page_keys) & seen_article_keys
        if overlap:
            raise BackendCatalogError("backend catalog incomplete: repeated article")
        items.extend(page_items)
        seen_article_keys.update(page_keys)
        current_begin += page_size
        if current_begin >= total_count:
            complete = begin == 0
            terminal_reason = "total_count_reached" if complete else "resume_segment_complete"
            return CatalogResult(tuple(items), total_count, total_count, terminal_reason, complete)
