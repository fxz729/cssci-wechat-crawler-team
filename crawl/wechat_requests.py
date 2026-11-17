import requests

from crawl.wechat_urls import is_official_article_url
from metrics.providers.wechat_authorized import WechatAuthorizedProvider
from metrics.providers.wechat_backend import WechatBackendClient


class WechatRequestsClient:
    def __init__(
        self,
        credential,
        fakeid: str,
        *,
        session_factory=requests.Session,
        backend_client_factory=WechatBackendClient,
        provider_factory=WechatAuthorizedProvider,
    ):
        self.credential = credential
        self.fakeid = fakeid
        self.session = session_factory()
        self.session.trust_env = False
        self.backend = backend_client_factory.from_session_credential(
            credential,
            fakeid,
            session=self.session,
        )
        self.provider = provider_factory(
            cookie=credential.cookie,
            appmsg_token=_cookie_value(credential.cookie, "appmsg_token"),
            user_agent=credential.user_agent,
            session=self.session,
        )

    def __enter__(self) -> "WechatRequestsClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session.close()
        return None

    def fetch_publish_page(self, begin: int = 0, count: int = 5):
        return self.backend.fetch_publish_page(begin, count)

    def fetch_article_page(self, item) -> tuple[str, str]:
        if not is_official_article_url(item.link):
            raise RuntimeError("untrusted article URL")
        response = self.session.get(
            item.link,
            headers={
                "Cookie": self.credential.cookie,
                "User-Agent": self.credential.user_agent,
            },
            timeout=30,
        )
        response.raise_for_status()
        if not is_official_article_url(response.url):
            raise RuntimeError("untrusted article redirect")
        return response.url, response.text

    def fetch_metrics(self, article):
        return self.provider.fetch(article)


def _cookie_value(cookie: str, name: str) -> str:
    prefix = f"{name}="
    for part in cookie.split(";"):
        candidate = part.strip()
        if candidate.startswith(prefix):
            return candidate[len(prefix):]
    return ""
