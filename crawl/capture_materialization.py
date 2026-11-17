"""Materialize validated client events without importing desktop or capture controls."""
from __future__ import annotations

import json
from pathlib import Path

from capture.reader import to_wire_event
from crawl.article_persistence import persist_history_article
from crawl.client_capture import build_capture_metadata
from crawl.pilot import _time
from storage.models import Article, ArticleMetric


def _html_escape(value) -> str:
    from html import escape
    return escape(str(value or ""), quote=True)


def _article_wrapper(article_event: dict, metric: dict | None, account_name: str) -> str:
    article = article_event["article"]
    stamp = _time(article.get("publish_time"))
    metadata = "".join(
        f'<meta name="{name}" content="{_html_escape(article.get(value, ""))}">'
        for name, value in (("author", "author"), ("description", "digest"))
    )
    metadata += f'<meta property="og:title" content="{_html_escape(article.get("title", ""))}">'
    metadata += f'<meta property="og:image" content="{_html_escape(article.get("cover_url", ""))}">'
    public_vars = "".join(
        f"var {name}={json.dumps(str(value or ''), ensure_ascii=False)};"
        for name, value in (("biz", article_event.get("biz")), ("mid", article_event.get("mid")),
                            ("idx", article_event.get("idx")), ("sn", article_event.get("sn")))
    )
    if stamp:
        public_vars += f'var ct="{int(stamp.timestamp())}";'
    if metric and metric.get("comment_id"):
        public_vars += f'var comment_id="{_html_escape(metric["comment_id"])}";'
    author = article.get("author") or ""
    author_node = f'<span id="js_author_name">{_html_escape(author)}</span>' if author else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' + metadata
        + "</head><body>"
        + f'<h1 id="activity-name">{_html_escape(article.get("title", ""))}</h1>'
        + f'<span id="js_name">{_html_escape(account_name)}</span>' + author_node
        + str(article.get("content_html") or "") + f"<script>{public_vars}</script></body></html>"
    )


def _db_row(stored: Article) -> dict:
    row = {column.name: getattr(stored, column.name) for column in Article.__table__.columns}
    row["media_assets"] = json.loads(stored.media_assets_json or "[]")
    row["image_urls"] = json.loads(stored.image_urls_json or "[]")
    row["field_provenance"] = json.loads(stored.field_provenance_json or "{}")
    return row


def persist_capture(repo, account, target, article_event: dict, metric: dict | None, bundle: Path) -> dict:
    article = article_event["article"]
    html = _article_wrapper(article_event, metric, target.account_name)
    item = type("CapturedItem", (), {
        "mid": article_event["mid"], "idx": article_event["idx"],
        "title": article.get("title", ""), "author": article.get("author", ""),
        "digest": article.get("digest", ""), "cover_url": article.get("cover_url", ""),
        "link": article.get("url", ""), "publish_time": _time(article.get("publish_time")),
    })()
    client = type("CapturedClient", (), {"fetch_article_page": lambda _self, _item: (item.link, html)})()
    saved = persist_history_article(repo, account, client, item, bundle / "captured_content", with_metrics=False)
    folder = bundle / "articles" / f"{article_event['mid']}_{article_event['idx']}"
    folder.mkdir(parents=True, exist_ok=True)
    local_html = folder / "article.html"
    local_text = folder / "article.txt"
    local_fragment = folder / "content_fragment.html"
    local_html.write_text(html, encoding="utf-8")
    local_fragment.write_text(str(article.get("content_html") or ""), encoding="utf-8")
    with repo.session_factory() as db:
        stored = db.query(Article).filter_by(id=saved.id).one()
        base_row = _db_row(stored)
    record = build_capture_metadata(
        article_event, metric, base_row, journal_name=target.account_name,
        profile_url=target.profile_url, local_html=local_html.relative_to(bundle).as_posix(),
        local_text=local_text.relative_to(bundle).as_posix(),
        local_article_dir=folder.relative_to(bundle).as_posix(),
    )
    local_text.write_text(str(record.get("content_text") or ""), encoding="utf-8")
    with repo.session_factory() as db:
        stored = db.query(Article).filter_by(id=saved.id).one()
        stored.sn = record["sn"]
        stored.title = record["title"]
        stored.author = record["author"]
        stored.digest = record["digest"]
        stored.cover_image_url = record["cover_url"]
        stored.source_url = record["article_url"]
        stored.canonical_url = record["article_url"]
        stored.content_html = record["content_fragment_html"] or stored.content_html
        stored.comment_id = record["comment_id"]
        stored.profile_ext_source = "fiddler_classic:/s"
        stored.field_provenance_json = json.dumps(record["field_provenance"], ensure_ascii=False)
        db.commit()
    evidence = {
        "schema_version": 1,
        "article_event": to_wire_event(article_event),
        "metric_event": to_wire_event(metric) if metric else None,
        "metric_fields": record["metric_fields"],
    }
    record["capture_evidence"] = (folder / "capture_evidence.json").relative_to(bundle).as_posix()
    _write_json(folder / "capture_evidence.json", evidence)
    _write_json(folder / "metadata.json", record)
    if metric:
        with repo.session_factory() as db:
            prior = any(
                json.loads(snapshot.raw_payload_json or "{}").get("event_id") == metric.get("event_id")
                for snapshot in db.query(ArticleMetric).filter_by(article_id=saved.id).all()
            )
        if not prior:
            snapshot = repo.insert_metrics_snapshot(
                saved.id, provider="wechat-client-http", provider_priority=120,
                raw_payload=metric, **metric["metrics"],
            )
            with repo.session_factory() as db:
                db.query(ArticleMetric).filter_by(id=snapshot.id).update({"captured_at": _time(metric.get("captured_at"))})
                db.commit()
    return record


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)
