from pathlib import Path
from urllib.parse import urlparse
import hashlib
import requests

from export.csv_exporter import export_articles_csv
from export.json_exporter import export_articles_json


class RealtimeExporter:
    def __init__(self, repo, output_dir: Path, formats: list[str] | None = None, image_fetcher=None):
        self.repo = repo
        self.output_dir = Path(output_dir)
        self.formats = formats or ["csv", "json"]
        self.image_fetcher = image_fetcher or _fetch_image

    def update(self, account_name: str | None = None, *, download_images: bool = False) -> list[Path]:
        rows = self.repo.list_articles_for_export(account_name=account_name)
        basename = account_name or "cssci_wechat_articles"
        if download_images:
            images_dir = self.output_dir / "images" / basename
            images_dir.mkdir(parents=True, exist_ok=True)
            self._download_images(rows, images_dir)
        export_rows = [clean_export_row(row) for row in rows]
        written: list[Path] = []
        if "csv" in self.formats:
            csv_dir = self.output_dir / "csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            csv_path = csv_dir / f"{basename}.csv"
            export_articles_csv(export_rows, str(csv_path), default_export_headers(export_rows))
            written.append(csv_path)
        if "json" in self.formats:
            json_dir = self.output_dir / "json"
            json_dir.mkdir(parents=True, exist_ok=True)
            json_path = json_dir / f"{basename}.json"
            export_articles_json(export_rows, str(json_path))
            written.append(json_path)
        return written

    def _download_images(self, rows: list[dict], images_dir: Path) -> None:
        for row in rows:
            prefix = _safe_name(str(row.get("title") or row.get("mid") or "article"))[:40]
            for index, url in enumerate(row.get("_image_urls_for_download", []), start=1):
                suffix = _image_suffix(url)
                filename = f"{prefix}_{index}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}{suffix}"
                path = images_dir / filename
                if path.exists():
                    continue
                try:
                    path.write_bytes(self.image_fetcher(url))
                except Exception:
                    continue


def clean_export_row(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def default_export_headers(rows: list[dict]) -> list[str]:
    preferred = [
        "account_name", "author", "publish_time", "title", "title_length",
        "digest", "digest_length", "comment_id", "content", "word_count",
        "image_count", "images_folder", "video_count", "audio_count",
        "read_count", "watching_count", "like_count", "favorite_count",
        "share_count", "comment_count", "collect_num", "position_in_batch",
        "copyright_text", "biz", "mid", "idx", "sn", "batch_key",
        "source_url", "canonical_url", "req_id", "profile_ext_source",
        "is_original", "copyright_stat", "has_tts", "album_id", "album_name",
        "quarter", "is_weekday",
        "profile_url", "cover_image_url", "content_html", "image_urls", "media_assets",
        "field_provenance", "metrics_provider", "metrics_captured_at", "metrics_status",
        "metric_field_status", "crawl_status",
    ]
    removed = {"hour", "month", "reward_count", "weekday", "year"}
    keys = {key for row in rows for key in row.keys()} - removed
    return preferred + sorted(keys - set(preferred))


def _fetch_image(url: str) -> bytes:
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.content


def _image_suffix(url: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in [".jpg", ".jpeg", ".png", ".webp"]:
        if path.endswith(suffix):
            return suffix
    return ".jpg"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value).strip("_") or "article"
