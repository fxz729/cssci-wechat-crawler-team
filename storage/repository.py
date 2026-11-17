from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func

from utils.image_filter import clean_image_urls

from storage.models import (
    Account,
    AccountCandidate,
    Article,
    ArticleMetric,
    CrawlCheckpoint,
    FailedCrawlTask,
    JournalSeed,
    PublishBatch,
    utcnow,
)


class Repository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def upsert_journal_seed(
        self,
        catalog_version: str,
        discipline: str,
        tier: str,
        journal_name: str,
        normalized_name: str,
        aliases: list[str],
        source_file: str,
        source_line: int | None,
    ) -> JournalSeed:
        with self.session_factory() as session:
            seed = (
                session.query(JournalSeed)
                .filter_by(
                    catalog_version=catalog_version,
                    discipline=discipline,
                    tier=tier,
                    journal_name=journal_name,
                )
                .first()
            )
            if seed is None:
                seed = JournalSeed(
                    catalog_version=catalog_version,
                    discipline=discipline,
                    tier=tier,
                    journal_name=journal_name,
                )
                session.add(seed)
            seed.normalized_name = normalized_name
            seed.aliases_json = json.dumps(aliases, ensure_ascii=False)
            seed.source_file = source_file
            seed.source_line = source_line
            session.commit()
            return seed

    def upsert_account_candidate(
        self,
        journal_seed_id: int,
        query_text: str,
        candidate_name: str,
        candidate_wechat_id: str = "",
        candidate_profile_url: str = "",
        candidate_description: str = "",
        is_verified: bool = False,
        evidence: dict | None = None,
        score: float = 0.0,
        platform: str = "sogou_weixin",
    ) -> AccountCandidate:
        with self.session_factory() as session:
            candidate = (
                session.query(AccountCandidate)
                .filter_by(
                    journal_seed_id=journal_seed_id,
                    candidate_name=candidate_name,
                    candidate_wechat_id=candidate_wechat_id,
                    platform=platform,
                )
                .first()
            )
            if candidate is None:
                candidate = AccountCandidate(
                    journal_seed_id=journal_seed_id,
                    candidate_name=candidate_name,
                    candidate_wechat_id=candidate_wechat_id,
                    platform=platform,
                )
                session.add(candidate)
            candidate.query_text = query_text
            candidate.candidate_profile_url = candidate_profile_url
            candidate.candidate_description = candidate_description
            candidate.is_verified = is_verified
            candidate.evidence_json = json.dumps(evidence or {}, ensure_ascii=False)
            candidate.score = score
            session.commit()
            return candidate

    def auto_accept_high_score_candidates(self, threshold: float = 0.92) -> int:
        accepted = 0
        with self.session_factory() as session:
            candidates = (
                session.query(AccountCandidate)
                .filter(AccountCandidate.score >= threshold)
                .filter(AccountCandidate.review_status == "pending")
                .all()
            )
            by_seed: dict[int, list[AccountCandidate]] = {}
            for candidate in candidates:
                by_seed.setdefault(candidate.journal_seed_id, []).append(candidate)

            for seed_id, seed_candidates in by_seed.items():
                best_score = max(candidate.score for candidate in seed_candidates)
                best = [candidate for candidate in seed_candidates if candidate.score == best_score]
                if len(best) != 1:
                    for candidate in seed_candidates:
                        candidate.review_status = "uncertain"
                    continue
                candidate = best[0]
                candidate.review_status = "auto_accepted"
                account = (
                    session.query(Account)
                    .filter_by(journal_seed_id=seed_id, canonical_name=candidate.candidate_name)
                    .first()
                )
                if account is None:
                    account = Account(journal_seed_id=seed_id, canonical_name=candidate.candidate_name)
                    session.add(account)
                account.wechat_id = candidate.candidate_wechat_id
                account.profile_url = candidate.candidate_profile_url
                account.description = candidate.candidate_description
                account.discovery_source = candidate.platform
                account.confirmation_method = "auto_high_score"
                account.reviewed_at = utcnow()
                accepted += 1
            session.commit()
        return accepted

    def get_account_by_name(self, canonical_name: str) -> Account | None:
        with self.session_factory() as session:
            return session.query(Account).filter_by(canonical_name=canonical_name).first()

    def list_accounts(self, limit: int | None = None) -> list[Account]:
        with self.session_factory() as session:
            query = session.query(Account).filter_by(status="active").order_by(Account.canonical_name.asc())
            if limit:
                query = query.limit(limit)
            return query.all()

    def sync_target_accounts(self, targets) -> tuple[int, int]:
        target_names = {target.account_name for target in targets}
        created_or_updated = 0
        with self.session_factory() as session:
            for target in targets:
                seed = session.query(JournalSeed).filter_by(
                    catalog_version="2025-2026",
                    discipline=target.discipline,
                    tier=target.tier,
                    journal_name=target.journal_name,
                ).first()
                if seed is None:
                    seed = JournalSeed(
                        catalog_version="2025-2026",
                        discipline=target.discipline,
                        tier=target.tier,
                        journal_name=target.journal_name,
                    )
                    session.add(seed)
                seed.normalized_name = target.journal_name
                seed.aliases_json = json.dumps([target.journal_name], ensure_ascii=False)
                seed.source_file = target.source_file
                seed.source_line = target.source_line
                session.flush()

                account = session.query(Account).filter_by(canonical_name=target.account_name).first()
                if account is None:
                    account = Account(journal_seed_id=seed.id, canonical_name=target.account_name)
                    session.add(account)
                account.journal_seed_id = seed.id
                account.biz = target.biz
                account.profile_url = target.profile_url
                account.status = "active"
                account.discovery_source = "profile_link_catalog"
                account.confirmation_method = "profile_link_verified"
                account.reviewed_at = utcnow()
                created_or_updated += 1

            deactivated = session.query(Account).filter(
                Account.status == "active",
                ~Account.canonical_name.in_(target_names),
            ).update({Account.status: "inactive"}, synchronize_session=False)
            session.commit()
        return created_or_updated, deactivated

    def update_account_biz(self, account_id: int, biz: str) -> None:
        with self.session_factory() as session:
            account = session.query(Account).filter_by(id=account_id).first()
            if account is not None and biz and account.biz != biz:
                account.biz = biz
                session.commit()

    def list_articles(self, account_name: str | None = None, limit: int | None = None) -> list[Article]:
        with self.session_factory() as session:
            query = session.query(Article).join(Account, Article.account_id == Account.id)
            if account_name:
                query = query.filter(Account.canonical_name == account_name)
            query = query.order_by(Article.publish_time.desc())
            if limit:
                query = query.limit(limit)
            return query.all()

    def upsert_publish_batch(
        self,
        account_id: int,
        biz: str,
        mid: str,
        publish_time: datetime | None,
        source_url: str,
    ) -> PublishBatch:
        batch_key = f"{biz}:{mid}"
        with self.session_factory() as session:
            batch = session.query(PublishBatch).filter_by(batch_key=batch_key).first()
            if batch is None:
                batch = PublishBatch(account_id=account_id, biz=biz, mid=mid, batch_key=batch_key)
                session.add(batch)
            batch.publish_time = publish_time
            batch.source_url = source_url
            session.commit()
            return batch

    def upsert_article(
        self,
        account_id: int,
        publish_batch_id: int,
        biz: str,
        mid: str,
        idx: int,
        sn: str,
        title: str,
        author: str,
        publish_time: datetime | None,
        digest: str,
        content_text: str,
        image_urls: list[str],
        source_url: str,
        canonical_url: str,
        raw_html_path: str,
        raw_text_path: str,
        province_or_region: str = "",
        copyright_text: str = "",
        content_html: str = "",
        cover_image_url: str = "",
        req_id: str = "",
        comment_id: str = "",
        word_count: int | None = None,
        is_original: bool | None = None,
        copyright_stat: int | None = None,
        audio_count: int = 0,
        video_count: int = 0,
        has_tts: bool = False,
        album_id: str = "",
        album_name: str = "",
        collect_num: int | None = None,
        profile_ext_source: str = "",
        crawl_status: str = "success",
        media_assets: list[dict] | None = None,
        field_provenance: dict | None = None,
    ) -> Article:
        with self.session_factory() as session:
            article = session.query(Article).filter_by(biz=biz, mid=mid, idx=idx).first()
            if article is None:
                article = Article(account_id=account_id, publish_batch_id=publish_batch_id, biz=biz, mid=mid, idx=idx)
                session.add(article)
            article.account_id = account_id
            article.publish_batch_id = publish_batch_id
            article.sn = sn
            article.title = title
            article.author = author
            article.publish_time = publish_time
            article.province_or_region = province_or_region
            article.copyright_text = copyright_text
            article.digest = digest
            article.content_html = content_html
            article.content_text = content_text
            article.cover_image_url = cover_image_url
            article.image_urls_json = json.dumps(image_urls, ensure_ascii=False)
            if media_assets is not None:
                article.media_assets_json = json.dumps(media_assets, ensure_ascii=False)
            if field_provenance is not None:
                article.field_provenance_json = json.dumps(field_provenance, ensure_ascii=False)
            article.image_count = len(image_urls)
            article.source_url = source_url
            article.canonical_url = canonical_url
            article.raw_html_path = raw_html_path
            article.raw_text_path = raw_text_path
            article.position_in_batch = idx
            article.is_head_article = idx == 1
            article.content_hash = _content_hash(content_text)
            article.req_id = req_id
            article.comment_id = comment_id
            article.word_count = word_count
            article.is_original = is_original
            article.copyright_stat = copyright_stat
            article.audio_count = audio_count
            article.video_count = video_count
            article.has_tts = has_tts
            article.album_id = album_id
            article.album_name = album_name
            article.collect_num = collect_num
            article.profile_ext_source = profile_ext_source
            article.crawl_status = crawl_status
            session.commit()
            self._refresh_batch_count(session, publish_batch_id)
            session.commit()
            return article

    def update_article_metrics_metadata(
        self,
        article_id: int,
        collect_num: int | None,
        album_id: str = "",
        album_name: str = "",
    ) -> None:
        with self.session_factory() as session:
            article = session.query(Article).filter_by(id=article_id).first()
            if article is None:
                return
            if collect_num is not None:
                article.collect_num = collect_num
            if album_id:
                article.album_id = album_id
            if album_name:
                article.album_name = album_name
            session.commit()

    def insert_metrics_snapshot(
        self,
        article_id: int,
        provider: str,
        provider_priority: int = 0,
        read_count: int | None = None,
        like_count: int | None = None,
        watching_count: int | None = None,
        reward_count: int | None = None,
        share_count: int | None = None,
        favorite_count: int | None = None,
        subscriber_count: int | None = None,
        comment_count: int | None = None,
        raw_payload: dict | None = None,
        is_authorized_source: bool = False,
    ) -> ArticleMetric:
        with self.session_factory() as session:
            metric = ArticleMetric(
                article_id=article_id,
                provider=provider,
                provider_priority=provider_priority,
                read_count=read_count,
                like_count=like_count,
                watching_count=watching_count,
                reward_count=reward_count,
                share_count=share_count,
                favorite_count=favorite_count,
                subscriber_count=subscriber_count,
                comment_count=comment_count,
                raw_payload_json=json.dumps(raw_payload or {}, ensure_ascii=False),
                is_authorized_source=is_authorized_source,
            )
            session.add(metric)
            session.commit()
            return metric

    def complete_article_with_metrics(self, article_id: int, snapshot) -> ArticleMetric:
        with self.session_factory() as session:
            article = session.query(Article).filter_by(id=article_id).one()
            payload = snapshot.raw_payload.get("appmsgext") or {}
            article.collect_num = snapshot.collect_num
            article.album_id = str(payload.get("_album_id") or "")
            article.album_name = payload.get("_album_name") or ""
            article.crawl_status = "success"
            metric = ArticleMetric(
                article_id=article_id,
                provider=snapshot.provider,
                provider_priority=snapshot.provider_priority,
                read_count=snapshot.read_count,
                like_count=snapshot.like_count,
                watching_count=snapshot.watching_count,
                reward_count=snapshot.reward_count,
                share_count=snapshot.share_count,
                favorite_count=snapshot.favorite_count,
                subscriber_count=snapshot.subscriber_count,
                comment_count=snapshot.comment_count,
                raw_payload_json=json.dumps(snapshot.raw_payload or {}, ensure_ascii=False),
                is_authorized_source=snapshot.is_authorized_source,
            )
            session.add(metric)
            session.commit()
            return metric

    def get_latest_metrics(self, article_id: int) -> ArticleMetric | None:
        with self.session_factory() as session:
            return (
                session.query(ArticleMetric)
                .filter_by(article_id=article_id)
                .order_by(ArticleMetric.provider_priority.desc(), ArticleMetric.captured_at.desc())
                .first()
            )

    def list_journal_seeds(self, journal_name: str | None = None, limit: int | None = None) -> list[JournalSeed]:
        with self.session_factory() as session:
            query = session.query(JournalSeed).order_by(JournalSeed.discipline.asc(), JournalSeed.journal_name.asc())
            if journal_name:
                query = query.filter(JournalSeed.journal_name == journal_name)
            if limit:
                query = query.limit(limit)
            return query.all()

    def list_candidates(self, status: str | None = None, limit: int = 50) -> list[AccountCandidate]:
        with self.session_factory() as session:
            query = session.query(AccountCandidate).order_by(AccountCandidate.score.desc())
            if status:
                query = query.filter(AccountCandidate.review_status == status)
            return query.limit(limit).all()

    def save_crawl_checkpoint(
        self,
        task_name: str,
        account_name: str,
        page: int,
        processed_urls: list[str],
        status: str = "running",
    ) -> CrawlCheckpoint:
        with self.session_factory() as session:
            checkpoint = session.query(CrawlCheckpoint).filter_by(task_name=task_name).first()
            if checkpoint is None:
                checkpoint = CrawlCheckpoint(task_name=task_name)
                session.add(checkpoint)
            checkpoint.account_name = account_name
            checkpoint.page = page
            checkpoint.processed_urls_json = json.dumps(list(dict.fromkeys(processed_urls)), ensure_ascii=False)
            checkpoint.status = status
            session.commit()
            return checkpoint

    def get_crawl_checkpoint(self, task_name: str) -> CrawlCheckpoint | None:
        with self.session_factory() as session:
            return session.query(CrawlCheckpoint).filter_by(task_name=task_name).first()

    def record_failed_task(
        self,
        task_name: str,
        account_name: str,
        url: str,
        error_message: str,
    ) -> FailedCrawlTask:
        with self.session_factory() as session:
            failed = session.query(FailedCrawlTask).filter_by(task_name=task_name, url=url).first()
            if failed is None:
                failed = FailedCrawlTask(task_name=task_name, url=url, account_name=account_name)
                session.add(failed)
            failed.retry_count = (failed.retry_count or 0) + 1
            failed.last_error = error_message
            failed.status = "failed"
            session.commit()
            return failed

    def mark_failed_task_success(self, task_name: str, url: str) -> None:
        with self.session_factory() as session:
            failed = session.query(FailedCrawlTask).filter_by(task_name=task_name, url=url).first()
            if failed is not None:
                failed.status = "success"
                session.commit()

    def list_retryable_failed_tasks(self, max_attempts: int = 3) -> list[FailedCrawlTask]:
        with self.session_factory() as session:
            return (
                session.query(FailedCrawlTask)
                .filter(FailedCrawlTask.status == "failed", FailedCrawlTask.retry_count < max_attempts)
                .order_by(FailedCrawlTask.updated_at.asc())
                .all()
            )

    def list_articles_for_export(self, account_name: str | None = None) -> list[dict]:
        with self.session_factory() as session:
            query = session.query(Article, Account).join(Account, Article.account_id == Account.id)
            if account_name:
                query = query.filter(Account.canonical_name == account_name)
            articles_with_accounts = query.order_by(Article.publish_time.desc()).all()
            article_ids = [article.id for article, _account in articles_with_accounts]
            metrics_by_article: dict[int, ArticleMetric] = {}
            if article_ids:
                metrics = (
                    session.query(ArticleMetric)
                    .filter(ArticleMetric.article_id.in_(article_ids))
                    .order_by(ArticleMetric.provider_priority.desc(), ArticleMetric.captured_at.desc())
                    .all()
                )
                for metric in metrics:
                    metrics_by_article.setdefault(metric.article_id, metric)
            rows: list[dict] = []
            for article, account in articles_with_accounts:
                metric = metrics_by_article.get(article.id)
                clean_images = clean_image_urls(json.loads(article.image_urls_json or "[]"))
                favorite_count = metric.favorite_count if metric else None
                metric_field_status = {
                    name: "observed" if metric is not None and getattr(metric, name) is not None else "not_observed"
                    for name in ("read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count")
                }
                observed_count = sum(status == "observed" for status in metric_field_status.values())
                metrics_status = (
                    "complete" if observed_count == len(metric_field_status)
                    else "partial" if observed_count else "not_observed"
                )
                field_provenance = json.loads(article.field_provenance_json or "{}")
                if article.collect_num is not None:
                    # Legacy article metadata has no metric observation timestamp.
                    field_provenance.setdefault("collect_num", {
                        "source": "article.collect_num", "captured_at": None, "status": "capture_time_unknown",
                    })
                rows.append({
                    "account_name": account.canonical_name,
                    "_account_biz": account.biz,
                    "title": article.title,
                    "author": article.author,
                    "publish_time": article.publish_time,
                    "read_count": metric.read_count if metric else None,
                    "like_count": metric.like_count if metric else None,
                    "favorite_count": favorite_count,
                    "watching_count": metric.watching_count if metric else None,
                    "reward_count": metric.reward_count if metric else None,
                    "share_count": metric.share_count if metric else None,
                    "comment_count": metric.comment_count if metric else None,
                    "copyright_text": article.copyright_text or _copyright_text(article.copyright_stat),
                    "position_in_batch": article.position_in_batch,
                    "batch_key": f"{article.biz}:{article.mid}",
                    "biz": article.biz,
                    "mid": article.mid,
                    "idx": article.idx,
                    "sn": article.sn,
                    "digest": _clean_text(article.digest),
                    "content": _clean_text(article.content_text),
                    "image_count": len(clean_images),
                    "images_folder": f"images/{account.canonical_name}",
                    "_image_urls_for_download": clean_images,
                    "source_url": article.source_url,
                    "canonical_url": article.canonical_url,
                    "req_id": article.req_id,
                    "comment_id": article.comment_id,
                    "word_count": article.word_count,
                    "is_original": article.is_original,
                    "copyright_stat": article.copyright_stat,
                    "audio_count": article.audio_count,
                    "video_count": article.video_count,
                    "has_tts": article.has_tts,
                    "album_id": article.album_id,
                    "album_name": article.album_name,
                    "collect_num": article.collect_num,
                    "profile_ext_source": article.profile_ext_source,
                    "year": article.publish_time.year if article.publish_time else None,
                    "month": article.publish_time.month if article.publish_time else None,
                    "quarter": ((article.publish_time.month - 1) // 3 + 1) if article.publish_time else None,
                    "weekday": article.publish_time.weekday() if article.publish_time else None,
                    "hour": article.publish_time.hour if article.publish_time else None,
                    "is_weekday": (article.publish_time.weekday() < 5) if article.publish_time else None,
                    "title_length": len(article.title),
                    "digest_length": len(article.digest),
                    "profile_url": account.profile_url,
                    "cover_image_url": article.cover_image_url,
                    "content_html": article.content_html,
                    "image_urls": json.loads(article.image_urls_json or "[]"),
                    "media_assets": json.loads(article.media_assets_json or "[]"),
                    "field_provenance": field_provenance,
                    "metrics_provider": metric.provider if metric else None,
                    "metrics_captured_at": metric.captured_at if metric else None,
                    "metrics_status": metrics_status,
                    "metric_field_status": metric_field_status,
                    "crawl_status": article.crawl_status,
                })
            return rows

    def get_stats(self) -> dict[str, int]:
        with self.session_factory() as session:
            return {
                "journal_seeds": session.query(JournalSeed).count(),
                "account_candidates": session.query(AccountCandidate).count(),
                "accounts": session.query(Account).count(),
                "publish_batches": session.query(PublishBatch).count(),
                "articles": session.query(Article).count(),
                "metrics": session.query(ArticleMetric).count(),
            }

    def _refresh_batch_count(self, session, publish_batch_id: int) -> None:
        count = session.query(func.count(Article.id)).filter_by(publish_batch_id=publish_batch_id).scalar() or 0
        batch = session.query(PublishBatch).filter_by(id=publish_batch_id).first()
        if batch is not None:
            batch.article_count = count


def _copyright_text(copyright_stat: int | None) -> str:
    if copyright_stat == 100:
        return "原创"
    if copyright_stat == 101:
        return "转载"
    return ""


def _clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
