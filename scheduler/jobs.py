from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJobInfo:
    name: str
    interval: str
    description: str


def planned_jobs() -> list[ScheduledJobInfo]:
    return [
        ScheduledJobInfo("crawl_confirmed_accounts", "every 30 minutes", "增量抓取已确认公众号"),
        ScheduledJobInfo("retry_failed_articles", "every 2 hours", "重试失败文章"),
        ScheduledJobInfo("backfill_authorized_metrics", "every 6 hours", "回填授权指标快照"),
    ]


def format_dry_run_jobs() -> str:
    lines = ["计划中的调度任务："]
    for job in planned_jobs():
        lines.append(f"- {job.name}: {job.interval}，{job.description}")
    return "\n".join(lines)
