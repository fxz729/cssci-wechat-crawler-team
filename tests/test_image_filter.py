from parsers.article_parser import parse_article_html


HTML = """
<html><body>
<h1 id="activity-name">文章</h1>
<a id="js_name">北京大学教育评论</a>
<div id="js_content">
<p>正文</p>
<img data-src="https://mmbiz.qpic.cn/paper.jpg" />
<img data-src="https://mmbiz.qpic.cn/icon.gif" />
<img data-src="https://mmbiz.qpic.cn/emoji.gif" class="emoji" />
<img data-src="https://mmbiz.qpic.cn/ad.jpg" data-ratio="0.1" />
</div>
</body></html>
"""


def test_article_parser_keeps_only_content_images():
    parsed = parse_article_html(HTML)

    assert parsed.image_urls == ["https://mmbiz.qpic.cn/paper.jpg"]
    assert parsed.image_count == 1
