import json
from pathlib import Path

import pytest

from crawl.capture_cache import (
    build_capture_summary,
    cache_entry_path,
    read_capture_entry,
    write_capture_entry,
)


BIZ = "MzA-test_biz="
SN = "a" * 32


def article_event(*, title="测试文章", mid="123", idx=1, sn=SN):
    return {
        "schema_version": 1,
        "event_id": "article-1",
        "captured_at": "2026-09-07T08:00:00+00:00",
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "article",
        "biz": BIZ,
        "mid": mid,
        "idx": idx,
        "sn": sn,
        "article": {
            "title": title,
            "author": "作者",
            "account_name": "测试刊",
            "digest": "摘要",
            "publish_time": "2026-09-07T12:00:00+08:00",
            "url": f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={mid}&idx={idx}&sn={sn}",
            "cover_url": "https://mmbiz.qpic.cn/example/0",
            "content_html": "<p>正文</p>",
        },
    }


def metric_event(*, mid="123", idx=1, sn=SN, favorite=0):
    fields = {
        "$html.cgiData.read_num": "11",
        "$html.appmsg_bar_data.old_like_count": "2",
        "$html.appmsg_bar_data.like_count": "3",
        "$html.appmsg_bar_data.share_count": "4",
        "$html.appmsg_bar_data.collect_count": str(favorite),
        "$html.appmsg_bar_data.comment_count": "6",
    }
    return {
        "schema_version": 1,
        "event_id": "metric-1",
        "captured_at": "2026-09-07T08:00:01+00:00",
        "source": "fiddler_classic",
        "endpoint": "/s",
        "kind": "metrics",
        "biz": BIZ,
        "mid": mid,
        "idx": idx,
        "sn": sn,
        "metric_format": "client_html_bootstrap_v1",
        "http_status": 200,
        "process": "wechatappex:1234",
        "session_id": 9,
        "business_ret": 0,
        "comment_id": "88",
        "html_identity": {"biz": BIZ, "mid": mid, "idx": str(idx), "sn": sn},
        "bootstrap_fields": {path: [{"raw_value": value}] for path, value in fields.items()},
    }


def test_cache_round_trip_is_atomic_sanitized_and_keeps_zero(tmp_path):
    item = {"title": "测试文章", "runtime": "runtime-1"}
    written = write_capture_entry(tmp_path, 0, item, article_event(), metric_event(favorite=0), biz=BIZ)

    path = cache_entry_path(tmp_path, 0)
    assert path.is_file()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(raw, ensure_ascii=False).lower()
    for secret in ("cookie", "authorization", "pass_ticket", "request_headers"):
        assert secret not in serialized

    loaded = read_capture_entry(tmp_path, 0, item, biz=BIZ)
    assert loaded["complete"] is True
    assert loaded["identity"] == {"biz": BIZ, "mid": "123", "idx": 1, "sn": SN}
    assert loaded["metric_event"]["metrics"]["favorite_count"] == 0
    assert written["sha256"] == loaded["sha256"]


def test_cache_runtime_rebind_preserves_signed_article_identity_and_metrics(tmp_path):
    import crawl.capture_cache as cache

    old_item = {"title": "测试文章", "runtime": "runtime-old"}
    new_item = {"title": "测试文章", "runtime": "runtime-new"}
    write_capture_entry(tmp_path, 0, old_item, article_event(), metric_event(favorite=0), biz=BIZ)
    before = read_capture_entry(tmp_path, 0, old_item, biz=BIZ)

    assert cache.rebind_capture_entry_runtime(tmp_path, 0, new_item, biz=BIZ) is True
    after = read_capture_entry(tmp_path, 0, new_item, biz=BIZ)

    assert after["identity"] == before["identity"]
    assert after["article_event"] == before["article_event"]
    assert after["metric_event"] == before["metric_event"]
    assert after["catalog_runtime"] == "runtime-new"
    assert after["sha256"] != before["sha256"]
    assert cache.rebind_capture_entry_runtime(tmp_path, 0, new_item, biz=BIZ) is False


def test_cache_rejects_identity_and_catalog_title_mismatch(tmp_path):
    item = {"title": "测试文章", "runtime": "runtime-1"}
    with pytest.raises(ValueError, match="cache_identity_mismatch"):
        write_capture_entry(tmp_path, 0, item, article_event(), metric_event(mid="456"), biz=BIZ)
    with pytest.raises(ValueError, match="cache_catalog_title_mismatch"):
        write_capture_entry(tmp_path, 0, item, article_event(title="另一篇"), metric_event(), biz=BIZ)


def test_cache_summary_reports_missing_and_partial_without_coercing(tmp_path):
    catalog = {"items": [
        {"title": "完整", "runtime": "r1"},
        {"title": "指标缺失", "runtime": "r2"},
        {"title": "尚未访问", "runtime": "r3"},
    ]}
    write_capture_entry(tmp_path, 0, catalog["items"][0], article_event(title="完整"), metric_event(), biz=BIZ)
    partial = metric_event(mid="456", favorite=7)
    partial["bootstrap_fields"]["$html.appmsg_bar_data.comment_count"] = [{"raw_value": ""}]
    write_capture_entry(
        tmp_path, 1, catalog["items"][1],
        article_event(title="指标缺失", mid="456"), partial, biz=BIZ,
    )

    summary = build_capture_summary(tmp_path, catalog, biz=BIZ)
    assert summary["catalog_count"] == 3
    assert summary["complete_count"] == 1
    assert summary["partial_count"] == 1
    assert summary["missing_count"] == 1
    assert summary["complete"] is False
    assert summary["recapture_indices"] == [1, 2]
    assert summary["items"][1]["missing_metric_fields"] == ["comment_count"]


def test_cache_path_is_fixed_inside_base_and_invalid_index_is_rejected(tmp_path):
    path = cache_entry_path(tmp_path, 3)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.as_posix().endswith("capture_cache/articles/000003/capture_evidence.json")
    with pytest.raises(ValueError, match="invalid_cache_index"):
        cache_entry_path(tmp_path, -1)


def test_capture_cache_records_explicit_prior_local_origin(tmp_path):
    item = {"title": "测试文章", "runtime": "runtime-1"}
    write_capture_entry(tmp_path, 0, item, article_event(), metric_event(), biz=BIZ, cache_origin="prior_local_event")
    loaded = read_capture_entry(tmp_path, 0, item, biz=BIZ)
    assert loaded["cache_origin"] == "prior_local_event"
    summary = build_capture_summary(tmp_path, {"items":[item]}, biz=BIZ)
    assert summary["prior_local_count"] == 1
