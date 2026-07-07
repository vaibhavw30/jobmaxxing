import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobmaxxing.scheduling.nightly import RunResult, format_summary, _log_result, _prune_logs


def _r(name, status, exit_code=0):
    return RunResult(name=name, status=status, exit_code=exit_code, duration_s=1.0, output=f"{name} out")


def test_summary_all_ok_with_count():
    results = [_r("discover_jobspy", "ok"), _r("enrich_workday", "ok")]
    title, body = format_summary(results, 37)
    assert title == "jobmaxxing nightly ✓"
    assert "2/2 workers ok" in body
    assert "37 new postings" in body


def test_summary_marks_failure_and_names_it():
    results = [_r("discover_jobspy", "ok"), _r("recover_jd", "failed", exit_code=3)]
    title, body = format_summary(results, 5)
    assert title == "jobmaxxing nightly ✗"
    assert "1/2 ok" in body
    assert "recover_jd failed" in body
    assert "5 new postings" in body


def test_summary_omits_count_when_none():
    title, body = format_summary([_r("verify_url", "ok")], None)
    assert "new postings" not in body
    assert "1/1 workers ok" in body


def test_summary_timeout_is_a_failure():
    title, body = format_summary([_r("enrich_workday", "timeout", exit_code=None)], 0)
    assert title.endswith("✗")
    assert "enrich_workday timeout" in body


def test_log_result_writes_header_and_output(tmp_path):
    log = tmp_path / "run.log"
    _log_result(log, RunResult(name="discover_jobspy", status="ok", exit_code=0,
                               duration_s=2.5, output="scraped 42\n"))
    text = log.read_text()
    assert "discover_jobspy: ok" in text
    assert "exit=0" in text
    assert "scraped 42" in text


def test_log_result_none_is_noop():
    _log_result(None, RunResult(name="x", status="ok", exit_code=0, duration_s=0.0, output=""))  # no raise


def test_prune_removes_old_keeps_new(tmp_path):
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = tmp_path / "nightly-20260601-000000.log"
    new = tmp_path / "nightly-20260706-000000.log"
    other = tmp_path / "keep-me.txt"
    for p in (old, new, other):
        p.write_text("x")
    old_ts = (now - timedelta(days=20)).timestamp()
    new_ts = (now - timedelta(days=1)).timestamp()
    os.utime(old, (old_ts, old_ts))
    os.utime(new, (new_ts, new_ts))
    _prune_logs(tmp_path, now, keep_days=14)
    assert not old.exists()          # 20 days > 14 → pruned
    assert new.exists()              # 1 day → kept
    assert other.exists()            # non nightly-*.log untouched
