from copy import deepcopy
import csv
import json

import pytest

from discovery.reprint_search import ReprintResponseError, parse_reprint_page, export_probe

BIZ = "target-biz"
SN = "a" * 32


def article(mid="1", *, biz=BIZ, title="Example"):
    return {
        "url": f"http://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx=1&sn={SN}&key=not-for-export",
        "title": title, "nickname": "Target journal", "author": "Editor",
        "digest": "Exact source digest", "content": "<p>Full body</p><p>Second line</p>",
        "cover_url": "https://mmbiz.qpic.cn/example.jpg",
    }


def payload(items):
    return {"base_resp": {"ret": 0}, "total": 10000, "list": items}


def test_filters_by_url_biz_not_nickname_and_preserves_raw_content():
    wrong = article("2", biz="other-biz")
    page = parse_reprint_page(payload([article(), wrong]), BIZ, begin=0)
    assert page["returned_count"] == 2
    assert page["filtered_count"] == 1
    row = page["articles"][0]
    assert row["biz"] == BIZ
    assert row["content_html"] == "<p>Full body</p><p>Second line</p>"
    assert row["content"] == "Full body\nSecond line"
    assert row["digest"] == "Exact source digest"
    assert "key=" not in row["canonical_url"]
    assert row["publish_time"] is None
    for key in ("read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count"):
        assert row[key] is None
    assert row["metrics_status"] == "not_returned"


def test_short_page_is_not_terminal_and_total_is_only_search_hint():
    page = parse_reprint_page(payload([article(str(n)) for n in range(1, 10)]), BIZ, begin=20)
    assert page["next_begin"] == 30
    assert page["total_hint"] == 10000
    assert page["catalog_complete"] is False
    assert page["scope"] == "original_article_keyword_search"


def test_no_matching_target_does_not_end_search_pagination():
    page = parse_reprint_page(payload([article(biz="other")]), BIZ, begin=30)
    assert page["articles"] == []
    assert page["next_begin"] == 40


def test_empty_search_results_never_prove_full_journal_coverage():
    page = parse_reprint_page(payload([]), BIZ, begin=40)
    assert page["next_begin"] is None
    assert page["catalog_complete"] is False


@pytest.mark.parametrize("code", [200013, 64701, None, False])
def test_business_error_or_missing_code_is_not_an_empty_success(code):
    body = payload([])
    body["base_resp"]["ret"] = code
    with pytest.raises(ReprintResponseError):
        parse_reprint_page(body, BIZ)


@pytest.mark.parametrize("url", [
    "https://example.com/s?__biz=target-biz&mid=1&idx=1&sn=" + SN,
    "https://mp.weixin.qq.com/s?__biz=target-biz&mid=1&idx=1&sn=" + SN + "&mid=2",
    "https://mp.weixin.qq.com/s?__biz=target-biz&mid=../bad&idx=1&sn=" + SN,
])
def test_untrusted_or_ambiguous_article_identity_is_rejected(url):
    item = article()
    item["url"] = url
    with pytest.raises(ReprintResponseError):
        parse_reprint_page(payload([item]), BIZ)


def evidence_page(items, begin=0):
    return {
        "name": "keyword", "query": "journal", "target_biz": BIZ, "begin": begin,
        "count": 10, "http_status": 200, "business_ret": 0, "total_hint": 10000,
        "target_articles": items, "mode": "background_same_session",
    }


def test_export_is_idempotent_keeps_legacy_columns_and_local_body(tmp_path):
    evidence = {"schema_version": 1, "captured_at": "2026-09-09T00:00:00Z", "pages": [evidence_page([article()]), evidence_page([article()], 10)]}
    first = export_probe(evidence, tmp_path)
    second = export_probe(evidence, tmp_path)
    assert first == second
    assert first["article_count"] == 1
    path = tmp_path / "文章数据.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["favorite_count"] == ""
    assert rows[0]["digest"] == "Exact source digest"
    assert (tmp_path / rows[0]["local_text"]).read_text(encoding="utf-8") == "Full body\nSecond line"
    assert "Content-Security-Policy" in (tmp_path / rows[0]["local_html"]).read_text(encoding="utf-8")
    assert "key=" not in json.dumps(rows)
    assert first["catalog_complete"] is False


def test_error_page_does_not_hide_earlier_successes(tmp_path):
    failed = evidence_page([])
    failed["business_ret"] = 64701
    result = export_probe({"schema_version": 1, "pages": [evidence_page([article()]), failed]}, tmp_path)
    assert result["article_count"] == 1
    assert result["rejected_pages"] == 1


def test_conflicting_identity_is_not_silently_overwritten(tmp_path):
    altered = deepcopy(article())
    altered["url"] = altered["url"].replace(SN, "b" * 32)
    with pytest.raises(ReprintResponseError):
        export_probe({"schema_version": 1, "pages": [evidence_page([article(), altered])]}, tmp_path)

def test_long_body_is_not_truncated_and_empty_digest_is_not_inferred(tmp_path):
    item = article()
    item["content"] = "<p>" + ("long body " * 18000) + "</p>"
    item["digest"] = ""
    out = export_probe({"schema_version": 1, "pages": [evidence_page([item])]}, tmp_path)
    limit = csv.field_size_limit(2_000_000)
    try:
        with (tmp_path / out["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
    finally:
        csv.field_size_limit(limit)
    assert row["content_html"] == item["content"]
    assert row["digest"] == ""


def test_empty_content_does_not_become_a_success():
    item = article()
    item["content"] = ""
    row = parse_reprint_page(payload([item]), BIZ)["articles"][0]
    assert row["content_status"] == "empty"
    assert row["metrics_status"] == "not_returned"