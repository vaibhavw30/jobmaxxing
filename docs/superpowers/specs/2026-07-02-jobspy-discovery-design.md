# JobSpy discovery source — design

## Context
Phase 5a (broader discovery). Today the feed comes from curated GitHub Simplify-format lists +
watchlist ATS boards (Greenhouse/Lever/Ashby), polled every 3h in CI. This adds a **local,
operator-run** worker that pulls internship postings from the big job boards via the free
open-source **JobSpy** library (`python-jobspy`), scraped from the operator's **residential IP**.

It must be local-only (like `enrich_workday`/`recover_jd`): the job boards aggressively 429 datacenter
IPs, so this can never run in CI. Free — no paid scraping API. Research (JobSpy docs + community, 2026):
**Indeed** is the most reliable JobSpy target ("no rate limiting", descriptions by default,
`job_type="internship"` supported); **LinkedIn** is the most restrictive (rate-limits ~the 10th page
on a single IP; proxies needed beyond) but valuable and can fetch full JDs. Glassdoor/Google/ZipRecruiter
are all aggressively anti-bot.

## Goal
`python -m jobmaxxing.discover_jobspy` — a bounded, fail-soft local worker that scrapes the configured
internship searches, normalizes results into `JobRecord`s, and ingests them into the shared `jobs`
table via the **existing** `ingest_records` (dedupe + URL-canonicalization + storage). Many results
carry descriptions → they route immediately (no enrichment needed). All network/pandas access is
behind an **injected scrape function**, so the whole thing is unit/integration-tested with fakes and
CI never imports JobSpy.

## Design

### Site selection & config (`config/jobspy.yaml`, new)
Default: **Indeed (primary) + LinkedIn (bounded secondary)**, US-wide incl. remote, internships.
Glassdoor/Google/ZipRecruiter off by default (tunable). `is_remote` is NOT forced — "US-wide +
remote" means search the US (which includes remote roles). LinkedIn `results_wanted` is kept small so
a single residential IP stays under its ~10th-page limit.

```yaml
location: "United States"
country_indeed: "USA"
job_type: "internship"
hours_old: 720                 # ~30 days; drop ancient postings
linkedin_fetch_description: true
sites: ["indeed", "linkedin"]  # tunable: glassdoor, google, zip_recruiter also supported
results_wanted:                # per site, per search term
  indeed: 200
  linkedin: 75                 # bounded — under LinkedIn's ~10th-page single-IP limit
  glassdoor: 100
  google: 100
  zip_recruiter: 100
search_terms:                  # seeded from the 8 resume types; tune freely
  - "software engineer intern"
  - "machine learning intern"
  - "quantitative trader intern"
  - "quantitative developer intern"
  - "data scientist intern"
  - "artificial intelligence intern"
  - "robotics intern"
  - "autonomous vehicle intern"
```

### Components / files
- **`config/jobspy.yaml`** (new) — the searches above.
- **`src/jobmaxxing/discovery/__init__.py`** (new).
- **`src/jobmaxxing/discovery/jobspy_source.py`** (new):
  - `parse_jobspy(rows: list[dict], *, site: str) -> list[JobRecord]` — **pure, defensive**. `source =
    f"jobspy:{site}"`. For each row: skip unless it has a non-blank `title`, `company`, and `job_url`
    (fail-soft, mirrors the ATS/Simplify adapters). Map:
    - `url = job_url`
    - `description` = the `description` string, or `None` if missing/blank/NaN (pandas gives `float('nan')`
      for empty cells — use a `_clean_str` helper that rejects non-str and NaN).
    - `location` = a `location` string if present, else join non-empty `city`/`state`/`country` with
      ", "; `None` if empty.
    - `posted_at` = `date_posted` coerced to a UTC `datetime` (accept `datetime`, `date`→midnight UTC,
      ISO `str`; `None` on NaN/blank/parse-failure).
    - `external_id` = `job_url` (stable per posting).
    - `is_active = True`.
    - `dedupe_key = make_dedupe_key(company, title)` (the existing normalizer). `JobRecord.__post_init__`
      already strips whitespace.
    Returns `list[JobRecord]`. No pandas/network import.
  - `discover_jobspy(conn, *, scrape, config, now) -> dict` — the worker (only `scrape` is injected,
    mirroring `run_sources`, which calls `ingest_records` directly). For each `site` in
    `config["sites"]`, for each `term` in `config["search_terms"]`: build a `search` dict (site, term,
    location, results_wanted=`config["results_wanted"][site]`, hours_old, country_indeed, job_type, and
    `linkedin_fetch_description` when `site == "linkedin"`); call `rows = scrape(search)`; `records =
    parse_jobspy(rows, site=site)`; `counts = ingest_records(conn, records, now=now)`; record
    `report[f"jobspy:{site}:{slug(term)}"] = {"status": "ok", **counts}`.
    **Fail-soft:** wrap each search in `try/except Exception` → on error log a warning and set
    `report[key] = {"status": "error", "error": str(exc)}`; the next search still runs. Return the
    report dict.
  - `_jobspy_scrape(search) -> list[dict]` — the **thin real adapter** (the ONLY network/pandas code):
    lazy `from jobspy import scrape_jobs`; call `scrape_jobs(site_name=[search["site"]],
    search_term=search["term"], location=..., results_wanted=..., hours_old=..., country_indeed=...,
    job_type=..., linkedin_fetch_description=search.get("linkedin_fetch_description", False))`; return
    `df.to_dict("records")` (empty list if the DataFrame is empty/None). Injected as the default
    `scrape`, so unit/integration tests pass a fake and never import JobSpy.
  - `load_jobspy_config(path=None) -> dict` — `yaml.safe_load` of `config/jobspy.yaml` (mirrors
    `routing.config.load_routing_config`); path override for tests.
  - `main() -> None` — `logging.basicConfig(...)`; `load_settings()`; `load_jobspy_config()`;
    `psycopg.connect`; `report = discover_jobspy(conn, scrape=_jobspy_scrape, config=cfg,
    now=datetime.now(timezone.utc))`; log a per-source summary. Mirrors `enrich_workday`'s `main`.
- **`src/jobmaxxing/discover_jobspy.py`** (new) — CLI shim: `from .discovery.jobspy_source import main`.
- **`pyproject.toml`** — `[project.optional-dependencies]` add `discovery = ["python-jobspy>=1.1.80"]`
  (pulls pandas; lazy-imported, so CI/base install is unaffected).
- **`README.md`** — a "JobSpy discovery (local, operator-run)" section: `uv sync --extra discovery`,
  `uv run python -m jobmaxxing.discover_jobspy` (residential IP), points at `config/jobspy.yaml`, notes
  it's NOT in CI and that results with descriptions route immediately.

### Data flow
`config/jobspy.yaml` → per (site, term): `scrape` (DataFrame→list[dict]) → `parse_jobspy` → `JobRecord`s
(`source="jobspy:{site}"`) → `ingest_records` (dedupe via `dedupe_key` + URL canonicalize + upsert). New
rows enter the same `jobs` table the CI pollers use, so routing / enrichment / the triage table / the
recency sort all work on them unchanged. Indeed rows (and LinkedIn with `fetch_description`) arrive with
`description` set → routable on the next `route` run without enrichment.

### Error handling / robustness
- **Fail-soft per search** — one site/term's 429, network error, or parse blowup never blocks the
  others; each is caught, logged, and surfaced in the report as `status: "error"`.
- **Defensive parse** — malformed/blank rows skipped; NaN-safe for `description`/`date_posted`.
- **Bounded** — `results_wanted` per site (JobSpy also caps ~1,000/search); `hours_old` drops ancient.
- **Lazy import** — `discovery/jobspy_source.py` imports fine without the `discovery` extra; only
  `_jobspy_scrape` imports `jobspy`, and only when actually invoked.
- **No CI workflow** — this is local-only by design (like `enrich_workday`); no `.github/workflows`
  change. The operator runs it on their residential IP and spaces runs to avoid 429s.

### Testing (pyramid; no regression to existing sources)
- **Unit — `tests/test_jobspy_parse.py`:** `parse_jobspy` maps a fake row list correctly
  (`source="jobspy:indeed"`, url/description/location/posted_at/external_id/dedupe_key); skips rows
  missing title/company/job_url; `float('nan')` description → `None`; NaN/blank `date_posted` →
  `posted_at=None`; `date`→UTC `datetime`; location assembled from city/state/country.
- **Integration — `tests/test_jobspy_discover.py`** (pytest-postgresql `conn` + `apply_migrations`):
  `discover_jobspy` with a **fake `scrape`** returning canned rows → rows land in `jobs` (query the DB),
  dedupe holds (same key twice → one row), `source` is `jobspy:{site}`; **fail-soft** — a `scrape` that
  raises for one (site, term) leaves the others ingested and marks that search `status:"error"` in the
  report. (Uses the real `ingest_records`.)
- **Config — `tests/test_jobspy_config.py`:** `load_jobspy_config()` parses the shipped
  `config/jobspy.yaml` and contains `sites` (incl. indeed+linkedin), `search_terms`, `results_wanted`.
- **E2E — `tests/test_jobspy_e2e.py`** (skip unless `JOBMAXXING_E2E=1` AND `jobspy` importable): one
  tiny real `_jobspy_scrape` Indeed search (small `results_wanted`) returns list-of-dicts and
  `parse_jobspy` yields records without error. Network-gated, mirrors `test_workday_e2e`.
- **No regression:** existing source/pipeline tests (`test_github_lists`, `test_ats`, `test_merge`,
  `test_integration`, etc.) stay green — this only adds a new source module + config + extra.

## Out of scope
Scheduling (the separate launchd sub-project); proxy rotation (bounded pulls + residential IP instead);
salary fields (not on `JobRecord`); Google/Glassdoor/ZipRecruiter default-on (tunable); a one-off
inter-search sleep (operator spaces runs); backfilling old whitespace-y company rows (separate task).

## Execution
Isolated git worktree off `main`; subagent-driven TDD (implementer reads the sources/normalize/pipeline
modules + an existing local worker for context first); two-stage review (spec → quality) per task;
merge to `main`; push (gh `vaibhavw30`).
