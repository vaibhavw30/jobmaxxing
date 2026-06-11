# Core Feed Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deduped, auto-updating Postgres feed of internship postings, populated by independent Python pollers (GitHub curated lists + Greenhouse/Lever/Ashby ATS) running on GitHub Actions cron.

**Architecture:** A `src/jobmaxxing/` Python package. Source adapters are **pure transforms** (`parsed JSON → list[JobRecord]`) so they test against recorded fixtures with no network. A central pipeline applies an age cutoff and upserts into Postgres with conflict-driven enrichment (a pure `merge_records` function). Each poller wraps its body in try/except and exits 0 so one dead source never fails a run.

**Tech Stack:** Python 3.12, `uv`, `httpx` (HTTP), `psycopg` 3 (Postgres), `pyyaml`, `python-dotenv`, `pytest` + `pytest-postgresql`. Supabase Postgres. GitHub Actions cron.

**Spec:** `docs/superpowers/specs/2026-06-11-core-feed-ingestion-design.md`

---

## File Structure

```
pyproject.toml                       # uv project + deps + pytest config
.python-version                      # 3.12
.env.example                         # DATABASE_URL template (real .env gitignored)
.gitignore
README.md                            # provisioning + usage

src/jobmaxxing/
  __init__.py
  config.py                          # Settings (DATABASE_URL), watchlist loader
  models.py                          # JobRecord dataclass
  normalize.py                       # dedupe_key, canonicalize_url, age cutoff, ATS_SOURCES
  merge.py                           # merge_records (pure enrich-on-conflict logic)
  store.py                           # psycopg upsert_jobs (insert / lock+merge+update)
  migrate.py                         # apply migrations/*.sql in order
  pipeline.py                        # age-filter + dedupe_key fill + upsert orchestration
  run.py                             # CLI entry points; per-source try/except, exit 0
  sources/
    __init__.py
    github_lists.py                  # parse_simplify_format (Simplify/vanshb03/pitt-csc)
    ats.py                           # parse_greenhouse / parse_lever / parse_ashby
  fetch.py                           # thin httpx GET-json wrappers (not unit-tested)

config/
  watchlist.yaml                     # company -> ats -> token (operator fills in)

migrations/
  0001_create_jobs.sql
  0002_views.sql

.github/workflows/
  pollers.yml                        # scheduled + workflow_dispatch

tests/
  fixtures/
    simplify.json
    greenhouse.json
    lever.json
    ashby.json
  test_normalize.py
  test_merge.py
  test_store.py
  test_github_lists.py
  test_ats.py
  test_pipeline.py
  test_resilience.py
```

---

## Task 1: Project scaffolding (boilerplate first)

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `src/jobmaxxing/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: Pin Python and create the uv project files**

Create `.python-version`:

```
3.12
```

Create `pyproject.toml`:

```toml
[project]
name = "jobmaxxing"
version = "0.1.0"
description = "Internship recruiting pipeline - core feed ingestion"
requires-python = ">=3.12,<3.13"
dependencies = [
    "httpx>=0.27",
    "psycopg[binary]>=3.2",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-postgresql>=6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/jobmaxxing"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

Create `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
uv.lock
```

Create `.env.example`:

```
# Supabase Postgres connection string (session pooler or direct)
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/postgres
```

Create `src/jobmaxxing/__init__.py`:

```python
"""Internship recruiting pipeline - core feed ingestion."""
```

- [ ] **Step 2: Write a smoke test**

Create `tests/test_smoke.py`:

```python
import jobmaxxing


def test_package_imports():
    assert jobmaxxing.__doc__
```

- [ ] **Step 3: Sync deps and run the smoke test (verify it passes)**

Run:

```bash
uv python pin 3.12
uv sync
uv run pytest tests/test_smoke.py -v
```

Expected: 1 passed. (`uv sync` creates `.venv` and installs deps; `uv run` executes inside it.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .python-version .gitignore .env.example src/jobmaxxing/__init__.py tests/test_smoke.py
git commit -m "chore: scaffold jobmaxxing python project with uv"
```

---

## Task 2: JobRecord model

**Files:**
- Create: `src/jobmaxxing/models.py`, `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from datetime import datetime, timezone

from jobmaxxing.models import JobRecord


def test_jobrecord_defaults():
    rec = JobRecord(source="github:simplify", company="Acme", title="SWE Intern", url="https://x/y")
    assert rec.alt_urls == []
    assert rec.is_active is True
    assert rec.external_id is None
    assert rec.dedupe_key == ""


def test_jobrecord_accepts_all_fields():
    rec = JobRecord(
        source="greenhouse",
        company="Acme",
        title="SWE Intern",
        url="https://x/y",
        external_id="123",
        location="NYC",
        description="JD text",
        posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_active=False,
        alt_urls=["https://a"],
        dedupe_key="acme|swe intern",
    )
    assert rec.external_id == "123"
    assert rec.alt_urls == ["https://a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.models'`

- [ ] **Step 3: Write the implementation**

Create `src/jobmaxxing/models.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class JobRecord:
    """A normalized posting as produced by a source adapter."""

    source: str
    company: str
    title: str
    url: str
    external_id: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    is_active: bool = True
    alt_urls: list[str] = field(default_factory=list)
    dedupe_key: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/models.py tests/test_models.py
git commit -m "feat: add JobRecord model"
```

---

## Task 3: Normalization (dedupe key, URL canonicalization, age cutoff)

**Files:**
- Create: `src/jobmaxxing/normalize.py`, `tests/test_normalize.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_normalize.py`:

```python
from datetime import datetime, timedelta, timezone

from jobmaxxing.normalize import (
    ATS_SOURCES,
    canonicalize_url,
    make_dedupe_key,
    within_age_cutoff,
)


def test_dedupe_key_collapses_case_punctuation_whitespace():
    a = make_dedupe_key("Acme, Inc.", "Software   Engineer Intern")
    b = make_dedupe_key("acme inc", "software engineer intern")
    assert a == b
    assert a == "acme inc|software engineer intern"


def test_dedupe_key_distinguishes_different_titles():
    assert make_dedupe_key("Acme", "SWE Intern") != make_dedupe_key("Acme", "ML Intern")


def test_canonicalize_url_strips_query_fragment_and_trailing_slash():
    url = "HTTPS://Boards.Greenhouse.io/acme/jobs/123/?utm_source=x#apply"
    assert canonicalize_url(url) == "https://boards.greenhouse.io/acme/jobs/123"


def test_canonicalize_url_keeps_root_path():
    assert canonicalize_url("https://acme.com/") == "https://acme.com/"


def test_age_cutoff_keeps_recent_and_null_dates():
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    assert within_age_cutoff(now - timedelta(days=10), now) is True
    assert within_age_cutoff(None, now) is True  # no date -> never drop


def test_age_cutoff_rejects_old():
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    assert within_age_cutoff(now - timedelta(days=300), now) is False


def test_ats_sources_constant():
    assert ATS_SOURCES == {"greenhouse", "lever", "ashby"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.normalize'`

- [ ] **Step 3: Write the implementation**

Create `src/jobmaxxing/normalize.py`:

```python
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

ATS_SOURCES = {"greenhouse", "lever", "ashby"}

# ~8 months
MAX_AGE_DAYS = 243

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    value = value.lower().strip()
    value = _PUNCT.sub(" ", value)
    value = _WS.sub(" ", value).strip()
    return value


def make_dedupe_key(company: str, title: str) -> str:
    """Soft cross-source collapse key: normalized(company) | normalized(title)."""
    return f"{normalize_text(company)}|{normalize_text(title)}"


def canonicalize_url(url: str) -> str:
    """Lowercase scheme+host, drop query/fragment, strip trailing slash (keep root)."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def within_age_cutoff(
    posted_at: datetime | None, now: datetime, max_age_days: int = MAX_AGE_DAYS
) -> bool:
    """True if the posting should be ingested. Null dates are kept (we lack evidence it's stale)."""
    if posted_at is None:
        return True
    return posted_at >= now - timedelta(days=max_age_days)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/normalize.py tests/test_normalize.py
git commit -m "feat: add normalization (dedupe key, url canonicalization, age cutoff)"
```

---

## Task 4: Merge logic (enrich-on-conflict, pure function)

**Files:**
- Create: `src/jobmaxxing/merge.py`, `tests/test_merge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_merge.py`:

```python
from jobmaxxing.merge import merge_records
from jobmaxxing.models import JobRecord


def _rec(**kw):
    base = dict(source="github:simplify", company="Acme", title="SWE Intern", url="https://list/apply", dedupe_key="acme|swe intern")
    base.update(kw)
    return JobRecord(**base)


def test_merge_fills_description_when_existing_null():
    existing = _rec(description=None)
    incoming = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", description="full JD")
    merged = merge_records(existing, incoming)
    assert merged.description == "full JD"


def test_merge_keeps_existing_description_when_present():
    existing = _rec(description="original")
    incoming = _rec(description="newer")
    merged = merge_records(existing, incoming)
    assert merged.description == "original"


def test_merge_promotes_ats_url_over_list_url():
    existing = _rec(source="github:simplify", url="https://list/apply")
    incoming = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1")
    merged = merge_records(existing, incoming)
    assert merged.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert "https://list/apply" in merged.alt_urls
    assert merged.url not in merged.alt_urls


def test_merge_does_not_demote_existing_ats_url_for_list_url():
    existing = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1")
    incoming = _rec(source="github:simplify", url="https://list/apply")
    merged = merge_records(existing, incoming)
    assert merged.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert "https://list/apply" in merged.alt_urls


def test_merge_never_loses_a_url_and_dedups_alt_urls():
    existing = _rec(url="https://a", alt_urls=["https://b"])
    incoming = _rec(url="https://a", alt_urls=["https://b", "https://c"])
    merged = merge_records(existing, incoming)
    assert set(merged.alt_urls) == {"https://b", "https://c"}
    assert merged.url == "https://a"


def test_merge_fills_external_id_and_location_when_missing():
    existing = _rec(external_id=None, location=None)
    incoming = _rec(source="greenhouse", external_id="gh-1", location="NYC")
    merged = merge_records(existing, incoming)
    assert merged.external_id == "gh-1"
    assert merged.location == "NYC"


def test_merge_refreshes_is_active_from_incoming():
    existing = _rec(is_active=True)
    incoming = _rec(is_active=False)
    merged = merge_records(existing, incoming)
    assert merged.is_active is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.merge'`

- [ ] **Step 3: Write the implementation**

Create `src/jobmaxxing/merge.py`:

```python
from .models import JobRecord
from .normalize import ATS_SOURCES


def _is_ats(source: str) -> bool:
    return source.split(":", 1)[0] in ATS_SOURCES


def merge_records(existing: JobRecord, incoming: JobRecord) -> JobRecord:
    """Combine an existing row with a newly-seen duplicate. Never drops a URL.

    Rules:
    - canonical url: prefer an ATS source url over a non-ATS one; otherwise keep existing.
    - description / external_id / location / posted_at: fill only when existing is null.
    - alt_urls: every other url seen, deduped, excluding the chosen canonical url.
    - is_active: refreshed from the incoming (most recent) row.
    """
    incoming_ats = _is_ats(incoming.source)
    existing_ats = _is_ats(existing.source)

    if incoming_ats and not existing_ats:
        canonical_url = incoming.url
        source = incoming.source
    else:
        canonical_url = existing.url
        source = existing.source

    seen = [*existing.alt_urls, *incoming.alt_urls, existing.url, incoming.url]
    alt_urls = [u for u in dict.fromkeys(seen) if u != canonical_url]

    return JobRecord(
        source=source,
        company=existing.company,
        title=existing.title,
        url=canonical_url,
        external_id=existing.external_id or incoming.external_id,
        location=existing.location or incoming.location,
        description=existing.description or incoming.description,
        posted_at=existing.posted_at or incoming.posted_at,
        is_active=incoming.is_active,
        alt_urls=alt_urls,
        dedupe_key=existing.dedupe_key,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/merge.py tests/test_merge.py
git commit -m "feat: add enrich-on-conflict merge_records"
```

---

## Task 5: Database migration SQL + migrate runner

**Files:**
- Create: `migrations/0001_create_jobs.sql`, `migrations/0002_views.sql`, `src/jobmaxxing/config.py`, `src/jobmaxxing/migrate.py`

- [ ] **Step 1: Write the schema migration**

Create `migrations/0001_create_jobs.sql`:

```sql
create table if not exists jobs (
  id              uuid primary key default gen_random_uuid(),

  dedupe_key      text not null,
  source          text not null,
  external_id     text,

  company         text not null,
  title           text not null,
  location        text,
  url             text not null,
  alt_urls        text[] not null default '{}',
  description     text,
  posted_at       timestamptz,
  is_active       boolean not null default true,

  scraped_at      timestamptz not null default now(),

  -- later-phase columns (unused this sprint)
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

create index if not exists jobs_is_active_idx on jobs (is_active);
create index if not exists jobs_source_idx on jobs (source);
create index if not exists jobs_scraped_at_idx on jobs (scraped_at desc);
create index if not exists jobs_status_idx on jobs (status);
```

Create `migrations/0002_views.sql`:

```sql
-- Active postings not yet routed: the operator's working queue.
create or replace view active_unrouted as
  select id, company, title, location, url, source, posted_at, scraped_at
  from jobs
  where is_active = true and resume_type is null
  order by scraped_at desc;
```

- [ ] **Step 2: Write the failing test for the migrate runner**

Create `tests/test_store.py` (we will extend it in Task 7; start with the migration check):

```python
import os

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    # pytest-postgresql provides a fresh database via the `postgresql` fixture.
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        yield c


def test_apply_migrations_creates_jobs_table(conn):
    apply_migrations(conn)
    row = conn.execute("select count(*) from jobs").fetchone()
    assert row[0] == 0
    # view exists
    conn.execute("select * from active_unrouted").fetchall()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.migrate'` (or a pytest-postgresql setup error — see note below).

> **Note (one-time dev setup):** `pytest-postgresql` needs a local PostgreSQL server binary to spin up ephemeral databases. On macOS: `brew install postgresql@16` and ensure `pg_ctl`/`initdb` are on `PATH` (e.g. `export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"`). This is only for running tests, not for production.

- [ ] **Step 4: Write the config and migrate runner**

Create `src/jobmaxxing/config.py`:

```python
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    database_url: str


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return Settings(database_url=url)


def load_watchlist(path: Path | None = None) -> list[dict]:
    path = path or REPO_ROOT / "config" / "watchlist.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("companies", [])
```

Create `src/jobmaxxing/migrate.py`:

```python
from pathlib import Path

import psycopg

from .config import REPO_ROOT, load_settings

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Run every migrations/*.sql in filename order. Idempotent (uses IF NOT EXISTS / OR REPLACE)."""
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text())
        applied.append(path.name)
    conn.commit()
    return applied


def main() -> None:
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        applied = apply_migrations(conn)
    print(f"Applied migrations: {', '.join(applied)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_store.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add migrations/ src/jobmaxxing/config.py src/jobmaxxing/migrate.py tests/test_store.py
git commit -m "feat: add jobs schema, views, and migration runner"
```

---

## Task 6: Store layer (upsert with insert / lock+merge+update)

**Files:**
- Create: `src/jobmaxxing/store.py`
- Modify: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests (append to tests/test_store.py)**

Add to `tests/test_store.py`:

```python
from jobmaxxing.models import JobRecord
from jobmaxxing.store import upsert_jobs


def _rec(**kw):
    base = dict(source="github:simplify", company="Acme", title="SWE Intern", url="https://list/apply", dedupe_key="acme|swe intern")
    base.update(kw)
    return JobRecord(**base)


def test_upsert_inserts_new_row(conn):
    apply_migrations(conn)
    counts = upsert_jobs(conn, [_rec()])
    assert counts == {"inserted": 1, "merged": 0}
    row = conn.execute("select company, title, url from jobs").fetchone()
    assert row == ("Acme", "SWE Intern", "https://list/apply")


def test_upsert_merges_duplicate_and_enriches(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(source="github:simplify", url="https://list/apply", description=None)])
    counts = upsert_jobs(
        conn,
        [_rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", external_id="gh-1", description="full JD")],
    )
    assert counts == {"inserted": 0, "merged": 1}
    row = conn.execute(
        "select count(*), max(url), max(description), max(external_id), array_to_string(alt_urls, ',') from jobs"
    ).fetchone()
    assert row[0] == 1                                   # still one row
    assert row[1] == "https://boards.greenhouse.io/acme/jobs/1"  # ATS url promoted
    assert row[2] == "full JD"                           # description enriched
    assert row[3] == "gh-1"
    assert "https://list/apply" in row[4]                # old url preserved in alt_urls


def test_upsert_is_idempotent(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec()])
    upsert_jobs(conn, [_rec()])
    count = conn.execute("select count(*) from jobs").fetchone()[0]
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.store'`

- [ ] **Step 3: Write the implementation**

Create `src/jobmaxxing/store.py`:

```python
import psycopg
from psycopg.rows import dict_row

from .merge import merge_records
from .models import JobRecord

_INSERT_COLS = (
    "dedupe_key, source, external_id, company, title, location, "
    "url, alt_urls, description, posted_at, is_active"
)


def _record_values(rec: JobRecord) -> tuple:
    return (
        rec.dedupe_key,
        rec.source,
        rec.external_id,
        rec.company,
        rec.title,
        rec.location,
        rec.url,
        rec.alt_urls,
        rec.description,
        rec.posted_at,
        rec.is_active,
    )


def _row_to_record(row: dict) -> JobRecord:
    return JobRecord(
        source=row["source"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        external_id=row["external_id"],
        location=row["location"],
        description=row["description"],
        posted_at=row["posted_at"],
        is_active=row["is_active"],
        alt_urls=list(row["alt_urls"]),
        dedupe_key=row["dedupe_key"],
    )


def upsert_jobs(conn: psycopg.Connection, records: list[JobRecord]) -> dict:
    """Insert new rows; for dedupe_key conflicts, lock the row, merge, and update.

    Per-record transaction with SELECT ... FOR UPDATE makes overlapping pollers safe.
    """
    counts = {"inserted": 0, "merged": 0}
    for rec in records:
        with conn.transaction():
            existing = conn.execute(
                "select * from jobs where dedupe_key = %s for update",
                (rec.dedupe_key,),
                row_factory=dict_row,
            ).fetchone()

            if existing is None:
                conn.execute(
                    f"insert into jobs ({_INSERT_COLS}) values "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    _record_values(rec),
                )
                counts["inserted"] += 1
            else:
                merged = merge_records(_row_to_record(existing), rec)
                conn.execute(
                    "update jobs set source=%s, external_id=%s, location=%s, "
                    "url=%s, alt_urls=%s, description=%s, posted_at=%s, is_active=%s "
                    "where dedupe_key=%s",
                    (
                        merged.source,
                        merged.external_id,
                        merged.location,
                        merged.url,
                        merged.alt_urls,
                        merged.description,
                        merged.posted_at,
                        merged.is_active,
                        merged.dedupe_key,
                    ),
                )
                counts["merged"] += 1
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 4 passed (migration test + 3 store tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/store.py tests/test_store.py
git commit -m "feat: add upsert_jobs store layer with lock+merge"
```

---

## Task 7: GitHub-list source adapter (Simplify format)

**Files:**
- Create: `src/jobmaxxing/sources/__init__.py`, `src/jobmaxxing/sources/github_lists.py`, `tests/fixtures/simplify.json`, `tests/test_github_lists.py`

> The Simplify, vanshb03, and pitt-csc lists are forks sharing the same `listings.json` shape, so one parser handles all three with a different `source` label and list URL.

- [ ] **Step 1: Create the recorded fixture**

Create `tests/fixtures/simplify.json`:

```json
[
  {
    "id": "abc123",
    "company_name": "Acme",
    "title": "Software Engineer Intern",
    "locations": ["New York, NY", "Remote"],
    "url": "https://simplify.jobs/p/abc123?utm_source=GHList",
    "date_posted": 1749600000,
    "active": true
  },
  {
    "id": "def456",
    "company_name": "Globex",
    "title": "ML Intern",
    "locations": [],
    "url": "https://globex.com/careers/def456",
    "date_posted": 1749600000,
    "active": false
  }
]
```

- [ ] **Step 2: Write the failing tests**

Create `src/jobmaxxing/sources/__init__.py`:

```python
```

Create `tests/test_github_lists.py`:

```python
import json
from datetime import timezone
from pathlib import Path

from jobmaxxing.sources.github_lists import parse_simplify_format

FIXTURE = Path(__file__).parent / "fixtures" / "simplify.json"


def test_parse_simplify_maps_fields():
    payload = json.loads(FIXTURE.read_text())
    records = parse_simplify_format(payload, source="github:simplify")
    first = records[0]
    assert first.source == "github:simplify"
    assert first.company == "Acme"
    assert first.title == "Software Engineer Intern"
    assert first.location == "New York, NY, Remote"
    assert first.external_id == "abc123"
    assert first.is_active is True
    assert first.posted_at.tzinfo == timezone.utc
    assert first.dedupe_key == "acme|software engineer intern"


def test_parse_simplify_handles_empty_locations_and_inactive():
    payload = json.loads(FIXTURE.read_text())
    second = parse_simplify_format(payload, source="github:simplify")[1]
    assert second.location is None
    assert second.is_active is False


def test_parse_simplify_skips_entries_missing_required_fields():
    payload = [{"title": "No Company"}, {"company_name": "No Title"}]
    assert parse_simplify_format(payload, source="github:simplify") == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_github_lists.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.sources.github_lists'`

- [ ] **Step 4: Write the implementation**

Create `src/jobmaxxing/sources/github_lists.py`:

```python
from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key


def parse_simplify_format(payload: list[dict], source: str) -> list[JobRecord]:
    """Parse a Simplify-format listings.json (Simplify / vanshb03 / pitt-csc forks).

    Skips entries missing company_name, title, or url.
    """
    records: list[JobRecord] = []
    for entry in payload:
        company = entry.get("company_name")
        title = entry.get("title")
        url = entry.get("url")
        if not company or not title or not url:
            continue

        locations = entry.get("locations") or []
        location = ", ".join(locations) if locations else None

        posted_at = None
        epoch = entry.get("date_posted")
        if epoch:
            posted_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        records.append(
            JobRecord(
                source=source,
                company=company,
                title=title,
                url=url,
                external_id=entry.get("id"),
                location=location,
                posted_at=posted_at,
                is_active=bool(entry.get("active", True)),
                dedupe_key=make_dedupe_key(company, title),
            )
        )
    return records
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_github_lists.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/jobmaxxing/sources/__init__.py src/jobmaxxing/sources/github_lists.py tests/fixtures/simplify.json tests/test_github_lists.py
git commit -m "feat: add Simplify-format github list adapter"
```

---

## Task 8: ATS source adapters (Greenhouse, Lever, Ashby)

**Files:**
- Create: `src/jobmaxxing/sources/ats.py`, `tests/fixtures/greenhouse.json`, `tests/fixtures/lever.json`, `tests/fixtures/ashby.json`, `tests/test_ats.py`

- [ ] **Step 1: Create the recorded fixtures**

Create `tests/fixtures/greenhouse.json`:

```json
{
  "jobs": [
    {
      "id": 1001,
      "title": "Software Engineer Intern",
      "absolute_url": "https://boards.greenhouse.io/acme/jobs/1001",
      "updated_at": "2026-06-01T12:00:00-04:00",
      "location": {"name": "New York, NY"},
      "content": "We are hiring a software engineer intern. Python, distributed systems."
    }
  ]
}
```

Create `tests/fixtures/lever.json`:

```json
[
  {
    "id": "lev-2002",
    "text": "Quant Developer Intern",
    "hostedUrl": "https://jobs.lever.co/acme/lev-2002",
    "createdAt": 1748736000000,
    "categories": {"location": "Chicago, IL"},
    "descriptionPlain": "Low-latency C++ market data systems."
  }
]
```

Create `tests/fixtures/ashby.json`:

```json
{
  "jobs": [
    {
      "id": "ash-3003",
      "title": "ML Engineer Intern",
      "jobUrl": "https://jobs.ashbyhq.com/acme/ash-3003",
      "publishedAt": "2026-05-20T00:00:00Z",
      "location": "Remote",
      "descriptionPlain": "Training and inference, transformers, model eval."
    }
  ]
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ats.py`:

```python
import json
from datetime import timezone
from pathlib import Path

from jobmaxxing.sources.ats import parse_ashby, parse_greenhouse, parse_lever

FIX = Path(__file__).parent / "fixtures"


def test_parse_greenhouse():
    payload = json.loads((FIX / "greenhouse.json").read_text())
    rec = parse_greenhouse(payload, company="Acme")[0]
    assert rec.source == "greenhouse"
    assert rec.company == "Acme"
    assert rec.title == "Software Engineer Intern"
    assert rec.url == "https://boards.greenhouse.io/acme/jobs/1001"
    assert rec.external_id == "1001"
    assert rec.location == "New York, NY"
    assert "software engineer intern" in rec.description.lower()
    assert rec.posted_at.tzinfo is not None
    assert rec.dedupe_key == "acme|software engineer intern"


def test_parse_lever():
    payload = json.loads((FIX / "lever.json").read_text())
    rec = parse_lever(payload, company="Acme")[0]
    assert rec.source == "lever"
    assert rec.title == "Quant Developer Intern"
    assert rec.url == "https://jobs.lever.co/acme/lev-2002"
    assert rec.external_id == "lev-2002"
    assert rec.location == "Chicago, IL"
    assert rec.description.startswith("Low-latency")
    assert rec.posted_at.tzinfo == timezone.utc


def test_parse_ashby():
    payload = json.loads((FIX / "ashby.json").read_text())
    rec = parse_ashby(payload, company="Acme")[0]
    assert rec.source == "ashby"
    assert rec.title == "ML Engineer Intern"
    assert rec.url == "https://jobs.ashbyhq.com/acme/ash-3003"
    assert rec.external_id == "ash-3003"
    assert rec.location == "Remote"
    assert rec.posted_at.tzinfo is not None


def test_ats_parsers_skip_entries_missing_required_fields():
    assert parse_greenhouse({"jobs": [{"id": 9}]}, company="Acme") == []
    assert parse_lever([{"id": "x"}], company="Acme") == []
    assert parse_ashby({"jobs": [{"id": "x"}]}, company="Acme") == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.sources.ats'`

- [ ] **Step 4: Write the implementation**

Create `src/jobmaxxing/sources/ats.py`:

```python
from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key


def _record(company: str, source: str, title, url, external_id, location, description, posted_at):
    if not title or not url:
        return None
    return JobRecord(
        source=source,
        company=company,
        title=title,
        url=url,
        external_id=str(external_id) if external_id is not None else None,
        location=location,
        description=description,
        posted_at=posted_at,
        is_active=True,
        dedupe_key=make_dedupe_key(company, title),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_greenhouse(payload: dict, company: str) -> list[JobRecord]:
    out = []
    for job in payload.get("jobs", []):
        loc = (job.get("location") or {}).get("name")
        rec = _record(
            company=company,
            source="greenhouse",
            title=job.get("title"),
            url=job.get("absolute_url"),
            external_id=job.get("id"),
            location=loc,
            description=job.get("content"),
            posted_at=_parse_iso(job.get("updated_at")),
        )
        if rec:
            out.append(rec)
    return out


def parse_lever(payload: list[dict], company: str) -> list[JobRecord]:
    out = []
    for job in payload:
        posted_at = None
        created = job.get("createdAt")
        if created:
            posted_at = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
        rec = _record(
            company=company,
            source="lever",
            title=job.get("text"),
            url=job.get("hostedUrl"),
            external_id=job.get("id"),
            location=(job.get("categories") or {}).get("location"),
            description=job.get("descriptionPlain"),
            posted_at=posted_at,
        )
        if rec:
            out.append(rec)
    return out


def parse_ashby(payload: dict, company: str) -> list[JobRecord]:
    out = []
    for job in payload.get("jobs", []):
        rec = _record(
            company=company,
            source="ashby",
            title=job.get("title"),
            url=job.get("jobUrl"),
            external_id=job.get("id"),
            location=job.get("location"),
            description=job.get("descriptionPlain"),
            posted_at=_parse_iso(job.get("publishedAt")),
        )
        if rec:
            out.append(rec)
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ats.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/jobmaxxing/sources/ats.py tests/fixtures/greenhouse.json tests/fixtures/lever.json tests/fixtures/ashby.json tests/test_ats.py
git commit -m "feat: add Greenhouse/Lever/Ashby ATS adapters"
```

---

## Task 9: Pipeline (age filter + upsert orchestration)

**Files:**
- Create: `src/jobmaxxing/pipeline.py`, `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.pipeline import ingest_records


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _rec(title, posted_at):
    return JobRecord(
        source="github:simplify",
        company="Acme",
        title=title,
        url=f"https://x/{title}",
        posted_at=posted_at,
        dedupe_key=f"acme|{title.lower()}",
    )


def test_ingest_filters_old_and_upserts(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    records = [
        _rec("recent", now - timedelta(days=10)),
        _rec("ancient", now - timedelta(days=400)),
        _rec("undated", None),
    ]
    counts = ingest_records(conn, records, now=now)
    assert counts["inserted"] == 2          # recent + undated
    assert counts["skipped_old"] == 1
    titles = {r[0] for r in conn.execute("select title from jobs").fetchall()}
    assert titles == {"recent", "undated"}


def test_ingest_canonicalizes_urls_before_storing(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    rec = JobRecord(
        source="github:simplify",
        company="Acme",
        title="SWE Intern",
        url="https://simplify.jobs/p/abc?utm_source=x",
        alt_urls=["https://acme.com/careers/1/?ref=y"],
        posted_at=now,
        dedupe_key="acme|swe intern",
    )
    ingest_records(conn, [rec], now=now)
    row = conn.execute("select url, array_to_string(alt_urls, ',') from jobs").fetchone()
    assert row[0] == "https://simplify.jobs/p/abc"          # query stripped
    assert row[1] == "https://acme.com/careers/1"           # alt url canonicalized too
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.pipeline'`

- [ ] **Step 3: Write the implementation**

Create `src/jobmaxxing/pipeline.py`:

```python
from datetime import datetime

import psycopg

from .models import JobRecord
from .normalize import canonicalize_url, within_age_cutoff
from .store import upsert_jobs


def _canonicalize(rec: JobRecord) -> JobRecord:
    rec.url = canonicalize_url(rec.url)
    rec.alt_urls = [canonicalize_url(u) for u in rec.alt_urls]
    return rec


def ingest_records(conn: psycopg.Connection, records: list[JobRecord], now: datetime) -> dict:
    """Canonicalize URLs, apply the age cutoff, then upsert the survivors.

    Canonicalization happens here (the single chokepoint before storage) so tracking-param
    variants of the same link collapse in `url`/`alt_urls`.
    """
    fresh = [_canonicalize(r) for r in records if within_age_cutoff(r.posted_at, now)]
    skipped_old = len(records) - len(fresh)
    counts = upsert_jobs(conn, fresh)
    return {**counts, "skipped_old": skipped_old}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/pipeline.py tests/test_pipeline.py
git commit -m "feat: add ingest pipeline (age filter + url canonicalization + upsert)"
```

---

## Task 10: Fetch helpers + resilient runner (try/except, exit 0)

**Files:**
- Create: `src/jobmaxxing/fetch.py`, `src/jobmaxxing/run.py`, `config/watchlist.yaml`, `tests/test_resilience.py`

- [ ] **Step 1: Write the failing resilience test**

The runner must isolate sources: a source that raises is logged and skipped, and sibling sources still ingest. We test `run_sources` with fake source callables (no network).

Create `tests/test_resilience.py`:

```python
from datetime import datetime, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.run import run_sources


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _good_source():
    return [
        JobRecord(source="github:simplify", company="Acme", title="SWE Intern",
                  url="https://x/1", dedupe_key="acme|swe intern")
    ]


def _broken_source():
    raise RuntimeError("source is down")


def test_run_sources_isolates_failures(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    report = run_sources(
        conn,
        sources=[("broken", _broken_source), ("good", _good_source)],
        now=now,
    )
    # broken source recorded as failed, good source still ingested
    assert report["broken"]["status"] == "failed"
    assert "source is down" in report["broken"]["error"]
    assert report["good"]["status"] == "ok"
    assert report["good"]["inserted"] == 1
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resilience.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobmaxxing.run'`

- [ ] **Step 3: Write the fetch helpers, runner, and watchlist config**

Create `src/jobmaxxing/fetch.py`:

```python
import httpx

DEFAULT_TIMEOUT = 30.0


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT):
    """GET a URL and return parsed JSON. Raises on HTTP error (caller isolates per-source)."""
    resp = httpx.get(url, timeout=timeout, headers={"User-Agent": "jobmaxxing/0.1"})
    resp.raise_for_status()
    return resp.json()
```

Create `config/watchlist.yaml`:

```yaml
# Companies whose ATS boards we poll directly (full JDs).
# Fill in real entries. `token` is the public board slug.
companies: []
  # - company: "Example Corp"
  #   ats: greenhouse
  #   token: examplecorp
  # - company: "Acme"
  #   ats: lever
  #   token: acme
  # - company: "Globex"
  #   ats: ashby
  #   token: globex
```

Create `src/jobmaxxing/run.py`:

```python
import sys
from datetime import datetime, timezone

import psycopg

from .config import load_settings, load_watchlist
from .fetch import fetch_json
from .models import JobRecord
from .pipeline import ingest_records
from .sources.ats import parse_ashby, parse_greenhouse, parse_lever
from .sources.github_lists import parse_simplify_format

# Simplify-format curated lists (raw listings.json URLs).
GITHUB_LISTS = [
    ("github:simplify", "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("github:vanshb03", "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("github:pitt-csc", "https://raw.githubusercontent.com/pittcsc/Summer2026-Internships/dev/.github/scripts/listings.json"),
]

_ATS_PARSERS = {
    "greenhouse": (parse_greenhouse, "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"),
    "lever": (parse_lever, "https://api.lever.co/v0/postings/{token}?mode=json"),
    "ashby": (parse_ashby, "https://api.ashbyhq.com/posting-api/job-board/{token}"),
}


def _github_list_source(source: str, url: str):
    def fetch():
        return parse_simplify_format(fetch_json(url), source=source)
    return fetch


def _ats_source(company: str, ats: str, token: str):
    parser, url_tmpl = _ATS_PARSERS[ats]

    def fetch():
        return parser(fetch_json(url_tmpl.format(token=token)), company=company)
    return fetch


def build_sources() -> list[tuple[str, callable]]:
    sources: list[tuple[str, callable]] = []
    for source, url in GITHUB_LISTS:
        sources.append((source, _github_list_source(source, url)))
    for entry in load_watchlist():
        label = f"{entry['ats']}:{entry['token']}"
        sources.append((label, _ats_source(entry["company"], entry["ats"], entry["token"])))
    return sources


def run_sources(conn: psycopg.Connection, sources, now: datetime) -> dict:
    """Run each source in isolation. One failing source never blocks the others."""
    report: dict = {}
    for name, fetch in sources:
        try:
            records: list[JobRecord] = fetch()
            counts = ingest_records(conn, records, now=now)
            report[name] = {"status": "ok", **counts}
            print(f"[{name}] ok: {counts}")
        except Exception as exc:  # noqa: BLE001 - per-source isolation is the whole point
            report[name] = {"status": "failed", "error": str(exc)}
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
    return report


def main() -> None:
    settings = load_settings()
    now = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        run_sources(conn, build_sources(), now=now)
    # Always exit 0: a failing source is logged, not fatal.
    sys.exit(0)


if __name__ == "__main__":
    main()
```

> **Note:** `datetime.now(timezone.utc)` lives only in `main()` (real runtime). Tests always pass an explicit `now`, so they stay deterministic.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resilience.py -v`
Expected: 1 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/jobmaxxing/fetch.py src/jobmaxxing/run.py config/watchlist.yaml tests/test_resilience.py
git commit -m "feat: add resilient source runner and fetch helpers"
```

---

## Task 11: GitHub Actions workflow

**Files:**
- Create: `.github/workflows/pollers.yml`

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/pollers.yml`:

```yaml
name: pollers

on:
  schedule:
    - cron: "0 */3 * * *"   # every 3 hours (lists + ATS together; drift is fine)
  workflow_dispatch: {}       # manual trigger; NO pull_request (fork PRs must not touch secrets)

permissions:
  contents: read

concurrency:
  group: pollers
  cancel-in-progress: false

jobs:
  poll:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"

      - name: Sync deps
        run: uv sync --no-dev

      - name: Run pollers
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: uv run python -m jobmaxxing.run
```

- [ ] **Step 2: Validate the workflow parses (yaml lint)**

Run:

```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/pollers.yml')); print('workflow yaml ok')"
```

Expected: `workflow yaml ok`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/pollers.yml
git commit -m "ci: add scheduled pollers workflow"
```

---

## Task 12: README + provisioning docs

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the README**

Create `README.md`:

```markdown
# jobmaxxing — core feed ingestion

Auto-updating, deduped Postgres feed of internship postings. Phase 1 of the
recruiting pipeline (see `docs/PRD.md`, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md`,
and `docs/superpowers/specs/2026-06-11-core-feed-ingestion-design.md`).

## Setup

1. **Provision Supabase.** Create a free Supabase project. Copy the Postgres
   connection string (Project Settings → Database → Connection string).
2. **Local env.** `cp .env.example .env` and set `DATABASE_URL`.
3. **Install.** `uv sync`
4. **Migrate.** `uv run python -m jobmaxxing.migrate`
5. **CI secret.** In the GitHub repo: Settings → Secrets and variables → Actions
   → add `DATABASE_URL`. The repo is public, so secrets are only exposed to
   `schedule`/`workflow_dispatch` runs, never fork PRs.

## Adding a watch-list company

Edit `config/watchlist.yaml`:

```yaml
companies:
  - company: "Example Corp"
    ats: greenhouse        # greenhouse | lever | ashby
    token: examplecorp     # public board slug
```

## Running

- Manually: `uv run python -m jobmaxxing.run`
- Scheduled: GitHub Actions runs `.github/workflows/pollers.yml` every 3 hours.

## Querying the feed

Use the Supabase SQL editor / table browser. Convenience view:

```sql
select * from active_unrouted;   -- active postings not yet routed
```

## Tests

```bash
uv run pytest
```

Store/pipeline tests need a local PostgreSQL binary for `pytest-postgresql`
(`brew install postgresql@16`; put its `bin/` on PATH).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add setup and usage README"
```

---

## Self-Review notes (for the implementer)

- **Spec coverage:** schema (Task 5), dedupe_key=company+title with alt_urls/external_id (Tasks 3–4, 6), enrich-on-conflict (Task 4, 6), age cutoff + is_active (Tasks 3, 9), GitHub-list + ATS adapters with fixtures (Tasks 7–8), source isolation/exit-0 (Task 10), GitHub Actions + public-repo guardrails (Task 11), Supabase provisioning + SQL views (Tasks 5, 12), fixture-based no-network tests (Tasks 7–8). All spec sections map to a task.
- **Run the full suite** (`uv run pytest`) after Task 10 and again at the end; everything from Task 5 on needs the local Postgres binary.
- **Verify live source shapes** (open item from spec §11): before trusting the `listings.json` URLs and Ashby endpoint in `run.py`, fetch one of each by hand and confirm the JSON shape matches the fixtures. Adjust the parser + fixture together if a real payload differs.
```
