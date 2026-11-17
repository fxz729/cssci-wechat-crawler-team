from scheduler.jobs import format_dry_run_jobs


def start_daemon(dry_run: bool = False) -> int:
    if dry_run:
        print(format_dry_run_jobs())
        return 0
    raise NotImplementedError("守护调度会在核心抓取链路稳定后启用；请先使用 dry-run 查看任务。")
