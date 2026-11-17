from dataclasses import dataclass
from datetime import datetime
from html import unescape
from json import loads
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ProfileArticleItem:
    biz: str
    mid: str
    idx: int
    sn: str
    title: str
    digest: str
    cover_url: str
    publish_time: datetime | None
    copyright_stat: int | None  # 100=original, 101=reprint
    audio_fileid: int
    duration: int   # seconds
    is_multi: bool  # is this part of a multi-article batch
    content_url: str


def _parse_appmsg(appmsg: dict, pub_time: datetime | None, is_multi: bool) -> ProfileArticleItem:
    raw_url = unescape(appmsg.get("content_url", "")).replace("amp;", "")
    url = raw_url.split("#wechat_redirect")[0]
    qs = parse_qs(urlparse(url).query)
    return ProfileArticleItem(
        biz=qs.get("__biz", [""])[0],
        mid=qs.get("mid", [""])[0],
        idx=int(qs.get("idx", [0])[0]),
        sn=qs.get("sn", [""])[0],
        title=unescape(appmsg.get("title", "")),
        digest=unescape(appmsg.get("digest", "")),
        cover_url=unescape(appmsg.get("cover", "")),
        publish_time=pub_time,
        copyright_stat=appmsg.get("copyright_stat"),
        audio_fileid=appmsg.get("audio_fileid", 0),
        duration=appmsg.get("duration", 0),
        is_multi=is_multi,
        content_url=url,
    )


def fetch_article_page(session, creds, offset: int) -> list[ProfileArticleItem]:
    """Fetch one page of profile_ext?action=getmsg. Returns [] when no more articles."""
    url = (
        f"https://mp.weixin.qq.com/mp/profile_ext?action=getmsg"
        f"&__biz={creds.biz}&f=json&offset={offset}&count=10&is_ok=1&scene=124"
        f"&uin={creds.uin}&key={creds.key}&pass_ticket={creds.pass_ticket}"
        f"&wxtoken=&appmsg_token={creds.appmsg_token}&x5=0"
    )
    resp = session.get(url, headers={
        "Cookie": creds.cookie,
        "User-Agent": creds.user_agent,
        "Referer": "https://mp.weixin.qq.com/",
        "X-Requested-With": "XMLHttpRequest",
    }, verify=False)
    resp.raise_for_status()
    data = resp.json()
    ret = data.get("ret", 0)
    if ret != 0:
        raise RuntimeError(f"WeChat API error ret={ret} errmsg={data.get('errmsg', '')!r}")

    raw_list = data.get("general_msg_list", "")
    if not raw_list:
        return []
    msg_list = loads(raw_list).get("list", [])
    if not msg_list:
        return []

    items: list[ProfileArticleItem] = []
    for msg in msg_list:
        ts = msg.get("comm_msg_info", {}).get("datetime")
        pub_time = datetime.fromtimestamp(ts) if ts else None
        ext = msg.get("app_msg_ext_info")
        if not ext:
            continue
        items.append(_parse_appmsg(ext, pub_time, is_multi=False))
        for sub in ext.get("multi_app_msg_item_list", []):
            items.append(_parse_appmsg(sub, pub_time, is_multi=True))
    return items
