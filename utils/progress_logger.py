import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
import sys

try:
    from loguru import logger as _logger
    _LOGURU = True
except ImportError:
    _LOGURU = False

_FALLBACK_LOG_PATH: Path | None = None


def setup_logging(log_dir: Path | str | None = None) -> None:
    """Configure loguru sinks or the dependency-free fallback file sink."""
    global _FALLBACK_LOG_PATH
    log_dir = Path(log_dir) if log_dir else Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _FALLBACK_LOG_PATH = log_dir / f"crawler_{datetime.now():%Y-%m-%d}.log"
    if not _LOGURU:
        return
    _logger.remove()
    _logger.add(
        sys.stderr,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
    )
    _logger.add(
        log_dir / "crawler_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        encoding="utf-8",
        enqueue=False,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )


class ProgressLogger:
    def __init__(self, stream: TextIO | None = None, event_path: Path | str | None = None):
        self.stream = stream
        self.event_path = Path(event_path) if event_path else None

    def account_started(self, account_name: str, total_results: int) -> None:
        self._emit("INFO", f"开始抓取 | account={account_name} | total={total_results}")

    def article_saved(self, account_name: str, title: str, stored: int, total: int) -> None:
        self._emit("SUCCESS", f"[{stored}/{total}] 已保存 | account={account_name} | title={title}")

    def article_skipped(self, account_name: str, url: str, reason: str) -> None:
        self._emit("DEBUG", f"跳过 | account={account_name} | reason={reason}")

    def article_failed(self, account_name: str, url: str, error: str, retry_count: int) -> None:
        self._emit("WARNING", f"失败 | account={account_name} | retry={retry_count} | error={error}")

    def export_updated(self, account_name: str, paths: list) -> None:
        rendered = ", ".join(str(p) for p in paths)
        self._emit("INFO", f"导出已更新 | account={account_name} | files={rendered}")

    def page_fetched(self, account_name: str, page_num: int, count: int) -> None:
        self._emit("INFO", f"第 {page_num} 页 | account={account_name} | count={count}")

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def history_account_started(self, account_name: str, current: int, total: int) -> None:
        self._emit("INFO", f"[账号 {current}/{total}] 开始抓取 | account={_safe_text(account_name)}")

    def session_preflight_started(
        self,
        account_name: str,
        candidate: int,
        total: int,
        transport: str,
    ) -> None:
        self._emit(
            "INFO",
            f"会话预检 | account={_safe_text(account_name)} | candidate={candidate}/{total} | transport={transport}",
        )

    def session_preflight_finished(self, account_name: str, usable: bool, transport: str = "") -> None:
        status = "usable" if usable else "unavailable"
        suffix = f" | transport={transport}" if transport else ""
        self._emit("INFO", f"会话预检结果 | account={_safe_text(account_name)} | status={status}{suffix}")

    def transport_selected(self, account_name: str, transport: str) -> None:
        self._emit("INFO", f"传输已选择 | account={_safe_text(account_name)} | transport={transport}")

    def transport_fallback(self, account_name: str, source: str, target: str) -> None:
        self._emit("WARNING", f"传输切换 | account={_safe_text(account_name)} | {source} -> {target}")

    def catalog_page_fetched(
        self,
        account_name: str,
        begin: int,
        count: int,
        total: int,
        page_size: int,
    ) -> None:
        page = begin // page_size + 1
        self._emit(
            "INFO",
            f"目录页 | account={_safe_text(account_name)} | page={page} | begin={begin} | count={count} | total={total}",
        )

    def history_article_started(
        self,
        account_name: str,
        current: int,
        total: int,
        title: str,
    ) -> None:
        self._emit(
            "INFO",
            f"[文章 {current}] 开始 | account={_safe_text(account_name)} | 目录总量={total} | title={_safe_text(title)}",
        )

    def history_article_saved(
        self,
        account_name: str,
        current: int,
        total: int,
        title: str,
        content_chars: int,
        image_count: int,
        metrics_count: int,
    ) -> None:
        self._emit(
            "SUCCESS",
            f"[文章 {current}] 已保存 | account={_safe_text(account_name)} | 目录总量={total} | "
            f"title={_safe_text(title)} | 正文字符={content_chars} | 图片={image_count} | 指标={metrics_count}/6",
        )

    def history_article_failed(
        self,
        account_name: str,
        current: int,
        total: int,
        title: str,
        error_type: str,
    ) -> None:
        self._emit(
            "WARNING",
            f"[文章 {current}] 失败 | account={_safe_text(account_name)} | 目录总量={total} | "
            f"title={_safe_text(title)} | error={_safe_text(error_type)}",
        )

    def checkpoint_saved(self, account_name: str, page: int, processed: int, status: str) -> None:
        self._emit(
            "INFO",
            f"checkpoint 已保存 | account={_safe_text(account_name)} | page={page} | processed={processed} | status={status}",
        )

    def history_account_finished(
        self,
        account_name: str,
        processed: int,
        failed: int,
        completed: bool,
    ) -> None:
        status = "completed" if completed else "incomplete"
        self._emit(
            "INFO",
            f"账号汇总 | account={_safe_text(account_name)} | 成功={processed} | 失败={failed} | status={status}",
        )

    def history_finished(self, completed: int, total: int) -> None:
        self._emit("INFO", f"历史抓取汇总 | completed={completed}/{total}")

    def _emit(self, level: str, message: str) -> None:
        message = _safe_text(message, limit=1000)
        timestamp = datetime.now(timezone.utc).isoformat()
        if self.event_path is not None:
            self.event_path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "schema_version": 1,
                "captured_at": timestamp,
                "level": level,
                "message": message,
            }
            with self.event_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
                file.flush()
                try:
                    import os
                    os.fsync(file.fileno())
                except OSError:
                    pass
        if self.stream is not None:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.stream.write(f"{ts} | {message}\n")
            self.stream.flush()
        elif _LOGURU:
            _logger.opt(depth=2).log(level, message)
        else:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} | {level:<8} | {message}\n"
            if _FALLBACK_LOG_PATH is not None:
                with _FALLBACK_LOG_PATH.open("a", encoding="utf-8") as file:
                    file.write(line)
                    file.flush()
            else:
                sys.stdout.write(line)
                sys.stdout.flush()


def _safe_text(value: object, limit: int = 160) -> str:
    rendered = " ".join(str(value or "").split())
    rendered = re.sub(r"https?://\S+", "<url>", rendered, flags=re.IGNORECASE)
    rendered = re.sub(
        r"\b(credential|cookie|token|fakeid|fingerprint|pass_ticket|appmsg_token|slave_sid|wap_sid2|data_ticket)\s*[:=]\s*\S+",
        "<redacted>",
        rendered,
        flags=re.IGNORECASE,
    )
    return rendered[:limit]
