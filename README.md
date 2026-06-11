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

## Status & open items

This is Phase 1 (core feed) only. Routing, tailoring, the MCP server, JobSpy, and
Gmail ingestion are later phases. Before relying on a source in production, verify
its live JSON shape against the recorded fixtures in `tests/fixtures/` — the real
Simplify/Greenhouse/Lever/Ashby payloads should be spot-checked once (spec §11).
