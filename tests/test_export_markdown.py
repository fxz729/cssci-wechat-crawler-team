from datetime import datetime, timezone

from export.markdown_exporter import export_articles_markdown


def test_export_articles_markdown_matches_sample_header_and_fields():
    rows = [
        {
            "title": "教育评价悖论：表征、成因与化解",
            "author": "刘振天 周子怡",
            "publish_time": datetime(2025, 4, 11, 9, 0, tzinfo=timezone.utc),
            "read_count": 3269,
            "like_count": 32,
            "favorite_count": 12,
            "watching_count": 6,
            "reward_count": 0,
            "share_count": 355,
            "subscriber_count": None,
            "comment_count": 0,
            "copyright_text": "11",
            "position_in_batch": 1,
            "digest": "刘振天（厦门大学 高等教育发展研究中心）",
            "raw_html_path": "59194e8815c58b81b0b198bd4171965b.html",
            "source_url": "https://weixin.sogou.com/link",
            "canonical_url": "https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=abc",
        }
    ]

    markdown = export_articles_markdown("北京大学教育评论", rows)

    assert markdown.startswith("# 北京大学教育评论\n\n共 1 条记录")
    assert "| 标题 | 作者 | 发文时间 | 阅读量 | 点赞 | 收藏 | 在看 | 赞赏 | 转发 | 评论 | 版权 | 位置 | 摘要 | 原文文件 | 链接 | 原文链接 |" in markdown
    assert "订阅数" not in markdown
    assert "发文地区" not in markdown
    assert "\"教育评价悖论：表征、成因与化解\"" in markdown
    assert "| 3269 | 32 | 12 | 6 | 0 | 355 | 0 |" in markdown
