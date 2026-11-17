from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os

import requests

from crawl.errors import SessionUnavailable


@dataclass(frozen=True)
class BackendArticleItem:
    title: str
    author: str
    digest: str
    cover_url: str
    link: str
    mid: str
    idx: int
    publish_time: datetime | None


def parse_publish_page(publish_page: str) -> list[BackendArticleItem]:
    page = json.loads(publish_page) if isinstance(publish_page, str) else publish_page
    items: list[BackendArticleItem] = []
    for publish in page.get("publish_list", []):
        info_raw = publish.get("publish_info")
        if not info_raw:
            continue
        info = json.loads(info_raw) if isinstance(info_raw, str) else info_raw
        sent_time = (info.get("sent_info") or {}).get("time")
        publish_time = datetime.fromtimestamp(sent_time, tz=timezone.utc) if sent_time else None
        for appmsg in info.get("appmsgex", []):
            mid = str(appmsg.get("appmsgid") or "")
            idx = int(appmsg.get("itemidx") or 1)
            items.append(BackendArticleItem(
                title=appmsg.get("title", ""),
                author=appmsg.get("author_name", ""),
                digest=appmsg.get("digest", ""),
                cover_url=appmsg.get("cover", ""),
                link=appmsg.get("link", ""),
                mid=mid,
                idx=idx,
                publish_time=publish_time,
            ))
    return items


class WechatBackendClient:
    def __init__(
        self,
        cookie: str,
        token: str,
        fakeid: str,
        fingerprint: str = "",
        user_agent: str = "Mozilla/5.0",
        referer: str = "https://mp.weixin.qq.com/",
        session=None,
    ):
        self.cookie = cookie
        self.token = token
        self.fakeid = fakeid
        self.fingerprint = fingerprint
        self.user_agent = user_agent
        self.referer = referer
        self.session = session or requests.Session()

    @classmethod
    def from_session_credential(cls, credential, fakeid: str, *, session=None) -> "WechatBackendClient":
        return cls(
            cookie=credential.cookie,
            token=credential.backend_token,
            fakeid=fakeid,
            fingerprint=credential.fingerprint,
            user_agent=credential.user_agent,
            session=session,
        )

    @classmethod
    def from_env(cls) -> "WechatBackendClient":
        return cls(
            cookie=os.environ["WECHAT_COOKIE"],
            token=os.environ["WECHAT_MP_TOKEN"],
            fakeid=os.environ["WECHAT_FAKEID"],
            fingerprint=os.getenv("WECHAT_FINGERPRINT", ""),
            user_agent=os.getenv("WECHAT_USER_AGENT", "Mozilla/5.0"),
            referer=os.getenv("WECHAT_REFERER", "https://mp.weixin.qq.com/"),
        )

    def fetch_publish_items(self, begin: int = 0, count: int = 5) -> list[BackendArticleItem]:
        return list(self.fetch_publish_page(begin, count)[0])

    def fetch_publish_page(self, begin: int = 0, count: int = 5) -> tuple[list[BackendArticleItem], int]:
        response = self.session.get(
            "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
            params={
                "sub": "list",
                "search_field": "null",
                "begin": str(begin),
                "count": str(count),
                "query": "",
                "fakeid": self.fakeid,
                "type": "101_1",
                "free_publish_type": "1",
                "sub_action": "list_ex",
                "fingerprint": self.fingerprint,
                "token": self.token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            },
            headers={
                "Cookie": self.cookie,
                "User-Agent": self.user_agent,
                "Referer": self.referer,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise SessionUnavailable("invalid_backend_response") from None
        base_resp = payload.get("base_resp", {})
        if base_resp.get("ret") != 0:
            raise SessionUnavailable("backend_rejected")
        publish_page = payload.get("publish_page", {})
        page = json.loads(publish_page) if isinstance(publish_page, str) else publish_page
        return parse_publish_page(page), int(page.get("total_count") or payload.get("total_count") or 0)
