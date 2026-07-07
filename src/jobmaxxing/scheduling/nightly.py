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
