from metrics.providers.base import MetricsSnapshot


class ManualImportProvider:
    provider = "manual-import"
    provider_priority = 80

    def from_row(self, row: dict) -> MetricsSnapshot:
        return MetricsSnapshot(
            provider=self.provider,
            provider_priority=self.provider_priority,
            read_count=_as_int(row.get("阅读量")),
            like_count=_as_int(row.get("点赞")),
            watching_count=_as_int(row.get("在看")),
            reward_count=_as_int(row.get("赞赏")),
            share_count=_as_int(row.get("转发")),
            subscriber_count=_as_int(row.get("订阅数")),
            comment_count=_as_int(row.get("评论")),
            is_authorized_source=False,
        )


def _as_int(value) -> int | None:
    if value in (None, "", "-100"):
        return None
    return int(value)
