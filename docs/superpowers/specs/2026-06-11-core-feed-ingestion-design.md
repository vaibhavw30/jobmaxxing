# Spec — Phase 1: Core Feed Ingestion

**Sprint:** Phase 1 of the Internship Recruiting Pipeline (`docs/PRD.md` §10.1, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md` §11.1)
**Author:** Vaibhav
**Date:** 2026-06-11
**Status:** Approved for planning

---

## 1. Goal & rationale

Build the **reliable core feed**: a deduped, auto-updating Postgres table of internship postings, populated by independent Python pollers running on GitHub Actions cron. Sources are the curated GitHub internship lists and the public ATS board APIs (Greenhouse / Lever / Ashby).

This is the first sprint because:
- It is **useful standalone** — a deduped, auto-refreshing feed beats manual searching with zero LLM/tailoring.
- It is **zero-LLM and near-zero-cost**, safe to run all season.
- Every later phase (routing, tailoring, MCP) **reads from the table this sprint creates** — it is the foundation.

**Out of scope this sprint** (later phases, do not build): routing, tailoring, MCP server, JobSpy aggregator, Gmail alert parser, any review UI beyond saved SQL views. **No LLM calls anywhere in this sprint.**

## 2. Success criteria

- Running the pollers populates the `jobs` table with normalized, deduped postings from at least the GitHub-list and ATS sources.
- The same role appearing in multiple sources collapses to **one** row, and **no source URL for that role is lost**.
- Re-running any poller does not create duplicates or corrupt existing rows (idempotent).
- One source failing (network error, schema change, bad payload) does not prevent other sources in the same run from ingesting; the failure is logged with a reason.
- The operator can query the feed via the Supabase SQL editor / table browser, aided by saved views.
- Pure logic (normalization, dedupe key, URL canonicalization, upsert-enrich) is covered by tests that run against recorded fixtures, no live network.

## 3. Architecture

```
  GitHub Actions cron
        │
        ▼
  pollers/  (independent Python scripts)
   ├── github_lists  ── parse listings.json adapters ──┐
   └── ats           ── greenhouse / lever / ashby  ───┤
                                                       ▼
                                         normalize() → upsert()  (shared core)
                                                       │
                                                       ▼
                                          Supabase Postgres: jobs table
                                                       │
                                                       ▼
                                       operator queries via Supabase SQL / views
```

- A `pollers/` Python package. Each poller is an independent entry point that fetches **one source family**, normalizes to the `jobs` schema, and bulk-upserts.
- Shared core module(s): `normalize` (dedupe key, URL canonicalization, field cleaning, age-cutoff) and `store` (upsert-with-enrich against Supabase).
- Each poller wraps its whole body in try/except that logs structured counts and **exits 0** — one dead source never fails the GitHub Actions run.
- No orchestration primitive beyond cron + the table. No queue, no broker, no real-time anything (per tech plan §2).

## 4. Data model

One table, created in full now (tailoring columns nullable and unused this sprint, so no later migration churn).

```sql
create table jobs (
  id              uuid primary key default gen_random_uuid(),

  -- identity / dedupe
  dedupe_key      text not null,          -- normalized(company || title); soft cross-source collapse key
  source          text not null,          -- 'github:simplify', 'greenhouse', 'lever', 'ashby', ...
  external_id     text,                    -- stable ATS job id where available (greenhouse/lever/ashby)

  -- posting fields
  company         text not null,
  title           text not null,
  location        text,
  url             text not null,           -- canonical preferred link (ATS / full-JD wins over a list redirect)
  alt_urls        text[] default '{}',     -- every other URL seen for this job; a merge never drops a link
  description     text,                     -- full JD when available; null if link-only
  posted_at       timestamptz,
  is_active       boolean default true,    -- from the source's own open/closed marker when present

  -- bookkeeping
  scraped_at      timestamptz default now(),

  -- later-phase columns (created now, unused this sprint)
  resume_type     text,
  route_method    text,
  route_confidence real,
  status          text not null default 'new',
  artifact_prefix text,
  score_before    jsonb,
  score_after     jsonb,
  notes           text,

  unique (dedupe_key)
);

create index on jobs (is_active);
create index on jobs (source);
create index on jobs (scraped_at desc);
create index on jobs (status);
```

### 4.1 Dedupe key

`dedupe_key = normalized(company || title)` where `normalized` = lowercase, trim, collapse internal whitespace, strip punctuation, strip common title noise (e.g. trailing "(Remote)", season/year qualifiers handled conservatively — see open items). This is the **soft cross-source collapse key**: a GitHub-list entry and the direct ATS entry for the same role share company+title and therefore collapse to one row, even though their URLs differ.

**Accepted tradeoff:** two genuinely different reqs at one company with an identical title (e.g. two "Software Engineer Intern" roles on different teams) may false-merge into one row. For a single-user feed this is acceptable; `external_id` (below) and `alt_urls` mean we still retain the distinct links and can tell them apart on inspection. This is a deliberate choice over a URL-based key, which would *under*-merge and defeat cross-source dedupe.

### 4.2 Identity & "never forget a job"

- **`external_id`** captures the ATS's own stable job id (Greenhouse/Lever/Ashby return one) — a precise identity signal where we have it.
- **`url`** holds the single canonical preferred link, preferring an ATS/full-JD link over a list apply-redirect.
- **`alt_urls`** accumulates every *other* URL seen for the collapsed job, so a merge never discards a link. This directly addresses the "make sure we're not forgetting jobs" requirement.

### 4.3 Upsert / enrich on conflict

Every poller upserts with `on conflict (dedupe_key) do update` that **enriches** rather than blindly overwrites:
- Fill `description` only if currently null (a richer source later upgrades a link-only row to a full JD).
- Prefer an ATS `url`: if the incoming row is an ATS source and the existing `url` is a list redirect, promote the ATS link to `url` and demote the old one into `alt_urls`.
- Append any new incoming URL to `alt_urls` (dedup the array; never lose a link).
- Set `external_id` when newly known and currently null.
- Refresh `is_active` and `posted_at` if the incoming source provides a more authoritative value.
- Leave later-phase columns (`resume_type`, `status`, scores…) untouched.

Idempotent by construction: re-running a poller on unchanged source data produces no net change.

## 5. Sources & ingest rules

### 5.1 GitHub curated lists
- **Reliable path:** lists that publish structured `listings.json` — Simplify, vanshb03, Pitt CSC. Each list gets its own small adapter mapping its JSON shape → the `jobs` schema.
- **Best-effort/secondary:** README-table-only lists (e.g. SWE List) parsed defensively; failure here is logged and skipped, never fatal.
- Cadence: every 2–4 hours.

### 5.2 ATS direct pollers
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: public board endpoint per company.
- Driven by a **`watchlist.yaml`** config (company → ATS provider → token/slug). The actual company list is a config artifact the operator fills in; the spec/code ship the mechanism and an empty-or-seed config, not a hardcoded list.
- These return **full JDs** — the valuable text for later routing/tailoring.
- Cadence: every 4–6 hours.

### 5.3 Age cutoff & active flag
- Where a source provides a date, **skip postings older than ~8 months**.
- Where no date is available, ingest (do **not** silently drop — we don't have evidence it's stale).
- Store the source's own open/closed marker in `is_active` when present; default `true`.
- Closed-but-recent roles are retained (as `is_active=false`) for historical record; the operator filters on `is_active` when querying.

## 6. Reliability & ops

- **Source isolation:** each poller try/excepts its whole body, logs structured counts (`ingested`, `skipped`, `failed` with reason), exits 0.
- **DB-level idempotency:** `unique (dedupe_key)` + enrich-on-conflict upsert means overlapping sources and re-runs can't duplicate or corrupt.
- **Scheduling:** GitHub Actions scheduled workflows. Cron may drift 10–30 min or occasionally skip under load — acceptable at this cadence; the feed is not real-time.
- **Observability:** every run's log shows per-source ingested/skipped/failed with reasons. Failures are visible, not silent.

## 7. Security (public repo)

The repo is **public**. The Supabase service key lives in **GitHub Actions encrypted secrets** (not committed). Guardrails:
- Never echo secrets in logs or job output.
- Pollers run only on `schedule` and manual `workflow_dispatch` — **not** on `pull_request` from forks (fork PRs cannot read repo secrets, and we don't grant them the chance).
- `watchlist.yaml` and source configs contain only public board tokens/slugs (already public information) — safe to commit.

## 8. Setup (this sprint includes provisioning)

No Supabase project exists yet. Setup steps, in order:
1. Provision a Supabase project (free tier); capture the connection string + service key.
2. Add the service key (and project URL) as GitHub Actions secrets.
3. Run the schema migration (§4) against the project.
4. Land the saved SQL views (e.g. `active_unrouted` = `is_active and resume_type is null`).
5. Configure `watchlist.yaml` with the initial target companies (operator-provided; can start small).

## 9. Testing strategy

TDD on the pure, high-value logic; mock the thin network layer.
- **Normalization:** `dedupe_key` derivation, URL canonicalization (strip tracking/query params, trailing slash), `alt_urls` merge/dedup, age-cutoff decision. Pure functions, table-driven tests.
- **Source adapters:** each GitHub-list and ATS adapter tested against **recorded fixture payloads** (real captured JSON committed under `tests/fixtures/`), asserting correct mapping to the `jobs` schema. **No live network in tests.**
- **Upsert/enrich:** the conflict-resolution logic (description fill, ATS-url promotion, alt_urls append, external_id set) tested against a local/test Postgres.
- **Poller resilience:** a poller given a malformed/throwing source logs a failure and exits 0 without affecting a sibling source.

## 10. Deliverables

- Schema migration for the `jobs` table + saved SQL views.
- `pollers/` Python package: shared `normalize` + `store` core, GitHub-list adapters (Simplify, vanshb03, Pitt CSC), ATS adapters (Greenhouse, Lever, Ashby).
- `watchlist.yaml` config mechanism (+ seed entries).
- GitHub Actions workflow(s) for scheduled + manual runs.
- Test suite with recorded fixtures.
- A short README: provisioning steps, how to add a watch-list company, how to query the feed.

## 11. Open items (resolve during implementation, not blocking the spec)

- Exact title-normalization aggressiveness (how much season/year/“(Remote)” noise to strip without over-merging distinct roles). Start conservative; tune against real ingested data.
- Final initial `watchlist.yaml` company set (operator fills in; mechanism is what this sprint delivers).
- Whether Pitt CSC / which specific lists currently expose clean `listings.json` vs README-only — verify live during implementation and wire adapters accordingly.
- Exact GitHub Actions cron expressions within the 2–4h / 4–6h target bands.
