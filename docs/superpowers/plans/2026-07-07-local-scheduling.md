# Local nightly scheduling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local macOS launchd job that runs the four residential-IP workers (`discover_jobspy`, `enrich_workday`, `recover_jd`, `verify_url`) sequentially at 12am local time and posts one macOS notification summarizing the run.

**Architecture:** A Python orchestrator (`jobmaxxing.nightly`) with a pure/injected core — `run_nightly` runs each worker as a subprocess through an **injected** runner, is fail-soft per worker, then counts new rows and fires a notification through injected `db_delta`/`notifier`. Only the thin default boundaries (`_subprocess_runner`, `_osascript_notifier`, `_db_delta`) touch subprocess/osascript/DB. A committed `.plist` template + README document the launchd install. Local-only; no CI change.

**Tech Stack:** Python 3.12, stdlib `subprocess`/`plistlib`/`pathlib`, psycopg3 (existing), pytest. macOS launchd + `osascript` (operator side, not tested in CI).

## Global Constraints
- Python **3.12**. Run pytest with the Postgres binary on PATH:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`. (The new tests here
  need no Postgres, but the full-suite step does.)
- **Local-only:** NO `.github/workflows` change. This scheduler runs on the operator's Mac, like the
  workers it invokes.
- **No extra imports at module top:** `src/jobmaxxing/scheduling/nightly.py` must import fine WITHOUT the
  `headless`/`discovery` extras installed — it never imports playwright/jobspy; it invokes the workers as
  **subprocesses** (`[sys.executable, "-m", "jobmaxxing.<name>"]`), which load their own extras.
- **Reuse, don't reinvent:** `load_settings` (`..config`); the shim pattern
  (`src/jobmaxxing/discover_jobspy.py` → `from .discovery.jobspy_source import main`); the
  `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")`
  + final `print` convention every worker's `main` uses.
- **No regression:** the full existing suite stays green.
- Push with the `vaibhavw30` gh account.

---

## Task 1: Pure core — `RunResult`, `format_summary`, log helpers

**Why:** The unit-testable, side-effect-free (or filesystem-only) pieces the orchestrator builds on: the
result record, the pure notification-text composer, per-run log appends, and age-based log pruning.

**Files:**
- Create: `src/jobmaxxing/scheduling/__init__.py` (empty), `src/jobmaxxing/scheduling/nightly.py`
- Test: `tests/test_nightly_helpers.py` (create)

**Interfaces:**
- Produces:
  - `RunResult` dataclass — `name: str, status: str ("ok"|"failed"|"timeout"), exit_code: int | None,
    duration_s: float, output: str = ""`.
  - `format_summary(results: list[RunResult], new_postings: int | None) -> tuple[str, str]` — pure;
    `(title, body)`.
  - `_log_result(log_file: Path | None, result: RunResult) -> None` — append a header + full output.
  - `_prune_logs(log_dir, now: datetime, keep_days: int = 14) -> None` — delete `nightly-*.log` older than
    `keep_days` by mtime.

- [ ] **Step 1: Write failing tests.** Create `tests/test_nightly_helpers.py`:
```python
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
```
Run: `uv run pytest tests/test_nightly_helpers.py -q` → FAIL (module missing).

- [ ] **Step 2: Implement.** Create `src/jobmaxxing/scheduling/__init__.py` (empty). Create
`src/jobmaxxing/scheduling/nightly.py`:
```python
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
```

- [ ] **Step 3: Run** `uv run pytest tests/test_nightly_helpers.py -q` → PASS (7).

- [ ] **Step 4: Commit**
```bash
git add src/jobmaxxing/scheduling/__init__.py src/jobmaxxing/scheduling/nightly.py tests/test_nightly_helpers.py
git commit -m "schedule: RunResult + format_summary + log helpers (pure core)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `run_nightly` orchestration (fail-soft, ordered, notifies)

**Files:**
- Modify: `src/jobmaxxing/scheduling/nightly.py` (add `run_nightly`)
- Test: `tests/test_nightly_run.py` (create)

**Interfaces:**
- Consumes: `RunResult`, `format_summary`, `_log_result`, `default_workers`, `DEFAULT_TIMEOUT_S` (Task 1).
- Produces: `run_nightly(workers=None, *, runner, notifier, db_delta, now, log_file=None,
  timeout=DEFAULT_TIMEOUT_S) -> dict`. Calls `runner(name, argv, timeout) -> RunResult` per worker in
  order; **fail-soft** (a raised exception → a `failed` RunResult, batch continues); logs each result;
  then `new = db_delta(now)` (exception → `None`), `title, body = format_summary(results, new)`,
  `notifier(title, body)` (exception swallowed). Returns
  `{"results": [...], "new_postings": new, "title": title, "body": body}`.

- [ ] **Step 1: Write failing tests.** Create `tests/test_nightly_run.py`:
```python
from datetime import datetime, timezone

from jobmaxxing.scheduling.nightly import RunResult, run_nightly


def _ok(name):
    return RunResult(name=name, status="ok", exit_code=0, duration_s=0.1, output=f"{name} done\n")


def test_runs_workers_in_order_and_notifies_once():
    calls = []
    notifications = []

    def runner(name, argv, timeout):
        calls.append(name)
        return _ok(name)

    workers = [("a", ["x"]), ("b", ["y"]), ("c", ["z"])]
    report = run_nightly(workers, runner=runner, notifier=lambda t, b: notifications.append((t, b)),
                         db_delta=lambda since: 12, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert calls == ["a", "b", "c"]                     # order preserved
    assert len(notifications) == 1                      # exactly one notification
    assert notifications[0][0] == "jobmaxxing nightly ✓"
    assert "12 new postings" in notifications[0][1]
    assert report["new_postings"] == 12
    assert [r.status for r in report["results"]] == ["ok", "ok", "ok"]


def test_failsoft_a_raising_runner_does_not_abort_the_batch():
    calls = []

    def runner(name, argv, timeout):
        calls.append(name)
        if name == "b":
            raise RuntimeError("worker crashed")
        return _ok(name)

    workers = [("a", ["x"]), ("b", ["y"]), ("c", ["z"])]
    report = run_nightly(workers, runner=runner, notifier=lambda t, b: None,
                         db_delta=lambda since: 0, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert calls == ["a", "b", "c"]                     # c still ran after b raised
    statuses = {r.name: r.status for r in report["results"]}
    assert statuses == {"a": "ok", "b": "failed", "c": "ok"}
    assert report["title"] == "jobmaxxing nightly ✗"


def test_db_delta_failure_yields_none_count_and_still_notifies():
    notifications = []

    def bad_delta(since):
        raise RuntimeError("db down")

    report = run_nightly([("a", ["x"])], runner=lambda n, a, t: _ok(n),
                         notifier=lambda t, b: notifications.append((t, b)),
                         db_delta=bad_delta, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert report["new_postings"] is None
    assert len(notifications) == 1                      # notification still fired
    assert "new postings" not in notifications[0][1]


def test_notifier_failure_is_swallowed():
    def boom(title, body):
        raise RuntimeError("osascript missing")

    # must not raise
    report = run_nightly([("a", ["x"])], runner=lambda n, a, t: _ok(n), notifier=boom,
                         db_delta=lambda since: 1, now=datetime(2026, 7, 7, tzinfo=timezone.utc))
    assert report["results"][0].status == "ok"


def test_writes_each_result_to_the_log(tmp_path):
    log = tmp_path / "nightly.log"
    run_nightly([("a", ["x"]), ("b", ["y"])], runner=lambda n, a, t: _ok(n),
                notifier=lambda t, b: None, db_delta=lambda since: 0,
                now=datetime(2026, 7, 7, tzinfo=timezone.utc), log_file=log)
    text = log.read_text()
    assert "a: ok" in text and "b: ok" in text
    assert "a done" in text and "b done" in text
```
Run: `uv run pytest tests/test_nightly_run.py -q` → FAIL (`run_nightly` missing).

- [ ] **Step 2: Implement.** Append to `src/jobmaxxing/scheduling/nightly.py`:
```python
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
```

- [ ] **Step 3: Run** `uv run pytest tests/test_nightly_run.py tests/test_nightly_helpers.py -q` → PASS (12).

- [ ] **Step 4: Commit**
```bash
git add src/jobmaxxing/scheduling/nightly.py tests/test_nightly_run.py
git commit -m "schedule: fail-soft run_nightly orchestrator (ordered, notifies once)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Default boundaries + `main` + CLI shim

**Files:**
- Modify: `src/jobmaxxing/scheduling/nightly.py` (add `_subprocess_runner`, `_osascript_notifier`,
  `_db_delta`, `main`)
- Create: `src/jobmaxxing/nightly.py` (shim)
- Test: `tests/test_nightly_runner.py` (create)

**Interfaces:**
- Consumes: `RunResult`, `run_nightly`, `_prune_logs` (Tasks 1-2), `load_settings` (`..config`).
- Produces:
  - `_subprocess_runner(name, argv, timeout) -> RunResult` — real `subprocess.run`; `returncode==0`→`ok`,
    else `failed`; `TimeoutExpired`→`timeout`. Captures stdout+stderr into `output`.
  - `_osascript_notifier(title, body) -> None` — `osascript -e 'display notification …'` (json-quoted).
  - `_db_delta(since) -> int` — `select count(*) from jobs where scraped_at >= since`.
  - `main() -> None`.
  - `python -m jobmaxxing.nightly`.

- [ ] **Step 1: Write failing tests.** Create `tests/test_nightly_runner.py` (real subprocess, no network,
no Postgres):
```python
import sys

from jobmaxxing.scheduling.nightly import _subprocess_runner


def test_runner_ok_captures_output():
    r = _subprocess_runner("echo", [sys.executable, "-c", "print('hello nightly')"], timeout=30)
    assert r.status == "ok"
    assert r.exit_code == 0
    assert "hello nightly" in r.output


def test_runner_nonzero_exit_is_failed():
    r = _subprocess_runner("boom", [sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30)
    assert r.status == "failed"
    assert r.exit_code == 3


def test_runner_timeout_is_timeout():
    r = _subprocess_runner("slow", [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert r.status == "timeout"
    assert r.exit_code is None


def test_shim_exposes_main():
    import jobmaxxing.nightly as shim
    from jobmaxxing.scheduling.nightly import main
    assert shim.main is main
```
Run: `uv run pytest tests/test_nightly_runner.py -q` → FAIL (`_subprocess_runner`/shim missing).

- [ ] **Step 2: Implement boundaries + main.** Append to `src/jobmaxxing/scheduling/nightly.py`:
```python
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
```

- [ ] **Step 3: Create the shim** `src/jobmaxxing/nightly.py`:
```python
"""CLI shim: `python -m jobmaxxing.nightly` (local macOS nightly scheduler entrypoint)."""

from .scheduling.nightly import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run** `uv run pytest tests/test_nightly_runner.py -q` → PASS (4).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/scheduling/nightly.py src/jobmaxxing/nightly.py tests/test_nightly_runner.py
git commit -m "schedule: subprocess/osascript/db boundaries + main + CLI shim

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: launchd plist template + README + full suite

**Files:**
- Create: `scripts/com.jobmaxxing.nightly.plist`, `tests/test_nightly_plist.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing (config + docs). The plist invokes `uv run python -m jobmaxxing.nightly`.

- [ ] **Step 1: Write the failing plist-structure test.** Create `tests/test_nightly_plist.py`:
```python
import plistlib
from pathlib import Path


def test_plist_is_valid_and_scheduled_at_midnight():
    data = plistlib.loads(Path("scripts/com.jobmaxxing.nightly.plist").read_bytes())
    assert data["Label"] == "com.jobmaxxing.nightly"
    assert data["StartCalendarInterval"] == {"Hour": 0, "Minute": 0}   # 12am local
    assert data["ProgramArguments"][-2:] == ["-m", "jobmaxxing.nightly"]
    assert data["RunAtLoad"] is False
```
Run: `uv run pytest tests/test_nightly_plist.py -q` → FAIL (file missing).

- [ ] **Step 2: Create the plist** `scripts/com.jobmaxxing.nightly.plist` (operator edits the two
`/Users/YOU/...` paths + the `uv` path to their machine):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jobmaxxing.nightly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>jobmaxxing.nightly</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/jobmaxxing</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>/Users/YOU/Library/Logs/jobmaxxing/launchd.out</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/Library/Logs/jobmaxxing/launchd.err</string>
</dict>
</plist>
```

- [ ] **Step 3: Run** `uv run pytest tests/test_nightly_plist.py -q` → PASS (1).

- [ ] **Step 4: README section.** In `README.md`, near the other local-worker sections, add:
```markdown
### Nightly scheduling (local, macOS)

Run the four residential-IP workers automatically once a night via launchd — "the production cron."
At 12am local time it runs, in order, `discover_jobspy` → `enrich_workday` → `recover_jd` → `verify_url`
(sequentially, so only one worker uses your home IP at a time), then posts one macOS notification with a
recap. Nothing here runs in CI.

One-time setup:

    uv sync --extra headless --extra discovery
    uv run playwright install chromium                       # for enrich_workday
    cp scripts/com.jobmaxxing.nightly.plist ~/Library/LaunchAgents/
    # edit ~/Library/LaunchAgents/com.jobmaxxing.nightly.plist:
    #   - ProgramArguments[0]  -> output of `which uv`
    #   - WorkingDirectory     -> this repo's absolute path
    #   - Standard{Out,Error}Path -> /Users/<you>/Library/Logs/jobmaxxing/launchd.{out,err}
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobmaxxing.nightly.plist

`StartCalendarInterval` uses the Mac's **local** time (12am ET when your timezone is Eastern) and
**catches up on wake** if the laptop slept through midnight. Per-run logs land in
`~/Library/Logs/jobmaxxing/nightly-*.log` (pruned after 14 days). Run it by hand any time with:

    uv run python -m jobmaxxing.nightly

To remove the schedule: `launchctl bootout gui/$(id -u)/com.jobmaxxing.nightly`.
```

- [ ] **Step 5: Full suite (no regression).**
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: PASS (all; the new nightly tests add ~17, no skips added).

- [ ] **Step 6: Confirm no CI change.**
Run: `git status --porcelain .github/`
Expected: empty output (this feature touches no workflow).

- [ ] **Step 7: Commit**
```bash
git add scripts/com.jobmaxxing.nightly.plist tests/test_nightly_plist.py README.md
git commit -m "schedule: launchd plist template + README (nightly local scheduler)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green.
2. Module imports without the extras: `uv run python -c "import jobmaxxing.scheduling.nightly as m;
   print(hasattr(m, 'run_nightly'))"` → `True` (no playwright/jobspy import at module load).
3. Manual dry run (operator, real DB + extras synced): `uv run python -m jobmaxxing.nightly` → runs the
   four workers, writes `~/Library/Logs/jobmaxxing/nightly-*.log`, prints the summary, and (on macOS)
   posts one notification.

## Risks & notes
- **Single residential IP** — sequential execution is the mitigation; do not parallelize the workers.
- **launchd env is minimal** — the plist sets `WorkingDirectory` (so `.env`/uv resolve) and `PATH`; the
  operator must fill the absolute `uv`/repo paths. Documented in the README step.
- **Timezone** — `StartCalendarInterval` is local time; if the Mac isn't on Eastern, adjust `Hour`.
- **osascript in a login session** — a LaunchAgent (not Daemon) runs in the GUI session, so notifications
  appear; a notifier failure is swallowed so a headless/ssh run still completes.
- **`_db_delta` counts `scraped_at >= batch_start`** — the workers scrape after `now` is captured, so new
  rows are counted; a DB hiccup degrades to no count, never a crash.

## Execution
Isolated git worktree off `main`; subagent-driven TDD, one task per subagent (implementer reads
`discover_jobspy.py` for the shim pattern and any worker `main` for the logging/print convention first);
two-stage review (spec → quality) per task; full-suite green; merge to `main`; push (gh `vaibhavw30`).
```
