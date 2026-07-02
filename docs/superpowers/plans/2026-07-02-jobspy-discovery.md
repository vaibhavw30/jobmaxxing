# JobSpy discovery source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, operator-run `python -m jobmaxxing.discover_jobspy` worker that scrapes configured internship searches via the free JobSpy library and ingests them into the shared `jobs` table.

**Architecture:** A pure defensive adapter (`parse_jobspy`) turns JobSpy DataFrame rows into `JobRecord`s; a fail-soft worker (`discover_jobspy`) iterates the configured (site, term) searches through an **injected** `scrape` function and ingests via the existing `ingest_records`. Only a thin `_jobspy_scrape` touches JobSpy/pandas/network (lazy import), so everything is unit/integration-tested with fakes and CI never imports JobSpy. A small `canonicalize_url` fix keeps Indeed's identity-in-query links usable.

**Tech Stack:** Python 3.12, psycopg3, PyYAML, `python-jobspy` (opt-in `discovery` extra, pulls pandas), pytest + pytest-postgresql.

## Global Constraints
- Python **3.12**; run pytest with the Postgres binary on PATH:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- **Local-only:** no `.github/workflows` change — this worker runs on the operator's residential IP
  (datacenter IPs get 429'd), exactly like `enrich_workday`/`recover_jd`.
- **Opt-in extra, lazy import:** `discovery/jobspy_source.py` must import fine WITHOUT the `discovery`
  extra installed; only `_jobspy_scrape` may `import jobspy`, and only when called. The base package and
  CI must never require `python-jobspy`.
- **No regression:** existing source/pipeline/normalize tests (`test_github_lists`, `test_ats`,
  `test_merge`, `test_pipeline`, `test_integration`, and any normalize tests) stay green.
- **Reuse, don't reinvent:** `JobRecord` (`models.py`, already whitespace-strips in `__post_init__`),
  `make_dedupe_key` (`normalize.py`), `ingest_records` (`pipeline.py`), `REPO_ROOT` + `yaml.safe_load`
  config pattern (`routing/config.py::load_routing_config`), the `enrich_workday.py` shim + `conn`
  fixture (`postgresql` + `apply_migrations`) test pattern.
- Push with the `vaibhavw30` gh account.

---

## Task 1: `canonicalize_url` — preserve query for identity-in-query hosts

**Why:** `ingest_records` → `_canonicalize` → `canonicalize_url` strips ALL query params. Indeed job
URLs are `https://www.indeed.com/viewjob?jk=<id>` — the `jk` IS the job identity, so stripping it
yields a dead `…/viewjob` link (breaking the triage "Open posting" button). The function's own
docstring flags this exact case. Fix: keep the query for an allowlist of identity-in-query hosts. Safe
for existing sources — none of them use `indeed.com`/`glassdoor.com` URLs.

**Files:**
- Modify: `src/jobmaxxing/normalize.py` (`canonicalize_url`)
- Test: `tests/test_normalize_url.py` (create)

**Interfaces:**
- Produces: `canonicalize_url(url: str) -> str` — unchanged signature; now keeps the query string when
  the host is (a subdomain of) an entry in `_IDENTITY_QUERY_HOSTS = ("indeed.com", "glassdoor.com")`.

- [ ] **Step 1: Write failing tests.** Create `tests/test_normalize_url.py`:
```python
from jobmaxxing.normalize import canonicalize_url


def test_indeed_keeps_jk_query():
    assert canonicalize_url("https://www.indeed.com/viewjob?jk=abc123&utm=x") == \
        "https://www.indeed.com/viewjob?jk=abc123&utm=x"

def test_glassdoor_keeps_query():
    assert canonicalize_url("https://www.glassdoor.com/job-listing/x?jl=999") == \
        "https://www.glassdoor.com/job-listing/x?jl=999"

def test_non_identity_host_still_strips_query():
    # unchanged behavior for the existing sources
    assert canonicalize_url("https://simplify.jobs/p/x?utm_source=g") == "https://simplify.jobs/p/x"

def test_linkedin_path_identity_unaffected():
    assert canonicalize_url("https://www.linkedin.com/jobs/view/12345?trk=y") == \
        "https://www.linkedin.com/jobs/view/12345"

def test_schemeless_returned_unchanged():
    assert canonicalize_url("indeed.com/viewjob?jk=z") == "indeed.com/viewjob?jk=z"
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_normalize_url.py -q`
Expected: FAIL (indeed/glassdoor cases strip the query today).

- [ ] **Step 2: Implement.** In `src/jobmaxxing/normalize.py`, replace the body of `canonicalize_url`.
The current function is:
```python
def canonicalize_url(url: str) -> str:
    """..."""
    stripped = url.strip()
    parts = urlsplit(stripped)
    if not parts.scheme or not parts.netloc:
        return stripped
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
```
Add a module-level constant near the other regexes and update the function:
```python
# Hosts that encode job identity in the query string (e.g. Indeed's ?jk=, Glassdoor's ?jl=).
# For these, the query is KEPT so the stored link stays usable; all other hosts drop it (tracking).
_IDENTITY_QUERY_HOSTS = ("indeed.com", "glassdoor.com")


def canonicalize_url(url: str) -> str:
    """Lowercase scheme+host, drop fragment, strip trailing slash (keep root). Drops the query for
    tracking-param collapse EXCEPT on identity-in-query hosts (_IDENTITY_QUERY_HOSTS), where the query
    is preserved so the job link isn't broken. Non-absolute URLs are returned unchanged."""
    stripped = url.strip()
    parts = urlsplit(stripped)
    if not parts.scheme or not parts.netloc:
        return stripped
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    keep_query = any(host == h or host.endswith("." + h) for h in _IDENTITY_QUERY_HOSTS)
    query = parts.query if keep_query else ""
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))
```
(Keep the existing module docstring/CAVEAT comment above the function if present; it now describes the mitigated behavior.)

- [ ] **Step 3: Run the new test + the existing normalize/merge/pipeline suites** (no regression).
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_normalize_url.py tests/test_merge.py tests/test_pipeline.py -q`
Expected: PASS (all). If a pre-existing test asserted query-stripping for an identity host, it would
surface here — there are none, so all pass.

- [ ] **Step 4: Commit**
```bash
git add src/jobmaxxing/normalize.py tests/test_normalize_url.py
git commit -m "normalize: keep query for identity-in-query hosts (Indeed/Glassdoor) in canonicalize_url

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `parse_jobspy` — pure defensive adapter

**Files:**
- Create: `src/jobmaxxing/discovery/__init__.py` (empty), `src/jobmaxxing/discovery/jobspy_source.py`
- Test: `tests/test_jobspy_parse.py`

**Interfaces:**
- Consumes: `JobRecord` (`..models`), `make_dedupe_key` (`..normalize`).
- Produces: `parse_jobspy(rows: list[dict], *, site: str) -> list[JobRecord]`. `source = f"jobspy:{site}"`.
  Skips rows lacking a non-blank `title`/`company`/`job_url`. Maps `job_url`→`url` (+`external_id`),
  `description` (NaN/blank→None), `location` (`location` str, else join city/state/country),
  `date_posted`→`posted_at` (UTC datetime; NaN/blank/bad→None), `dedupe_key=make_dedupe_key(company,title)`.

- [ ] **Step 1: Write failing unit tests.** Create `tests/test_jobspy_parse.py`:
```python
from datetime import date, datetime, timezone

from jobmaxxing.discovery.jobspy_source import parse_jobspy


def test_parse_maps_all_fields():
    rows = [{
        "title": "Software Engineer Intern", "company": "Acme",
        "job_url": "https://www.indeed.com/viewjob?jk=abc123",
        "description": "Build APIs.", "location": "Remote",
        "date_posted": date(2026, 6, 20),
    }]
    rec = parse_jobspy(rows, site="indeed")[0]
    assert rec.source == "jobspy:indeed"
    assert rec.company == "Acme" and rec.title == "Software Engineer Intern"
    assert rec.url == "https://www.indeed.com/viewjob?jk=abc123"
    assert rec.external_id == "https://www.indeed.com/viewjob?jk=abc123"
    assert rec.description == "Build APIs."
    assert rec.location == "Remote"
    assert rec.posted_at == datetime(2026, 6, 20, tzinfo=timezone.utc)
    assert rec.dedupe_key == "acme|software engineer intern"

def test_parse_skips_rows_missing_required_fields():
    rows = [
        {"title": "SWE Intern", "company": "Acme"},                     # no job_url
        {"title": "SWE Intern", "job_url": "https://x/1"},              # no company
        {"company": "Acme", "job_url": "https://x/2"},                  # no title
        {"title": " ", "company": "Acme", "job_url": "https://x/3"},    # blank title
    ]
    assert parse_jobspy(rows, site="indeed") == []

def test_parse_nan_description_and_date_become_none():
    nan = float("nan")
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "description": nan, "date_posted": nan}]
    rec = parse_jobspy(rows, site="linkedin")[0]
    assert rec.description is None and rec.posted_at is None
    assert rec.source == "jobspy:linkedin"

def test_parse_location_from_city_state_country():
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "city": "Atlanta", "state": "GA", "country": "USA"}]
    assert parse_jobspy(rows, site="indeed")[0].location == "Atlanta, GA, USA"

def test_parse_iso_string_date():
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "date_posted": "2026-06-01"}]
    assert parse_jobspy(rows, site="indeed")[0].posted_at == datetime(2026, 6, 1, tzinfo=timezone.utc)
```
Run: `uv run pytest tests/test_jobspy_parse.py -q` → FAIL (module/function missing).

- [ ] **Step 2: Implement.** Create `src/jobmaxxing/discovery/__init__.py` (empty). Create
`src/jobmaxxing/discovery/jobspy_source.py`:
```python
"""JobSpy discovery source — a local, operator-run worker (residential IP; `discovery` extra).

parse_jobspy is a pure, defensive adapter (no pandas/network). Only _jobspy_scrape imports jobspy,
lazily, so this module imports fine without the extra and CI never touches JobSpy.
"""

import logging
from datetime import date, datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key

logger = logging.getLogger(__name__)


def _clean_str(value):
    """A trimmed non-empty string, or None. Non-strings (incl. pandas NaN floats) -> None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _coerce_dt(value):
    """Coerce JobSpy's date_posted to a tz-aware UTC datetime, or None (NaN/blank/unparseable)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _location(row):
    loc = _clean_str(row.get("location"))
    if loc:
        return loc
    parts = [_clean_str(row.get(k)) for k in ("city", "state", "country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def parse_jobspy(rows, *, site):
    """Normalize JobSpy rows (DataFrame.to_dict('records')) into JobRecords. Defensive: rows missing
    title/company/job_url are skipped (fail-soft). source = f'jobspy:{site}'."""
    source = f"jobspy:{site}"
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = _clean_str(row.get("company"))
        title = _clean_str(row.get("title"))
        url = _clean_str(row.get("job_url"))
        if not company or not title or not url:
            continue
        records.append(JobRecord(
            source=source,
            company=company,
            title=title,
            url=url,
            external_id=url,
            location=_location(row),
            description=_clean_str(row.get("description")),
            posted_at=_coerce_dt(row.get("date_posted")),
            dedupe_key=make_dedupe_key(company, title),
        ))
    return records
```
Note: check `datetime` before `date` (datetime is a `date` subclass).

- [ ] **Step 3: Run** `uv run pytest tests/test_jobspy_parse.py -q` → PASS (5).

- [ ] **Step 4: Commit**
```bash
git add src/jobmaxxing/discovery/__init__.py src/jobmaxxing/discovery/jobspy_source.py tests/test_jobspy_parse.py
git commit -m "discovery: parse_jobspy pure adapter (JobSpy rows -> JobRecord)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: config + `discover_jobspy` worker (fail-soft) + integration

**Files:**
- Create: `config/jobspy.yaml`
- Modify: `src/jobmaxxing/discovery/jobspy_source.py` (add `load_jobspy_config`, `_slug`, `discover_jobspy`)
- Test: `tests/test_jobspy_config.py`, `tests/test_jobspy_discover.py`

**Interfaces:**
- Consumes: `parse_jobspy` (Task 2), `ingest_records` (`..pipeline`), `REPO_ROOT` (`..config`), `yaml`.
- Produces:
  - `load_jobspy_config(path=None) -> dict`
  - `discover_jobspy(conn, *, scrape, config, now) -> dict` — iterates `config["sites"] × config["search_terms"]`,
    calls the injected `scrape(search) -> list[dict]`, `parse_jobspy`, then `ingest_records`; fail-soft
    per search; returns `{f"jobspy:{site}:{slug}": {"status": "ok", **counts} | {"status": "error", "error": str}}`.

- [ ] **Step 1: Create `config/jobspy.yaml`:**
```yaml
# JobSpy discovery searches (local worker: python -m jobmaxxing.discover_jobspy). Tune freely.
location: "United States"
country_indeed: "USA"
job_type: "internship"
hours_old: 720                 # ~30 days; drop ancient postings
linkedin_fetch_description: true
sites: ["indeed", "linkedin"]  # also supported: glassdoor, google, zip_recruiter
results_wanted:                # per site, per search term
  indeed: 200
  linkedin: 75                 # bounded — under LinkedIn's ~10th-page single-IP rate limit
  glassdoor: 100
  google: 100
  zip_recruiter: 100
search_terms:
  - "software engineer intern"
  - "machine learning intern"
  - "quantitative trader intern"
  - "quantitative developer intern"
  - "data scientist intern"
  - "artificial intelligence intern"
  - "robotics intern"
  - "autonomous vehicle intern"
```

- [ ] **Step 2: Write failing config + integration tests.** Create `tests/test_jobspy_config.py`:
```python
from jobmaxxing.discovery.jobspy_source import load_jobspy_config


def test_shipped_config_parses_with_indeed_and_linkedin():
    cfg = load_jobspy_config()
    assert "indeed" in cfg["sites"] and "linkedin" in cfg["sites"]
    assert cfg["search_terms"]                       # non-empty
    assert cfg["job_type"] == "internship"
    assert cfg["results_wanted"]["linkedin"] <= 100  # bounded for a single residential IP
```
Create `tests/test_jobspy_discover.py`:
```python
from datetime import date, datetime, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.discovery.jobspy_source import discover_jobspy


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_discover_ingests_indeed_and_is_failsoft_on_linkedin(conn):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def fake_scrape(search):
        if search["site"] == "linkedin":
            raise RuntimeError("429 blocked")
        return [{"title": "SWE Intern", "company": "Acme",
                 "job_url": "https://www.indeed.com/viewjob?jk=abc123",
                 "description": "api work", "date_posted": date(2026, 6, 20), "location": "Remote"}]

    config = {"sites": ["indeed", "linkedin"], "search_terms": ["software engineer intern"],
              "results_wanted": {"indeed": 10, "linkedin": 10}, "location": "United States"}
    report = discover_jobspy(conn, scrape=fake_scrape, config=config, now=now)

    assert report["jobspy:indeed:software-engineer-intern"]["status"] == "ok"
    assert report["jobspy:linkedin:software-engineer-intern"]["status"] == "error"

    row = conn.execute(
        "select company, title, source, description, url from jobs"
    ).fetchone()
    assert row[:4] == ("Acme", "SWE Intern", "jobspy:indeed", "api work")
    assert row[4] == "https://www.indeed.com/viewjob?jk=abc123"   # Task-1 fix: jk preserved end-to-end


def test_discover_dedupes_same_posting_across_terms(conn):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def fake_scrape(search):
        return [{"title": "SWE Intern", "company": "Acme",
                 "job_url": "https://www.indeed.com/viewjob?jk=abc123",
                 "date_posted": date(2026, 6, 20)}]

    config = {"sites": ["indeed"], "search_terms": ["term a", "term b"],
              "results_wanted": {"indeed": 10}}
    discover_jobspy(conn, scrape=fake_scrape, config=config, now=now)
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1   # one dedupe_key
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_jobspy_config.py tests/test_jobspy_discover.py -q` → FAIL (`load_jobspy_config`/`discover_jobspy` missing).

- [ ] **Step 3: Implement** in `src/jobmaxxing/discovery/jobspy_source.py`. Add these imports at the top
(alongside the existing ones): `import yaml`, `from ..config import REPO_ROOT`, `from ..pipeline import
ingest_records`. Then add:
```python
def load_jobspy_config(path=None) -> dict:
    """Load config/jobspy.yaml (mirrors routing.config.load_routing_config). Missing file -> {}."""
    path = path or REPO_ROOT / "config" / "jobspy.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _slug(term: str) -> str:
    return term.strip().lower().replace(" ", "-")


def discover_jobspy(conn, *, scrape, config, now) -> dict:
    """Run each (site, search_term) via the injected scrape fn, parse + ingest. Fail-soft per search:
    a 429/network/parse error on one never blocks the rest. Returns a per-search report."""
    sites = config.get("sites", [])
    terms = config.get("search_terms", [])
    results_wanted = config.get("results_wanted", {})
    report = {}
    for site in sites:
        for term in terms:
            key = f"jobspy:{site}:{_slug(term)}"
            search = {
                "site": site,
                "term": term,
                "location": config.get("location"),
                "results_wanted": results_wanted.get(site, 50),
                "hours_old": config.get("hours_old"),
                "country_indeed": config.get("country_indeed"),
                "job_type": config.get("job_type"),
            }
            if site == "linkedin":
                search["linkedin_fetch_description"] = config.get("linkedin_fetch_description", False)
            try:
                rows = scrape(search)
                records = parse_jobspy(rows, site=site)
                counts = ingest_records(conn, records, now=now)
                report[key] = {"status": "ok", **counts}
            except Exception as exc:  # fail-soft: one search never blocks the others
                logger.warning("jobspy search failed [%s]: %s", key, exc)
                report[key] = {"status": "error", "error": str(exc)}
    return report
```

- [ ] **Step 4: Run** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_jobspy_config.py tests/test_jobspy_discover.py tests/test_jobspy_parse.py -q` → PASS.

- [ ] **Step 5: Commit**
```bash
git add config/jobspy.yaml src/jobmaxxing/discovery/jobspy_source.py tests/test_jobspy_config.py tests/test_jobspy_discover.py
git commit -m "discovery: jobspy config + fail-soft discover_jobspy worker

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: real scrape adapter + entrypoint + packaging + docs + e2e

**Files:**
- Modify: `src/jobmaxxing/discovery/jobspy_source.py` (add `_jobspy_scrape`, `main`)
- Create: `src/jobmaxxing/discover_jobspy.py` (shim), `tests/test_jobspy_e2e.py`
- Modify: `pyproject.toml` (add `discovery` extra), `README.md` (new section)

**Interfaces:**
- Consumes: `discover_jobspy`, `load_jobspy_config` (Task 3), `load_settings` (`..config`), `psycopg`.
- Produces: `_jobspy_scrape(search) -> list[dict]` (lazy `import jobspy`); `main() -> None`;
  `python -m jobmaxxing.discover_jobspy`.

- [ ] **Step 1: Write the e2e test (skip-by-default).** Create `tests/test_jobspy_e2e.py`:
```python
"""Live JobSpy scrape — skipped unless JOBMAXXING_E2E=1 and the `discovery` extra is installed."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1", reason="set JOBMAXXING_E2E=1 for the live JobSpy scrape")


def test_live_indeed_scrape_parses():
    pytest.importorskip("jobspy")
    from jobmaxxing.discovery.jobspy_source import _jobspy_scrape, parse_jobspy
    rows = _jobspy_scrape({"site": "indeed", "term": "software engineer intern",
                           "location": "United States", "results_wanted": 5,
                           "country_indeed": "USA", "job_type": "internship"})
    assert isinstance(rows, list)
    records = parse_jobspy(rows, site="indeed")   # >= 0; network-dependent
    assert isinstance(records, list)
```
Run: `uv run pytest tests/test_jobspy_e2e.py -q` → **SKIPPED** (JOBMAXXING_E2E unset). That's the pass
condition for this step.

- [ ] **Step 2: Implement `_jobspy_scrape` + `main`** in `src/jobmaxxing/discovery/jobspy_source.py`.
Add `import psycopg` and `from ..config import load_settings, REPO_ROOT` (REPO_ROOT already imported in
Task 3 — keep one import), and `from datetime import timezone` is already imported. Append:
```python
def _jobspy_scrape(search: dict) -> list[dict]:
    """The ONLY network/pandas code: call JobSpy and return list-of-dict rows. Lazily imports jobspy so
    the module loads without the `discovery` extra."""
    from jobspy import scrape_jobs

    kwargs = dict(
        site_name=[search["site"]],
        search_term=search["term"],
        location=search.get("location"),
        results_wanted=search.get("results_wanted", 50),
        job_type=search.get("job_type"),
    )
    if search.get("hours_old") is not None:
        kwargs["hours_old"] = search["hours_old"]
    if search.get("country_indeed"):
        kwargs["country_indeed"] = search["country_indeed"]
    if "linkedin_fetch_description" in search:
        kwargs["linkedin_fetch_description"] = search["linkedin_fetch_description"]
    df = scrape_jobs(**kwargs)
    if df is None or len(df) == 0:
        return []
    return df.to_dict("records")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    config = load_jobspy_config()
    with psycopg.connect(settings.database_url) as conn:
        report = discover_jobspy(conn, scrape=_jobspy_scrape, config=config,
                                 now=datetime.now(timezone.utc))
    ok = sum(1 for r in report.values() if r.get("status") == "ok")
    for key, res in report.items():
        logger.info("%s: %s", key, res)
    print(f"jobspy discovery: {ok}/{len(report)} searches ok")
```

- [ ] **Step 3: Create the CLI shim** `src/jobmaxxing/discover_jobspy.py`:
```python
"""CLI shim: `python -m jobmaxxing.discover_jobspy` (run LOCALLY; needs the `discovery` extra)."""

from .discovery.jobspy_source import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add the `discovery` extra** to `pyproject.toml` under `[project.optional-dependencies]`:
```toml
discovery = ["python-jobspy>=1.1.79"]
```
(If `uv` cannot resolve that floor, lower it — the pin only needs `scrape_jobs`.) Then verify the
module still imports WITHOUT the extra:
Run: `uv run python -c "import jobmaxxing.discovery.jobspy_source as m; print('imports without jobspy:', hasattr(m, 'parse_jobspy'))"`
Expected: `imports without jobspy: True` (no ImportError — `jobspy` is only imported inside `_jobspy_scrape`).

- [ ] **Step 5: README section.** In `README.md`, after the "JD recovery (find-elsewhere, local)"
subsection (or near the other local workers), add:
```markdown
### JobSpy discovery (local, operator-run)

Pull internship postings from the big job boards (Indeed + LinkedIn by default) with the free
[JobSpy](https://github.com/speedyapply/JobSpy) library. **Run LOCALLY on a residential IP** — the
boards 429 datacenter IPs, so this is never in CI.

    uv sync --extra discovery
    uv run python -m jobmaxxing.discover_jobspy

It reads `config/jobspy.yaml` (sites, search terms seeded from the 8 resume types, `results_wanted`,
US-wide + remote, `job_type: internship`), scrapes each (site, term), and ingests results into the same
`jobs` table as the CI pollers — deduped by `company|title`. Indeed rows (and LinkedIn with
`linkedin_fetch_description`) arrive with descriptions, so they route immediately. Fail-soft: one
site/term getting rate-limited never blocks the rest. Space out runs to avoid 429s (LinkedIn is the
touchiest — its `results_wanted` is kept small).
```

- [ ] **Step 6: Full suite (no regression).**
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: PASS (all; `test_jobspy_e2e` SKIPPED; skip count +1 vs before).

- [ ] **Step 7: Commit**
```bash
git add src/jobmaxxing/discovery/jobspy_source.py src/jobmaxxing/discover_jobspy.py tests/test_jobspy_e2e.py pyproject.toml uv.lock README.md
git commit -m "discovery: jobspy real scrape adapter, entrypoint, `discovery` extra, docs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green,
   `test_jobspy_e2e` skipped.
2. Module imports without the extra (Task 4 Step 4 command) → True.
3. Optional live run (operator, residential IP, real DB): `uv sync --extra discovery && uv run python -m
   jobmaxxing.discover_jobspy` → logs per-search counts; new `jobspy:indeed`/`jobspy:linkedin` rows in
   the triage table with working "Open posting" links (jk preserved). Optionally
   `JOBMAXXING_E2E=1 uv run --extra discovery pytest tests/test_jobspy_e2e.py -v`.

## Risks & notes
- **Indeed link integrity** — solved by Task 1 (`canonicalize_url` keeps `?jk=`). The Task-3 integration
  test asserts it survives end-to-end through `ingest_records`.
- **Soft dedupe by `company|title`** — a company's multiple same-titled internships collapse to one row;
  this is the existing project-wide behavior (accepted), not new. Cross-source collapse with the GitHub
  lists is intended.
- **`posted_at=None` rows** — JobSpy rows without a date pass `parse_jobspy` with `posted_at=None`;
  confirm `within_age_cutoff(None, now)` keeps them (it should — unknown age is kept). The integration
  tests use dated rows for determinism.
- **Rate limits** — fail-soft + bounded `results_wanted` + local residential IP; no proxy handling (out
  of scope). LinkedIn kept small on purpose.
- **Lazy import** — `jobspy` only imported in `_jobspy_scrape`; verified by the Task-4 import check.

## Execution
Isolated git worktree off `main`; subagent-driven TDD, one task per subagent (implementer reads
`models.py`, `normalize.py`, `pipeline.py`, and `enrich_workday.py` for context first); two-stage review
(spec → quality) per task; full-suite green; merge to `main`; push (gh `vaibhavw30`).
