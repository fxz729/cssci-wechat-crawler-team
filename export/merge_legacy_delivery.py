"""One-time merge of legacy session_production delivery rows.

Legacy rows are written first, MSAA bundle rows follow. Duplicate identities
prefer the newer MSAA values while retaining legacy-only fields. Attachment
paths are converted to absolute paths under the unified delivery/附件 folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime

from export.msaa_global_csv import _header_union, article_key


try:
    csv.field_size_limit(2**31 - 1)
except OverflowError:
    pass


PATH_FIELDS = (
    "local_html",
    "local_text",
    "local_article_dir",
    "local_metadata",
    "images_folder",
    "media_manifest",
    "local_readable_html",
    "capture_evidence",
)
LIST_PATH_FIELDS = (
    "media_local_paths",
    "image_local_paths",
    "audio_local_paths",
    "video_local_paths",
)


def _clean_relative(value) -> str:
    return str(value or "").strip().replace("\\", "/")


def _legacy_absolute(value, unified_root: Path) -> str:
    text = _clean_relative(value)
    if not text or Path(text).is_absolute():
        return text
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("附件/"):
        text = text[3:]
    return str((unified_root / "旧数据" / text).resolve())


def _new_absolute(value, unified_root: Path) -> str:
    text = _clean_relative(value)
    if not text or Path(text).is_absolute():
        return text
    if text.startswith("./"):
        text = text[2:]
    for prefix in ("../journals/", "journals/"):
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        parts = rest.split("/", 1)
        if not parts or not parts[0]:
            break
        journal = parts[0]
        relative = parts[1] if len(parts) > 1 else ""
        if relative.startswith("bundle/"):
            relative = relative[len("bundle/"):]
        return str((unified_root / "新数据" / journal / relative).resolve())
    return str((unified_root / "新数据" / text).resolve())


def _rewrite_paths(row: dict, legacy: bool, unified_root: Path) -> dict:
    result = dict(row)
    convert = _legacy_absolute if legacy else _new_absolute
    for field in PATH_FIELDS:
        if field in result:
            result[field] = convert(result.get(field), unified_root)
    for field in LIST_PATH_FIELDS:
        if field not in result:
            continue
        raw = result.get(field) or ""
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            result[field] = json.dumps(
                [convert(value, unified_root) for value in values],
                ensure_ascii=False,
            )
    return result


def _merge_row(legacy_row: dict, new_row: dict, headers: list[str]) -> dict:
    merged = dict(legacy_row)
    for name, value in new_row.items():
        if str(value or "") != "":
            merged[name] = value
    return {name: merged.get(name, "") for name in headers}


def merge_legacy_delivery(
    legacy_csv: Path,
    delivery_csv: Path,
    *,
    unified_root: Path | None = None,
) -> dict:
    legacy_csv = Path(legacy_csv).resolve()
    delivery_csv = Path(delivery_csv).resolve()
    unified_root = (
        Path(unified_root).resolve()
        if unified_root is not None
        else delivery_csv.parent / "附件"
    )
    if not legacy_csv.is_file():
        raise FileNotFoundError(legacy_csv)
    if not delivery_csv.is_file():
        raise FileNotFoundError(delivery_csv)
    if not unified_root.is_dir():
        raise FileNotFoundError(unified_root)

    headers = _header_union([legacy_csv, delivery_csv])

    legacy_keys: set[tuple[str, ...]] = set()
    with legacy_csv.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, strict=True):
            legacy_keys.add(article_key(row))

    overlap: dict[tuple[str, ...], dict] = {}
    new_keys: set[tuple[str, ...]] = set()
    with delivery_csv.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, strict=True):
            key = article_key(row)
            new_keys.add(key)
            if key in legacy_keys and key not in overlap:
                overlap[key] = row

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = delivery_csv.with_name(delivery_csv.stem + f".pre_unified_merge_{stamp}" + delivery_csv.suffix)
    shutil.copy2(delivery_csv, backup)

    temporary = None
    seen: set[tuple[str, ...]] = set()
    legacy_count = 0
    new_count = 0
    merged_count = 0
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8-sig", newline="", dir=delivery_csv.parent,
            prefix="." + delivery_csv.name, suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()

            with legacy_csv.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source, strict=True):
                    key = article_key(row)
                    if key in seen:
                        continue
                    old_row = _rewrite_paths(row, True, unified_root)
                    if key in overlap:
                        new_row = _rewrite_paths(overlap[key], False, unified_root)
                        writer.writerow(_merge_row(old_row, new_row, headers))
                        merged_count += 1
                    else:
                        writer.writerow({name: old_row.get(name, "") for name in headers})
                        legacy_count += 1
                    seen.add(key)

            with delivery_csv.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source, strict=True):
                    key = article_key(row)
                    if key in seen:
                        continue
                    new_row = _rewrite_paths(row, False, unified_root)
                    writer.writerow({name: new_row.get(name, "") for name in headers})
                    seen.add(key)
                    new_count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, delivery_csv)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()

    report = {
        "legacy_rows": len(legacy_keys),
        "new_rows": len(new_keys),
        "overlap_rows": len(overlap),
        "merged_overlap_rows": merged_count,
        "legacy_only_rows": legacy_count,
        "new_only_rows": new_count,
        "total_rows": legacy_count + merged_count + new_count,
        "column_count": len(headers),
        "backup": str(backup),
        "unified_attachments": str(unified_root),
        "delivery_csv": str(delivery_csv),
    }
    (delivery_csv.parent / "delivery_legacy_merge_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--unified-root", type=Path)
    args = parser.parse_args(argv)
    report = merge_legacy_delivery(
        args.legacy,
        args.delivery,
        unified_root=args.unified_root,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
