"""Rewrite delivery CSV absolute attachment paths as paths relative to the CSV."""
from __future__ import annotations
import argparse, csv, json, os, sys
from pathlib import Path
csv.field_size_limit(sys.maxsize)

PATH_FIELDS = {"local_html", "local_text", "local_metadata", "local_article_dir", "local_readable_html", "images_folder", "copyright_path"}
LIST_PATH_FIELDS = {"media_local_paths", "image_local_paths", "audio_local_paths", "video_local_paths"}

def rel(value: str, delivery_root: Path) -> str:
    value = str(value or "")
    if not value:
        return value
    normalized = value.replace("\\", "/")
    marker = "/output/rollout/full_126/delivery/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    if not Path(value).is_absolute():
        return value
    parts = list(Path(value).parts)
    for index, part in enumerate(parts):
        if str(part).lower() == "delivery" and index + 1 < len(parts):
            return Path(*parts[index + 1:]).as_posix()
    try:
        return Path(os.path.relpath(Path(value), delivery_root)).as_posix()
    except ValueError:
        return value

def rewrite(input_csv: Path, output_csv: Path, delivery_root: Path) -> dict:
    with input_csv.open(encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        headers = list(reader.fieldnames or [])
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        changed = 0
        with output_csv.open("w", encoding="utf-8-sig", newline="") as dst:
            writer = csv.DictWriter(dst, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                before = dict(row)
                for field in headers:
                    if field in PATH_FIELDS or field.startswith("local_"):
                        row[field] = rel(row.get(field, ""), delivery_root)
                    elif field in LIST_PATH_FIELDS:
                        try:
                            values = json.loads(row.get(field) or "[]")
                        except (TypeError, json.JSONDecodeError):
                            values = None
                        if isinstance(values, list):
                            row[field] = json.dumps([rel(v, delivery_root) for v in values], ensure_ascii=False)
                writer.writerow(row)
                count += 1
                changed += before != row
    return {"input": str(input_csv), "output": str(output_csv), "rows": count, "changed_rows": changed}

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--delivery-root", type=Path, required=True)
    args=p.parse_args(argv)
    print(json.dumps(rewrite(args.input,args.output,args.delivery_root),ensure_ascii=False))
    return 0
if __name__ == "__main__": raise SystemExit(main())
