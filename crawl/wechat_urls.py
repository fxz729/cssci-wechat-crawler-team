from urllib.parse import urlparse


def is_official_article_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "mp.weixin.qq.com"
        and (parsed.path == "/s" or parsed.path.startswith("/s/"))
    )
