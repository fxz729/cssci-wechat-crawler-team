from datetime import datetime, timezone

import pytest

from crawl.backend_catalog import BackendCatalogError, collect_backend_catalog
from metrics.providers.wechat_backend import BackendArticleItem


def _item(mid: str, idx: int = 1) -> BackendArticleItem:
    return BackendArticleItem(
        title=f"article-{mid}-{idx}",
        author="account",
        digest="",
        cover_url="",
        link=f"https://mp.weixin.qq.com/s?__biz=target-biz&mid={mid}&idx={idx}&sn=synthetic",
        mid=mid,
        idx=idx,
        publish_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class ClientStub:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_publish_page(self, begin, count):
        self.calls.append((begin, count))
        return self.pages[begin]


def test_collects_all_publish_batches_and_keeps_secondary_articles():
    client = ClientStub({
        0: ([_item("1"), _item("2"), _item("2", 2)], 3),
        2: ([_item("3")], 3),
    })

    result = collect_backend_catalog(client, page_size=2)

    assert [(item.mid, item.idx) for item in result.items] == [("1", 1), ("2", 1), ("2", 2), ("3", 1)]
    assert client.calls == [(0, 2), (2, 2)]
    assert result.complete is True
    assert result.next_begin == 3
    assert result.total_count == 3


def test_stops_after_first_empty_page_when_total_count_is_zero():
    client = ClientStub({0: ([], 0)})

    result = collect_backend_catalog(client, page_size=5)

    assert result.items == ()
    assert result.complete is True
    assert client.calls == [(0, 5)]


def test_resumed_catalog_segment_is_not_reported_as_complete_catalog():
    client = ClientStub({2: ([_item("3")], 3)})

    result = collect_backend_catalog(client, begin=2, page_size=2)

    assert result.complete is False
    assert result.terminal_reason == "resume_segment_complete"


@pytest.mark.parametrize(
    "pages",
    [
        {0: ([_item("1")], 3), 2: ([], 3)},
        {0: ([_item("1"), _item("2")], 3), 2: ([_item("2")], 3)},
    ],
)
def test_rejects_incomplete_or_repeated_pages(pages):
    client = ClientStub(pages)

    with pytest.raises(BackendCatalogError, match="incomplete"):
        collect_backend_catalog(client, page_size=2)
