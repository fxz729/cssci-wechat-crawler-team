"""Merge MSAA per-journal bundle CSVs into one cross-journal CSV.

The exporter is append-only after the first merge: rows already present in the
delivery CSV are preserved, and only new unique bundle rows are appended.
New bundle attachments are mirrored into delivery/附件/新数据 and exported with
absolute paths.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


PATH_FIELDS = (
    "local_html",
    "local_text",
    "local_article_dir",
    "capture_evidence",
    "local_readable_html",
    "images_folder",
)
LIST_PATH_FIELDS = (
    "media_local_paths",
    "image_local_paths",
    "audio_local_paths",
    "video_local_paths",
)


CHINESE_HEADER_LABELS = {
    "account_name": "公众号名称",
    "author": "作者",
    "publish_time": "发布时间",
    "title": "文章标题",
    "title_length": "标题长度",
    "digest": "摘要",
    "digest_length": "摘要长度",
    "comment_id": "评论ID",
    "content": "正文文本",
    "word_count": "正文字数",
    "image_count": "图片数",
    "images_folder": "图片文件夹",
    "video_count": "视频数",
    "audio_count": "音频数",
    "read_count": "阅读数",
    "watching_count": "在看数",
    "like_count": "点赞数",
    "favorite_count": "收藏数",
    "share_count": "分享数",
    "comment_count": "评论数",
    "collect_num": "原始收藏数",
    "position_in_batch": "批次位置",
    "copyright_text": "版权文本",
    "biz": "biz",
    "mid": "mid",
    "idx": "idx",
    "sn": "sn",
    "batch_key": "批次键",
    "source_url": "来源URL",
    "canonical_url": "微信原文URL",
    "req_id": "请求ID",
    "profile_ext_source": "历史目录来源",
    "is_original": "是否原创",
    "copyright_stat": "版权状态",
    "has_tts": "是否有语音",
    "album_id": "合集ID",
    "album_name": "合集名称",
    "quarter": "季度",
    "is_weekday": "是否工作日",
    "profile_url": "公众号主页链接",
    "cover_image_url": "封面图URL",
    "content_html": "正文HTML",
    "image_urls": "图片URL列表",
    "media_assets": "媒体资源",
    "field_provenance": "字段来源",
    "metrics_provider": "指标来源",
    "metrics_captured_at": "指标采集时间",
    "metrics_status": "指标状态",
    "metric_field_status": "指标字段状态",
    "crawl_status": "抓取状态",
    "url": "URL",
    "content_text": "正文纯文本",
    "content_origin": "正文来源",
    "publish_timestamp": "发布时间戳",
    "cover_url": "封面图链接",
    "local_html": "正文HTML本地路径",
    "local_text": "正文文本本地路径",
    "local_metadata": "本地元数据路径",
    "local_article_dir": "文章文件夹",
    "metric_fields": "指标字段",
    "identity_matches": "身份核验",
    "business_ret": "业务返回码",
    "six_metrics_present": "六项指标齐全",
    "body_sha256": "正文字节SHA256",
    "captured_at": "采集时间",
    "media_urls": "媒体URL列表",
    "media_manifest": "媒体清单路径",
    "media_status": "媒体状态",
    "media_downloaded": "已下载媒体数",
    "media_pending": "待处理媒体数",
    "media_local_paths": "媒体本地路径列表",
    "image_local_paths": "图片本地路径列表",
    "audio_local_paths": "音频本地路径列表",
    "video_local_paths": "视频本地路径列表",
    "journal_name": "期刊名称",
    "journal_id": "期刊编号",
    "article_key": "文章唯一键",
    "has_text": "有正文",
    "has_images": "有图片",
    "has_audio": "有音频",
    "has_video": "有视频",
    "media_index": "媒体索引",
    "article_url": "文章URL",
    "content_fragment_html": "正文片段HTML",
    "local_readable_html": "可读正文HTML本地路径",
    "capture_evidence": "采集证据",
    "media_manifest_status": "媒体清单状态",
    "http_status": "HTTP状态",
    "fiddler_session_id": "Fiddler会话",
    "article_event_id": "文章事件ID",
    "metric_event_id": "指标事件ID",
}


def _output_path(run_dir: Path) -> Path:
    return Path(run_dir) / "delivery" / "文章数据.csv"


def _chinese_headers(headers: list[str]) -> list[str]:
    labels = []
    used = set()
    for name in headers:
        base = CHINESE_HEADER_LABELS.get(name)
        label = f"{base} ({name})" if base else name
        candidate = label
        suffix = 2
        while candidate in used:
            candidate = f"{label}_{suffix}"
            suffix += 1
        used.add(candidate)
        labels.append(candidate)
    return labels


def _write_chinese_header_copy(source: Path, destination: Path) -> Path:
    temporary = None
    try:
        with source.open(encoding="utf-8-sig", newline="") as source_stream, tempfile.NamedTemporaryFile(
            "w", encoding="utf-8-sig", newline="", dir=destination.parent,
            prefix="." + destination.name, suffix=".tmp", delete=False,
        ) as target_stream:
            temporary = Path(target_stream.name)
            reader = csv.reader(source_stream, strict=True)
            writer = csv.writer(target_stream)
            try:
                source_headers = next(reader)
            except StopIteration:
                source_headers = []
            writer.writerow(_chinese_headers(source_headers))
            width = len(source_headers)
            for row in reader:
                if len(row) < width:
                    row += [""] * (width - len(row))
                writer.writerow(row)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def _prefix(value: str, prefix: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    return prefix + value.replace("\\", "/")


def _identity_text(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def article_key(row: dict) -> tuple[str, ...]:
    """Return a stable key for deduplicating legacy and MSAA rows."""
    biz = str(row.get("biz") or "").strip()
    mid = _identity_text(row.get("mid"))
    idx = _identity_text(row.get("idx"))
    if mid and idx:
        return ("id", biz, mid, idx)
    sn = str(row.get("sn") or "").strip()
    if sn:
        return ("sn", biz, sn)
    for field in ("canonical_url", "article_url", "source_url", "url"):
        candidate = str(row.get(field) or "").strip()
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            continue
        url_mid = _identity_text(query.get("mid", [""])[0])
        url_idx = _identity_text(query.get("idx", [""])[0])
        url_sn = str(query.get("sn", [""])[0] or "").strip()
        url_biz = str(query.get("__biz", [biz])[0] or biz).strip()
        if url_mid and url_idx:
            return ("url_id", url_biz, url_mid, url_idx)
        if url_sn:
            return ("url_sn", url_biz, url_sn)
    journal = str(row.get("journal_name") or row.get("account_name") or "").strip()
    title = str(row.get("title") or "").strip()
    publish_time = str(row.get("publish_time") or "").strip()
    return ("title", journal, title, publish_time)


def _header_union(csv_paths: list[Path]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for path in csv_paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            try:
                current = next(reader)
            except StopIteration:
                continue
        for name in current:
            if name and name not in seen:
                seen.add(name)
                headers.append(name)
    for name in ("journal_id", "journal_name"):
        if name not in seen:
            seen.add(name)
            headers.append(name)
    return headers


def _rewrite_row(source_row: dict, entry: dict, prefix: str, headers: list[str]) -> dict:
    row = dict(source_row)
    row["journal_id"] = f"{int(entry['ordinal']):03d}"
    row["journal_name"] = entry.get("journal_name") or entry.get("account_name") or ""
    for field in PATH_FIELDS:
        if field in row:
            row[field] = _prefix(row[field], prefix)
    for field in LIST_PATH_FIELDS:
        if field not in row:
            continue
        try:
            values = json.loads(row[field] or "[]")
        except json.JSONDecodeError:
            values = []
        if isinstance(values, list):
            row[field] = json.dumps(
                [_prefix(value, prefix) for value in values],
                ensure_ascii=False,
            )
    return {name: row.get(name, "") for name in headers}


def _absolute_unified_path(value, unified_root: Path, journal_dir: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or Path(text).is_absolute():
        return text
    prefixes = ("../journals/", "journals/")
    for prefix in prefixes:
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        parts = rest.split("/", 1)
        if len(parts) < 2:
            break
        journal = parts[0]
        relative = parts[1]
        if relative.startswith("bundle/"):
            relative = relative[len("bundle/"):]
        return str((unified_root / "新数据" / journal / relative).resolve())
    return str((unified_root / "新数据" / journal_dir / text).resolve())


def _bundle_relative(value: str, bundle: Path) -> str:
    text = str(value or "").strip()
    if not text or not Path(text).is_absolute():
        return text
    try:
        return Path(text).resolve().relative_to(bundle.resolve()).as_posix()
    except (ValueError, OSError):
        return text


def _normalize_bundle_row(source_row: dict, bundle: Path) -> dict:
    row = dict(source_row)
    for field in PATH_FIELDS:
        if field in row:
            row[field] = _bundle_relative(row[field], bundle)
    for field in LIST_PATH_FIELDS:
        if field not in row:
            continue
        try:
            values = json.loads(row[field] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            row[field] = json.dumps(
                [_bundle_relative(value, bundle) for value in values],
                ensure_ascii=False,
            )
    return row


def _copy_article_dir(source_row: dict, bundle: Path, journal_dir: str, unified_root: Path) -> None:
    relative = str(source_row.get("local_article_dir") or "").strip().replace("\\", "/")
    if not relative:
        mid = _identity_text(source_row.get("mid"))
        idx = _identity_text(source_row.get("idx")) or "1"
        relative = f"articles/{mid}_{idx}"
    if not relative.startswith("articles/"):
        relative = "articles/" + relative
    source_dir = (bundle / relative).resolve()
    destination = (unified_root / "新数据" / journal_dir / relative).resolve()
    if not source_dir.is_dir() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        shutil.copytree(source_dir, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _rewrite_unified_row(
    source_row: dict,
    entry: dict,
    prefix: str,
    headers: list[str],
    *,
    bundle: Path,
    unified_root: Path,
) -> dict:
    journal_dir = bundle.parent.name
    source_row = _normalize_bundle_row(source_row, bundle)
    _copy_article_dir(source_row, bundle, journal_dir, unified_root)
    row = _rewrite_row(source_row, entry, prefix, headers)
    for field in PATH_FIELDS:
        if field in row:
            row[field] = _absolute_unified_path(row[field], unified_root, journal_dir)
    for field in LIST_PATH_FIELDS:
        if field not in row:
            continue
        try:
            values = json.loads(row[field] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            row[field] = json.dumps(
                [_absolute_unified_path(value, unified_root, journal_dir) for value in values],
                ensure_ascii=False,
            )
    return {name: row.get(name, "") for name in headers}


def _article_media_path_map(article_csv: Path) -> dict[tuple[str, str, str], list[str]]:
    result = {}
    if not article_csv.is_file():
        return result
    with article_csv.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            try:
                values = json.loads(row.get("media_local_paths") or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
            if isinstance(values, list):
                result[(str(row.get("biz") or ""), str(row.get("mid") or ""), str(row.get("idx") or ""))] = [str(v) for v in values]
    return result


def _write_global_media_index(run_dir: Path, output_dir: Path, manifest: dict, unified_root: Path) -> dict:
    paths = sorted(run_dir.glob("journals/*/bundle/媒体索引.csv"))
    output = output_dir / "媒体索引.csv"
    standard = [
        "journal_id", "journal_name", "article_key", "biz", "mid", "idx",
        "article_title", "kind", "role", "resource_index", "asset_index",
        "url", "local_path", "status", "bytes", "sha256", "error_code",
    ]
    headers = _header_union(paths) if paths else []
    headers = [name for name in standard if name not in headers] + headers
    by_dir = {Path(entry["base"]).name: entry for entry in manifest.get("journals", [])}
    article_paths = _article_media_path_map(output_dir / "文章数据.csv")
    temporary = None
    count = 0
    seen = set()
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8-sig", newline="", dir=output_dir,
            prefix="." + output.name, suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            for path in paths:
                journal_dir = path.parent.parent.name
                entry = by_dir.get(journal_dir) or {}
                bundle = path.parent
                with path.open(encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        local_path = _bundle_relative(row.get("local_path", ""), bundle)
                        if local_path and not Path(local_path).is_absolute():
                            local_path = str((unified_root / "新数据" / journal_dir / local_path).resolve())
                        candidate = Path(local_path) if local_path else None
                        if candidate is not None and not candidate.exists():
                            article_key = (str(row.get("biz") or ""), str(row.get("mid") or ""), str(row.get("idx") or ""))
                            alternatives = article_paths.get(article_key) or []
                            try:
                                asset_index = int(row.get("asset_index") or row.get("resource_index") or 0)
                            except (TypeError, ValueError):
                                asset_index = 0
                            if 0 <= asset_index < len(alternatives):
                                local_path = alternatives[asset_index]
                        row["local_path"] = local_path
                        row["journal_id"] = f"{int(entry.get('ordinal') or 0):03d}" if entry else ""
                        row["journal_name"] = entry.get("journal_name") or entry.get("account_name") or ""
                        key = tuple(str(row.get(name) or "") for name in (
                            "biz", "mid", "idx", "kind", "role", "resource_index",
                            "asset_index", "url", "local_path",
                        ))
                        if key in seen:
                            continue
                        seen.add(key)
                        writer.writerow({name: row.get(name, "") for name in headers})
                        count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"media_csv": str(output), "media_count": count}


def merge(
    run_dir: Path,
    output_csv: Path | None = None,
    *,
    write_chinese_copy: bool = False,
) -> dict:
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8-sig"))
    by_base = {Path(entry["base"]).resolve(): entry for entry in manifest["journals"]}
    output = Path(output_csv).resolve() if output_csv else _output_path(run_dir)
    csv_paths = sorted(run_dir.glob("journals/*/bundle/文章数据.csv"))
    output.parent.mkdir(parents=True, exist_ok=True)
    unified_root = output.parent / "附件"
    unified_root.mkdir(parents=True, exist_ok=True)
    header_paths = ([output] if output.exists() else []) + csv_paths
    headers = _header_union(header_paths)
    temporary = None
    journals_with_csv = 0
    existing_count = 0
    appended_count = 0
    seen: set[tuple[str, ...]] = set()
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8-sig", newline="", dir=output.parent,
            prefix="." + output.name, suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()

            if output.exists():
                with output.open(encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source, strict=True)
                    for source_row in reader:
                        key = article_key(source_row)
                        if key in seen:
                            continue
                        writer.writerow({name: source_row.get(name, "") for name in headers})
                        seen.add(key)
                        existing_count += 1

            for csv_path in csv_paths:
                bundle = csv_path.parent
                entry = by_base.get(bundle.parent.resolve())
                if not entry:
                    continue
                prefix = f"../journals/{bundle.parent.name}/bundle/"
                with csv_path.open(encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source, strict=True)
                    for source_row in reader:
                        key = article_key(source_row)
                        if key in seen:
                            continue
                        writer.writerow(
                            _rewrite_unified_row(
                                source_row,
                                entry,
                                prefix,
                                headers,
                                bundle=bundle,
                                unified_root=unified_root,
                            )
                        )
                        seen.add(key)
                        appended_count += 1
                journals_with_csv += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        media_summary = _write_global_media_index(run_dir, output.parent, manifest, unified_root)
        chinese_output = None
        if write_chinese_copy:
            chinese_output = output.with_name("文章数据_中文表头.csv")
            _write_chinese_header_copy(output, chinese_output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {
        "run_dir": str(run_dir),
        "csv": str(output),
        "journal_count": journals_with_csv,
        "article_count": existing_count + appended_count,
        "existing_count": existing_count,
        "appended_count": appended_count,
        "column_count": len(headers),
        "unified_attachments": str(unified_root),
        "media_csv": media_summary["media_csv"],
        "media_count": media_summary["media_count"],
        "chinese_csv": str(chinese_output) if chinese_output is not None else "",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--chinese-header-copy", action="store_true")
    args = parser.parse_args(argv)
    summary = merge(
        args.run_dir,
        args.output,
        write_chinese_copy=args.chinese_header_copy,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["article_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
