"""Bounded replay of an operator-supplied WeChat profile-home request.

Secrets stay in memory. This experiment does not operate the UI, modify Fiddler,
resume production, infer article IDs, or claim the API covers a full journal.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html import escape, unescape
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests

from export.csv_exporter import export_articles_csv
from export.realtime_exporter import default_export_headers
from parsers.article_parser import parse_article_html
from parsers.url_parser import parse_wechat_url

ENDPOINT = "https://mp.weixin.qq.com/mp/profile_ext"
METRICS = dict(read_count="read_num", like_count="old_like_count", watching_count="like_count",
               share_count="share_count", favorite_count="collect_count", comment_count="comment_count")


class ProbeError(ValueError):
    pass


@dataclass(repr=False)
class HomeRequest:
    biz: str
    params: dict = field(repr=False)
    headers: dict = field(repr=False)

    def __repr__(self):
        return "HomeRequest(credentials=<redacted>)"


def read_home_request(text):
    match = re.search(r"^GET\s+(\S+)\s+HTTP/[^\n]+", text, re.M)
    if not match:
        raise ProbeError("missing_home_request")
    url = urlsplit(unescape(match[1]))
    query = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme != "https" or url.netloc != "mp.weixin.qq.com" or url.path != "/mp/profile_ext"
            or any(len(v) != 1 for v in query.values()) or query.get("action") != ["home"]
            or not all(query.get(k, [""])[0] for k in ("__biz", "key", "uin", "pass_ticket"))):
        raise ProbeError("invalid_home_request")
    params = {k: v[0] for k, v in query.items()}
    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", params["__biz"]):
        raise ProbeError("invalid_biz")
    block = re.split(r"\r?\n\r?\n", text[match.end():].lstrip("\r\n"), maxsplit=1)[0]
    headers = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.lower() in ("user-agent", "cookie", "accept", "accept-language"):
            headers[name] = value.strip()
    return HomeRequest(params["__biz"], params, headers)


def _article_identity(url):
    if not isinstance(url, str):
        raise ProbeError("invalid_article_url")
    parsed = urlsplit(unescape(url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme not in ("http", "https") or parsed.netloc != "mp.weixin.qq.com" or parsed.path != "/s"
            or any(len(query.get(k, [])) != 1 for k in ("__biz", "mid", "idx", "sn"))):
        raise ProbeError("invalid_article_identity")
    parts = parse_wechat_url(url)
    if (not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", parts.biz or "")
            or not re.fullmatch(r"\d{1,30}", parts.mid or "") or not parts.idx or not 1 <= parts.idx <= 100
            or not re.fullmatch(r"[a-fA-F0-9]{32}", parts.sn or "")):
        raise ProbeError("invalid_article_identity")
    return parts


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect_catalog(session, template, *, max_pages=30, sleep=time.sleep, on_page=None, start_offset=0):
    if (type(max_pages) is not int or not 1 <= max_pages <= 100
            or type(start_offset) is not int or start_offset < 0):
        raise ProbeError("invalid_page_limit")
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "target_biz": template.biz,
              "pages": [], "articles": [], "pagination_complete": False, "catalog_complete": False,
              "client_ui_actions": 0, "credentials_exported": False, "production_resumed": False,
              "stop_reason": "page_limit"}
    offset, seen = start_offset, set()
    for number in range(max_pages):
        if number:
            sleep(1.6)
        params = {**template.params, "action": "getmsg", "f": "json", "offset": str(offset), "count": "10", "is_ok": "1"}
        row = {"offset": offset, "http_status": None, "business_ret": None}
        result["pages"].append(row)
        try:
            response = session.get(ENDPOINT, params=params, headers=template.headers,
                                   timeout=(10, 20), allow_redirects=False)
            row.update(http_status=response.status_code, payload_sha256=hashlib.sha256(response.content).hexdigest())
            if response.status_code != 200:
                result["stop_reason"] = "http_rejected"
                break
            payload = response.json()
            if not isinstance(payload, dict) or type(payload.get("ret")) is not int:
                raise ProbeError("invalid_catalog_payload")
            row["business_ret"] = payload["ret"]
            if payload["ret"] != 0:
                result["stop_reason"] = "business_rejected"
                break
            raw = payload.get("general_msg_list")
            messages = json.loads(raw) if isinstance(raw, str) else raw
            groups = messages.get("list") if isinstance(messages, dict) else None
            if not isinstance(groups, list):
                raise ProbeError("invalid_catalog_list")
            row.update(group_count=len(groups), can_msg_continue=payload.get("can_msg_continue"),
                       next_offset=payload.get("next_offset"), new_article_count=0, unresolved_count=0, placeholder_count=0)
            for group in groups:
                ext = group.get("app_msg_ext_info") or {}
                if not ext:
                    row["unresolved_count"] += 1
                    continue
                for article in [ext, *ext.get("multi_app_msg_item_list", [])]:
                    if not any(article.get(key) for key in ("title", "content_url", "content")):
                        row["placeholder_count"] += 1
                        continue
                    try:
                        parts = _article_identity(article.get("content_url"))
                        if parts.biz != template.biz:
                            raise ProbeError("cross_biz")
                    except ProbeError:
                        row["unresolved_count"] += 1
                        continue
                    identity = (parts.biz, parts.mid, parts.idx)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    result["articles"].append({
                        "biz": parts.biz, "mid": parts.mid, "idx": parts.idx, "sn": parts.sn,
                        "url": parts.canonical_url, "title": unescape(article.get("title") or ""),
                        "author": unescape(article.get("author") or ""), "digest": unescape(article.get("digest") or ""),
                        "cover_url": article.get("cover") or "", "source_offset": offset,
                        "publish_timestamp": (group.get("comm_msg_info") or {}).get("datetime"),
                    })
                    row["new_article_count"] += 1
            if on_page:
                on_page(result)
            # Observed counterexample: can_msg_continue=0 on every nonempty page.
            # Follow the returned cursor, not guessed mid values or offset+count.
            if not groups:
                result.update(pagination_complete=True, stop_reason="empty_page")
                break
            if row["unresolved_count"]:
                result["stop_reason"] = "unresolved_identity"
                break
            next_offset = payload.get("next_offset")
            if type(next_offset) is not int or next_offset <= offset:
                result["stop_reason"] = "invalid_cursor"
                break
            if row["new_article_count"] == 0:
                result["stop_reason"] = "no_new_articles"
                break
            offset = next_offset
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            result["stop_reason"] = "network_or_format_error"
            break
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


_TOKEN = re.compile(r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|[A-Za-z_$][A-Za-z0-9_$]*|\d+(?:\.\d+)?|[^\s]')


def _unquote(token):
    pieces, i = [], 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while i < len(token) - 1:
        char = token[i]
        if char == "\\":
            i += 1
            char = token[i]
            if char in ("u", "x"):
                length = 4 if char == "u" else 2
                char = chr(int(token[i + 1:i + 1 + length], 16))
                i += length
            else:
                char = escapes.get(char, char)
        pieces.append(char)
        i += 1
    return "".join(pieces)


def parse_server_data(body):
    """Parse a literal object only; never eval article JavaScript."""
    found, assignments = None, 0
    for script in re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", body, re.I):
        if "cgiDataNew" not in script:
            continue
        tokens = [m[0] for m in _TOKEN.finditer(script) if not m[0].startswith(("//", "/*"))]
        for i in range(len(tokens) - 4):
            if tokens[i:i + 4] != ["window", ".", "cgiDataNew", "="] or tokens[i + 4] == "=":
                continue
            # Observed bundled scripts repeat this no-op initializer. It cannot
            # overwrite an existing literal object. Do not relax any other assignment.
            if tokens[i:i + 12] == ["window", ".", "cgiDataNew", "=", "window", ".", "cgiDataNew", "|", "|", "{", "}", ";"]:
                continue
            assignments += 1
            if assignments != 1 or tokens[i + 4] != "{":
                raise ProbeError("ambiguous_server_data")
            depth, output, j = 0, [], i + 4
            while j < len(tokens):
                token = tokens[j]
                after = tokens[j + 1] if j + 1 < len(tokens) else ""
                if token.startswith(('"', "'")):
                    output.append(json.dumps(_unquote(token)))
                elif token == "*" and after == "1":
                    j += 2
                    continue
                elif token == "," and after in ("}", "]"):
                    j += 1
                    continue
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token) and after == ":":
                    output.append(json.dumps(token))
                elif re.match(r"[A-Za-z_$`(]", token) and token not in ("true", "false", "null"):
                    raise ProbeError("nonliteral_server_data")
                else:
                    output.append(token)
                depth += (token == "{") - (token == "}")
                if depth == 0:
                    try:
                        found = json.loads("".join(output))
                    except ValueError:
                        raise ProbeError("invalid_server_literal") from None
                    break
                j += 1
            else:
                raise ProbeError("unclosed_server_literal")
    if not isinstance(found, dict):
        raise ProbeError("missing_server_data")
    return found





def article_snapshot(body, expected_url):
    expected = _article_identity(expected_url)
    data = parse_server_data(body)
    actual = _article_identity(data.get("link"))
    if any(getattr(expected, key) != getattr(actual, key) for key in ("biz", "mid", "idx", "sn")):
        raise ProbeError("article_identity_mismatch")
    ret = (data.get("base_resp") or {}).get("ret")
    if ret not in (0, "0") or isinstance(ret, bool):
        raise ProbeError("article_business_rejected")
    bar = (data.get("user_info") or {}).get("appmsg_bar_data") or {}
    metrics, fields = {}, {}
    for name, raw_name in METRICS.items():
        value = bar.get(raw_name)
        metrics[name] = int(value) if not isinstance(value, bool) and re.fullmatch(r"\d{1,15}", str(value)) else None
        fields["$html.cgiDataNew.user_info.appmsg_bar_data." + raw_name] = {
            "present": raw_name in bar, "raw_value": value if isinstance(value, (str, int)) else None}
    video_id = (data.get("video_page_info") or {}).get("video_id")
    assets = ([{"kind": "video", "id": video_id}] if isinstance(video_id, str)
              and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", video_id) else [])
    # Picture posts carry their body gallery here, with no content_noencode.
    # Share-cover thumbnails and embedded ads are not body-image resources.
    pictures = data.get("picture_page_info_list")
    if isinstance(pictures, list):
        for picture in pictures:
            url = picture.get("cdn_url") if isinstance(picture, dict) else None
            if isinstance(url, str) and url:
                assets.append({"kind": "image", "url": url})
    return {"article_type": str(data.get("type") or ""), "media_assets": assets,
            "biz": actual.biz, "mid": actual.mid, "idx": actual.idx, "sn": actual.sn, "url": actual.canonical_url,
            "title": data.get("title") or "", "author": data.get("author") or "",
            "account_name": data.get("nick_name") or "", "digest": data.get("desc") or "",
            "content_html": data.get("content_noencode") or "", "cover_url": data.get("cdn_url") or "",
            "publish_timestamp": data.get("create_time"), "comment_id": data.get("comment_id") or "",
            "metrics": metrics, "metric_fields": fields, "identity_matches": True, "business_ret": 0,
            "six_metrics_present": all(value is not None for value in metrics.values()),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "captured_at": datetime.now(timezone.utc).isoformat()}


def export_samples(samples, output_dir):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for article in samples:
        parts = _article_identity(article["url"])
        parsed = parse_article_html('<div id="js_content">' + article["content_html"] + '</div>', parts.canonical_url)
        if parsed is None:
            raise ProbeError("empty_article_content")
        folder = out / "附件" / f"{parts.mid}_{parts.idx}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "正文.txt").write_text(parsed.content_text, encoding="utf-8")
        csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
        html = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + escape(csp, quote=True) + '"><h1>' + escape(article["title"]) + '</h1>' + article["content_html"]
        (folder / "正文.html").write_text(html, encoding="utf-8")
        row = {**article, **article["metrics"], "canonical_url": parts.canonical_url,
               "content": parsed.content_text, "content_text": parsed.content_text,
               "media_assets": parsed.media_assets, "image_urls": parsed.image_urls,
               "image_count": parsed.image_count, "audio_count": parsed.audio_count, "video_count": parsed.video_count,
               "local_html": (folder / "正文.html").relative_to(out).as_posix(),
               "local_text": (folder / "正文.txt").relative_to(out).as_posix(),
               "metrics_status": "complete" if article["six_metrics_present"] else "partial",
               "metrics_provider": "background_client_session_html", "metrics_captured_at": article.get("captured_at"),
               "content_origin": "cgiDataNew.content_noencode", "media_status": "not_downloaded"}
        timestamp = article.get("publish_timestamp")
        row["publish_time"] = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat() if str(timestamp).isdigit() else None
        rows.append(row)
        export_articles_csv(rows, str(out / "文章数据.csv"), default_export_headers(rows))
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--sample-count", type=int, default=3)
    args = parser.parse_args()
    if not 0 <= args.sample_count <= 10:
        parser.error("sample-count must be 0..10")
    template = read_home_request(args.request_file.read_text(encoding="utf-8-sig"))
    args.output.mkdir(parents=True, exist_ok=True)
    samples = []
    with requests.Session() as session:
        session.trust_env = False
        catalog = collect_catalog(session, template, max_pages=args.max_pages,
                                  on_page=lambda value: _atomic_json(args.output / "catalog_progress.json", value))
        _atomic_json(args.output / "catalog_result.json", catalog)
        export_articles_csv(catalog["articles"], str(args.output / "历史目录.csv"), default_export_headers(catalog["articles"]))
        for item in catalog["articles"][:args.sample_count]:
            time.sleep(1.6)
            try:
                response = session.get(item["url"], headers=template.headers, timeout=(10, 20), allow_redirects=False)
                if response.status_code != 200:
                    raise ProbeError("article_http_rejected")
                sample = article_snapshot(response.text, item["url"])
                samples.append(sample)
                _atomic_json(args.output / "article_samples.json", {"schema_version": 1, "articles": samples})
                export_samples(samples, args.output / "交付样本")
            except (requests.RequestException, ProbeError):
                _atomic_json(args.output / "article_stop.json", {"reason": "article_response_rejected", "url": item["url"]})
                break
    print(json.dumps({"catalog_articles": len(catalog["articles"]), "pagination_complete": catalog["pagination_complete"],
                      "catalog_complete": False, "article_samples": len(samples), "stop_reason": catalog["stop_reason"]}))


if __name__ == "__main__":
    main()