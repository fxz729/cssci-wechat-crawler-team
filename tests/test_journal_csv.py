import csv
import json
from pathlib import Path

import pytest

from export.journal_csv import write_journal_csv_bundle
from export.realtime_exporter import default_export_headers


def make_bundle(tmp_path, content="第一段\n第二段"):
    folder = tmp_path / "articles" / "123_1"
    folder.mkdir(parents=True)
    (folder / "article.txt").write_text(content, encoding="utf-8")
    (folder / "article.html").write_text("<p>正文</p>", encoding="utf-8")
    image = folder / "images" / "image.jpg"
    image.parent.mkdir()
    image.write_bytes(b"media-test")
    row = {"biz":"biz", "mid":"123", "idx":1, "sn":"a"*32, "account_name":"期刊",
           "title":"标题", "digest":"原始摘要", "author":"", "content_text":content,
           "content_html":"<p>正文</p>", "local_html":"articles/123_1/article.html",
           "local_text":"articles/123_1/article.txt", "publish_time":"2026-01-01T00:00:00+00:00",
           "metrics":{"read_count":1, "favorite_count":0, "comment_count":None},
           "cookie":"MUST_NOT_EXPORT"}
    (folder / "metadata.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    media = [{"article_key":"123_1", "mid":"123", "idx":1, "kind":"image",
              "status":"downloaded", "local_path":"articles/123_1/images/image.jpg", "bytes":10}]
    (tmp_path / "media_rows.json").write_text(json.dumps(media), encoding="utf-8")
    return folder


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def test_csv_preserves_legacy_fields_full_text_zero_and_blank(tmp_path):
    content = "首段,含逗号\n" + "长正文" * 14000
    make_bundle(tmp_path, content)
    result = write_journal_csv_bundle(tmp_path)
    path = tmp_path / "文章数据.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    headers, rows = read_csv(path)
    assert set(default_export_headers([])).issubset(headers)
    assert rows[0]["content"] == content
    assert rows[0]["digest"] == "原始摘要"
    assert rows[0]["favorite_count"] == "0"
    assert rows[0]["comment_count"] == ""
    assert rows[0]["author"] == ""
    assert "MUST_NOT_EXPORT" not in path.read_text(encoding="utf-8-sig")
    assert result["article_count"] == 1
    assert result["media_count"] == 1
    for local in json.loads(rows[0]["media_local_paths"]):
        assert (tmp_path / local).is_file()
    assert (tmp_path / rows[0]["local_text"]).is_file()


def test_csv_export_is_repeatable_without_duplicate_rows(tmp_path):
    make_bundle(tmp_path)
    write_journal_csv_bundle(tmp_path)
    write_journal_csv_bundle(tmp_path)
    assert len(read_csv(tmp_path / "文章数据.csv")[1]) == 1
    assert len(read_csv(tmp_path / "媒体索引.csv")[1]) == 1


def test_csv_rejects_escaping_media_path(tmp_path):
    make_bundle(tmp_path)
    (tmp_path / "media_rows.json").write_text(json.dumps([
        {"article_key":"123_1","status":"downloaded","local_path":"../outside.txt"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe_bundle_path"):
        write_journal_csv_bundle(tmp_path)


def test_csv_without_media_is_partial_not_empty_content(tmp_path):
    folder = make_bundle(tmp_path)
    (tmp_path / "media_rows.json").write_text("[]", encoding="utf-8")
    write_journal_csv_bundle(tmp_path)
    row = read_csv(tmp_path / "文章数据.csv")[1][0]
    assert row["content"] == "第一段\n第二段"
    assert row["media_manifest_status"] == "not_collected"
