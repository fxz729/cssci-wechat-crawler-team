from pathlib import Path
import re
from urllib.parse import urlencode

from parsers.article_parser import parse_article_html
from parsers.url_parser import parse_wechat_url


_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_=-]+")


def persist_article_files(data_dir: Path, biz: str, mid: str, idx: int, html: str, text: str) -> tuple[str, str]:
    if not _SAFE_IDENTIFIER.fullmatch(biz) or not _SAFE_IDENTIFIER.fullmatch(mid):
        raise ValueError("unsafe article identifier")
    html_dir = data_dir / "raw_html" / biz
    text_dir = data_dir / "raw_text" / biz
    html_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / f"{mid}_{idx}.html"
    text_path = text_dir / f"{mid}_{idx}.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    return str(html_path), str(text_path)


def parse_fetched_article(html: str, final_url: str):
    article = parse_article_html(html, final_url)
    url_parts = parse_wechat_url(final_url)
    if not url_parts.biz or not url_parts.mid or url_parts.idx is None:
        fallback_url = _canonical_url_from_html(html)
        if fallback_url:
            url_parts = parse_wechat_url(fallback_url)
    return article, url_parts


def _canonical_url_from_html(html: str) -> str | None:
    biz = _extract_js_value(html, "biz") or _extract_js_value(html, "__biz")
    mid = _extract_js_value(html, "mid") or _extract_js_value(html, "appmsgid")
    idx = _extract_js_value(html, "idx") or "1"
    sn = _extract_js_value(html, "sn")
    if not biz or not mid:
        return None
    params = {"__biz": biz, "mid": mid, "idx": idx}
    if sn:
        params["sn"] = sn
    return "https://mp.weixin.qq.com/s?" + urlencode(params)


def _extract_js_value(html: str, name: str) -> str | None:
    patterns = [
        rf"var\s+{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]",
        rf"{re.escape(name)}\s*:\s*['\"]([^'\"]+)['\"]",
        rf"['\"]{re.escape(name)}['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None
