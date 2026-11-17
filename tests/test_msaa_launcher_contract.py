from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_msaa_launcher_is_one_click_and_uses_name_search_backend():
    path = ROOT / "scripts" / "start_msaa_full_run.ps1"
    assert path.exists()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    assert "[int]$MediaWorkers = 4" in text
    for phrase in (
        "crawl.msaa_roster_runner",
        "--run-dir",
        "--start",
        "--seed-dir",
        "capture_control.json",
        "Fiddler.exe",
        "export.msaa_global_csv",
        "prepare_skipped_retries.py",
        "import_cached_msaa.py",
        "--preflight-only",
        "PreflightOnly",
        "--refresh-article",
        "RefreshArticle",
    ):
        assert phrase in text
    assert text.index("prepare_skipped_retries.py") < text.index("crawl.msaa_roster_runner")
    for forbidden in ("pass_ticket", "appmsg_token", "wxuin", "Authorization"):
        assert forbidden not in text


def test_msaa_capture_retries_missing_article_before_failing_journal():
    path = ROOT / "scripts" / "run_wechat_msaa_capture.ps1"
    text = path.read_text(encoding="utf-8-sig")
    assert "[int]$CaptureRetries = 4" in text
    assert "capture_missing_retry_target_lost" in text
    assert "Close-Browser" in text


def test_msaa_capture_writes_state_with_shared_read_write_retries():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "function Set-SharedUtf8Text" in text
    assert "Set-SharedUtf8Text $statePath" in text
    assert "Set-Content -LiteralPath $statePath" not in text


def test_msaa_capture_appends_article_log_with_shared_read_write():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "function Add-SharedUtf8Line" in text
    assert "[System.IO.FileShare]::ReadWrite" in text
    assert "Add-SharedUtf8Line $outputPath" in text
    assert "Add-Content -LiteralPath $outputPath" not in text


def test_msaa_capture_persists_incomplete_metrics_instead_of_dropping_article():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "metrics_incomplete" in text
    assert 'throw "metrics_missing:' not in text


def test_production_launcher_chains_capture_retry_backfill_and_final_export():
    path = ROOT / "scripts" / "start_full_126_production.ps1"
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "start_msaa_full_run.ps1" in text
    assert "auto_retry_full_run.ps1" in text
    assert "backfill_msaa_queue.ps1" in text
    assert "prepare_skipped_retries.py" in text
    assert "--chinese-header-copy" in text
    assert "run_control.json" in text
    assert "exit 130" in text
    assert "[switch]$PlanOnly" in text
    assert "production_plan" in text
    assert "production_final_status" in text
    assert "$remaining" in text
    assert "--dry-run" in text
    assert "article_retry_count" in text
    assert "backfill_plan" in text


def test_auto_retry_can_start_after_initial_runner_has_already_exited():
    text = (ROOT / "scripts" / "auto_retry_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "if ($RunnerPid -gt 0)" in text
    assert "initial_runner_already_exited" in text


def test_msaa_capture_retries_incomplete_metrics_and_queues_persistent_gaps():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "metrics_incomplete_retry" in text
    assert "$pendingAttempts -le $CaptureRetries" in text
    assert "metrics_retry_queue.jsonl" in text
    assert "metrics_incomplete_after_retries" in text
    assert "Add-SharedUtf8Line $metricsRetryQueuePath" in text


def test_msaa_capture_does_not_rescan_confirmed_card_subtrees():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    marker = "$cards.Add([pscustomobject]@{"
    start = text.index(marker)
    assert "\n                            return\n" in text[start:start + 600]


def test_msaa_capture_recovers_matching_metrics_after_article_pairing():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "function Read-MetricsFromOffset" in text
    assert "$captureOffset = $lastOffset" in text
    assert "$capture.Metrics = Read-MetricsFromOffset $CaptureInbox $captureOffset $capture.Article" in text
    assert "'$html.appmsg_bar_data.'" in text
    assert "'$html.cgiData.'" in text


def test_msaa_capture_skips_unopenable_card_instead_of_aborting_journal():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "browser_window_missing_skip" in text
    assert "throw 'browser_window_missing'" not in text
    assert "$lastOffset = (Get-Item -LiteralPath $CaptureInbox).Length" in text


def test_msaa_capture_skips_persistent_single_article_failure():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "skipped_titles" in text
    assert "capture_missing_skip" in text
    assert "status = 'skipped'" in text


def test_msaa_card_visibility_handles_partially_visible_and_tall_cards():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "$fullyVisible = ([int]$card.Top -ge [int]$profileState.ProfileTop) -and ([int]$card.Bottom -le [int]$profileState.ProfileBottom)" in text
    assert "$tallerThanView = $cardHeight -gt $viewHeight" in text
    assert "if ((-not $fullyVisible) -and (-not $tallerThanView))" in text
    assert "$clickY = if ($fullyVisible) { [int]$card.CenterY } else { [int](($clickTop + $clickBottom) / 2) }" in text


def test_msaa_capture_reuses_card_cache_and_periodically_rescans_after_closing_article():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "Close-Browser $browser" in text
    assert "$cacheHits++" in text
    assert "if ($cacheHits -ge 5) { $profileState = $null; $cacheHits = 0 }" in text


def test_msaa_card_scanner_requires_article_card_height():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "[int]$MinCardHeight = 40" in text
    assert "$location.Height -ge $MinCardHeight" in text


def test_msaa_card_scanner_ignores_expand_remaining_control():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "余下\\s*\\d+\\s*篇" in text
    assert "查看全部" in text


def test_msaa_runner_uses_name_search_not_profile_url_navigation():
    text = (ROOT / "crawl" / "msaa_roster_runner.py").read_text(encoding="utf-8")
    assert "profile_switch.ps1" in text
    assert "-Account" in text
    assert "H5SubscriptionProfileWnd" in text
    assert "open_journal_home.ps1" not in text
    assert "ProfileUrl" not in text


def test_msaa_importer_reuses_existing_bundle_and_csv_contract():
    text = (ROOT / "crawl" / "msaa_capture.py").read_text(encoding="utf-8")
    for phrase in (
        "write_journal_csv_bundle",
        "fill_missing_identity",
        "fill_missing_metrics",
        "persist",
    ):
        assert phrase in text


def test_msaa_capture_resumes_from_last_processed_anchor():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    for phrase in (
        "last_processed_title",
        "processed_count",
        "Restore-ProfileAnchor",
        "resume_anchor_searching",
        "resume_anchor_fallback_top",
        "Get-Content -LiteralPath $outputPath -Tail 1",
    ):
        assert phrase in text


def test_msaa_capture_persists_resume_cursor_after_each_article():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "$state.last_processed_title = $card.Title" in text
    assert "$state.processed_count = $processed.Count" in text


def test_profile_switch_for_msaa_validates_target_account_name():
    text = (ROOT / "crawl" / "profile_switch.ps1").read_text(encoding="utf-8-sig")
    assert "$ForMsaa" in text
    assert "Get-H5ProfileName" in text
    assert "showName=" in text
    assert "profile_name_timeout" in text


def test_open_search_window_uses_current_wechat_main_window_and_search_root():
    path = ROOT / "crawl" / "open_search_window.ps1"
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    for phrase in (
        "WeChatMainWndForPC",
        "搜一搜",
        "sidebar_offset",
        "SetCursorPos",
        "mouse_event",
    ):
        assert phrase in text
    runner = (ROOT / "crawl" / "msaa_roster_runner.py").read_text(encoding="utf-8")
    assert "open_search_window.ps1" in runner


def test_status_report_includes_pending_skip_retry_journals():
    text = (ROOT / "scripts" / "show_full_run_status.ps1").read_text(encoding="utf-8-sig")
    assert "pending_skip_retry" in text
    assert "last_processed_title" in text


def test_msaa_launcher_refreshes_cached_articles_by_default():
    text = (ROOT / "scripts" / "start_msaa_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "[switch]$RefreshArticle = $true" in text


def test_independent_backfill_starts_each_journal_from_top():
    launcher = (ROOT / "scripts" / "start_msaa_full_run.ps1").read_text(encoding="utf-8-sig")
    runner = (ROOT / "crawl" / "msaa_roster_runner.py").read_text(encoding="utf-8")
    capture = (ROOT / "crawl" / "msaa_capture.py").read_text(encoding="utf-8")
    capture_ps = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "--start-from-top" in launcher
    assert '"--start-from-top"' in runner
    assert "start_from_top" in capture
    assert "-StartFromTop" in capture
    assert "[switch]$StartFromTop" in capture_ps
    assert "-not $StartFromTop" in capture_ps


def test_msaa_capture_reuses_card_cache_when_skipping_processed_cards():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "# 跳过已处理卡片时复用缓存坐标" in text
    assert "$skipScanScrolls -ge 20" in text


def test_auto_retry_uses_project_launcher_and_skips_completed_with_skips():
    text = (ROOT / "scripts" / "auto_retry_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "Join-Path $PSScriptRoot 'start_msaa_full_run.ps1'" in text
    assert "'completed', 'completed_with_skips'" in text
    assert "msaa_skipped" not in text
    assert "'-WaitSeconds', '45'" in text
    assert "'-RefreshArticle'" in text


def test_msaa_capture_treats_card_growth_as_progress():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "$lastCardCount = -1" in text
    assert "$profileState.Cards.Count -ne $lastCardCount" in text
    assert "目录卡片数变化说明微信仍在加载历史文章" in text


def test_msaa_capture_selects_article_history_tab_and_uses_fast_skip():
    text = (ROOT / "scripts" / "run_wechat_msaa_capture.ps1").read_text(encoding="utf-8-sig")
    assert "function Select-ArticleHistoryTab" in text
    assert "article_tab_selected" in text
    assert "$skipWheelDelta = [int]$ScrollDelta * 3" in text
    assert "$skipScanScrolls -ge 20" in text


def test_backfill_queue_wrapper_forces_one_journal_without_main_queue_blocking():
    path = ROOT / "scripts" / "backfill_msaa_queue.ps1"
    text = path.read_text(encoding="utf-8-sig")
    assert "journal_backfill_queue.jsonl" in text
    assert "'-Backfill'" in text
    assert "'-MaxJournals', '1'" in text
    assert "backfill_results.jsonl" in text
    assert "backfill_progress.json" in text
    assert "[switch]$PlanOnly" in text
    assert "backfill_plan" in text


def test_auto_retry_prefers_never_attempted_pending_journals():
    text = (ROOT / "scripts" / "auto_retry_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "$freshPending = @($pending | Where-Object { $_.status -eq 'pending' })" in text
    assert "$startCandidates = if ($freshPending.Count -gt 0) { $freshPending } else { $pending }" in text


def test_independent_backfill_is_excluded_from_bulk_retry():
    text = (ROOT / "scripts" / "auto_retry_full_run.ps1").read_text(encoding="utf-8-sig")
    assert "'needs_backfill'" in text
    runner = (ROOT / "crawl" / "msaa_roster_runner.py").read_text(encoding="utf-8")
    assert "capture_stalled_no_articles:" in runner
    assert "requires_independent_backfill" in runner

def test_msaa_launcher_merges_existing_bundles_before_capture():
    text = (ROOT / "scripts" / "start_msaa_full_run.ps1").read_text(encoding="utf-8-sig")
    prepare_pos = text.index("prepare_skipped_retries.py")
    merge_pos = text.index("export.msaa_global_csv")
    runner_pos = text.index("crawl.msaa_roster_runner")
    assert prepare_pos < merge_pos < runner_pos
