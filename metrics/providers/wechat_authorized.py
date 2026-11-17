import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

from crawl.errors import SessionUnavailable
from metrics.providers.base import MetricsSnapshot


class WechatAuthorizedProvider:
    provider = "wechat-authorized"
    provider_priority = 100

    def __init__(
        self,
        cookie: str | None,
        appmsg_token: str | None = None,
        mp_token: str | None = None,
        fakeid: str | None = None,
        fingerprint: str | None = None,
        pass_ticket: str | None = None,
        uin: str | None = None,
        key: str | None = None,
        user_agent: str | None = None,
        referer: str | None = None,
        force_article_page_session: bool = False,
        session=None,
    ):
        self.cookie = cookie
        self.appmsg_token = appmsg_token
        self.mp_token = mp_token
        self.fakeid = fakeid
        self.fingerprint = fingerprint
        self.pass_ticket = pass_ticket
        self.uin = uin
        self.key = key
        self.user_agent = user_agent or "Mozilla/5.0"
        self.referer = referer or "https://mp.weixin.qq.com/"
        self.force_article_page_session = force_article_page_session
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "WechatAuthorizedProvider":
        return cls(
            cookie=os.getenv("WECHAT_COOKIE"),
            appmsg_token=os.getenv("WECHAT_APPMSG_TOKEN"),
            mp_token=os.getenv("WECHAT_MP_TOKEN"),
            fakeid=os.getenv("WECHAT_FAKEID"),
            fingerprint=os.getenv("WECHAT_FINGERPRINT"),
            pass_ticket=_url_decode_env("WECHAT_PASS_TICKET"),
            uin=_url_decode_env("WECHAT_UIN"),
            key=_url_decode_env("WECHAT_KEY"),
            user_agent=os.getenv("WECHAT_USER_AGENT"),
            referer=os.getenv("WECHAT_REFERER"),
            force_article_page_session=os.getenv("WECHAT_ARTICLE_PAGE_SESSION") == "1",
        )

    @property
    def has_article_token(self) -> bool:
        return bool(self.cookie and self.appmsg_token)

    @property
    def has_mp_backend_token(self) -> bool:
        return bool(self.cookie and self.mp_token and self.fakeid)

    @property
    def has_article_page_session(self) -> bool:
        return bool(self.cookie and self.force_article_page_session)

    @property
    def is_configured(self) -> bool:
        return self.has_article_token or self.has_article_page_session or self.has_mp_backend_token

    @property
    def status_message(self) -> str:
        if self.has_article_token:
            return "授权文章指标配置已存在"
        if self.has_article_page_session:
            return "文章页会话指标配置已存在"
        if self.has_mp_backend_token:
            return "公众号后台授权配置已存在"
        missing = []
        if not self.cookie:
            missing.append("WECHAT_COOKIE")
        if not self.appmsg_token and not self.mp_token and not self.force_article_page_session:
            missing.append("WECHAT_APPMSG_TOKEN、WECHAT_MP_TOKEN 或 WECHAT_ARTICLE_PAGE_SESSION=1")
        if self.mp_token and not self.fakeid:
            missing.append("WECHAT_FAKEID")
        return "缺少环境变量：" + ", ".join(missing)

    @property
    def redacted_config(self) -> str:
        if not self.is_configured:
            return "<not-configured>"
        parts = ["WECHAT_COOKIE=<redacted>"]
        if self.appmsg_token:
            parts.append("WECHAT_APPMSG_TOKEN=<redacted>")
        if self.force_article_page_session:
            parts.append("WECHAT_ARTICLE_PAGE_SESSION=1")
        if self.mp_token:
            parts.append("WECHAT_MP_TOKEN=<redacted>")
        if self.fakeid:
            parts.append("WECHAT_FAKEID=<redacted>")
        if self.fingerprint:
            parts.append("WECHAT_FINGERPRINT=<redacted>")
        if self.pass_ticket:
            parts.append("WECHAT_PASS_TICKET=<redacted>")
        if self.uin:
            parts.append("WECHAT_UIN=<redacted>")
        if self.key:
            parts.append("WECHAT_KEY=<redacted>")
        return "; ".join(parts)

    def fetch(self, article) -> MetricsSnapshot:
        if self.has_article_token or self.has_article_page_session:
            return self.fetch_article_metrics(article)
        snapshots = self.fetch_for_articles([article])
        return snapshots.get((str(article.mid), int(article.idx)), MetricsSnapshot(
            provider=self.provider,
            provider_priority=self.provider_priority,
            is_authorized_source=True,
        ))

    def fetch_for_articles(self, articles) -> dict[tuple[str, int], MetricsSnapshot]:
        if self.has_article_token or self.has_article_page_session:
            snapshots: dict[tuple[str, int], MetricsSnapshot] = {}
            for article in articles:
                if not article:
                    continue
                snapshots[(str(article.mid), int(article.idx))] = self.fetch_article_metrics(article)
            return snapshots
        dated_articles = [article for article in articles if article and article.publish_time]
        if not dated_articles:
            return {}
        windows = _date_windows(
            min(article.publish_time for article in dated_articles),
            max(article.publish_time for article in dated_articles),
        )
        snapshots: dict[tuple[str, int], MetricsSnapshot] = {}
        for begin, end in windows:
            snapshots.update(self.fetch_for_range(begin, end))
        return snapshots

    def fetch_article_metrics(self, article) -> MetricsSnapshot:
        if not (self.has_article_token or self.has_article_page_session):
            raise RuntimeError("文章页指标回填需要 WECHAT_COOKIE 与 WECHAT_APPMSG_TOKEN，或显式启用 WECHAT_ARTICLE_PAGE_SESSION=1")
        params = _article_params(article)
        try:
            article_token = self.appmsg_token or ""
            article_cookie = self.cookie or ""
            article_referer = getattr(article, "canonical_url", "") or self.referer
            page_response = self.session.get(
                "https://mp.weixin.qq.com/s",
                params={
                    "__biz": params["biz"],
                    "mid": params["mid"],
                    "idx": params["idx"],
                    "sn": params.get("sn", ""),
                    "uin": "777",
                    "key": "777",
                    "pass_ticket": "",
                    "devicetype": "UnifiedPCWindows",
                    "version": "f2541b36",
                    "lang": "zh_CN",
                },
                headers={
                    "Cookie": article_cookie,
                    "User-Agent": self.user_agent,
                    "Referer": self.referer,
                },
                timeout=30,
            )
            page_response.raise_for_status()
            refreshed_cookies = page_response.cookies.get_dict()
            article_token = refreshed_cookies.get("appmsg_token") or article_token
            article_cookie = _merge_cookie_header(article_cookie, refreshed_cookies)
            article_referer = page_response.url or article_referer
            comment_id = getattr(article, "comment_id", "") or _comment_id_from_article(article)
            ext_response = self.session.post(
                "https://mp.weixin.qq.com/mp/getappmsgext",
                params={
                    "f": "json",
                    "mock": "",
                    "uin": "777",
                    "key": "777",
                    "pass_ticket": "",
                    "wxtoken": "777",
                    "devicetype": "UnifiedPCWindows",
                    "clientversion": "f2541b36",
                    "version": "f2541b36",
                    "user_article_role": "0",
                    "fasttmplajax": "1",
                    "__biz": params["biz"],
                    "appmsg_token": article_token,
                    "x5": "0",
                },
                data={
                    "r": "0",
                    "sn": params.get("sn", ""),
                    "mid": params["mid"],
                    "idx": params["idx"],
                    "comment_id": comment_id or "",
                    "appmsg_type": "9",
                    "__biz": params["biz"],
                    "pass_ticket": "",
                    "is_need_ticket": "0",
                    "is_need_ad": "0",
                    "is_need_reward": "0",
                    "is_only_read": "1",
                    "scene": "38",
                    "appmsg_like_type": "2",
                    "devicetype": "UnifiedPCWindows",
                    "clientversion": "f2541b36",
                    "version": "f2541b36",
                    "user_article_role": "0",
                    "req_id": getattr(article, "req_id", "") or "",
                    "title": getattr(article, "title", "") or "",
                    "abtest_cookie": "",
                    "both_ad": "0",
                    "reward_uin_count": "0",
                    "send_time": "",
                    "msg_daily_idx": "1",
                    "is_original": "0",
                    "is_temp_url": "0",
                    "item_show_type": "0",
                    "tmp_version": "1",
                    "more_read_type": "0",
                    "related_video_sn": "",
                    "related_video_num": "5",
                    "vid": "",
                    "is_pay_subscribe": "0",
                    "pay_subscribe_uin_count": "0",
                    "has_red_packet_cover": "0",
                    "album_id": "undefined",
                    "album_video_num": "5",
                    "cur_album_id": "undefined",
                    "is_public_related_video": "NaN",
                    "encode_info_by_base64": "undefined",
                    "exptype": "",
                    "export_key_extinfo": "",
                    "business_type": "0",
                },
                headers={
                    "Cookie": article_cookie,
                    "User-Agent": self.user_agent,
                    "Referer": article_referer,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30,
            )
            ext_response.raise_for_status()
            ext_payload = ext_response.json()
            comment_payload = {}
            if comment_id:
                comment_response = self.session.get(
                    "https://mp.weixin.qq.com/mp/appmsg_comment",
                    params={
                        "action": "getcomment",
                        "comment_id": comment_id,
                        "__biz": params["biz"],
                        "appmsgid": params["mid"],
                        "idx": params["idx"],
                        "appmsg_token": article_token,
                        "uin": "777",
                        "key": "777",
                        "pass_ticket": "",
                        "wxtoken": "777",
                        "devicetype": "UnifiedPCWindows",
                        "clientversion": "",
                        "f": "json",
                        "offset": "0",
                        "limit": "0",
                    },
                    headers={
                        "Cookie": article_cookie,
                        "User-Agent": self.user_agent,
                        "Referer": article_referer,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=30,
                )
                comment_response.raise_for_status()
                comment_payload = comment_response.json()
        except requests.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in {401, 403, 429}:
                raise SessionUnavailable(f"http_{status_code}") from None
            raise RuntimeError("微信文章指标请求失败：HTTPError") from None
        except Exception as exc:
            raise RuntimeError(f"微信文章指标请求失败：{type(exc).__name__}") from None
        ext_ret = (ext_payload.get("base_resp") or {}).get("ret")
        if ext_ret not in (None, 0):
            raise RuntimeError(f"微信文章指标接口返回错误：ret={ext_ret}")
        if "appmsgstat" not in ext_payload:
            raise RuntimeError("微信文章指标接口未返回 appmsgstat，可能是授权凭据失效或会话降级")
        comment_ret = (comment_payload.get("base_resp") or {}).get("ret")
        if comment_payload and comment_ret not in (None, 0):
            comment_payload = {}
        return parse_article_ext_metrics(ext_payload, comment_payload)

    def fetch_for_range(self, begin: datetime, end: datetime) -> dict[tuple[str, int], MetricsSnapshot]:
        if not self.has_mp_backend_token:
            raise RuntimeError("公众号后台指标回填需要 WECHAT_COOKIE、WECHAT_MP_TOKEN 和 WECHAT_FAKEID")
        try:
            response = self.session.get(
                "https://mp.weixin.qq.com/misc/appmsganalysis",
                params={
                    "action": "get_article_list",
                    "begin_timestamp": str(int(begin.timestamp())),
                    "end_timestamp": str(int(end.timestamp())),
                    "article_source": "9999",
                    "offset": "0",
                    "count": "100",
                    "token": self.mp_token,
                    "lang": "zh_CN",
                    "f": "json",
                    "ajax": "1",
                },
                headers={
                    "Cookie": self.cookie or "",
                    "User-Agent": self.user_agent,
                    "Referer": self.referer,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"微信后台指标请求失败：{type(exc).__name__}") from None
        base_resp = payload.get("base_resp") or {}
        if base_resp.get("ret") != 0:
            raise RuntimeError(f"微信后台指标接口返回错误：ret={base_resp.get('ret')}")
        return parse_backend_article_metrics(payload)


def parse_article_ext_metrics(ext_payload: dict, comment_payload: dict | None = None) -> MetricsSnapshot:
    stat = ext_payload.get("appmsgstat") or ext_payload
    comments = comment_payload or {}
    collect_num = _first_int(stat, ["collect_num", "star_num"])
    album_info = ext_payload.get("appmsg_album_extinfo") or {}
    album_id = str(album_info.get("album_id") or album_info.get("album_id_str") or "")
    album_name = album_info.get("album_name") or ""
    comment_count = _first_int(comments, ["total_count", "elected_comment_total_cnt", "comment_count", "comment_num"])
    if comment_count is None:
        comment_count = _first_int(ext_payload, ["comment_count", "comment_num"])
    return MetricsSnapshot(
        provider=WechatAuthorizedProvider.provider,
        provider_priority=WechatAuthorizedProvider.provider_priority,
        read_count=_first_int(stat, ["read_num", "read_count"]),
        like_count=_first_int(stat, ["old_like_num", "like_count"]),
        watching_count=_first_int(stat, ["like_num", "watching_count", "zaikan_num"]),
        share_count=_first_int(stat, ["share_num", "share_count"]),
        favorite_count=collect_num if collect_num is not None else _first_int(
            stat, ["favorite_num", "favorite_count", "fav_count", "collection_num", "collection_count"]
        ),
        comment_count=comment_count,
        collect_num=collect_num,
        raw_payload={
            "appmsgext": {
                **_safe_payload(ext_payload),
                "_collect_num": collect_num,
                "_album_id": album_id,
                "_album_name": album_name,
            },
            "comment": _safe_payload(comments),
        },
        is_authorized_source=True,
    )


def _url_decode_env(name: str) -> str | None:
    value = os.getenv(name)
    return unquote(value) if value else value


def _article_params(article) -> dict[str, str]:
    parsed = urlparse(getattr(article, "canonical_url", "") or "")
    query = parse_qs(parsed.query)
    biz = _first_query(query, "__biz")
    mid = _first_query(query, "mid") or str(getattr(article, "mid", ""))
    idx = _first_query(query, "idx") or str(getattr(article, "idx", ""))
    sn = _first_query(query, "sn") or str(getattr(article, "sn", ""))
    if not biz or not mid or not idx:
        raise RuntimeError("文章 URL 缺少 __biz/mid/idx，无法请求文章指标")
    return {"biz": biz, "mid": mid, "idx": idx, "sn": sn}


def _comment_id_from_article(article) -> str | None:
    raw_html_path = getattr(article, "raw_html_path", "") or ""
    if not raw_html_path:
        return None
    path = Path(raw_html_path)
    if not path.exists():
        return None
    html = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"comment_id\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return match.group(1) if match else None


def _first_query(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _merge_cookie_header(cookie_header: str, updates: dict[str, str]) -> str:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            cookies[name] = value
    cookies.update({name: value for name, value in updates.items() if name and value})
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _safe_payload(payload):
    blocked = {
        "base_resp", "token", "ticket", "pass_ticket", "appmsg_token", "cookie",
        "wxtoken", "wxtokenkey", "wap_sid2", "slave_sid", "data_ticket",
        "openid", "identity_name", "nick_name", "head_img_url", "user_name", "uin",
    }
    if isinstance(payload, dict):
        return {key: _safe_payload(value) for key, value in payload.items() if key not in blocked}
    if isinstance(payload, list):
        return [_safe_payload(value) for value in payload]
    return payload


def parse_backend_article_metrics(payload: dict) -> dict[tuple[str, int], MetricsSnapshot]:
    snapshots: dict[tuple[str, int], MetricsSnapshot] = {}
    for item in payload.get("article_list") or []:
        mid = str(item.get("msg_id") or item.get("msgid") or item.get("appmsgid") or "")
        idx = _as_int(item.get("item_idx") or item.get("idx") or item.get("itemidx"))
        if not mid or idx is None:
            continue
        snapshots[(mid, idx)] = MetricsSnapshot(
            provider=WechatAuthorizedProvider.provider,
            provider_priority=WechatAuthorizedProvider.provider_priority,
            read_count=_first_int(item, ["read_num", "total_read_uv", "pv"]),
            like_count=_first_int(item, ["old_like_num", "like_count"]),
            watching_count=_first_int(item, ["like_num", "zaikan_num", "watching_count"]),
            share_count=_first_int(item, ["share_num", "share_count"]),
            favorite_count=_first_int(item, ["collect_num", "star_num", "favorite_count", "fav_count", "collection_num", "collection_count"]),
            comment_count=_first_int(item, ["comment_count", "comment_num"]),
            raw_payload=_safe_payload(item),
            is_authorized_source=True,
        )
    return snapshots


def _date_windows(begin: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    current = begin
    while current <= end:
        window_end = min(current + timedelta(days=90), end)
        windows.append((current, window_end))
        current = window_end + timedelta(seconds=1)
    return windows


def _first_int(row: dict, keys: list[str]) -> int | None:
    for key in keys:
        value = _as_int(row.get(key))
        if value is not None:
            return value
    return None


def _as_int(value) -> int | None:
    if value in (None, "", "-"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
