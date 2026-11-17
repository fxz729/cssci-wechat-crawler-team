from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JournalSeed(Base):
    __tablename__ = "journal_seeds"
    __table_args__ = (UniqueConstraint("catalog_version", "discipline", "tier", "journal_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    catalog_version: Mapped[str] = mapped_column(String(20), nullable=False)
    discipline: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(30), nullable=False)
    journal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    source_file: Mapped[str] = mapped_column(String(500), default="")
    source_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountCandidate(Base):
    __tablename__ = "account_candidates"
    __table_args__ = (UniqueConstraint("journal_seed_id", "candidate_name", "candidate_wechat_id", "platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_seed_id: Mapped[int] = mapped_column(ForeignKey("journal_seeds.id"), nullable=False)
    query_text: Mapped[str] = mapped_column(String(300), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), default="sogou_weixin")
    candidate_name: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_wechat_id: Mapped[str] = mapped_column(String(200), default="")
    candidate_profile_url: Mapped[str] = mapped_column(String(1000), default="")
    candidate_description: Mapped[str] = mapped_column(Text, default="")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    review_status: Mapped[str] = mapped_column(String(30), default="pending")
    review_note: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("journal_seed_id", "canonical_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_seed_id: Mapped[int] = mapped_column(ForeignKey("journal_seeds.id"), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    wechat_id: Mapped[str] = mapped_column(String(200), default="")
    biz: Mapped[str] = mapped_column(String(200), default="")
    account_aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    profile_url: Mapped[str] = mapped_column(String(1000), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    verified_type: Mapped[str] = mapped_column(String(100), default="")
    region: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    discovery_source: Mapped[str] = mapped_column(String(100), default="sogou_weixin")
    confirmation_method: Mapped[str] = mapped_column(String(50), default="manual_review")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PublishBatch(Base):
    __tablename__ = "publish_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    biz: Mapped[str] = mapped_column(String(200), nullable=False)
    mid: Mapped[str] = mapped_column(String(100), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    crawl_status: Mapped[str] = mapped_column(String(30), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("biz", "mid", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    publish_batch_id: Mapped[int] = mapped_column(ForeignKey("publish_batches.id"), nullable=False)
    biz: Mapped[str] = mapped_column(String(200), nullable=False)
    mid: Mapped[str] = mapped_column(String(100), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    sn: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    author: Mapped[str] = mapped_column(String(300), default="")
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    province_or_region: Mapped[str] = mapped_column(String(100), default="")
    copyright_text: Mapped[str] = mapped_column(String(200), default="")
    digest: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    content_text: Mapped[str] = mapped_column(Text, default="")
    cover_image_url: Mapped[str] = mapped_column(String(1000), default="")
    image_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    media_assets_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    field_provenance_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    canonical_url: Mapped[str] = mapped_column(String(1000), default="")
    raw_html_path: Mapped[str] = mapped_column(String(500), default="")
    raw_text_path: Mapped[str] = mapped_column(String(500), default="")
    position_in_batch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_head_article: Mapped[bool] = mapped_column(Boolean, default=False)
    crawl_status: Mapped[str] = mapped_column(String(30), default="success")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # extended fields from profile_ext / HTML
    req_id: Mapped[str] = mapped_column(String(200), default="")
    comment_id: Mapped[str] = mapped_column(String(200), default="")
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_original: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    copyright_stat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_count: Mapped[int] = mapped_column(Integer, default=0)
    video_count: Mapped[int] = mapped_column(Integer, default=0)
    has_tts: Mapped[bool] = mapped_column(Boolean, default=False)
    album_id: Mapped[str] = mapped_column(String(100), default="")
    album_name: Mapped[str] = mapped_column(String(300), default="")
    collect_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_ext_source: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArticleMetric(Base):
    __tablename__ = "article_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_priority: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watching_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favorite_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscriber_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    is_authorized_source: Mapped[bool] = mapped_column(Boolean, default=False)


class CrawlCheckpoint(Base):
    __tablename__ = "crawl_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    account_name: Mapped[str] = mapped_column(String(200), default="")
    page: Mapped[int] = mapped_column(Integer, default=1)
    processed_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(30), default="running")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def processed_urls(self) -> list[str]:
        import json
        return json.loads(self.processed_urls_json or "[]")


class FailedCrawlTask(Base):
    __tablename__ = "failed_crawl_tasks"
    __table_args__ = (UniqueConstraint("task_name", "url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_name: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="failed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (UniqueConstraint("article_id", "provider", "comment_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    comment_id: Mapped[str] = mapped_column(String(200), nullable=False)
    author_name: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
