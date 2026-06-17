# jobmaxxing — core feed ingestion

Auto-updating, deduped Postgres feed of internship postings. Phase 1 of the
recruiting pipeline (see `docs/PRD.md`, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md`,
and `docs/superpowers/specs/2026-06-11-core-feed-ingestion-design.md`).

Independent pollers (curated GitHub internship lists + Greenhouse/Lever/Ashby ATS
boards) normalize postings into one deduped `jobs` table. Each source is isolated:
one failing source never blocks the others.

## Setup

1. **Provision Supabase.** Create a free Supabase project. Copy the Postgres
   connection string (Project Settings → Database → Connection string).
2. **Local env.** `cp .env.example .env` and set `DATABASE_URL`.
3. **Install.** `uv sync`
4. **Migrate.** `uv run python -m jobmaxxing.migrate`
5. **CI secret.** In the GitHub repo: Settings → Secrets and variables → Actions
   → add `DATABASE_URL`. The repo is public, so the pollers workflow runs only on
   `schedule`/`workflow_dispatch` (never fork PRs), so the secret is never exposed.

## Adding a watch-list company

Edit `config/watchlist.yaml`:

```yaml
companies:
  - company: "Example Corp"
    ats: greenhouse        # greenhouse | lever | ashby
    token: examplecorp     # public board slug / token
```

Invalid or incomplete entries are skipped with a warning, so a typo never aborts a run.

## Running

- Manually: `uv run python -m jobmaxxing.run`
- Scheduled: `.github/workflows/pollers.yml` runs every 3 hours.

A run logs per-source ingested/merged/skipped counts plus a summary; a failing
source is logged and skipped (the run still exits 0). A DB/config error (bad
`DATABASE_URL`) fails the run loudly — that's an operator setup error, not a
transient source failure.

## Nightly digest email

`uv run python -m jobmaxxing.report` prints a daily digest (new in-window roles in
the last 24h, the undecided backlog, and the manual-capture queue size). Add
`--email` to send it. `.github/workflows/nightly-report.yml` runs it once a day
(12:00 UTC) so the digest lands in your inbox.

Email goes over generic SMTP, so Gmail or Outlook both work — pick a sending
account and add these repo **secrets** (`DATABASE_URL` already exists):

| Secret | Gmail | Outlook |
|--------|-------|---------|
| `SMTP_HOST` | `smtp.gmail.com` | `smtp.office365.com` |
| `SMTP_USER` | your address | your address |
| `SMTP_PASS` | an **app password** (Google Account → Security → App passwords; a normal password won't work with 2FA) | an app password |
| `REPORT_TO` | recipient(s), comma-separated — can include both your Gmail and Outlook | |
| `SMTP_PORT` | optional, defaults to `587` | |
| `SMTP_FROM` | optional, defaults to `SMTP_USER` | |

Trigger it on demand from the Actions tab (**nightly-report → Run workflow**) to
test before waiting for the schedule.

## Querying the feed

Use the Supabase SQL editor / table browser. Convenience view:

```sql
select * from active_unrouted;   -- active postings not yet routed, newest first
```

## Tests

```bash
uv run pytest
```

The store/pipeline/runner tests use `pytest-postgresql`, which needs a local
PostgreSQL **server** binary (`initdb`/`pg_ctl`) on your `PATH`:

- macOS: `brew install postgresql` then ensure its `bin/` is on `PATH`
  (e.g. `export PATH="$(brew --prefix postgresql)/bin:$PATH"` — adjust the
  version suffix to whatever brew installed, e.g. `postgresql@16`).
- CI installs PostgreSQL automatically (see `.github/workflows/ci.yml`).

## Routing

After ingestion, postings are classified into one of 8 resume types
(`quant-trader, quant-dev, mle, swe, fdse, ai, robotics, av`).

- Run: `uv run python -m jobmaxxing.route` (also runs automatically as a second
  step in the pollers workflow, right after ingestion).
- **Deterministic first:** title signals are authoritative; a JD-keyword tie-break
  resolves most of the rest. The LLM is a bounded, schema-gated fallback used only
  for ambiguous postings that have a job description, and its answer is always
  validated against the type set (or it falls back to the deterministic pick). Each
  run logs the `rules` / `llm` / `deferred` split — tune `config/routing.yaml` to
  push `llm` down.
- **Manual override:** `uv run python -m jobmaxxing.route set <job_id> <type>`
  (sets `route_method='manual'`; automated routing never overwrites manual rows).
- **LLM keys:** set `OPENAI_API_KEY` / `XAI_API_KEY` / `ANTHROPIC_API_KEY` (env locally,
  GitHub Actions secrets in CI). Provider order and models are configured in
  `config/llm.yaml`; a provider with no key is simply skipped, so routing still works
  on the deterministic rules alone even with no LLM keys set.

## Tailoring

For a job the operator has approved, produce a tailored one-page résumé with a
deterministic before/after keyword-coverage score and LLM weakness/missing-keyword
feedback. **Operator-gated and run locally** — never automatic (cost control).

### LLM cost: tailoring uses your Claude subscription

The local tailoring step (`python -m jobmaxxing.tailor`) prefers the `claude-cli` provider —
it shells to `claude -p` on your **Claude subscription** instead of spending API tokens. Make
sure the `claude` CLI is installed and **logged in to your subscription** (`claude` then `/login`).
The adapter strips the API-billing credentials (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) from
the call so it can't accidentally bill the API, and runs the CLI tool-restricted (no Bash/Write/
Edit) so injected JD text can't drive it. If the CLI is absent (e.g. CI) or errors, the pipeline
automatically falls back to the API. Routing stays on the cheap API model. Optional check:
`JOBMAXXING_E2E=1 uv run pytest tests/test_llm_claude_cli_e2e.py -v`.

Setup:
- Install a LaTeX distribution providing `pdflatex` (e.g. MacTeX/TeX Live).
- Create an S3 bucket; set `S3_BUCKET` and the standard `AWS_*` credentials.
- Upload one base résumé per resume type to `s3://<bucket>/base/{type}/main.tex`
  (types: `quant-trader, quant-dev, mle, swe, fdse, ai, robotics, av`). The tailoring
  engine ships; the base résumé content is yours.
- Tune `rubrics/{type}.json` (the deterministic keyword dictionaries) over time.

Use:
- Approve: `uv run python -m jobmaxxing.tailor approve <job_id>` (sets `approved_for_tailoring`).
- Tailor: `uv run python -m jobmaxxing.tailor <job_id>` — runs the two-pass loop and writes
  `tailored.tex`, `tailored.pdf`, `review.json`, `diff.txt` to `s3://<bucket>/tailored/{job_id}/`,
  sets `score_before`/`score_after` and `status=tailored`.
- Review: `uv run python -m jobmaxxing.tailor review <job_id>` prints the artifact location.

The improvement score (keyword coverage) and the one-page check are computed in code, never
self-reported by the model. The human reviews the diff and moves the job to `applied`.

## Conversational interface (MCP)

Drive the whole pipeline from Claude Code via an MCP server — no dashboard.

- Register it: the repo ships `.mcp.json` (runs `uv run python -m jobmaxxing.mcp`); point Claude
  Code at this project so it launches the server. It reads `DATABASE_URL`, `S3_BUCKET`, `AWS_*`,
  and the LLM keys from the environment / `.env`. The `tailor_job` tool needs `pdflatex` locally.
- Tools: `query_jobs` (filter by status/type/company/recency), `preview_route` (stored route, or
  `rerun` to preview live), `set_route` (manual override), `approve` (gate for tailoring),
  `tailor_job` (run the loop — slow, ~30-120s), `get_review` (fetch review.json + diff), and
  `set_status` (move through the funnel incl. `applied`/`rejected`).
- Typical flow in chat: `query_jobs(status="routed")` -> `approve(<id>)` -> `tailor_job(<id>)` ->
  `get_review(<id>)` -> review the diff -> `set_status(<id>, "applied")`.
- Funnel at a glance (Supabase SQL editor): `select * from funnel_counts;` and
  `select * from review_queue;`.

## Workday enrichment (local, operator-run)

Workday job pages are Cloudflare-gated, so their descriptions are fetched by a **local**
headless-browser worker (kept out of CI). One-time setup:

    uv sync --extra headless
    uv run playwright install chromium

Then enrich description-less Workday rows (residential IP recommended — Cloudflare is
gentler on home IPs than datacenter ones):

    uv run python -m jobmaxxing.enrich_workday

It selects Workday rows still missing a description, fetches each via a tiered strategy
(plain cxs JSON → headless-cleared-context → headless render+intercept), and writes
descriptions back to the same database the CI pipeline uses. It is bounded (`max_jobs`
per run) and resumable — re-run it to drain the backlog. Blocked tenants are retried up
to a cap, then left alone.

To measure real-world yield or to run the live end-to-end test:

    uv run --extra headless python scripts/spike_workday.py 30
    JOBMAXXING_E2E=1 uv run --extra headless pytest tests/test_workday_e2e.py -v

### Nightly operator queue (MCP)

For Workday jobs neither the headless worker nor find-elsewhere could enrich, the MCP surfaces a
nightly worklist:

- `nightly_queue` — relevant, still-JD-less jobs to grab by hand. Open each in your own (non-bot)
  browser, read the JD, then `set_description(job_id, "<the JD text>")` — it stores the JD and
  resets routing so the next poll classifies it with the JD, ready to approve + tailor.
- `query_jobs(jd_source="recovered")` — list the auto-recovered JDs to spot-check; `reject_recovered(job_id)`
  discards a wrong one and returns the job to the nightly queue.

### JD recovery (find-elsewhere, local)

For Workday jobs that can't be enriched, run the recovery worker LOCALLY (residential IP — it
free-searches DuckDuckGo and reads `JobPosting` JSON-LD from aggregators/company sites):

    uv run python -m jobmaxxing.recover_jd

It targets relevant (title-routed), description-less Workday rows, accepts a JD only on a
req-id/back-link match or an LLM-confirmed fuzzy match, writes it with `jd_source='recovered'`
(flagged for review), and resets routing so the next poll re-routes it with the real JD.
Optional live check: `JOBMAXXING_E2E=1 uv run pytest tests/test_recover_e2e.py -v`.

### URL verification (local)

`uv run python -m jobmaxxing.verify_url` checks that the in-window triaged jobs' posting URLs still
resolve. When a URL is dead (404/410), it tries the job's other known URLs, then searches the web for
the same posting (reusing the recovery engine) and promotes a confidently-matched, working link to the
primary `url` (folding the dead one into `alt_urls`). When nothing resolves, it marks `url_status='dead'`
— the triage table shows a "⚠ dead link" marker and sinks the row to the bottom.

Run LOCALLY (DuckDuckGo rate-limits datacenter IPs), like `recover_jd`. Re-checks each job every ~14
days; a dead row stays dead (verify_attempts hits the cap) until re-run with a higher cap.

### Local triage table (recommended)

Interactive web table to triage routed jobs — no Google account, no auth, localhost only. Writes
straight to Postgres; re-running your local `tailor` picks up approved jobs automatically.

Setup: `uv sync --extra web`

Run:

    uv run --extra web python -m jobmaxxing.web

Then open `http://127.0.0.1:8765` in a browser. Set `WEB_PORT` to change the port.

The table shows routed jobs (those with a `resume_type`), newest first, defaulting to the
undecided view. Decision semantics:

- **Interested** → `approved_for_tailoring` (queues the job for your local `tailor` run)
- **Not interested** → `rejected`
- **Applied** → `applied`
- **↺ reset** → back to `routed`

Security posture: binds to `127.0.0.1` only, single-user, JSON-only POST with Host-header
allowlist — no external network exposure.

### Job decision sheet (Google Sheets, two-way) — superseded by the local triage table

*Superseded by the Local triage table above; kept for reference. Requires adding yourself as an
OAuth test user on the Google consent screen.*

Triage routed jobs in a spreadsheet instead of one MCP call at a time. One-time setup (auth as
**yourself** via Application Default Credentials — no service account, no key file, no sharing;
Google's "Secure by Default" policy blocks service-account keys on most new projects anyway):

1. In a Google Cloud project, enable the **Google Sheets API** and the **Google Drive API**.
2. Install gcloud (`brew install --cask google-cloud-sdk`) and authorize once:
   `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive`
   (if it errors on a quota project: `gcloud auth application-default set-quota-project <PROJECT_ID>`).
3. Create a Google Sheet in your own account; set `GSHEET_ID` (the id in its URL) in `.env`, leave
   `GOOGLE_SERVICE_ACCOUNT_FILE` blank; `uv sync --extra sheets`.

(Service-account auth is still supported: set `GOOGLE_SERVICE_ACCOUNT_FILE` to a JSON key path and
share the sheet with the account's email — but key creation is often org-policy-blocked, so ADC is the default.)

Then sync (locally, or the `sync_sheet` MCP tool): `uv run --extra sheets python -m jobmaxxing.sync_sheet`.
It pushes routed jobs (company, title, JD, status, …) into the sheet and pulls your decision columns
back into the funnel: **interested = Yes** → queued for tailoring, **No** → rejected, **applied** → applied.
Mark jobs in the sheet, re-run the sync, then your local `tailor` run picks up the interested ones.

## Docker / AWS portability

The repo ships a production-lean multi-stage Dockerfile for the **core pipeline** stages
(`run`, `enrich`, `route`, `migrate`, `recover_jd`, `web`).
Heavy local-only stages (`enrich_workday` = Playwright, `tailor` = pdflatex) are out of scope.

### Build

```bash
docker build -t jobmaxxing .
```

### Run a stage

All configuration is injected at runtime via environment variables — no secrets are baked in.

```bash
# Apply migrations
docker run --rm -e DATABASE_URL=postgres://user:pass@host:5432/db \
  jobmaxxing -m jobmaxxing.migrate

# Run pollers
docker run --rm -e DATABASE_URL=postgres://... \
  jobmaxxing -m jobmaxxing.run

# Route with LLM fallback (LLM keys optional — deterministic rules still work without them)
docker run --rm \
  -e DATABASE_URL=postgres://... \
  -e OPENAI_API_KEY=sk-... \
  -e ANTHROPIC_API_KEY=... \
  jobmaxxing -m jobmaxxing.route

# Local web triage table
docker run --rm \
  -e DATABASE_URL=postgres://... \
  -p 8765:8765 \
  jobmaxxing -m jobmaxxing.web
```

### In-scope vs out-of-scope

| Stage | In image | Notes |
|-------|----------|-------|
| `migrate` | Yes | |
| `run` (pollers) | Yes | |
| `enrich` | Yes | |
| `route` | Yes | LLM keys optional |
| `recover_jd` | Yes | |
| `web` | Yes | Flask; `--extra web` |
| `enrich_workday` | No | Requires Playwright/Chromium |
| `tailor` | No | Requires pdflatex/TeX Live |

### AWS path (July)

The image is already AWS-portable: env-var config, stateless compute, S3 via boto3 default
credential chain (IAM-role ready).  Next steps are: push to ECR, define a Fargate task per
stage, inject secrets from Parameter Store / Secrets Manager.  No application code changes needed.

See `docs/superpowers/specs/2026-06-16-dockerfile-aws-portability-design.md` for the full design.

## Status & open items

Phases 1–4 are built: core feed (ingestion), routing, tailoring, and the MCP
interface (above). JobSpy + Gmail discovery (Phase 5) and human-gated form-fill
(Phase 6) are still to come.

Before relying on a source in production, verify its live JSON shape against the
recorded fixtures in `tests/fixtures/` — the real Simplify/Greenhouse/Lever/Ashby
payloads should be spot-checked once (they were authored, not captured). The routing
signal dictionaries (`config/routing.yaml`) and the tailoring keyword rubrics
(`rubrics/{type}.json`) are seed values to tune against real jobs.
