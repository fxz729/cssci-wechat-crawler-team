from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    account_name: str
    author: str
    publish_time: datetime | None
    digest: str
    content_html: str
    content_text: str
    cover_image_url: str
    image_urls: list[str]
    image_count: int
    req_id: str
    comment_id: str
    word_count: int
    is_original: bool
    audio_count: int
    video_count: int
    media_assets: list[dict] = field(default_factory=list)
    field_provenance: dict = field(default_factory=dict)


def parse_article_html(html: str, url: str = "") -> ParsedArticle | None:
    soup = BeautifulSoup(html, "lxml")
    content_element = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if content_element is None:
        return None

    content_html = str(content_element)
    content_text = content_element.get_text(separator="\n", strip=True)
    image_urls = _extract_image_urls(content_element, url)
    media_assets = _extract_media_assets(content_element, image_urls, url)
    if not content_text and not media_assets:
        return None

    author, author_source = _extract_author(soup)
    digest = _meta(soup, "description")
    digest_source = 'meta[name="description"]' if digest else "missing"
    if not digest:
        digest = _meta_property(soup, "og:description")
        if digest:
            digest_source = 'meta[property="og:description"]'
    publish_time, time_source = _extract_publish_time(soup)
    content_source = "#js_content" if content_element.get("id") == "js_content" else ".rich_media_content"

    m = re.search(r"var\s+req_id\s*=\s*['\"]([^'\"]+)['\"]", html)
    req_id = m.group(1) if m else ""

    m = re.search(r"comment_id\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    comment_id = m.group(1) if m else ""

    return ParsedArticle(
        title=_text(soup, "#activity-name") or _text(soup, ".rich_media_title") or (soup.title.get_text(strip=True) if soup.title else ""),
        account_name=_text(soup, "#js_name") or _text(soup, "#js_wx_follow_nickname"),
        author=author,
        publish_time=publish_time,
        digest=digest,
        content_html=content_html,
        content_text=content_text,
        cover_image_url=_meta_property(soup, "og:image") or (image_urls[0] if image_urls else ""),
        image_urls=image_urls,
        image_count=len(image_urls),
        req_id=req_id,
        comment_id=comment_id,
        word_count=len(re.sub(r'\s', '', content_text)),
        is_original=bool(soup.select_one('#js_original, .original_area, .rich_media_meta_primary')),
        audio_count=sum(asset["kind"] == "audio" for asset in media_assets),
        video_count=sum(asset["kind"] == "video" for asset in media_assets),
        media_assets=media_assets,
        field_provenance={
            "author": author_source,
            "digest": digest_source,
            "publish_time": time_source,
            "media_assets": content_source if media_assets else "missing",
        },
    )


def _text(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    return element.get_text(strip=True) if element else ""


def _meta(soup: BeautifulSoup, name: str) -> str:
    element = soup.select_one(f"meta[name='{name}']")
    return element.get("content", "").strip() if element else ""


def _meta_property(soup: BeautifulSoup, property_name: str) -> str:
    element = soup.select_one(f"meta[property='{property_name}']")
    return element.get("content", "").strip() if element else ""


def _extract_author(soup: BeautifulSoup) -> tuple[str, str]:
    for selector in ("#js_author_name", "#js_author"):
        value = _text(soup, selector)
        if value:
            return value, selector
    selector = "#meta_content .rich_media_meta_text"
    for element in soup.select(selector):
        # This class is shared by the byline, account, date, originality and IP labels.
        if element.get("id") or set(element.get("class", [])) & {"rich_media_meta_nickname", "original_area"}:
            continue
        if element.select_one("#js_name, #js_wx_follow_nickname, #publish_time, #js_original"):
            continue
        value = element.get_text(strip=True)
        if value and value not in {"原创", "Original", "已修改"}:
            return value, selector
    value = _meta(soup, "author")
    return value, 'meta[name="author"]' if value else "missing"


def _script_value(soup: BeautifulSoup, name: str) -> str:
    pattern = rf"(?:\b(?:var|let|const)\s+|\bwindow\.){re.escape(name)}\s*=\s*(?:['\"]([^'\"]*)['\"]|(\d+)\b)"
    for script in soup.select("script"):
        match = re.search(pattern, script.get_text())
        if match:
            return match.group(1) or match.group(2) or ""
    return ""


def _extract_publish_time(soup: BeautifulSoup) -> tuple[datetime | None, str]:
    # ct is the original publication instant; an edited display date must not replace it.
    timestamp = _script_value(soup, "ct")
    if timestamp.isdigit():
        try:
            if int(timestamp) > 0:
                return datetime.fromtimestamp(int(timestamp), tz=timezone.utc), "script:ct"
        except (ValueError, OverflowError, OSError):
            pass

    for selector in ('meta[property="article:published_time"]', '[itemprop="datePublished"]'):
        element = soup.select_one(selector)
        if element is not None:
            value = element.get("content") or element.get("datetime") or element.get_text(strip=True)
            parsed = _parse_time(value)
            if parsed:
                return parsed, selector

    parsed = _parse_time(_script_value(soup, "createTime"))
    if parsed:
        return parsed, "script:createTime"

    element = soup.select_one("#publish_time")
    if element is not None:
        text = element.get_text(strip=True)
        if not re.search(r"修改|更新|edited|updated|modified", text, re.I):
            value = element.get("datetime") or text
            parsed = _parse_time(value)
            if parsed:
                return parsed, "#publish_time@datetime" if element.get("datetime") else "#publish_time"
    return None, "missing"


def _parse_time(value: str) -> datetime | None:
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
        for fmt in ["%Y年%m月%d日", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    # Unzoned WeChat publication labels use Beijing time, never the host's timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc)


def _media_url(value: str, base_url: str) -> str:
    if not value.strip():
        return ""
    try:
        resolved = urljoin(base_url or "https://mp.weixin.qq.com/", value.strip())
        parts = urlsplit(resolved)
        return resolved if parts.scheme in {"http", "https"} and parts.hostname else ""
    except ValueError:
        return ""


def _extract_media_assets(content_element, image_urls: list[str], url: str) -> list[dict]:
    assets = [{"kind": "image", "url": src} for src in image_urls]
    for kind, selector, id_attributes in (
        ("audio", "mpvoice, mp-voice, audio, .voice_card_v2", ("voice_encode_fileid", "data-voice_encode_fileid", "voice_id")),
        ("video", "mpvideo, mp-video, video, .video_iframe_wrp, .wxv_iframe, iframe.video_iframe, mp-common-videosnap", ("vid", "data-vid", "data-mpvid", "data-id")),
    ):
        for element in content_element.select(selector):
            if element.select_one(selector):
                continue  # Read the player, not its outer presentation wrapper.
            source = element.get("data-src") or element.get("src") or element.get("data-url") or ""
            if not source:
                source_element = element.select_one("source[src]")
                if source_element is not None:
                    source = source_element.get("src", "")
            resource_url = _media_url(source, url)
            media_id = next((element.get(attr, "").strip() for attr in id_attributes if element.get(attr)), "")
            if kind == "video" and not media_id and resource_url:
                media_id = (parse_qs(urlsplit(resource_url).query).get("vid") or [""])[0]
            asset = {"kind": kind}
            if resource_url:
                asset["url"] = resource_url
            if media_id:
                asset["id"] = media_id
            if len(asset) == 1 and element.name not in {"mpvoice", "mp-voice", "audio", "mpvideo", "mp-video", "video"}:
                continue
            if asset not in assets:
                assets.append(asset)
    return assets


def _extract_image_urls(content_element, url: str = "") -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for image in content_element.select("img"):
        src = _media_url(image.get("data-src") or image.get("src") or "", url)
        if not src or src in seen:
            continue
        if not _is_content_image(image, src):
            continue
        seen.add(src)
        urls.append(src)
    return urls


def _is_content_image(image, src: str) -> bool:
    if urlsplit(src).hostname not in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}:
        return False
    classes = " ".join(image.get("class", [])) if isinstance(image.get("class"), list) else str(image.get("class", ""))
    attrs_text = " ".join(f"{key}={value}" for key, value in image.attrs.items()).lower()
    # Filter decorative images by evidence, not the GIF format (which may be article content).
    if any(word in (classes + " " + attrs_text).lower() for word in ["emoji", "icon", "avatar", "profile", "qrcode", "qr_code", "logo"]):
        return False
    try:
        ratio = float(image.get("data-ratio", "1"))
    except ValueError:
        ratio = 1.0
    if ratio < 0.2:
        return False
    return True
