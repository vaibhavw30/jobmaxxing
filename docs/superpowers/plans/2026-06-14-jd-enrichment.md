# JD Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch job descriptions for description-less Greenhouse/Lever/Ashby/SmartRecruiters rows so the LLM router can classify the deferred backlog.

**Architecture:** A new `enrichment/` package with an adapter-per-ATS registry (each adapter: `matches`/`api_url`/`parse`), a bounded-concurrent `enrich_new(conn)` that selects supported description-less rows, fetches JDs over a thread pool, classifies failures (permanent vs transient), and batch-writes results. A new `enrich` workflow step runs between ingest and route. Mirrors the existing `routing/route.py` + `route.py` CLI-shim split and the batched-writes pattern.

**Tech Stack:** Python 3.12, psycopg3, httpx, `concurrent.futures.ThreadPoolExecutor`, pytest + pytest-postgresql.

**Spec:** `docs/superpowers/specs/2026-06-14-jd-enrichment-design.md`

---

## File structure

- Create `src/jobmaxxing/enrichment/__init__.py` — package marker + re-exports.
- Create `src/jobmaxxing/enrichment/adapters.py` — `adapter_for`, `SUPPORTED_HOSTS_SQL`, four adapter classes, `ADAPTERS` registry.
- Create `src/jobmaxxing/enrichment/enrich.py` — `classify_error`, `Outcome`, `_fetch_one`, `enrich_new`, `main`.
- Create `src/jobmaxxing/enrich.py` — CLI shim (`python -m jobmaxxing.enrich`), mirrors `src/jobmaxxing/route.py`.
- Create `migrations/0004_enrichment.sql` — 3 tracking columns.
- Create `tests/test_enrichment_adapters.py` — adapter unit tests.
- Create `tests/test_enrich_db.py` — `enrich_new` DB tests (pytest-postgresql, injected fake fetcher).
- Modify `.github/workflows/pollers.yml` — add "Enrich descriptions" step between *Run pollers* and *Route new postings*.

All tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

### Task 1: Migration 0004 — enrichment tracking columns

**Files:**
- Create: `migrations/0004_enrichment.sql`
- Test: `tests/test_enrich_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrich_db.py` with the shared fixture and a schema test:

```python
import httpx
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_migration_adds_enrichment_columns(conn):
    cols = {
        row[0]
        for row in conn.execute(
            "select column_name from information_schema.columns where table_name='jobs'"
        ).fetchall()
    }
    assert {"enrich_attempts", "enriched_at", "enrich_error"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py::test_migration_adds_enrichment_columns -v`
Expected: FAIL — columns missing.

- [ ] **Step 3: Write the migration**

Create `migrations/0004_enrichment.sql` (idempotent — `apply_migrations` re-runs every file each call):

```sql
-- Enrichment tracking: how many fetch attempts a row has had, when it was last
-- enriched, and the last error. Permanent failures are marked by setting
-- enrich_attempts to the cap so the candidate query stops reselecting them.
alter table jobs add column if not exists enrich_attempts int not null default 0;
alter table jobs add column if not exists enriched_at     timestamptz;
alter table jobs add column if not exists enrich_error    text;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py::test_migration_adds_enrichment_columns -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/0004_enrichment.sql tests/test_enrich_db.py
git commit -m "feat(enrich): migration 0004 adds enrichment tracking columns"
```

---

### Task 2: Adapter framework + Greenhouse adapter

**Files:**
- Create: `src/jobmaxxing/enrichment/__init__.py`
- Create: `src/jobmaxxing/enrichment/adapters.py`
- Test: `tests/test_enrichment_adapters.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrichment_adapters.py`:

```python
from jobmaxxing.enrichment.adapters import adapter_for, GreenhouseAdapter, SUPPORTED_HOSTS_SQL


def test_greenhouse_matches_and_api_url():
    url = "https://job-boards.greenhouse.io/incidentiq/jobs/7724767003"
    a = adapter_for(url)
    assert a is GreenhouseAdapter
    assert a.api_url(url) == (
        "https://boards-api.greenhouse.io/v1/boards/incidentiq/jobs/7724767003?content=true"
    )


def test_greenhouse_parse_unescapes_html_content():
    payload = {"content": "&lt;p&gt;Build &amp; ship&lt;/p&gt;"}
    assert GreenhouseAdapter.parse(payload, "https://job-boards.greenhouse.io/x/jobs/1") == "<p>Build & ship</p>"


def test_greenhouse_parse_returns_none_when_no_content():
    assert GreenhouseAdapter.parse({}, "https://job-boards.greenhouse.io/x/jobs/1") is None


def test_unsupported_host_has_no_adapter():
    assert adapter_for("https://comcast.wd5.myworkdayjobs.com/en-US/x/job/y/z_R1") is None


def test_supported_hosts_sql_covers_all_four():
    for frag in ("greenhouse", "lever", "ashbyhq", "smartrecruiters"):
        assert frag in SUPPORTED_HOSTS_SQL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrichment_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.enrichment.adapters`.

- [ ] **Step 3: Write the framework + Greenhouse adapter**

Create `src/jobmaxxing/enrichment/__init__.py`:

```python
from .adapters import ADAPTERS, SUPPORTED_HOSTS_SQL, adapter_for

__all__ = ["ADAPTERS", "SUPPORTED_HOSTS_SQL", "adapter_for"]
```

Create `src/jobmaxxing/enrichment/adapters.py`:

```python
"""URL -> per-job JSON API adapters for the clean-API ATS sources.

Each adapter is a stateless class with three classmethods:
  matches(url) -> bool        host/path test
  api_url(url) -> str         translate the human page URL to the JSON endpoint
  parse(payload, url) -> str | None   extract the description, or None if absent
A None parse result means the posting is gone/unparseable -> permanent failure.
"""

import html
import re


class GreenhouseAdapter:
    name = "greenhouse"
    # Both the new (job-boards) and classic (boards) hosts; {token}/jobs/{numeric id}.
    _RE = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        token, jid = m.group(1), m.group(2)
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}?content=true"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        content = payload.get("content")
        return html.unescape(content) if content else None


ADAPTERS = [GreenhouseAdapter]

# Coarse Postgres regex (case-insensitive ~*) used to keep the candidate query's LIMIT
# spent only on supported rows. adapter_for() is the precise per-row router/guard.
SUPPORTED_HOSTS_SQL = r"greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com"


def adapter_for(url: str):
    """Return the first adapter whose matches(url) is true, or None."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrichment_adapters.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/__init__.py src/jobmaxxing/enrichment/adapters.py tests/test_enrichment_adapters.py
git commit -m "feat(enrich): adapter framework + Greenhouse adapter"
```

---

### Task 3: Lever adapter

**Files:**
- Modify: `src/jobmaxxing/enrichment/adapters.py`
- Test: `tests/test_enrichment_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrichment_adapters.py`:

```python
from jobmaxxing.enrichment.adapters import LeverAdapter


def test_lever_matches_strips_apply_suffix_in_api_url():
    url = "https://jobs.lever.co/waabi/62700386-b9db-4c78-aec3-5ef59cbe841e/apply"
    a = adapter_for(url)
    assert a is LeverAdapter
    assert a.api_url(url) == (
        "https://api.lever.co/v0/postings/waabi/62700386-b9db-4c78-aec3-5ef59cbe841e?mode=json"
    )


def test_lever_parse_uses_description_plain():
    assert LeverAdapter.parse({"descriptionPlain": "Build robots"}, "u") == "Build robots"


def test_lever_parse_returns_none_when_empty():
    assert LeverAdapter.parse({"descriptionPlain": ""}, "u") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrichment_adapters.py -k lever -v`
Expected: FAIL — `ImportError: cannot import name 'LeverAdapter'`.

- [ ] **Step 3: Implement the Lever adapter**

In `src/jobmaxxing/enrichment/adapters.py`, add the class after `GreenhouseAdapter`:

```python
class LeverAdapter:
    name = "lever"
    # jobs.lever.co/{site}/{uuid}  (an optional /apply suffix is ignored by the regex).
    _RE = re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-fA-F-]+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        site, jid = m.group(1), m.group(2)
        return f"https://api.lever.co/v0/postings/{site}/{jid}?mode=json"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        return payload.get("descriptionPlain") or None
```

Update the registry line:

```python
ADAPTERS = [GreenhouseAdapter, LeverAdapter]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrichment_adapters.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/adapters.py tests/test_enrichment_adapters.py
git commit -m "feat(enrich): Lever adapter"
```

---

### Task 4: Ashby adapter

**Files:**
- Modify: `src/jobmaxxing/enrichment/adapters.py`
- Test: `tests/test_enrichment_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrichment_adapters.py`:

```python
from jobmaxxing.enrichment.adapters import AshbyAdapter

_ASHBY_URL = "https://jobs.ashbyhq.com/replit/12737078-74c7-4e63-98a7-5e8da1e9deb1/application"


def test_ashby_matches_and_api_url_is_org_board():
    a = adapter_for(_ASHBY_URL)
    assert a is AshbyAdapter
    assert a.api_url(_ASHBY_URL) == (
        "https://api.ashbyhq.com/posting-api/job-board/replit?includeCompensation=true"
    )


def test_ashby_parse_selects_posting_by_id_from_url():
    payload = {"jobs": [
        {"id": "other-uuid", "descriptionPlain": "no"},
        {"id": "12737078-74c7-4e63-98a7-5e8da1e9deb1", "descriptionPlain": "Do X at Replit"},
    ]}
    assert AshbyAdapter.parse(payload, _ASHBY_URL) == "Do X at Replit"


def test_ashby_parse_returns_none_when_posting_absent():
    assert AshbyAdapter.parse({"jobs": [{"id": "z", "descriptionPlain": "x"}]}, _ASHBY_URL) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrichment_adapters.py -k ashby -v`
Expected: FAIL — `ImportError: cannot import name 'AshbyAdapter'`.

- [ ] **Step 3: Implement the Ashby adapter**

In `src/jobmaxxing/enrichment/adapters.py`, add after `LeverAdapter`. Ashby has no per-job public endpoint; fetch the org board and select the posting whose `id` matches the URL's posting UUID:

```python
class AshbyAdapter:
    name = "ashby"
    # jobs.ashbyhq.com/{org}/{postingUuid}  (optional /application suffix ignored).
    _RE = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        org = cls._RE.search(url).group(1)
        return f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        posting_id = cls._RE.search(url).group(2)
        for posting in payload.get("jobs", []):
            if posting.get("id") == posting_id:
                return posting.get("descriptionPlain") or None
        return None  # posting no longer on the board -> permanent
```

Update the registry line:

```python
ADAPTERS = [GreenhouseAdapter, LeverAdapter, AshbyAdapter]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrichment_adapters.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/adapters.py tests/test_enrichment_adapters.py
git commit -m "feat(enrich): Ashby adapter"
```

---

### Task 5: SmartRecruiters adapter

**Files:**
- Modify: `src/jobmaxxing/enrichment/adapters.py`
- Test: `tests/test_enrichment_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrichment_adapters.py`:

```python
from jobmaxxing.enrichment.adapters import SmartRecruitersAdapter

_SR_URL = "https://jobs.smartrecruiters.com/AbbVie/3743990010199046"


def test_smartrecruiters_matches_and_api_url():
    a = adapter_for(_SR_URL)
    assert a is SmartRecruitersAdapter
    assert a.api_url(_SR_URL) == (
        "https://api.smartrecruiters.com/v1/companies/AbbVie/postings/3743990010199046"
    )


def test_smartrecruiters_parse_joins_description_and_qualifications():
    payload = {"jobAd": {"sections": {
        "jobDescription": {"text": "<p>Env</p>"},
        "qualifications": {"text": "<p>Q</p>"},
    }}}
    assert SmartRecruitersAdapter.parse(payload, _SR_URL) == "<p>Env</p>\n<p>Q</p>"


def test_smartrecruiters_parse_returns_none_when_no_sections():
    assert SmartRecruitersAdapter.parse({"jobAd": {"sections": {}}}, _SR_URL) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrichment_adapters.py -k smartrecruiters -v`
Expected: FAIL — `ImportError: cannot import name 'SmartRecruitersAdapter'`.

- [ ] **Step 3: Implement the SmartRecruiters adapter**

In `src/jobmaxxing/enrichment/adapters.py`, add after `AshbyAdapter`:

```python
class SmartRecruitersAdapter:
    name = "smartrecruiters"
    # jobs.smartrecruiters.com/{company}/{numeric postingId}
    _RE = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        company, posting_id = m.group(1), m.group(2)
        return f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        sections = payload.get("jobAd", {}).get("sections", {})
        parts = [
            sections.get(key, {}).get("text")
            for key in ("jobDescription", "qualifications")
        ]
        text = "\n".join(p for p in parts if p)
        return text or None
```

Update the registry line:

```python
ADAPTERS = [GreenhouseAdapter, LeverAdapter, AshbyAdapter, SmartRecruitersAdapter]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrichment_adapters.py -v`
Expected: PASS (14 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/adapters.py tests/test_enrichment_adapters.py
git commit -m "feat(enrich): SmartRecruiters adapter"
```

---

### Task 6: Error classification + single-row fetch outcome

**Files:**
- Create: `src/jobmaxxing/enrichment/enrich.py`
- Test: `tests/test_enrich_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrich_db.py` (after the fixture). These test the pure `_fetch_one` with a fake fetcher — no DB, no network:

```python
from jobmaxxing.enrichment.enrich import _fetch_one


def _http_error(status):
    req = httpx.Request("GET", "https://api.example/x")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def test_fetch_one_enriched_on_success():
    def fake(api_url):
        return {"content": "&lt;p&gt;hi&lt;/p&gt;"}
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "enriched"
    assert out.description == "<p>hi</p>"


def test_fetch_one_permanent_on_404():
    def fake(api_url):
        raise _http_error(404)
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "permanent"


def test_fetch_one_transient_on_429_and_timeout():
    def fake_429(api_url):
        raise _http_error(429)
    def fake_timeout(api_url):
        raise httpx.TimeoutException("slow")
    assert _fetch_one("i", "https://job-boards.greenhouse.io/x/jobs/1", fake_429).kind == "transient"
    assert _fetch_one("i", "https://job-boards.greenhouse.io/x/jobs/1", fake_timeout).kind == "transient"


def test_fetch_one_permanent_when_no_description_parsed():
    def fake(api_url):
        return {"content": ""}
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "permanent"


def test_fetch_one_permanent_when_unsupported_host():
    def fake(api_url):
        raise AssertionError("must not fetch an unsupported host")
    out = _fetch_one("id1", "https://comcast.wd5.myworkdayjobs.com/x/job/y/z_R1", fake)
    assert out.kind == "permanent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py -k fetch_one -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.enrichment.enrich`.

- [ ] **Step 3: Implement classification + `_fetch_one`**

Create `src/jobmaxxing/enrichment/enrich.py`:

```python
import logging
from dataclasses import dataclass

import httpx

from ..fetch import fetch_json as fetch_json_default
from .adapters import adapter_for

logger = logging.getLogger(__name__)

# 4xx (except 429) means retrying won't help -> permanent. 429 and 5xx -> transient.
_PERMANENT_HTTP = lambda code: 400 <= code < 500 and code != 429  # noqa: E731


def classify_error(exc: Exception) -> str:
    """'permanent' (never retry) or 'transient' (retry until cap)."""
    if isinstance(exc, httpx.HTTPStatusError) and _PERMANENT_HTTP(exc.response.status_code):
        return "permanent"
    return "transient"  # 429, 5xx, timeouts, connection errors


@dataclass
class Outcome:
    job_id: object
    kind: str           # "enriched" | "permanent" | "transient"
    description: str | None
    error: str | None


def _fetch_one(job_id, url: str, fetch_json) -> Outcome:
    """Fetch + parse one row's JD. Pure w.r.t. the DB; isolates all errors."""
    adapter = adapter_for(url)
    if adapter is None:
        return Outcome(job_id, "permanent", None, f"no adapter for {url}")
    try:
        payload = fetch_json(adapter.api_url(url))
    except Exception as exc:  # noqa: BLE001 - classify, never propagate
        return Outcome(job_id, classify_error(exc), None, f"{type(exc).__name__}: {exc}"[:500])
    description = adapter.parse(payload, url)
    if not description:
        return Outcome(job_id, "permanent", None, "no description in payload")
    return Outcome(job_id, "enriched", description, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py -k fetch_one -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/enrich.py tests/test_enrich_db.py
git commit -m "feat(enrich): error classification + single-row fetch outcome"
```

---

### Task 7: `enrich_new` — selection, concurrency, batched write

**Files:**
- Modify: `src/jobmaxxing/enrichment/enrich.py`
- Test: `tests/test_enrich_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrich_db.py`. The `_insert` helper and a routing fake fetcher drive every transition:

```python
from jobmaxxing.enrichment.enrich import enrich_new

_GH = "https://job-boards.greenhouse.io/acme/jobs/{n}"


def _insert(conn, *, dedupe_key, url, description="", attempts=0, route_method=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "enrich_attempts, route_method) values (%s,'github:simplify','Acme','Intern',%s,%s,%s,%s)",
        (dedupe_key, url, description, attempts, route_method),
    )
    conn.commit()


def _fake_fetch_ok(api_url):
    return {"content": "&lt;p&gt;A real JD with enough words&lt;/p&gt;"}


def test_enrich_new_fills_description_and_sets_enriched_at(conn):
    _insert(conn, dedupe_key="a", url=_GH.format(n=1))
    counts = enrich_new(conn, fetch_json=_fake_fetch_ok)
    assert counts == {"enriched": 1, "permanent_failed": 0, "transient_failed": 0, "candidates": 1}
    row = conn.execute(
        "select description, enriched_at, enrich_attempts from jobs where dedupe_key='a'"
    ).fetchone()
    assert row[0] == "<p>A real JD with enough words</p>"
    assert row[1] is not None
    assert row[2] == 0


def test_enrich_new_marks_permanent_and_stops_reselecting(conn):
    _insert(conn, dedupe_key="b", url=_GH.format(n=2))

    def fake_404(api_url):
        req = httpx.Request("GET", api_url)
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    counts = enrich_new(conn, fetch_json=fake_404, cap=3)
    assert counts["permanent_failed"] == 1
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='b'").fetchone()[0] == 3
    # second run: not reselected (attempts >= cap)
    counts2 = enrich_new(conn, fetch_json=fake_404, cap=3)
    assert counts2["candidates"] == 0


def test_enrich_new_transient_increments_then_caps(conn):
    _insert(conn, dedupe_key="c", url=_GH.format(n=3))

    def fake_timeout(api_url):
        raise httpx.TimeoutException("slow")

    for expected in (1, 2, 3):
        enrich_new(conn, fetch_json=fake_timeout, cap=3)
        got = conn.execute("select enrich_attempts from jobs where dedupe_key='c'").fetchone()[0]
        assert got == expected
    # now attempts == cap -> no longer a candidate
    assert enrich_new(conn, fetch_json=fake_timeout, cap=3)["candidates"] == 0


def test_enrich_new_respects_max_fetches(conn):
    for i in range(5):
        _insert(conn, dedupe_key=f"d{i}", url=_GH.format(n=100 + i))
    counts = enrich_new(conn, fetch_json=_fake_fetch_ok, max_fetches=2)
    assert counts["candidates"] == 2
    assert counts["enriched"] == 2


def test_enrich_new_skips_unsupported_and_manual_and_already_described(conn):
    _insert(conn, dedupe_key="wd", url="https://x.wd5.myworkdayjobs.com/en/x/job/y/z_R1")  # unsupported host
    _insert(conn, dedupe_key="man", url=_GH.format(n=9), route_method="manual")            # manual
    _insert(conn, dedupe_key="has", url=_GH.format(n=10), description="already here")        # has desc

    def fake_boom(api_url):
        raise AssertionError("should not fetch any of these rows")

    assert enrich_new(conn, fetch_json=fake_boom)["candidates"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py -k enrich_new -v`
Expected: FAIL — `ImportError: cannot import name 'enrich_new'`.

- [ ] **Step 3: Implement `enrich_new`**

Append to `src/jobmaxxing/enrichment/enrich.py`. Add imports at the top of the file (alongside the existing ones):

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

from ..config import load_settings
from .adapters import SUPPORTED_HOSTS_SQL
```

Then add the function:

```python
def enrich_new(
    conn: psycopg.Connection,
    *,
    max_fetches: int = 500,
    max_workers: int = 8,
    cap: int = 3,
    fetch_json=fetch_json_default,
) -> dict:
    """Fetch JDs for supported, description-less rows. Bounded-concurrent; batched write.

    Returns {enriched, permanent_failed, transient_failed, candidates}.
    """
    rows = conn.execute(
        "select id, url from jobs "
        "where coalesce(description, '') = '' "
        "and route_method is distinct from 'manual' "
        "and enrich_attempts < %s "
        "and url ~* %s "
        "order by scraped_at desc "
        "limit %s",
        (cap, SUPPORTED_HOSTS_SQL, max_fetches),
    ).fetchall()
    if not rows:
        return {"enriched": 0, "permanent_failed": 0, "transient_failed": 0, "candidates": 0}

    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one, job_id, url, fetch_json) for job_id, url in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    enriched = [(o.description, o.job_id) for o in outcomes if o.kind == "enriched"]
    permanent = [(cap, o.error, o.job_id) for o in outcomes if o.kind == "permanent"]
    transient = [(o.error, o.job_id) for o in outcomes if o.kind == "transient"]

    with conn.transaction(), conn.cursor() as cur:
        if enriched:
            cur.executemany(
                "update jobs set description=%s, enriched_at=now(), enrich_error=null where id=%s",
                enriched,
            )
        if permanent:
            cur.executemany(
                "update jobs set enrich_attempts=%s, enrich_error=%s where id=%s",
                permanent,
            )
        if transient:
            cur.executemany(
                "update jobs set enrich_attempts=enrich_attempts+1, enrich_error=%s where id=%s",
                transient,
            )

    counts = {
        "enriched": len(enriched),
        "permanent_failed": len(permanent),
        "transient_failed": len(transient),
        "candidates": len(rows),
    }
    logger.info("enrich summary: %s", counts)
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py -v`
Expected: PASS (all enrich_new + fetch_one + migration tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/enrich.py tests/test_enrich_db.py
git commit -m "feat(enrich): enrich_new selection + bounded concurrency + batched write"
```

---

### Task 8: merge-no-clobber durability test

**Files:**
- Test: `tests/test_enrich_db.py`

Confirms a re-ingested GitHub-list row (empty description) does not overwrite an enriched description or reset the tracking columns — the spec's key durability invariant.

- [ ] **Step 1: Write the failing test (expected to PASS immediately — characterization)**

Append to `tests/test_enrich_db.py`:

```python
from jobmaxxing.models import JobRecord
from jobmaxxing.store import upsert_jobs


def test_enriched_description_survives_reingest(conn):
    _insert(conn, dedupe_key="keep|me", url=_GH.format(n=42))
    enrich_new(conn, fetch_json=_fake_fetch_ok)
    before = conn.execute(
        "select description, enriched_at from jobs where dedupe_key='keep|me'"
    ).fetchone()
    assert before[0] == "<p>A real JD with enough words</p>"

    # GitHub list re-ingests the same job with NO description.
    rec = JobRecord(
        dedupe_key="keep|me", source="github:simplify", company="Acme", title="Intern",
        url=_GH.format(n=42), description=None,
    )
    upsert_jobs(conn, [rec])

    after = conn.execute(
        "select description, enriched_at from jobs where dedupe_key='keep|me'"
    ).fetchone()
    assert after[0] == before[0]      # description preserved (empty is falsy in merge)
    assert after[1] == before[1]      # enriched_at untouched by _UPDATE_SQL
```

> Note: `JobRecord`'s only required fields are `source`, `company`, `title`, `url` (all others default); the construction above is complete. `dedupe_key` must be non-empty — the upsert validates this.

- [ ] **Step 2: Run the test**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py::test_enriched_description_survives_reingest -v`
Expected: PASS (proves the invariant holds against current `merge_records`/`store`). If it FAILS, stop — the spec's durability assumption is violated and must be fixed before wiring the workflow.

- [ ] **Step 3: Commit**

```bash
git add tests/test_enrich_db.py
git commit -m "test(enrich): enriched description survives re-ingest (merge-no-clobber)"
```

---

### Task 9: CLI entrypoint

**Files:**
- Modify: `src/jobmaxxing/enrichment/enrich.py` (add `main`)
- Create: `src/jobmaxxing/enrich.py`
- Test: `tests/test_enrich_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrich_cli.py` (verifies the shim imports and exposes `main`, mirroring how `jobmaxxing.route` is structured):

```python
def test_enrich_cli_shim_exposes_main():
    import jobmaxxing.enrich as cli
    from jobmaxxing.enrichment.enrich import main
    assert cli.main is main
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.enrich` (or no `main`).

- [ ] **Step 3: Implement `main` + shim**

Append to `src/jobmaxxing/enrichment/enrich.py`:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        counts = enrich_new(conn)
        print(f"enriched: {counts}")
```

Create `src/jobmaxxing/enrich.py`:

```python
"""CLI entrypoint shim so `python -m jobmaxxing.enrich` works (parallel to jobmaxxing.route).

The implementation lives in the enrichment package; this module exposes its `main` at the
top-level module path the workflow invokes.
"""

from .enrichment.enrich import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrich_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/enrich.py src/jobmaxxing/enrich.py tests/test_enrich_cli.py
git commit -m "feat(enrich): python -m jobmaxxing.enrich CLI entrypoint"
```

---

### Task 10: Workflow wiring + full suite

**Files:**
- Modify: `.github/workflows/pollers.yml`

- [ ] **Step 1: Add the enrich step**

In `.github/workflows/pollers.yml`, insert this step **between** the `Run pollers` step and the `Route new postings` step (order matters — enrich must precede route so newly-filled JDs route in the same run):

```yaml
      - name: Enrich descriptions
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: uv run python -m jobmaxxing.enrich
```

- [ ] **Step 2: Verify the YAML is valid and step order is correct**

Run: `python -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/pollers.yml')); steps=[s.get('name') for s in d['jobs']['poll']['steps']]; print(steps); assert steps.index('Enrich descriptions')==steps.index('Run pollers')+1; assert steps.index('Enrich descriptions')<steps.index('Route new postings'); print('order ok')"`
Expected: prints the step list then `order ok`.

- [ ] **Step 3: Run the full test suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all prior tests + the new enrichment tests PASS (the 2 pdflatex tests skip by design).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/pollers.yml
git commit -m "feat(enrich): run enrichment between pollers and routing"
```

---

## Done criteria

- `uv run pytest -q` green (enrichment adapters, enrich DB transitions, merge-no-clobber, CLI shim) plus all pre-existing tests; 2 pdflatex tests skip.
- `python -m jobmaxxing.enrich` runs the enrich step against the live DB.
- Pollers workflow runs ingest → enrich → route in order.
- After merge: dispatch a poller run, confirm `enrich summary` shows non-zero `enriched`, and that a subsequent `route summary` shows `llm > 0` / falling `deferred` as JD-bearing rows become routable.
