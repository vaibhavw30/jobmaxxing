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

## Status & open items

Phases 1–4 are built: core feed (ingestion), routing, tailoring, and the MCP
interface (above). JobSpy + Gmail discovery (Phase 5) and human-gated form-fill
(Phase 6) are still to come.

Before relying on a source in production, verify its live JSON shape against the
recorded fixtures in `tests/fixtures/` — the real Simplify/Greenhouse/Lever/Ashby
payloads should be spot-checked once (they were authored, not captured). The routing
signal dictionaries (`config/routing.yaml`) and the tailoring keyword rubrics
(`rubrics/{type}.json`) are seed values to tune against real jobs.
