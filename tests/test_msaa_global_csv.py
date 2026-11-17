import csv
import json
from pathlib import Path

from export.msaa_global_csv import merge


def test_merge_msaa_bundles_into_one_csv_with_portable_paths(tmp_path):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "expected_count": 1,
        "journals": [{
            "ordinal": 1,
            "journal_name": "测试刊",
            "account_name": "测试刊",
            "biz": "biz-a",
            "base": str(base),
        }],
    }, ensure_ascii=False), encoding="utf-8")
    article_dir = bundle / "articles" / "1_1"
    (article_dir / "images").mkdir(parents=True)
    (article_dir / "article.html").write_text("正文", encoding="utf-8")
    (article_dir / "images" / "a.png").write_bytes(b"png")
    with (bundle / "文章数据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "biz", "mid", "idx", "title", "local_html", "image_local_paths",
        ))
        writer.writeheader()
        writer.writerow({
            "biz": "biz-a",
            "mid": "1",
            "idx": 1,
            "title": "标题",
            "local_html": "articles/1_1/article.html",
            "image_local_paths": json.dumps(["articles/1_1/images/a.png"], ensure_ascii=False),
        })

    summary = merge(run_dir)

    assert summary["article_count"] == 1
    with (run_dir / "delivery" / "文章数据.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["journal_id"] == "001"
    assert row["journal_name"] == "测试刊"
    expected_html = (run_dir / "delivery" / "附件" / "新数据" / "001_测试刊" / "articles" / "1_1" / "article.html").resolve()
    expected_image = (run_dir / "delivery" / "附件" / "新数据" / "001_测试刊" / "articles" / "1_1" / "images" / "a.png").resolve()
    assert row["local_html"] == str(expected_html)
    assert expected_html.is_file()
    assert json.loads(row["image_local_paths"]) == [str(expected_image)]
    assert expected_image.is_file()
    media_csv = run_dir / "delivery" / "媒体索引.csv"
    assert media_csv.is_file()


def test_merge_writes_chinese_header_delivery_copy(tmp_path):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "expected_count": 1,
        "journals": [{
            "ordinal": 1,
            "journal_name": "测试刊",
            "account_name": "测试刊",
            "biz": "biz-a",
            "base": str(base),
        }],
    }, ensure_ascii=False), encoding="utf-8")
    with (bundle / "文章数据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "biz", "mid", "digest", "content", "favorite_count",
            "comment_count", "media_index",
        ))
        writer.writeheader()
        writer.writerow({
            "biz": "biz-a",
            "mid": "1",
            "digest": "摘要内容",
            "content": "正文内容",
            "favorite_count": 6,
            "comment_count": 2,
            "media_index": json.dumps([{"kind": "image", "url": "https://example.test/a.png"}], ensure_ascii=False),
        })

    summary = merge(run_dir, write_chinese_copy=True)

    chinese_csv = Path(summary["chinese_csv"])
    assert chinese_csv.is_file()
    with chinese_csv.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        row = next(reader)
    assert "摘要 (digest)" in reader.fieldnames
    assert "正文文本 (content)" in reader.fieldnames
    assert "收藏数 (favorite_count)" in reader.fieldnames
    assert "评论数 (comment_count)" in reader.fieldnames
    assert "媒体索引 (media_index)" in reader.fieldnames
    assert row["摘要 (digest)"] == "摘要内容"
    assert row["正文文本 (content)"] == "正文内容"
    assert row["收藏数 (favorite_count)"] == "6"
    assert row["评论数 (comment_count)"] == "2"
    assert json.loads(row["媒体索引 (media_index)"])[0]["kind"] == "image"


def test_merge_handles_field_larger_than_default_csv_limit(tmp_path):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "expected_count": 1,
        "journals": [{
            "ordinal": 1,
            "journal_name": "测试刊",
            "account_name": "测试刊",
            "biz": "biz-a",
            "base": str(base),
        }],
    }, ensure_ascii=False), encoding="utf-8")
    long_text = "长" * 200000
    with (bundle / "文章数据.csv").open(
        "w", encoding="utf-8-sig", newline="",
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=("biz", "mid", "content"))
        writer.writeheader()
        writer.writerow({"biz": "biz-a", "mid": "1", "content": long_text})

    summary = merge(run_dir)

    assert summary["article_count"] == 1
    with (run_dir / "delivery" / "文章数据.csv").open(
        encoding="utf-8-sig", newline="",
    ) as stream:
        row = next(csv.DictReader(stream))
    assert row["content"] == long_text


def test_merge_preserves_existing_delivery_rows_when_bundle_changes(tmp_path):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_测试刊"
    bundle = base / "bundle"
    bundle.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "expected_count": 1,
        "journals": [{
            "ordinal": 1,
            "journal_name": "测试刊",
            "account_name": "测试刊",
            "biz": "biz-a",
            "base": str(base),
        }],
    }, ensure_ascii=False), encoding="utf-8")

    def write_bundle(mid):
        with (bundle / "文章数据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("biz", "mid", "idx", "title"))
            writer.writeheader()
            writer.writerow({"biz": "biz-a", "mid": str(mid), "idx": 1, "title": f"文章{mid}"})

    write_bundle(1)
    first = merge(run_dir)
    assert first["article_count"] == 1

    write_bundle(2)
    second = merge(run_dir)
    assert second["existing_count"] == 1
    assert second["appended_count"] == 1
    assert second["article_count"] == 2
    with (run_dir / "delivery" / "文章数据.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["mid"] for row in rows} == {"1", "2"}


def test_merge_normalizes_absolute_bundle_paths_and_copies_media(tmp_path):
    run_dir = tmp_path / "run"
    base = run_dir / "journals" / "001_abs"
    bundle = base / "bundle"
    article_dir = bundle / "articles" / "7_1"
    article_dir.mkdir(parents=True)
    (article_dir / "article.html").write_text("body", encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "expected_count": 1,
        "journals": [{"ordinal": 1, "journal_name": "abs", "account_name": "abs", "biz": "biz", "base": str(base)}],
    }), encoding="utf-8")
    with (bundle / "文章数据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("biz", "mid", "idx", "local_html", "local_article_dir"))
        writer.writeheader()
        writer.writerow({"biz": "biz", "mid": "7", "idx": 1,
                         "local_html": str(article_dir / "article.html"),
                         "local_article_dir": str(article_dir)})

    summary = merge(run_dir)

    assert summary["article_count"] == 1
    with (run_dir / "delivery" / "文章数据.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    expected = run_dir / "delivery" / "附件" / "新数据" / "001_abs" / "articles" / "7_1" / "article.html"
    assert row["local_html"] == str(expected.resolve())
    assert expected.is_file()
