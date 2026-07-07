# Local nightly scheduling for residential-IP workers — design

## Context
Four operator-run workers must run on the operator's **residential IP** (job boards / Cloudflare /
DuckDuckGo 429 datacenter IPs), so they are never in CI: `discover_jobspy` (`discovery` extra),
`enrich_workday` (`headless` extra), `recover_jd`, `verify_url`. Today the operator runs each by hand.
This is greenfield — no cron/launchd/plist exists anywhere in the repo (the JobSpy design doc explicitly
deferred "the separate launchd sub-project"). CI already schedules everything else (pollers/enrich/route
every 3h, LLM routing every ~4 days, nightly email report) via GitHub Actions.

Every worker shares the same shape: `python -m jobmaxxing.<name>`, `load_settings()` reads
`DATABASE_URL` from a `.env` in cwd, `logging.basicConfig(...)` to stderr plus a final summary `print`,
and each is **fail-soft, bounded, and resumable**.

## Goal
A single **local macOS launchd** job that, once nightly at **12am local time (ET)**, runs the four
residential-IP workers **sequentially** and posts **one macOS notification** summarizing the run —
"the production cron." Fully hands-off; the operator wakes to fresh results and a recap. Scope is
local-Mac only for now (no cloud/proxy). `route` (CI owns it) and `tailor` (operator-gated) are excluded.

## Design

### Mechanism
One **launchd LaunchAgent** (`com.jobmaxxing.nightly`) fires `uv run python -m jobmaxxing.nightly` at
12am. That entrypoint runs a small **Python orchestrator** that sequences the workers as subprocesses,
captures each one's output + exit code, composes a summary, and fires the notification. A LaunchAgent
(not a Daemon) runs in the operator's GUI session, so `osascript` notifications appear; it survives
reboot/logout and **catches up on wake** if 12am passed while the laptop was asleep.

Sequential execution is deliberate: only one worker touches the single residential IP at a time —
parallel Indeed/LinkedIn + Cloudflare + DuckDuckGo hits are exactly what trips 429s.

### Components / files
- **`src/jobmaxxing/nightly.py`** (new) — thin shim: `from .scheduling.nightly import main` (mirrors the
  other worker shims).
- **`src/jobmaxxing/scheduling/__init__.py`** (new, empty).
- **`src/jobmaxxing/scheduling/nightly.py`** (new) — the orchestrator. Pure core + injected side-effects,
  matching the repo pattern so it is fully unit-testable:
  - `WORKERS` — the default ordered list of `(name, argv)`:
    1. `discover_jobspy` 2. `enrich_workday` 3. `recover_jd` 4. `verify_url`. Each `argv` is
    `[sys.executable, "-m", "jobmaxxing.<name>"]` so subprocesses inherit the same venv (with the
    `headless`/`discovery` extras); the orchestrator itself imports none of those extras.
  - `RunResult` — a small dataclass: `name, status ("ok"|"failed"|"timeout"), exit_code, duration_s,
    tail (last non-empty output line)`.
  - `run_nightly(workers, *, runner, notifier, db_delta, now, log_dir) -> report` — for each
    `(name, argv)`: call `runner(argv, timeout)` → append output to the run log → record a `RunResult`.
    **Fail-soft:** a non-zero exit, timeout, or raised exception is caught and recorded; the next worker
    still runs. After all workers: `new = db_delta(since=now)`; `title, body = format_summary(results,
    new)`; `notifier(title, body)`. Returns `{"results": [...], "new_postings": new, "notified": True}`.
  - `format_summary(results, new_postings) -> (title, body)` — **pure**. `title = "jobmaxxing nightly ✓"`
    if all ok else `"jobmaxxing nightly ✗"`. `body` e.g. `"4/4 workers ok · 37 new postings"` or
    `"3/4 ok — recover_jd failed · 37 new · see log"`.
  - `main() -> None` — `logging.basicConfig(...)`; resolve `log_dir = ~/Library/Logs/jobmaxxing`;
    open a timestamped log `nightly-YYYYMMDD-HHMMSS.log`; prune logs older than 14 days; wire the real
    boundaries (`_subprocess_runner`, `_osascript_notifier`, `_db_delta`) and call `run_nightly`; print
    the summary. Mirrors `enrich_workday`'s `main`.
- **Injected default boundaries** (thin, side-effecting, not unit-tested — like the workers' real network
  paths):
  - `_subprocess_runner(argv, timeout) -> RunResult` — `subprocess.run(argv, capture_output=True,
    text=True, timeout=timeout)`; on `TimeoutExpired` → `status="timeout"`. Default per-worker timeout
    **1800 s (30 min)**.
  - `_osascript_notifier(title, body)` — `subprocess.run(["osascript", "-e", f'display notification
    "{body}" with title "{title}"'])`, best-effort (a notifier failure is logged, never raises).
  - `_db_delta(since) -> int` — `psycopg.connect(settings.database_url)`; `select count(*) from jobs
    where scraped_at >= %s`. Robust headline number, no stdout parsing. On error → returns `None`
    (summary then omits the count) so a DB hiccup never crashes the recap.
- **`scripts/com.jobmaxxing.nightly.plist`** (new) — the LaunchAgent template (committed for reference;
  operator copies to `~/Library/LaunchAgents/` and fills in the two absolute paths):
  - `Label` `com.jobmaxxing.nightly`
  - `ProgramArguments` `[<abs uv>, run, python, -m, jobmaxxing.nightly]`
  - `WorkingDirectory` = repo root (so `.env` + the uv project resolve)
  - `EnvironmentVariables` `PATH` includes `/opt/homebrew/bin`
  - `StartCalendarInterval` `Hour 0, Minute 0` (**local** time = 12am ET when the Mac's tz is Eastern)
  - `RunAtLoad` false; `ProcessType` Background
  - `StandardOutPath`/`StandardErrorPath` → `~/Library/Logs/jobmaxxing/launchd.{out,err}` (catch-all if
    the orchestrator crashes before its own logging)
- **`README.md`** — a "Nightly scheduling (local, macOS)" section: the one-time setup and the manual
  `uv run python -m jobmaxxing.nightly` test invocation.

### Data flow
launchd (12am local) → `uv run python -m jobmaxxing.nightly` → `run_nightly` → for each worker in order:
`_subprocess_runner` → append to run log → `RunResult`; then `_db_delta` → `format_summary` →
`_osascript_notifier`. Workers write to the same Postgres `jobs` table they already use; routing/triage
pick up the new rows on their existing schedules.

### Error handling / robustness
- **Fail-soft per worker** — non-zero exit, 30-min timeout, or exception is caught and recorded; the
  remaining workers still run. One stuck/failed worker never blocks the batch.
- **Always notify** — even if every worker fails or `DATABASE_URL` is unset, `run_nightly` still composes
  and fires a "batch failed"-flavored notification; a notifier failure is logged, never raised.
- **Bounded/resumable** — each worker already caps its own work and is safe to re-run; no network at 12am
  just means failures the next night retries.
- **No lockfile in v1** — workers are idempotent; a rare catch-up + manual overlap is harmless (noted as
  an optional future add).
- **Log hygiene** — one file per run; prune > 14 days each run so the dir stays bounded.

### Testing (matches repo TDD; no CI change)
- **Unit — `run_nightly`:** fake `runner` returning canned `RunResult`s including one `failed` and one
  `timeout` → assert (a) workers invoked in `WORKERS` order, (b) fail-soft: later workers still run after
  a failure/timeout, (c) the report reflects each worker's status, (d) `notifier` called exactly once
  with the right ok-vs-failed variant, (e) log lines written for each worker. Fakes for `notifier` and
  `db_delta`; `now`/`log_dir` injected for determinism.
- **Unit — `format_summary`:** pure; all-ok vs some-failed title/body; count present vs `None` (DB hiccup).
- **Unit — log pruning:** files older than 14 days removed, newer kept (inject `now` + a temp dir).
- **Not unit-tested (side-effects):** the plist, real `osascript`, real `subprocess`, `launchctl`,
  `_db_delta`'s live query — validated by the documented manual run `uv run python -m jobmaxxing.nightly`.

## Out of scope
Local `route`/`tailor` scheduling (CI / operator-gated); cloud/proxy scheduling; per-worker independent
cadences (the single nightly batch + workers' own internal bounds suffice — `verify_url` self-limits to
14-day-old jobs); lockfile/mutex; log rotation beyond age-pruning; Linux/systemd (macOS only for now).

## Execution
Spec + plan committed to `main` (as with prior features); implementation in an isolated git worktree via
subagent-driven TDD, two-stage review per task, merge to `main`, push (gh `vaibhavw30`).
