from export.journal_validation import prepare_workbook_rows, summarize_bundle


def test_summary_distinguishes_catalog_identity_content_metrics_and_media():
    catalog = {"items": [{"title": "甲"}, {"title": "乙"}, {"title": "丙"}]}
    state = {"jobs": {
        "0": {"status": "captured", "mid": "1", "idx": 1},
        "1": {"status": "partial", "mid": "2", "idx": 1},
        "2": {"status": "paused"},
    }}
    records = [
        {"mid": "1", "idx": 1, "content_html": "<p>a</p>", "content_text": "a", "metrics_status": "complete",
         "comment_id": "c1", "business_ret": 0,
         "read_count": 1, "like_count": 2, "watching_count": 3, "share_count": 4, "favorite_count": 5, "comment_count": 0},
        {"mid": "2", "idx": 1, "content_html": "<p>b</p>", "content_text": "b", "metrics_status": "partial",
         "comment_id": "c2", "business_ret": None,
         "read_count": 1, "like_count": None, "watching_count": None, "share_count": 4, "favorite_count": None, "comment_count": 0},
    ]
    media = [
        {"article_key": "1_1", "status": "downloaded"},
        {"article_key": "2_1", "status": "needs_resolution", "error_code": "player_url"},
    ]

    result = summarize_bundle(catalog, state, records, media)

    assert result["catalog_count"] == 3
    assert result["resolved_identity_count"] == 2
    assert result["record_count"] == 2
    assert result["content_html_count"] == 2
    assert result["metrics_complete_count"] == 1
    assert result["metric_field_counts"]["comment_count"] == 2
    assert result["article_field_counts"]["comment_id"] == 2
    assert result["business_ret_count"] == 1
    assert result["media_status_counts"] == {"downloaded": 1, "needs_resolution": 1}
    assert result["pending_media_count"] == 1


def test_prepare_workbook_rows_adds_article_evidence_path():
    rows = prepare_workbook_rows([{"mid": "1", "idx": 1}], [])
    assert rows[0]["capture_evidence"] == "articles/1_1/capture_evidence.json"
