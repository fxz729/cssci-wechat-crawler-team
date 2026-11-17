"""Experimental, offline import of observed reprint-search responses.

This is a discovery/content source, NOT a complete journal catalog or a metrics
provider. No login secrets, browser navigation, or production state are used.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape, unescape
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from export.csv_exporter import export_articles_csv
from export.realtime_exporter import default_export_headers
from parsers.article_parser import parse_article_html
from parsers.url_parser import parse_wechat_url

METRICS = ("read_count", "like_count", "watching_count", "share_count", "favorite_count", "comment_count")
SCOPE = "original_article_keyword_search"


class ReprintResponseError(ValueError):
    """Intentionally does not include request bodies, credentials, or raw URLs."""


def _identity(url):
    if not isinstance(url, str):
        raise ReprintResponseError("invalid_article_url")
    parsed = urlsplit(unescape(url))
    if (parsed.scheme not in ("http", "https") or parsed.netloc != "mp.weixin.qq.com"
            or parsed.path != "/s"):
        raise ReprintResponseError("untrusted_article_url")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(query.get(key, [])) != 1 for key in ("__biz", "mid", "idx", "sn")):
        raise ReprintResponseError("ambiguous_article_identity")
    parts = parse_wechat_url(url)
    if (not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", parts.biz or "")
            or not re.fullmatch(r"\d{1,30}", parts.mid or "")
            or not parts.idx or not 1 <= parts.idx <= 100
            or not re.fullmatch(r"[a-fA-F0-9]{32}", parts.sn or "")):
        raise ReprintResponseError("invalid_article_identity")
    return parts


def parse_reprint_page(payload, target_biz, *, begin=0, count=10):
    if (type(begin) is not int or begin < 0 or type(count) is not int or not 1 <= count <= 10
            or not isinstance(target_biz, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", target_biz)):
        raise ReprintResponseError("invalid_probe_parameters")
    base = payload.get("base_resp") if isinstance(payload, dict) else None
    ret = base.get("ret") if isinstance(base, dict) else None
    if type(ret) is not int or ret != 0:
        raise ReprintResponseError("business_rejected:" + (str(ret) if type(ret) is int else "missing"))
    items = payload.get("list")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ReprintResponseError("invalid_response_list")
    articles = []
    for item in items:
        parts = _identity(item.get("url"))
        if parts.biz != target_biz:
            continue
        values = {key: item.get(key) or "" for key in ("title", "author", "nickname", "digest", "content", "cover_url")}
        if any(not isinstance(value, str) for value in values.values()):
            raise ReprintResponseError("invalid_article_fields")
        parsed = parse_article_html('<div id="js_content">' + values["content"] + '</div>', parts.canonical_url)
        row = {
            "biz": parts.biz, "mid": parts.mid, "idx": parts.idx, "sn": parts.sn,
            "article_url": parts.canonical_url, "canonical_url": parts.canonical_url,
            "source_url": parts.canonical_url, "batch_key": parts.batch_key,
            "account_name": values["nickname"], "title": unescape(values["title"]),
            "author": unescape(values["author"]), "digest": unescape(values["digest"]),
            "content_html": values["content"], "content": parsed.content_text if parsed else "",
            "content_text": parsed.content_text if parsed else "", "cover_image_url": values["cover_url"],
            "publish_time": None, "comment_id": None, **{key: None for key in METRICS},
            "metrics_status": "not_returned", "metric_field_status": {key: "not_returned" for key in METRICS},
            "image_urls": parsed.image_urls if parsed else [], "media_assets": parsed.media_assets if parsed else [],
            "image_count": parsed.image_count if parsed else 0, "audio_count": parsed.audio_count if parsed else 0,
            "video_count": parsed.video_count if parsed else 0, "word_count": parsed.word_count if parsed else 0,
            "content_origin": "reprint_response_fragment", "discovery_scope": SCOPE,
            "content_status": "captured" if parsed else "empty", "media_status": "not_downloaded",
            "field_provenance": {key: "$.list[]." + value for key, value in {
                "title": "title", "author": "author", "digest": "digest", "content_html": "content",
                "cover_image_url": "cover_url", "account_name": "nickname", "article_url": "url",
            }.items()},
        }
        articles.append(row)
    return {"articles": articles, "returned_count": len(items), "filtered_count": len(items) - len(articles),
            "next_begin": begin + count if items else None, "total_hint": payload.get("total"),
            "scope": SCOPE, "catalog_complete": False}


def _write_csv(path, rows):
    temporary = path.with_suffix(path.suffix + ".tmp")
    export_articles_csv(rows, str(temporary), default_export_headers(rows))
    temporary.replace(path)


def export_probe(evidence, output_dir):
    if evidence.get("schema_version") != 1 or not isinstance(evidence.get("pages"), list):
        raise ReprintResponseError("invalid_evidence")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    records, rejected = {}, 0
    for page in evidence["pages"]:
        if page.get("http_status") != 200 or type(page.get("business_ret")) is not int or page["business_ret"] != 0:
            rejected += 1
            continue
        parsed = parse_reprint_page(
            {"base_resp": {"ret": page["business_ret"]}, "list": page["target_articles"], "total": page.get("total_hint")},
            page["target_biz"], begin=page["begin"], count=page["count"],
        )
        for row in parsed["articles"]:
            identity = (row["biz"], row["mid"], row["idx"])
            prior = records.get(identity)
            if prior and prior["sn"] != row["sn"]:
                raise ReprintResponseError("conflicting_article_identity")
            if prior:
                continue
            group = hashlib.sha256(row["biz"].encode()).hexdigest()[:12]
            folder = out / "附件" / group / f'{row["mid"]}_{row["idx"]}'
            folder.mkdir(parents=True, exist_ok=True)
            html_path, text_path = folder / "正文.html", folder / "正文.txt"
            csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
            html_path.write_text('<!doctype html><html><head><meta charset="utf-8">'
                                 '<meta http-equiv="Content-Security-Policy" content="' + escape(csp, quote=True) + '">'
                                 '<title>' + escape(row["title"]) + '</title></head><body>'
                                 '<h1>' + escape(row["title"]) + '</h1>' + row["content_html"] + '</body></html>', encoding="utf-8")
            text_path.write_text(row["content_text"], encoding="utf-8")
            row.update(local_html=html_path.relative_to(out).as_posix(), local_text=text_path.relative_to(out).as_posix(),
                       captured_at=evidence.get("captured_at"), business_ret=0, http_status=200,
                       source_endpoint="/cgi-bin/operate_appmsg?sub=check_appmsg_copyright_stat",
                       source_payload_sha256=page.get("payload_sha256", ""))
            records[identity] = row
            # Small proof export only: each article is visible even if a later page fails.
            _write_csv(out / "文章数据.csv", list(records.values()))
    if not records:
        _write_csv(out / "文章数据.csv", [])
    result = {"article_count": len(records), "rejected_pages": rejected, "catalog_complete": False,
              "metrics_complete": False, "media_downloaded": False, "csv": "文章数据.csv"}
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
    print(json.dumps(export_probe(evidence, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()