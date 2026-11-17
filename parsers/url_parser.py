from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse


@dataclass(frozen=True)
class WechatUrlParts:
    biz: str | None
    mid: str | None
    idx: int | None
    sn: str | None
    batch_key: str | None
    position_in_batch: int | None
    canonical_url: str


def parse_wechat_url(url: str) -> WechatUrlParts:
    normalized = unescape(url.strip())
    parsed = urlparse(normalized)
    query = parse_qs(parsed.query, keep_blank_values=True)

    biz = _first(query, "__biz")
    mid = _first(query, "mid")
    idx_value = _first(query, "idx")
    sn = _first(query, "sn")
    idx = int(idx_value) if idx_value and idx_value.isdigit() else None
    batch_key = f"{biz}:{mid}" if biz and mid else None

    canonical_params = {}
    for key in ["__biz", "mid", "idx", "sn"]:
        value = _first(query, key)
        if value is not None:
            canonical_params[key] = value
    canonical_url = "https://mp.weixin.qq.com/s"
    if canonical_params:
        canonical_url = f"{canonical_url}?{urlencode(canonical_params)}"

    return WechatUrlParts(
        biz=biz,
        mid=mid,
        idx=idx,
        sn=sn,
        batch_key=batch_key,
        position_in_batch=idx,
        canonical_url=canonical_url,
    )


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]
