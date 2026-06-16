# Nightly digest email — design

## Context
The pipeline already runs in the cloud for free: `.github/workflows/pollers.yml` executes
migrate → run → enrich → route **every 3 hours** against Supabase. Each stage only **logs** counts
(`run_sources` returns a per-source dict, logged as a "run summary" line — `run.py:88`); nothing is
aggregated, persisted, or sent anywhere. The operator wants a **once-a-day email digest** of what's
new and relevant, delivered to Gmail/Outlook. The triage web UI stays local (no deployment).

Key data constraint: `jobs.scraped_at` is bumped to `now()` on **every** re-poll (the merge UPDATE,
`store.py` `_UPDATE_SQL`), so it means "last seen", not "first seen" — it can't distinguish a brand-new
posting from a re-seen one. A daily "what's new since yesterday" digest therefore needs a real
first-seen timestamp.

## Goal
A `jobmaxxing.report` command that builds a daily digest (new in-window roles in the last 24h, totals,
manual-capture queue size), prints it, and can email it via generic SMTP (Gmail **or** Outlook), driven
by a new nightly GitHub Actions workflow. No change to the triage UI; no remote hosting.

## Design

### 1. `first_seen_at` column (migration `0009`)
```sql
alter table jobs add column if not exists first_seen_at timestamptz;
update jobs set first_seen_at = coalesce(posted_at, scraped_at) where first_seen_at is null;
alter table jobs alter column first_seen_at set default now();
```
- New inserts get `default now()` (the column is omitted from `_INSERT_COLS`, so the default applies —
  **no `store.py` change needed**). The merge UPDATE never touches it, so it stays at first-insert time.
- Backfill existing rows from `coalesce(posted_at, scraped_at)` so the first digest after deploy isn't
  flooded with the entire backlog as "new" (old postings get an old `first_seen_at`).
- Idempotent: `add column if not exists` + the backfill only touches `first_seen_at is null` rows, so
  the always-re-run migration runner is a no-op on subsequent runs.

### 2. Shared off-window predicate (`normalize.off_window_sql`)
The triage demotion and the digest's "in-window / visible" filter must agree on what's off-window.
Extract the predicate (currently inlined in `web/triage._demote_clause`) into `normalize.py`:
```python
def off_window_sql(in_window_labels) -> str:
    """SQL boolean: TRUE for github rows that are off the upcoming window — legacy (term IS NULL) or
    tagged with no in-window overlap. Untagged ('{}') and non-github (ATS) rows are FALSE (kept).
    Labels are canonical "Season YYYY" from a fixed season set + int years -> safe to inline."""
    arr = "array[" + ", ".join("'" + lbl + "'" for lbl in sorted(in_window_labels)) + "]::text[]"
    return (f"split_part(source, ':', 1) = 'github' and (term is null or "
            f"(cardinality(term) > 0 and not (term && {arr})))")
```
- Uses `split_part(source, ':', 1) = 'github'` instead of `source like 'github:%%'` — **no `%`**, so
  the fragment is safe whether or not the surrounding `execute()` passes params (the digest queries do
  pass a timestamp param; the `%%`-escaping the old clause needed was a latent footgun). Equivalent to
  the old `like 'github:%'` for the real source set (`github:simplify|vanshb03|pitt-csc` vs ATS).
- `web/triage._demote_clause(labels)` becomes `f"({off_window_sql(labels)}) asc"` — one source of truth.

### 3. `jobmaxxing/report.py`
```python
@dataclass
class Digest:
    now: datetime
    window: list[str]            # in-window labels, e.g. ["Fall 2026", ...]
    new_count: int               # routed+undecided rows first-seen in last 24h, in-window
    new_rows: list[dict]         # capped at NEW_ROWS_CAP=25: company,title,resume_type,term,url
    by_type: dict[str, int]      # new_rows grouped by resume_type
    total_undecided: int         # all routed+undecided in-window (the actionable backlog)
    queue_size: int              # count(*) from nightly_queue (relevant, still JD-less)
```
- `build_digest(conn, now)`: one set of read-only queries. "Visible/in-window" = `resume_type is not
  null and not (<off_window_sql(window)>)`; "undecided" adds `status in ('new','routed')`; "new" adds
  `first_seen_at >= now - interval '24 hours'`. `new_rows` ordered `route_confidence desc nulls last,
  posted_at desc` and capped. `queue_size` = `select count(*) from nightly_queue`.
- `render_text(digest) -> str` and `render_html(digest) -> str`: subject + body. Plain text is the
  canonical content; HTML is a light wrapper (table of new roles, links). Subject:
  `"jobmaxxing daily — {new_count} new in-window roles"`.
- `send_digest(digest, cfg, smtp_factory=smtplib.SMTP)`: builds a `EmailMessage` (text + html
  alternative) and sends via STARTTLS. `smtp_factory` is injectable so tests use a fake (no real SMTP).
- `SmtpConfig` from env (`load_smtp_config()`): `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`,
  `SMTP_PASS`, `SMTP_FROM` (default = `SMTP_USER`), `REPORT_TO` (comma-separated → list). Generic, so
  Gmail (`smtp.gmail.com`) and Outlook (`smtp.office365.com`) both work by config alone.
- CLI `python -m jobmaxxing.report [--email]`: always prints the text digest; with `--email`, also
  sends it (loads `SmtpConfig`, errors clearly if required env is missing). Uses `load_settings()` for
  `DATABASE_URL`, mirroring the other entrypoints.

### 4. `.github/workflows/nightly-report.yml`
A separate workflow (not folded into `pollers.yml`, so the 3-hourly poller stays noise-free):
```yaml
on:
  schedule:
    - cron: "0 12 * * *"   # 12:00 UTC ≈ 7–8am US Eastern; once daily
  workflow_dispatch: {}
```
One job: checkout → setup-uv → `uv sync --frozen --no-dev` → migrate → `uv run python -m
jobmaxxing.report --email`, with env from secrets: `DATABASE_URL`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `REPORT_TO`.

### 5. Manual operator steps (documented, not automatable here)
- Generate an **app password** (Gmail: Google Account → Security → App passwords; Outlook: similar) —
  normal passwords won't work with SMTP under 2FA.
- Add repo **secrets**: `SMTP_HOST` (`smtp.gmail.com` or `smtp.office365.com`), `SMTP_USER`,
  `SMTP_PASS` (the app password), `REPORT_TO` (one or more addresses, comma-separated). `DATABASE_URL`
  already exists. Optional: `SMTP_PORT`, `SMTP_FROM`.

## Components & data flow
- `migrations/0009_first_seen_at.sql` — new column + backfill.
- `src/jobmaxxing/normalize.py` — `off_window_sql(in_window_labels)`.
- `src/jobmaxxing/web/triage.py` — `_demote_clause` delegates to `off_window_sql`.
- `src/jobmaxxing/report.py` — `Digest`, `build_digest`, `render_text/html`, `SmtpConfig`,
  `load_smtp_config`, `send_digest`, `main`/CLI.
- `.github/workflows/nightly-report.yml` — daily schedule.
- `README.md` — a short "Nightly digest" section (setup + secrets).

## Testing (TDD)
- `tests/test_report.py` (pytest-postgresql):
  - `build_digest` counts new in-window routed rows in last 24h; excludes off-window (stale-tagged)
    rows from `total_undecided`; excludes a row first-seen 2 days ago from `new_count`; `by_type`
    groups correctly; `new_rows` capped + ordered; `queue_size` reads `nightly_queue`.
  - `render_text` / `render_html` include the new count, the role lines, and the window.
  - `send_digest` with a fake `smtp_factory` asserts STARTTLS+login+send called with the right
    recipients and that the message has both text and html parts; `load_smtp_config` parses
    comma-separated `REPORT_TO` and errors on missing required env.
- `tests/test_store.py`: `first_seen_at` is set on insert and **not** bumped by a re-ingest/merge
  (whereas `scraped_at` is) — the core reason the column exists.
- `tests/test_web_triage.py`: unchanged behavior after the `_demote_clause` refactor (all existing
  demotion tests still pass; add nothing).
- `tests/test_normalize.py`: `off_window_sql` emits the expected fragment for a sample window and an
  empty window (`array[]::text[]`).

## Out of scope
No remote hosting of the triage UI (stays local, no auth/HTTPS work); no persistence of the digest
(it's computed fresh each run); no per-run (3-hourly) emails — daily only; no Slack/Mac-native channel
(email only); no change to the pollers workflow or the pipeline stages themselves.

## Execution
Isolated worktree off `main` (`worktree-nightly-digest-email`); strict TDD; final review; merge to
`main`. `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before `uv run pytest`. SMTP secrets
+ app password are a manual operator step (cannot be done from here).
