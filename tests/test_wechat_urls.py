from crawl.wechat_urls import is_official_article_url


def test_accepts_official_wechat_article_url():
    assert is_official_article_url("https://mp.weixin.qq.com/s?__biz=target&mid=1&idx=1")
    assert is_official_article_url("https://mp.weixin.qq.com/s/synthetic-short-link")


def test_rejects_non_https_or_non_wechat_article_urls():
    assert not is_official_article_url("http://mp.weixin.qq.com/s?mid=1")
    assert not is_official_article_url("https://example.invalid/s?mid=1")
    assert not is_official_article_url("https://mp.weixin.qq.com/cgi-bin/appmsgpublish")
