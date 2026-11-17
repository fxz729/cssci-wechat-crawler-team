from dataclasses import dataclass
import time
from urllib.parse import quote

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class AccountCandidateResult:
    name: str
    wechat_id: str = ""
    profile_url: str = ""
    description: str = ""
    is_verified: bool = False
    evidence_titles: list[str] | None = None


@dataclass(frozen=True)
class ArticleSearchResult:
    title: str
    url: str
    account_name: str
    summary: str = ""


def build_sogou_search_url(query: str, page: int = 1, search_type: int = 2) -> str:
    return f"https://weixin.sogou.com/weixin?type={search_type}&query={quote(query)}&page={page}"




class SogouSearchClient:
    def __init__(self, headless: bool = True, timeout_ms: int = 30_000, rest_seconds: float = 1.5):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.rest_seconds = rest_seconds
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import Stealth
        except Exception:
            Stealth = None

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
            locale="zh-CN",
            viewport={"width": 1366, "height": 768},
        )
        self._context.set_default_timeout(self.timeout_ms)
        self._page = self._context.new_page()
        if Stealth is not None:
            Stealth().apply_stealth_sync(self._page)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._page:
            self._page.close()
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def fetch_article_html(self, url: str) -> tuple[str, str] | None:
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(max(self.rest_seconds, 3.0))
            final_url = page.url
            html = page.content()
            if "js_content" not in html and "rich_media_content" not in html:
                return None
            return final_url, html
        finally:
            page.close()

    def search_article_results(self, query: str, pages: int = 1) -> list[ArticleSearchResult]:
        results: list[ArticleSearchResult] = []
        for page in range(1, pages + 1):
            url = build_sogou_search_url(query, page=page, search_type=2)
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(self.rest_seconds)
            results.extend(parse_article_search_results(self._page.content()))
        return results

    def search_article_candidates(self, query: str, pages: int = 1) -> list[AccountCandidateResult]:
        merged: dict[str, AccountCandidateResult] = {}
        for page in range(1, pages + 1):
            url = build_sogou_search_url(query, page=page, search_type=2)
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(self.rest_seconds)
            for candidate in parse_article_search_candidates(self._page.content()):
                current = merged.get(candidate.name)
                if current is None:
                    merged[candidate.name] = candidate
                    continue
                titles = list(dict.fromkeys((current.evidence_titles or []) + (candidate.evidence_titles or [])))
                merged[candidate.name] = AccountCandidateResult(
                    name=current.name,
                    wechat_id=current.wechat_id or candidate.wechat_id,
                    profile_url=current.profile_url or candidate.profile_url,
                    description=current.description or candidate.description,
                    is_verified=current.is_verified or candidate.is_verified,
                    evidence_titles=titles,
                )
        return list(merged.values())




def parse_article_search_results(html: str) -> list[ArticleSearchResult]:
    soup = BeautifulSoup(html, "lxml")
    results: list[ArticleSearchResult] = []
    for item in soup.select("ul.news-list > li, .news-list li"):
        title_element = item.select_one("h3 a")
        account_element = item.select_one(".s-p .all-time-y2")
        if not title_element or not account_element:
            continue
        href = title_element.get("href", "").strip()
        if href.startswith("/"):
            href = "https://weixin.sogou.com" + href
        summary_element = item.select_one("p.txt-info")
        results.append(ArticleSearchResult(
            title=title_element.get_text(strip=True),
            url=href,
            account_name=account_element.get_text(strip=True),
            summary=summary_element.get_text(strip=True) if summary_element else "",
        ))
    return results


def parse_article_search_candidates(html: str) -> list[AccountCandidateResult]:
    candidates: dict[str, list[str]] = {}
    for result in parse_article_search_results(html):
        candidates.setdefault(result.account_name, [])
        if result.title:
            candidates[result.account_name].append(result.title)
    return [AccountCandidateResult(name=name, evidence_titles=titles) for name, titles in candidates.items()]
