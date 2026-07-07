"""Nightly scheduler — runs the four residential-IP workers sequentially and notifies (local macOS).

Pure/injected core (RunResult, format_summary, _log_result, _prune_logs, run_nightly) is unit-tested.
The default boundaries (_subprocess_runner, _osascript_notifier, _db_delta) + main are thin
side-effecting wrappers. This module imports fine without the headless/discovery extras — it invokes the
workers as subprocesses, never importing playwright/jobspy.
"""

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 1800  # 30 min per worker

# The four residential-IP workers, in dependency order (discover new -> fill JDs -> recover -> verify).
WORKER_NAMES = ["discover_jobspy", "enrich_workday", "recover_jd", "verify_url"]


def default_workers():
    """(name, argv) per worker. sys.executable so subprocesses inherit this venv + its extras."""
    return [(name, [sys.executable, "-m", f"jobmaxxing.{name}"]) for name in WORKER_NAMES]


@dataclass
class RunResult:
    name: str
    status: str            # "ok" | "failed" | "timeout"
    exit_code: int | None
    duration_s: float
    output: str = ""       # combined stdout+stderr, for the run log


def format_summary(results, new_postings):
    """Pure. -> (title, body). title carries ✓ (all ok) or ✗ (any failure/timeout)."""
    failed = [r for r in results if r.status != "ok"]
    total = len(results)
    ok = total - len(failed)
    title = f"jobmaxxing nightly {'✓' if not failed else '✗'}"
    if failed:
        parts = [f"{ok}/{total} ok", ", ".join(f"{r.name} {r.status}" for r in failed)]
    else:
        parts = [f"{ok}/{total} workers ok"]
    if new_postings is not None:
        parts.append(f"{new_postings} new postings")
    return title, " · ".join(parts)


def _log_result(log_file, result):
    """Append a header + the worker's full captured output to the run log (no-op if log_file is None)."""
    if log_file is None:
        return
    with open(log_file, "a") as f:
        f.write(f"\n=== {result.name}: {result.status} "
                f"(exit={result.exit_code}, {result.duration_s:.1f}s) ===\n")
        f.write(result.output)
        if not result.output.endswith("\n"):
            f.write("\n")


def _prune_logs(log_dir, now, keep_days=14):
    """Delete nightly-*.log files whose mtime is older than keep_days. Bounds the log dir."""
    cutoff = now - timedelta(days=keep_days)
    for p in Path(log_dir).glob("nightly-*.log"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            p.unlink(missing_ok=True)


def run_nightly(workers=None, *, runner, notifier, db_delta, now, log_file=None,
                timeout=DEFAULT_TIMEOUT_S):
    """Run each (name, argv) via runner sequentially, fail-soft; then count new rows + notify once."""
    if workers is None:
        workers = default_workers()
    results = []
    for name, argv in workers:
        try:
            result = runner(name, argv, timeout)
        except Exception as exc:  # fail-soft: a runner blowup never aborts the batch
            logger.warning("nightly worker crashed [%s]: %s", name, exc)
            result = RunResult(name=name, status="failed", exit_code=None,
                               duration_s=0.0, output=str(exc))
        results.append(result)
        _log_result(log_file, result)
    try:
        new_postings = db_delta(now)
    except Exception as exc:
        logger.warning("nightly db_delta failed: %s", exc)
        new_postings = None
    title, body = format_summary(results, new_postings)
    try:
        notifier(title, body)
    except Exception as exc:  # best-effort; a missing osascript never fails the run
        logger.warning("nightly notifier failed: %s", exc)
    return {"results": results, "new_postings": new_postings, "title": title, "body": body}


def _subprocess_runner(name, argv, timeout):
    """Run one worker as a subprocess; map exit/timeout to a RunResult. Captures stdout+stderr."""
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # text=True → stdout/stderr are str or None
        out = (exc.stdout or "") + (exc.stderr or "")
        return RunResult(name=name, status="timeout", exit_code=None,
                         duration_s=time.monotonic() - start, output=out)
    output = (proc.stdout or "") + (proc.stderr or "")
    status = "ok" if proc.returncode == 0 else "failed"
    return RunResult(name=name, status=status, exit_code=proc.returncode,
                     duration_s=time.monotonic() - start, output=output)


def _osascript_notifier(title, body):
    """Fire a macOS notification. json.dumps → safe AppleScript double-quoted string literals."""
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], check=False)


def _db_delta(since):
    """Count jobs rows scraped since the batch start — the 'N new postings' headline."""
    import psycopg

    from ..config import load_settings

    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute("select count(*) from jobs where scraped_at >= %s", (since,)).fetchone()
    return row[0] if row else 0


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = datetime.now(timezone.utc)
    log_dir = Path.home() / "Library" / "Logs" / "jobmaxxing"
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune_logs(log_dir, now)
    log_file = log_dir / f"nightly-{now.strftime('%Y%m%d-%H%M%S')}.log"
    report = run_nightly(runner=_subprocess_runner, notifier=_osascript_notifier,
                         db_delta=_db_delta, now=now, log_file=log_file)
    print(f"{report['title']} — {report['body']}")
