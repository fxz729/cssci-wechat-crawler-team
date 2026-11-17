import csv

from export.merge_legacy_delivery import merge_legacy_delivery


def test_merge_legacy_delivery_prepends_and_rewrites_paths(tmp_path):
    legacy = tmp_path / "legacy.csv"
    delivery = tmp_path / "delivery" / "文章数据.csv"
    unified = tmp_path / "delivery" / "附件"
    (unified / "旧数据").mkdir(parents=True)
    (unified / "新数据").mkdir(parents=True)
    delivery.parent.mkdir(parents=True, exist_ok=True)

    with legacy.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("biz", "mid", "idx", "title", "local_html", "old_only"))
        writer.writeheader()
        writer.writerow({
            "biz": "biz-a", "mid": "1", "idx": 1, "title": "旧标题",
            "local_html": "附件/old/1/article.html", "old_only": "保留",
        })
    with delivery.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("biz", "mid", "idx", "title", "local_html", "new_only"))
        writer.writeheader()
        writer.writerow({
            "biz": "biz-a", "mid": "1", "idx": 1, "title": "新标题",
            "local_html": "../journals/001_测试刊/bundle/articles/1_1/article.html",
            "new_only": "新增",
        })
        writer.writerow({
            "biz": "biz-a", "mid": "2", "idx": 1, "title": "第二篇",
            "local_html": "../journals/001_测试刊/bundle/articles/2_1/article.html",
            "new_only": "新增2",
        })

    report = merge_legacy_delivery(legacy, delivery, unified_root=unified)

    assert report["total_rows"] == 2
    with delivery.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["title"] == "新标题"
    assert rows[0]["old_only"] == "保留"
    assert rows[0]["new_only"] == "新增"
    assert rows[0]["local_html"].endswith("附件\\新数据\\001_测试刊\\articles\\1_1\\article.html")
    assert rows[1]["mid"] == "2"
