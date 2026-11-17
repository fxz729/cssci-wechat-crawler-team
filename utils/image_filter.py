from urllib.parse import urlparse, parse_qs


def clean_image_urls(urls: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not _looks_like_content_image(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def _looks_like_content_image(url: str) -> bool:
    lowered = (url or "").lower()
    if not lowered:
        return False
    if "mmbiz.qpic.cn" not in lowered and "mmbiz.qlogo.cn" not in lowered:
        return False
    if lowered.endswith(".gif") or "wx_fmt=gif" in lowered or ".gif?" in lowered:
        return False
    if any(token in lowered for token in ["emoji", "icon", "avatar", "qrcode", "qr_code", "logo"]):
        return False
    qs = parse_qs(urlparse(lowered).query)
    wx_fmt = (qs.get("wx_fmt") or [""])[0]
    if wx_fmt == "gif":
        return False
    return True
