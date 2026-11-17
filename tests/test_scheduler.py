from scheduler.jobs import format_dry_run_jobs


def test_scheduler_dry_run_lists_expected_jobs():
    output = format_dry_run_jobs()

    assert "crawl_confirmed_accounts" in output
    assert "retry_failed_articles" in output
    assert "backfill_authorized_metrics" in output
