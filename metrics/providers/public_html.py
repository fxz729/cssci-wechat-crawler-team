import re

from metrics.providers.base import MetricsSnapshot


class PublicHtmlProvider:
    provider = "public-html"
    provider_priority = 10

    def fetch_from_html(self, html: str) -> MetricsSnapshot:
        return MetricsSnapshot(
            provider=self.provider,
            provider_priority=self.provider_priority,
            read_count=_extract_int(html, "read_num"),
            like_count=_extract_int(html, "old_like_num"),
            watching_count=_extract_int(html, "like_num"),
            is_authorized_source=False,
        )


def _extract_int(html: str, name: str) -> int | None:
    match = re.search(rf"{re.escape(name)}\s*=\s*(\d+)", html)
    return int(match.group(1)) if match else None
