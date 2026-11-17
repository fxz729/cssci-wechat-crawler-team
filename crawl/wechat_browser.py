from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from crawl.browser import BrowserSession
from crawl.errors import SessionUnavailable
from crawl.wechat_urls import is_official_article_url
from metrics.providers.wechat_authorized import parse_article_ext_metrics
from metrics.providers.wechat_backend import parse_publish_page


_PAGE_SCRIPT = """
async (input) => {
  if (input.operation === "catalog") {
    const query = new URLSearchParams(input.params);
    const response = await fetch(`/cgi-bin/appmsgpublish?${query}`, {
      headers: {"X-Requested-With": "XMLHttpRequest"},
    });
    if (!response.ok) return {http_status: response.status};
    try {
      return {http_status: response.status, payload: await response.json()};
    } catch (_error) {
      return {http_status: response.status, invalid_payload: true};
    }
  }

  if (input.operation === "article") {
    const response = await fetch(input.url);
    return {http_status: response.status, url: response.url, html: await response.text()};
  }

  if (input.operation === "metrics") {
    const extQuery = new URLSearchParams(input.ext_query);
    const extResponse = await fetch(`/mp/getappmsgext?${extQuery}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new URLSearchParams(input.ext_body),
    });
    if (!extResponse.ok) return {http_status: extResponse.status};
    let extPayload;
    try {
      extPayload = await extResponse.json();
    } catch (_error) {
      return {http_status: extResponse.status, invalid_payload: true};
    }
    let commentPayload = {};
    if (input.comment_query.comment_id) {
      const commentQuery = new URLSearchParams(input.comment_query);
      const commentResponse = await fetch(`/mp/appmsg_comment?${commentQuery}`, {
        headers: {"X-Requested-With": "XMLHttpRequest"},
      });
      if (!commentResponse.ok) return {http_status: commentResponse.status};
      try {
        commentPayload = await commentResponse.json();
      } catch (_error) {
        return {http_status: commentResponse.status, invalid_payload: true};
      }
    }
    return {
      http_status: extResponse.status,
      ext_payload: extPayload,
      comment_payload: commentPayload,
    };
  }

  throw new Error("unsupported operation");
}
"""


class WechatBrowserClient:
    def __init__(
        self,
        credential,
        fakeid: str,
        *,
        headless: bool = True,
        browser_session_factory=BrowserSession,
    ):
        self.credential = credential
        self.fakeid = fakeid
        self.headless = headless
        self.browser_session_factory = browser_session_factory
        self._browser = None
        self._page = None

    def __enter__(self) -> "WechatBrowserClient":
        self._browser = self.browser_session_factory(
            headless=self.headless,
            user_agent=self.credential.user_agent,
        )
        self._browser.__enter__()
        self._browser.context.add_cookies(_playwright_cookies(self.credential.cookie))
        self._page = self._browser.new_page()
        self._page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._browser is not None:
            return self._browser.__exit__(exc_type, exc_value, traceback)
        return None

    def fetch_publish_page(self, begin: int = 0, count: int = 5):
        result = self._evaluate({
            "operation": "catalog",
            "params": {
                "sub": "list",
                "search_field": "null",
                "begin": str(begin),
                "count": str(count),
                "query": "",
                "fakeid": self.fakeid,
                "type": "101_1",
                "free_publish_type": "1",
                "sub_action": "list_ex",
                "fingerprint": self.credential.fingerprint,
                "token": self.credential.backend_token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            },
        })
        _require_http_ok(result, "catalog")
        if result.get("invalid_payload"):
            raise SessionUnavailable("invalid_browser_response")
        payload = result.get("payload") or {}
        ret = (payload.get("base_resp") or {}).get("ret")
        if ret != 0:
            raise SessionUnavailable("backend_rejected")
        publish_page = payload.get("publish_page") or {}
        if isinstance(publish_page, str):
            import json
            publish_page = json.loads(publish_page)
        return parse_publish_page(publish_page), int(
            publish_page.get("total_count") or payload.get("total_count") or 0
        )

    def fetch_article_page(self, item) -> tuple[str, str]:
        if not is_official_article_url(item.link):
            raise RuntimeError("untrusted article URL")
        result = self._evaluate({"operation": "article", "url": item.link})
        _require_http_ok(result, "article")
        final_url = result.get("url") or ""
        if not is_official_article_url(final_url):
            raise RuntimeError("untrusted article redirect")
        return final_url, result.get("html") or ""

    def fetch_metrics(self, article):
        params = _article_params(article)
        article_token = self._current_cookie("appmsg_token")
        result = self._evaluate({
            "operation": "metrics",
            "ext_query": _ext_query(params, article_token),
            "ext_body": _ext_body(article, params),
            "comment_query": _comment_query(article, params, article_token),
        })
        _require_http_ok(result, "metrics")
        if result.get("invalid_payload"):
            raise SessionUnavailable("invalid_browser_response")
        ext_payload = result.get("ext_payload") or {}
        ret = (ext_payload.get("base_resp") or {}).get("ret")
        if ret not in (None, 0):
            raise RuntimeError(f"browser metrics rejected: ret={ret}")
        if "appmsgstat" not in ext_payload:
            raise RuntimeError("browser metrics missing appmsgstat")
        comment_payload = result.get("comment_payload") or {}
        comment_ret = (comment_payload.get("base_resp") or {}).get("ret")
        if comment_payload and comment_ret not in (None, 0):
            raise RuntimeError(f"browser comments rejected: ret={comment_ret}")
        return parse_article_ext_metrics(ext_payload, comment_payload)

    def _current_cookie(self, name: str) -> str:
        if self._browser is None:
            return ""
        matches = [cookie for cookie in self._browser.context.cookies() if cookie.get("name") == name]
        return str(matches[-1].get("value") or "") if matches else ""

    def _evaluate(self, argument):
        if self._page is None:
            raise RuntimeError("browser client is not open")
        try:
            return self._page.evaluate(_PAGE_SCRIPT, argument)
        except Exception as exc:
            raise RuntimeError(f"browser request failed: {type(exc).__name__}") from None


def _playwright_cookies(cookie_header: str) -> list[dict[str, object]]:
    cookies: list[dict[str, object]] = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            cookies.append({
                "name": name,
                "value": value,
                "domain": "mp.weixin.qq.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            })
    return cookies


def _require_http_ok(result: dict, stage: str) -> None:
    status = result.get("http_status")
    if status is not None and int(status) in {401, 403, 429}:
        raise SessionUnavailable(f"http_{status}")
    if status is None or not 200 <= int(status) < 300:
        raise RuntimeError(f"browser {stage} HTTP error")


def _article_params(article) -> dict[str, str]:
    query = parse_qs(urlparse(getattr(article, "canonical_url", "") or "").query)
    biz = (query.get("__biz") or [""])[0]
    mid = (query.get("mid") or [str(getattr(article, "mid", ""))])[0]
    idx = (query.get("idx") or [str(getattr(article, "idx", ""))])[0]
    sn = (query.get("sn") or [str(getattr(article, "sn", ""))])[0]
    if not biz or not mid or not idx:
        raise RuntimeError("article identity is incomplete")
    return {"biz": biz, "mid": mid, "idx": idx, "sn": sn}


def _ext_query(params: dict[str, str], article_token: str) -> dict[str, str]:
    return {
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
    }


def _ext_body(article, params: dict[str, str]) -> dict[str, str]:
    return {
        "r": "0",
        "sn": params["sn"],
        "mid": params["mid"],
        "idx": params["idx"],
        "comment_id": getattr(article, "comment_id", "") or "",
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
    }


def _comment_query(article, params: dict[str, str], article_token: str) -> dict[str, str]:
    return {
        "action": "getcomment",
        "comment_id": getattr(article, "comment_id", "") or "",
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
    }
