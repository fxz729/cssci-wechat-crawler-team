from datetime import datetime, timezone

import pytest

from parsers.article_parser import ParsedArticle, parse_article_html


HTML = """
<html>
<head>
<meta name="author" content="北京大学教育评论" />
<meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg" />
<meta name="description" content="刘振天（厦门大学 高等教育发展研究中心）" />
<script>var ct = '1744333200';</script>
</head>
<body>
<h1 id="activity-name">教育评价悖论：表征、成因与化解</h1>
<a id="js_name">北京大学教育评论</a>
<span id="publish_time">2025年04月11日</span>
<div id="js_content">
<p>这是正文第一段。</p>
<img data-src="https://mmbiz.qpic.cn/image1.jpg" />
<img src="https://mmbiz.qlogo.cn/image2.jpg" />
<img src="https://example.com/not-wechat.jpg" />
</div>
</body>
</html>
"""


def test_parse_article_html_extracts_core_fields_and_image_urls():
    parsed = parse_article_html(HTML, url="https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=abc")

    assert parsed.title == "教育评价悖论：表征、成因与化解"
    assert parsed.account_name == "北京大学教育评论"
    assert parsed.author == "北京大学教育评论"
    assert parsed.publish_time.year == 2025
    assert parsed.digest == "刘振天（厦门大学 高等教育发展研究中心）"
    assert parsed.content_text == "这是正文第一段。"
    assert parsed.cover_image_url == "https://mmbiz.qpic.cn/cover.jpg"
    assert parsed.image_urls == ["https://mmbiz.qpic.cn/image1.jpg"]
    assert parsed.image_count == 1


def test_parse_article_html_returns_none_when_content_missing():
    parsed = parse_article_html("<html><h1 id='activity-name'>无正文</h1></html>", url="")

    assert parsed is None


NEW_FIELDS_HTML = """
<html><head></head><body>
<div id="js_content"><p>Hello world foo</p></div>
<script>var req_id = 'abc123';</script>
<script>var comment_id = 'cmt456';</script>
</body></html>
"""


def test_req_id_extracted():
    parsed = parse_article_html(NEW_FIELDS_HTML)
    assert parsed.req_id == "abc123"


def test_comment_id_extracted():
    parsed = parse_article_html(NEW_FIELDS_HTML)
    assert parsed.comment_id == "cmt456"


def test_word_count_excludes_whitespace():
    parsed = parse_article_html(NEW_FIELDS_HTML)
    # content_text = "Hello world foo" → non-whitespace = 13 chars
    assert parsed.word_count == len("Helloworldfoo")

def test_new_fields_keep_legacy_constructor_and_independent_defaults():
    first = ParsedArticle("题", "号", "", None, "", "<div/>", "", "", [], 0, "", "", 0, False, 0, 0)
    second = ParsedArticle("题", "号", "", None, "", "<div/>", "", "", [], 0, "", "", 0, False, 0, 0)

    assert first.media_assets == []
    assert first.field_provenance == {}
    first.media_assets.append({"kind": "image", "url": "https://mmbiz.qpic.cn/a.jpg"})
    first.field_provenance["author"] = "missing"
    assert second.media_assets == []
    assert second.field_provenance == {}


@pytest.mark.parametrize(
    "byline, source",
    [
        ('<span id="js_author_name">真实署名</span>', "#js_author_name"),
        ('<span id="js_author">真实署名</span>', "#js_author"),
        ('<div id="meta_content"><span class="rich_media_meta rich_media_meta_text">真实署名</span></div>',
         "#meta_content .rich_media_meta_text"),
    ],
)
def test_actual_byline_precedes_account_name_and_author_meta(byline, source):
    html = f'<meta name="author" content="期刊公众号">{byline}<a id="js_name">期刊公众号</a><div id="js_content">正文</div>'
    parsed = parse_article_html(html)

    assert parsed.account_name == "期刊公众号"
    assert parsed.author == "真实署名"
    assert parsed.field_provenance["author"] == source


def test_author_meta_is_used_when_no_visible_byline():
    parsed = parse_article_html('<meta name="author" content="元数据作者"><a id="js_name">期刊公众号</a><div id="js_content">正文</div>')

    assert parsed.author == "元数据作者"
    assert parsed.account_name == "期刊公众号"
    assert parsed.field_provenance["author"] == 'meta[name="author"]'


@pytest.mark.parametrize("account_id", ["js_name", "js_wx_follow_nickname"])
def test_missing_author_is_not_fabricated_from_account_or_other_metadata(account_id):
    html = f'''<div id="meta_content">
      <span class="rich_media_meta rich_media_meta_text original_area" id="js_original">原创</span>
      <span class="rich_media_meta rich_media_meta_text rich_media_meta_nickname"><a id="{account_id}">期刊公众号</a></span>
      <em class="rich_media_meta_text" id="publish_time">2026年09月06日</em>
      <span class="rich_media_meta_text" id="js_ip_wording">山东</span>
    </div><div id="js_content">正文</div>'''
    parsed = parse_article_html(html)

    assert parsed.account_name == "期刊公众号"
    assert parsed.author == ""
    assert parsed.field_provenance["author"] == "missing"


@pytest.mark.parametrize("metadata", ["", '<meta name="description" content="   ">'])
def test_missing_digest_is_not_fabricated_from_body(metadata):
    parsed = parse_article_html(f"{metadata}<div id='js_content'>{'正文' * 150}</div>")

    assert parsed.digest == ""
    assert parsed.field_provenance["digest"] == "missing"


@pytest.mark.parametrize(
    "metadata, expected, source",
    [
        ('<meta name="description" content="原始摘要"><meta property="og:description" content="备用摘要">',
         "原始摘要", 'meta[name="description"]'),
        ('<meta property="og:description" content="原始分享摘要">', "原始分享摘要", 'meta[property="og:description"]'),
    ],
)
def test_digest_preserves_original_metadata(metadata, expected, source):
    parsed = parse_article_html(f"{metadata}<div id='js_content'>不是摘要的正文</div>")

    assert parsed.digest == expected
    assert parsed.field_provenance["digest"] == source


@pytest.mark.parametrize(
    "markup, asset",
    [
        ('<img data-src="https://mmbiz.qpic.cn/paper.jpg">', {"kind": "image", "url": "https://mmbiz.qpic.cn/paper.jpg"}),
        ('<img data-src="https://mmbiz.qpic.cn/diagram?wx_fmt=gif">', {"kind": "image", "url": "https://mmbiz.qpic.cn/diagram?wx_fmt=gif"}),
        ('<img src="https://mmbiz.qpic.cn/diagram.gif">', {"kind": "image", "url": "https://mmbiz.qpic.cn/diagram.gif"}),
        ('<mpvoice voice_encode_fileid="voice-123"></mpvoice>', {"kind": "audio", "id": "voice-123"}),
        ('<mp-voice data-src="https://res.wx.qq.com/speech.mp3"></mp-voice>', {"kind": "audio", "url": "https://res.wx.qq.com/speech.mp3"}),
        ('<audio controls><source src="https://res.wx.qq.com/speech.mp3"></audio>', {"kind": "audio", "url": "https://res.wx.qq.com/speech.mp3"}),
        ('<video src="https://res.wx.qq.com/lecture.mp4"></video>', {"kind": "video", "url": "https://res.wx.qq.com/lecture.mp4"}),
        ('<mpvideo vid="wxv_123"></mpvideo>', {"kind": "video", "id": "wxv_123"}),
        ('<mp-video data-vid="wxv_456"></mp-video>', {"kind": "video", "id": "wxv_456"}),
        ('<iframe class="video_iframe" data-src="https://v.qq.com/iframe/player.html?vid=video123"></iframe>',
         {"kind": "video", "url": "https://v.qq.com/iframe/player.html?vid=video123", "id": "video123"}),
        ('<mp-common-videosnap data-type="video" data-id="export/abc123" data-url="https://findermp.video.qq.com/251/20350/stodownload?encfilekey=demo"></mp-common-videosnap>',
         {"kind": "video", "url": "https://findermp.video.qq.com/251/20350/stodownload?encfilekey=demo", "id": "export/abc123"}),
    ],
)
def test_media_only_articles_are_valid_and_expose_media_assets(markup, asset):
    parsed = parse_article_html(f'<h1 id="activity-name">媒体文章</h1><div id="js_content">{markup}</div>')

    assert parsed is not None
    assert parsed.content_text == ""
    assert parsed.word_count == 0
    assert parsed.digest == ""
    assert parsed.media_assets == [asset]
    assert parsed.image_count == int(asset["kind"] == "image")
    assert parsed.audio_count == int(asset["kind"] == "audio")
    assert parsed.video_count == int(asset["kind"] == "video")
    assert parsed.field_provenance["media_assets"] == "#js_content"


def test_media_urls_are_resolved_and_duplicate_wrappers_are_not_counted_twice():
    html = '''<div id="js_content">正文
      <img data-src="//mmbiz.qpic.cn/paper.jpg"><img data-src="//mmbiz.qpic.cn/paper.jpg">
      <div class="voice_card_v2"><mpvoice voice_encode_fileid="voice-1" data-src="/speech.mp3"></mpvoice></div>
      <div class="video_iframe_wrp"><video src="https://res.wx.qq.com/lecture.mp4"></video></div>
    </div>'''
    parsed = parse_article_html(html, url="https://mp.weixin.qq.com/s?__biz=test")

    assert parsed.media_assets == [
        {"kind": "image", "url": "https://mmbiz.qpic.cn/paper.jpg"},
        {"kind": "audio", "url": "https://mp.weixin.qq.com/speech.mp3", "id": "voice-1"},
        {"kind": "video", "url": "https://res.wx.qq.com/lecture.mp4"},
    ]
    assert parsed.image_urls == ["https://mmbiz.qpic.cn/paper.jpg"]
    assert parsed.audio_count == 1
    assert parsed.video_count == 1


def test_recognized_media_without_resource_address_retains_kind_only():
    parsed = parse_article_html('<div class="rich_media_content"><mpvideo></mpvideo></div>')

    assert parsed is not None
    assert parsed.media_assets == [{"kind": "video"}]
    assert parsed.field_provenance["media_assets"] == ".rich_media_content"


@pytest.mark.parametrize(
    "content",
    ["", "   ", '<img class="emoji" src="https://mmbiz.qpic.cn/emoji.gif">',
     '<div class="video_iframe_wrp"></div>', '<img src="javascript:alert(1)">',
     '<img src="https://mmbiz.qpic.cn.example.org/paper.jpg">'],
)
def test_empty_or_decorative_content_is_not_a_media_article(content):
    assert parse_article_html(f'<div id="js_content">{content}</div>') is None


@pytest.mark.parametrize("assignment", ["var ct = '1744333200';", 'const ct = "1744333200";', "let ct = 1744333200;", "window.ct = 1744333200;"])
def test_publication_unix_timestamp_is_timezone_aware_utc(assignment):
    parsed = parse_article_html(f"<script>{assignment}</script><div id='js_content'>正文</div>")

    assert parsed.publish_time == datetime(2025, 4, 11, 1, 0, tzinfo=timezone.utc)
    assert parsed.publish_time.tzinfo == timezone.utc
    assert parsed.field_provenance["publish_time"] == "script:ct"


@pytest.mark.parametrize("label", ["2026年09月06日 10:30", "2026-09-06 10:30", "2026/09/06 10:30", "2026-09-06T10:30:00+08:00"])
def test_publication_display_time_is_parsed_as_shanghai_then_utc(label):
    parsed = parse_article_html(f'<em id="publish_time">{label}</em><div id="js_content">正文</div>')

    assert parsed.publish_time == datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    assert parsed.field_provenance["publish_time"] == "#publish_time"


@pytest.mark.parametrize(
    "metadata, source",
    [
        ('<meta property="article:published_time" content="2026-09-06T02:30:00Z">', 'meta[property="article:published_time"]'),
        ('<meta itemprop="datePublished" content="2026-09-06T10:30:00+08:00">', '[itemprop="datePublished"]'),
        ('<time itemprop="datePublished" datetime="2026-09-06T10:30:00+08:00"></time>', '[itemprop="datePublished"]'),
        ('<time id="publish_time" datetime="2026-09-06T10:30:00+08:00"></time>', "#publish_time@datetime"),
        ('<script>var createTime = "2026-09-06 10:30";</script>', "script:createTime"),
    ],
)
def test_publication_metadata_structures_ignore_modification_metadata(metadata, source):
    html = f'''{metadata}<meta property="article:modified_time" content="2026-09-07T10:30:00+08:00">
      <script>var modify_time = '1788748200';</script><div id="js_content">正文</div>'''
    parsed = parse_article_html(html)

    assert parsed.publish_time == datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    assert parsed.field_provenance["publish_time"] == source


def test_original_timestamp_precedes_edited_display_date():
    parsed = parse_article_html('''<script>var ct = '1744333200';</script>
      <em id="publish_time">2026-09-06</em><span>已修改</span><div id="js_content">正文</div>''')

    assert parsed.publish_time == datetime(2025, 4, 11, 1, 0, tzinfo=timezone.utc)
    assert parsed.field_provenance["publish_time"] == "script:ct"


@pytest.mark.parametrize(
    "metadata",
    [
        '<em id="publish_time">修改于 2026-09-06 10:30</em>',
        '<em id="publish_time">2026-09-06 10:30 已更新</em>',
        '<time itemprop="dateModified" datetime="2026-09-06T10:30:00+08:00"></time>',
        '<meta property="article:modified_time" content="2026-09-06T10:30:00+08:00"><script>var update_time = 1744333200;</script>',
        '<p>正文示例：var ct = "1744333200";</p>',
    ],
)
def test_update_dates_and_article_text_are_not_publication_dates(metadata):
    parsed = parse_article_html(f"{metadata}<div id='js_content'>正文</div>")

    assert parsed.publish_time is None
    assert parsed.field_provenance["publish_time"] == "missing"


def test_invalid_timestamp_falls_back_to_valid_publication_label():
    parsed = parse_article_html('''<script>var ct = '99999999999999999999999';</script>
      <em id="publish_time">2026年09月06日</em><div id="js_content">正文</div>''')

    assert parsed.publish_time == datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)
    assert parsed.field_provenance["publish_time"] == "#publish_time"
