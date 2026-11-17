from contextlib import AbstractContextManager


class BrowserSession(AbstractContextManager):
    def __init__(self, headless: bool = True, timeout_ms: int = 30_000, user_agent: str | None = None):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent
        self._playwright = None
        self.browser = None
        self.context = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import Stealth
        except Exception:
            Stealth = None

        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        context_options = {"locale": "zh-CN"}
        if self.user_agent:
            context_options["user_agent"] = self.user_agent
        self.context = self.browser.new_context(**context_options)
        self.context.set_default_timeout(self.timeout_ms)
        if Stealth is not None:
            page = self.context.new_page()
            Stealth().apply_stealth_sync(page)
            page.close()
        return self

    def new_page(self):
        return self.context.new_page()

    def __exit__(self, exc_type, exc_value, traceback):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()
