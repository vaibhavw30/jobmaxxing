# Spec — JD Enrichment (clean-API ATS)

**Type:** New feature (additive pipeline step)
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** Phases 1–4 + batched-writes (all merged). Adds a new `enrich` step between ingest and route.

---

## 1. Problem & rationale

The GitHub-list pollers (Simplify / pitt-csc / vanshb03) ingest postings as **title + company + URL only — no job description**. The actual JD lives on the ATS page the URL points to. The router's LLM tiebreaker is deliberately gated on having a JD (`route_one`: title-only ambiguity defers "until a JD arrives"), so description-less rows that the deterministic rules can't classify are **deferred and never routed**.

Live numbers (2026-06-14): of ~14,300 ingested rows, **2,711 routed by rules** and **11,582 deferred — every one with an empty description** (`unrouted_withdesc = 0`). Loading an LLM key changed nothing, because the blocker is missing JDs, not a missing model. The unlock is **fetching the JD** for these rows so the router can classify them.

This spec covers the **clean-API ATS sources** whose JDs are reachable over plain HTTP JSON:

| ATS | Backlog rows (no-desc) | Reachability |
| --- | --- | --- |
| Workday (`myworkdayjobs`/oracle) | ~6,100 | **Cloudflare-gated — OUT OF SCOPE (Phase 5b)** |
| Greenhouse | ~1,245 | Public JSON API |
| SmartRecruiters | ~501 | Public JSON API |
| Lever | ~418 | Public JSON API |
| Ashby | ~302 | Public JSON API |
| iCIMS + bespoke long-tail | ~2,500 | **OUT OF SCOPE** |

v1 target: **Greenhouse + Lever + Ashby + SmartRecruiters (~2,466 rows)**. Workday was evaluated and explicitly deferred: every tenant tested (PTC, Comcast, Vizient, Varian) returns `403 cloudflare` on the `wday/cxs` JSON endpoint, so it needs headless-browser + anti-bot infrastructure — its own scoped phase, not allowed to hold the clean-API win hostage.

### A note on stale links (verified, expected)
Probing real backlog URLs showed some jobs are already **closed** — e.g. `job-boards.greenhouse.io/incidentiq/jobs/7496418003` 404s because that posting is gone from the live board. This is normal: the GitHub lists lag the ATS. Enrichment fills the **live** rows and marks **dead** ones permanently-failed (never retried). Actual enriched yield will be somewhat below 2,466; that is correct behavior, not a bug.

### Scope
**In:** a new `enrich` pipeline step + `src/jobmaxxing/enrichment/` (adapter registry for the four clean-API ATS, bounded-concurrent fetch, attempt/failure tracking), migration `0004`, a CLI entrypoint, and a workflow step. **Out:** Workday/iCIMS/bespoke; HTML-page scraping; changing the router, merge, or store *logic*; any new dependency for headless browsing.

## 2. Architecture

One new step inserted into the existing flow:

```
ingest (GitHub lists, no JD)  →  ENRICH (fetch JD for supported ATS)  →  route (rules + LLM now fires)  →  tailor
```

New package `src/jobmaxxing/enrichment/`:

- **`adapters.py`** — one adapter per ATS. An adapter is a small, independently-testable unit with three pure functions:
  - `matches(url: str) -> bool` — host/path pattern test.
  - `api_url(url: str) -> str` — translate the human page URL to the per-job (or per-board) JSON endpoint.
  - `parse(payload: dict, url: str) -> str | None` — extract the description text; `None` if the posting is absent (→ permanent fail).

  A registry `ADAPTERS: list[Adapter]` is scanned in order; `adapter_for(url)` returns the first match or `None` (unsupported → permanent skip, never counted as a fetchable candidate).

- **`enrich.py`** — `enrich_new(conn, *, max_fetches, max_workers=8, fetch_json=...)`, mirroring `route_new`'s shape and the batched-write pattern:
  1. Select candidate rows (see §4 query), bounded to `max_fetches`.
  2. Fan out fetches over a `ThreadPoolExecutor(max_workers)`; each task: `adapter.api_url` → `fetch_json` → `adapter.parse`, returning a per-row **outcome** (`enriched` + description / `permanent` / `transient`, with the error string). One row's failure never affects another.
  3. Collect all outcomes, then **batch-write** in one transaction via `executemany` (consistent with the batched-writes work): set `description` + `enriched_at` on successes; bump `enrich_attempts` and set `enrich_error` on failures; mark permanents so they are never reselected.
  4. Return counts `{enriched, permanent_failed, transient_failed, candidates}`.

- **`__main__.py`** (or `src/jobmaxxing/enrich.py` CLI shim) — `python -m jobmaxxing.enrich`, calling `enrich_new` with the configured per-run cap. Logs a one-line summary like the router does.

### Adapter endpoint contracts (pinned with captured fixtures during TDD)

The exact field paths and id handling are verified against **live-job** JSON fixtures in the plan; the confirmed shapes from probing:

- **Greenhouse** — URL `job-boards.greenhouse.io/{token}/jobs/{id}` (also classic `boards.greenhouse.io/...`). API: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?content=true`; description = `content` (HTML-escaped → unescape). A `404` here means the posting closed → **permanent**. (The board-list endpoint `…/boards/{token}/jobs?content=true` is available as a fallback/verification but the per-job endpoint is primary.)
- **Lever** — URL `jobs.lever.co/{site}/{id}[/apply]` (strip a trailing `/apply`). API: `https://api.lever.co/v0/postings/{site}/{id}?mode=json`; description = `descriptionPlain` (fallback `description`). `404` → **permanent**.
- **Ashby** — URL `jobs.ashbyhq.com/{org}/{postingId}[/application]`. API: `https://api.ashbyhq.com/posting-api/job-board/{org}` returns the board's postings (each carries `id` matching `{postingId}`); select the matching posting and read its description field (`descriptionPlain`/`descriptionHtml` — pinned by fixture). Posting absent from board → **permanent**.
- **SmartRecruiters** — URL `jobs.smartrecruiters.com/{company}/{postingId}`. API: `https://api.smartrecruiters.com/v1/companies/{company}/postings/{postingId}`; description assembled from `jobAd.sections.*.text` (path pinned by fixture). `404` → **permanent**.

## 3. Failure model (attempt-capped, permanent vs transient)

Each fetch yields exactly one classification:

| Class | Triggers | Action |
| --- | --- | --- |
| **enriched** | 200 + parseable, non-empty description | write `description`, set `enriched_at`; row exits candidate set because `description` is now non-empty |
| **permanent** | 404 / 410 / unparseable body / posting absent / unsupported host | set `enrich_error`, mark so it is **never reselected** (set `enrich_attempts = CAP`) |
| **transient** | timeout / connection error / 429 / 5xx | `enrich_attempts += 1`, set `enrich_error`; retried next run until `enrich_attempts >= CAP` |

`CAP` (default **3**) bounds transient retries. 429 from a shared API gateway is transient — so bounded concurrency (§5) and the retry model compose: a rate-limit blip just defers the row to a later run.

## 4. Schema — migration `0004_enrichment.sql`

Add three columns to `jobs` (additive, no backfill needed; `description` already exists):

```sql
alter table jobs
  add column enrich_attempts int not null default 0,
  add column enriched_at     timestamptz,
  add column enrich_error    text;
```

**Candidate selection** (the query `enrich_new` runs, bounded by `max_fetches`):

```sql
select id, url from jobs
where coalesce(description, '') = ''      -- still no JD
  and route_method is distinct from 'manual'
  and enrich_attempts < %(cap)s           -- not exhausted / not permanent-marked
  and url ~* %(supported_hosts)s           -- supported ATS only (see below)
order by scraped_at desc                   -- freshest first (most likely still live)
limit %(max_fetches)s
```

**The host filter must be in SQL, not Python.** Workday (~6,100 unsupported rows) dominates the freshest end of the backlog; if we filtered host-support in Python *after* a `LIMIT`, a run could pull 500 Workday rows, discard them all, and fetch almost nothing while supported rows sit deeper down. So the query restricts to supported hosts directly — `%(supported_hosts)s` is a single regex alternation built from the registry (e.g. `greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com`) so the `LIMIT` is spent only on fetchable rows. `adapter_for(url)` in Python is then the precise per-row router (and a final guard that skips any rare URL the coarse regex matched but no adapter claims). Enrichment writes `description` + the three tracking columns **by row `id`**; it **does not change `source`**.

## 5. Bounded concurrency

Fetches run on a `ThreadPoolExecutor` with **`max_workers = 8`** (tunable). Rationale: the clean APIs are **shared gateways** (`boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`, `api.smartrecruiters.com`), so unbounded fan-out would hammer a single host → 429s. A small pool is gentle on well-provisioned public APIs and collapses a ~15-minute sequential pass into ~2 minutes; past ~8–10 workers the marginal speedup is tiny while 429 risk climbs, and we are not latency-critical. `max_fetches`/run starts high (~**500**) so the ~2,466 backlog clears in a couple of runs, then steady-state enriches only newly-ingested supported rows.

## 6. Correctness invariants (preserved, with how)

| Invariant | How it holds |
| --- | --- |
| Re-ingested GitHub-list rows (empty desc, every 3h) do **not** clobber an enriched description | `merge_records` sets `description = primary.description or secondary.description`; empty/`None` is falsy in Python, so the non-empty enriched value always survives, regardless of primacy. **Verified against current merge.py.** |
| Enrichment-tracking columns survive re-ingest upserts | `_UPDATE_SQL` does not reference `enrich_attempts/enriched_at/enrich_error`, so `upsert_jobs` leaves them untouched. |
| Router behavior unchanged | No edit to `route_one`/`route_new`; enrichment only adds JD text, which the existing LLM path then consumes. |
| Per-row isolation | One fetch's exception is caught and classified; it never aborts the batch (mirrors `route_new`'s try/except). |
| Idempotent / no infinite work | Success removes a row from the candidate set (`description` non-empty); permanent marks `enrich_attempts = CAP`; both are stable across re-runs. |
| Atomic, batched write | Outcomes collected, then one `with conn.transaction()` + `executemany` per outcome kind (insert N/A; updates only). |
| Single-user assumption | One run at a time; no row-level locking needed (consistent with batched-writes spec §4). |

## 7. Testing (TDD)

- **Adapter unit tests** (`tests/test_enrichment_adapters.py`): for each ATS, captured **live-job** JSON fixture under `tests/fixtures/enrichment/{ats}.json` → assert `matches`, `api_url` (URL→endpoint translation, incl. Lever `/apply` stripping and the Greenhouse new-host id), and `parse` (correct description text). Plus a "posting absent" fixture → `parse` returns `None`.
- **`enrich_new` DB tests** (`tests/test_enrich_db.py`, pytest-postgresql), with an **injected fake `fetch_json`** (deterministic, no network) covering every transition:
  - success → `description` filled, `enriched_at` set, `enrich_attempts` unchanged, counts `{enriched:1}`.
  - permanent (fake raises a 404-class error) → `enrich_error` set, `enrich_attempts = CAP`, row not reselected on a second call.
  - transient (fake raises a timeout-class error) → `enrich_attempts += 1`, reselected next call; after `CAP` transient failures → no longer selected.
  - `max_fetches` bound respected; unsupported-host rows skipped (no slot consumed).
  - **merge-no-clobber**: enrich a row, then `upsert_jobs` the same dedupe_key with empty description → description and tracking columns preserved.
- **All existing `tests/` stay green** (router, store/merge, batched-writes invariants).
- Concurrency is tested deterministically by mocking the fetcher; we do not assert on threads.

## 8. Workflow wiring

`pollers.yml`: new step **"Enrich descriptions"** between *Run pollers* and *Route new postings*:

```yaml
- name: Enrich descriptions
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
  run: uv run python -m jobmaxxing.enrich
```

No new secret (clean APIs are unauthenticated). Ordering matters: enrich must precede route so newly-filled JDs are routable in the same run.

## 9. Deliverables

- `src/jobmaxxing/enrichment/{__init__,adapters,enrich}.py` + `src/jobmaxxing/enrich.py` CLI shim.
- `migrations/0004_enrichment.sql` (3 columns), applied via existing `migrate.py`.
- Captured fixtures `tests/fixtures/enrichment/*.json`; new tests `test_enrichment_adapters.py`, `test_enrich_db.py`.
- `pollers.yml` enrich step.
- No change to router/store/merge logic; all existing tests green.

## 10. Open items (resolve during implementation, not blocking)

- Pin each adapter's exact description field path and id handling against captured live-job fixtures (Ashby description field; SmartRecruiters `jobAd` section assembly; Greenhouse HTML unescape).
- Confirm Lever site-token extraction on a known-live posting (the probed sample was a closed job).
- Decide whether to strip HTML from descriptions (Greenhouse `content` is HTML; Lever/SR offer plain text). Start by storing what the API returns; the router/scorer already tolerate raw text. Add stripping only if it measurably helps routing.
- If steady-state shows many permanent 404s from one stale list, that is informational only (the lists lag ATS); no action unless yield is surprisingly low.
