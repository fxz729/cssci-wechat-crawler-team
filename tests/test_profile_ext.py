from datetime import datetime
from html import escape

from metrics.profile_ext import _parse_appmsg


def test_profile_ext_item_preserves_full_content_url():
    content_url = (
        "https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=abc"
        "&chksm=def&key=secret-key&exportkey=secret-export&pass_ticket=secret-ticket"
        "#wechat_redirect"
    )
    item = _parse_appmsg(
        {
            "content_url": escape(content_url),
            "title": "标题",
            "digest": "摘要",
            "cover": "cover",
            "copyright_stat": 100,
        },
        datetime(2026, 6, 18, 15, 1),
        is_multi=False,
    )

    assert item.content_url == content_url.split("#wechat_redirect")[0]
