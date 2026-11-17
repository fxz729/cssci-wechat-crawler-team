from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class MetricsSnapshot:
    provider: str
    provider_priority: int = 0
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    read_count: int | None = None
    like_count: int | None = None
    watching_count: int | None = None
    reward_count: int | None = None
    share_count: int | None = None
    favorite_count: int | None = None
    subscriber_count: int | None = None
    comment_count: int | None = None
    collect_num: int | None = None
    raw_payload: dict = field(default_factory=dict)
    is_authorized_source: bool = False
