import copy

from crawl.client_capture import build_capture_metadata


ARTICLE = {
    "schema_version": 1,
    "kind": "article",
    "biz": "Mzk3NTE3NTU0Ng==",
    "mid": "2247486972",
    "idx": 1,
    "sn": "74997aa366b733ad9624092f4004a9ec",
    "captured_at": "2026-09-06T16:16:20+00:00",
    "article": {
        "title": "征文启事",
        "author": "原始作者",
        "account_name": "页面原始昵称",
        "digest": "原始摘要",
        "cover_url": "https://mmbiz.qpic.cn/mmbiz_jpg/example/0?wx_fmt=jpeg",
        "url": "https://mp.weixin.qq.com/s?__biz=Mzk3NTE3NTU0Ng%3D%3D&mid=2247486972&idx=1&sn=74997aa366b733ad9624092f4004a9ec",
        "publish_time": "1780000000",
        "content_html": "<div id=\"js_content\"><p>正文</p></div>",
        "content_origin": "html_fragment",
    },
}

METRIC = {
    "kind": "metrics",
    "metric_format": "client_html_bootstrap_v1",
    "captured_at": "2026-09-06T16:16:20+00:00",
    "http_status": 200,
    "business_ret": 0,
    "session_id": 1319,
    "process": "wechatappex:20060",
    "comment_id": "4531441182274748428",
    "metrics": {
        "read_count": 2644,
        "like_count": 35,
        "watching_count": 7,
        "share_count": 319,
        "favorite_count": 72,
        "comment_count": 0,
    },
    "field_meta": {
        field: {
            "raw_field": f"$html.appmsg_bar_data.{field}",
            "raw_value": str(value),
            "status": "observed",
        }
        for field, value in {
            "read_count": 2644,
            "like_count": 35,
            "watching_count": 7,
            "share_count": 319,
            "favorite_count": 72,
            "comment_count": 0,
        }.items()
    },
}


BASE_ROW = {
    "content_text": "正文",
    "content_html": "<div id=\"js_content\"><p>正文</p></div>",
    "media_assets": [],
    "field_provenance": {"media_assets": "#js_content"},
}


def test_build_capture_metadata_keeps_event_fields_and_metric_provenance():
    row = build_capture_metadata(
        ARTICLE,
        METRIC,
        BASE_ROW,
        journal_name="当代世界社会主义问题",
        profile_url="https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=Mzk3NTE3NTU0Ng==",
        local_html="articles/2247486972_1/article.html",
        local_text="articles/2247486972_1/article.txt",
        local_article_dir="articles/2247486972_1",
    )

    assert row["account_name"] == "当代世界社会主义问题"
    assert row["article_account_name_raw"] == "页面原始昵称"
    assert row["cover_url"] == ARTICLE["article"]["cover_url"]
    assert row["article_url"] == ARTICLE["article"]["url"]
    assert row["comment_id"] == "4531441182274748428"
    assert row["business_ret"] == 0
    assert row["http_status"] == 200
    assert row["fiddler_session_id"] == 1319
    assert row["metrics_status"] == "complete"
    assert row["read_count"] == 2644
    assert row["favorite_count"] == 72
    assert row["comment_count"] == 0
    assert row["metric_fields"]["favorite_count"]["raw_field"]
    assert row["field_provenance"]["metrics"]["comment_count"]["source"] == "fiddler_classic:/s"


def test_build_capture_metadata_does_not_turn_missing_metrics_into_zero():
    row = build_capture_metadata(
        ARTICLE,
        None,
        copy.deepcopy(BASE_ROW),
        journal_name="当代世界社会主义问题",
        profile_url="",
        local_html="",
        local_text="",
        local_article_dir="",
    )

    assert row["metrics_status"] == "not_observed"
    assert all(row["metrics"][field] is None for field in (
        "read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count"
    ))
    assert row["comment_id"] == ""
    assert row["business_ret"] is None

