# Workday JD Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, operator-run worker that enriches Cloudflare-gated Workday job descriptions via a tiered fetch (plain cxs → headless-cleared-context cxs → headless render+intercept), reusing the Phase 5a enrichment schema and writer.

**Architecture:** Pure, browser-free logic + worker in `enrichment/workday.py` (fully unit/integration tested behind an injectable `WorkdayFetcher`); the only browser code is `PlaywrightFetcher` in `enrichment/playwright_fetcher.py` (validated by an e2e test + a spike). A shared `_apply_outcomes` writer is extracted from `enrich_new` so both engines write identically. Playwright is a local-only optional dependency (lazy-imported) so CI stays browser-free.

**Tech Stack:** Python 3.12, psycopg3, httpx, Playwright (local-only), `concurrent.futures.ThreadPoolExecutor`, pytest + pytest-postgresql.

**Spec:** `docs/superpowers/specs/2026-06-14-workday-enrichment-design.md`

---

## Concurrent execution structure

This plan runs as **two file-disjoint streams** off `main`, in isolated worktrees, concurrently — **no shared files, so no merge conflicts**:

- **Stream 1 — core logic + worker + writer refactor** (Tasks 1–6). Files: `src/jobmaxxing/enrichment/enrich.py`, `src/jobmaxxing/enrichment/workday.py`, `src/jobmaxxing/enrich_workday.py`, `tests/test_enrich_db.py`, `tests/test_workday_unit.py`, `tests/test_workday_db.py`. **Fully self-testable (no browser).**
- **Stream 2 — browser fetcher + packaging + e2e + docs** (Tasks 7–10). Files: `src/jobmaxxing/enrichment/playwright_fetcher.py`, `pyproject.toml`, `uv.lock`, `scripts/spike_workday.py`, `tests/test_workday_e2e.py`, `README.md`.

**Cross-stream dependency (logical, not a file conflict):** Stream 2's `playwright_fetcher.py` imports the exceptions + `_classify_status`/`_looks_like_challenge`/`workday_host` from Stream 1's `workday.py`. Stream 2 develops against the interface contract below and integrates when Stream 1 merges; its only tests are the e2e test (skipped unless `JOBMAXXING_E2E=1`), so it cannot run green standalone regardless. Stream 1 merges first; then Stream 2 integrates and the e2e/spike validate the browser path.

**Interface contract Stream 2 depends on** (all defined in Stream 1's `workday.py`):
- Exceptions: `WorkdayBlocked`, `WorkdayNotFound`, `WorkdayTransient`.
- `workday_host(url: str) -> str | None`.
- `_classify_status(status: int) -> None` (raises the right exception for non-200).
- `_looks_like_challenge(page_title: str) -> bool`.

All tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

# STREAM 1 — Core logic, worker, writer refactor

### Task 1: Extract `_apply_outcomes` shared writer (refactor)

Behavior-preserving extraction so both `enrich_new` and the Workday worker write outcomes identically.

**Files:**
- Modify: `src/jobmaxxing/enrichment/enrich.py`
- Test: `tests/test_enrich_db.py`

- [ ] **Step 1: Write a characterization test (must PASS against current code)**

Append to `tests/test_enrich_db.py`:

```python
def test_apply_outcomes_writes_each_kind(conn):
    from jobmaxxing.enrichment.enrich import Outcome, _apply_outcomes
    _insert(conn, dedupe_key="ao_e", url=_GH.format(n=701))
    _insert(conn, dedupe_key="ao_p", url=_GH.format(n=702))
    _insert(conn, dedupe_key="ao_t", url=_GH.format(n=703))
    ids = {k: conn.execute("select id from jobs where dedupe_key=%s", (k,)).fetchone()[0]
           for k in ("ao_e", "ao_p", "ao_t")}
    outcomes = [
        Outcome(ids["ao_e"], "enriched", "filled in", None),
        Outcome(ids["ao_p"], "permanent", "gone", None),
        Outcome(ids["ao_t"], "transient", "slow", None),
    ]
    counts = _apply_outcomes(conn, outcomes, cap=3)
    assert counts == {"enriched": 1, "permanent_failed": 1, "transient_failed": 1}
    assert conn.execute("select description from jobs where id=%s", (ids["ao_e"],)).fetchone()[0] == "filled in"
    assert conn.execute("select enrich_attempts from jobs where id=%s", (ids["ao_p"],)).fetchone()[0] == 3
    assert conn.execute("select enrich_attempts from jobs where id=%s", (ids["ao_t"],)).fetchone()[0] == 1
```

- [ ] **Step 2: Run it — fails (function doesn't exist yet)**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py::test_apply_outcomes_writes_each_kind -v`
Expected: FAIL — `ImportError: cannot import name '_apply_outcomes'`.

- [ ] **Step 3: Extract `_apply_outcomes` and call it from `enrich_new`**

In `src/jobmaxxing/enrichment/enrich.py`, add the helper (above `enrich_new`):

```python
def _apply_outcomes(conn, outcomes, *, cap):
    """Batch-write fetch outcomes in one transaction. Returns kind counts (no 'candidates').

    enriched -> set description + enriched_at, clear error (attempts intentionally NOT reset:
                a non-empty description already excludes the row from the candidate query).
    permanent -> set enrich_attempts = cap (never reselected) + error.
    transient -> enrich_attempts += 1 + error (retried until the cap).
    """
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
    return {"enriched": len(enriched), "permanent_failed": len(permanent), "transient_failed": len(transient)}
```

Then replace the inline write block in `enrich_new` (the `enriched = [...]` through the `counts = {...}` assignment) with:

```python
    counts = _apply_outcomes(conn, outcomes, cap=cap)
    counts["candidates"] = len(rows)
    logger.info("enrich summary: %s", counts)
    return counts
```

- [ ] **Step 4: Run the full enrich suite — all green (behavior preserved)**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_enrich_db.py -q`
Expected: PASS (the new characterization test + all pre-existing enrich tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/enrich.py tests/test_enrich_db.py
git commit -m "refactor(enrich): extract _apply_outcomes shared batched writer"
```

---

### Task 2: `workday.py` — URL translation + payload parsing (pure)

**Files:**
- Create: `src/jobmaxxing/enrichment/workday.py`
- Test: `tests/test_workday_unit.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workday_unit.py`:

```python
from jobmaxxing.enrichment.workday import workday_cxs_url, workday_host, parse_workday


def test_cxs_url_basic():
    u = "https://micron.wd1.myworkdayjobs.com/External/job/San-Jose-CA/Intern-ASIC_JR84107"
    assert workday_cxs_url(u) == (
        "https://micron.wd1.myworkdayjobs.com/wday/cxs/micron/External/job/San-Jose-CA/Intern-ASIC_JR84107"
    )


def test_cxs_url_strips_locale_prefix():
    u = "https://thales.wd3.myworkdayjobs.com/en-US/Careers/job/Glasgow/SW-Apprentice_R0298405"
    assert workday_cxs_url(u) == (
        "https://thales.wd3.myworkdayjobs.com/wday/cxs/thales/Careers/job/Glasgow/SW-Apprentice_R0298405"
    )


def test_cxs_url_non_workday_is_none():
    assert workday_cxs_url("https://job-boards.greenhouse.io/acme/jobs/1") is None


def test_workday_host():
    assert workday_host("https://psu.wd1.myworkdayjobs.com/PSU_Staff/job/Berks/Intern_REQ1") == (
        "psu.wd1.myworkdayjobs.com"
    )
    assert workday_host("https://x.greenhouse.io/y") is None


def test_parse_workday_extracts_html_description():
    payload = {"jobPostingInfo": {"jobDescription": "<p>Build chips</p>"}}
    assert parse_workday(payload) == "<p>Build chips</p>"


def test_parse_workday_none_when_absent_or_empty():
    assert parse_workday({"jobPostingInfo": {}}) is None
    assert parse_workday({}) is None
    assert parse_workday({"jobPostingInfo": {"jobDescription": ""}}) is None
```

- [ ] **Step 2: Run — fails (module missing)**

Run: `uv run pytest tests/test_workday_unit.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.enrichment.workday`.

- [ ] **Step 3: Implement the URL/parse logic**

Create `src/jobmaxxing/enrichment/workday.py`:

```python
"""Workday JD enrichment — pure logic + tiered worker (browser code lives in playwright_fetcher).

Run LOCALLY via `python -m jobmaxxing.enrich_workday` (needs the `headless` extra).
"""

import re

# https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
_WORKDAY_RE = re.compile(
    r"https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"            # optional locale prefix, stripped
    r"(?P<site>[^/]+)/job/(?P<rest>.+)$"
)


def workday_host(url: str) -> str | None:
    m = _WORKDAY_RE.match(url)
    return f"{m.group('tenant')}.{m.group('wd')}.myworkdayjobs.com" if m else None


def workday_cxs_url(url: str) -> str | None:
    """Translate a Workday job URL to its cxs JSON endpoint, or None if unrecognized."""
    m = _WORKDAY_RE.match(url)
    if not m:
        return None
    t, wd, site, rest = m.group("tenant"), m.group("wd"), m.group("site"), m.group("rest")
    return f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/job/{rest}"


def parse_workday(payload: dict) -> str | None:
    """Extract the (HTML) job description from a cxs payload, or None if absent."""
    jd = (payload or {}).get("jobPostingInfo", {}).get("jobDescription")
    return jd or None
```

- [ ] **Step 4: Run — passes**

Run: `uv run pytest tests/test_workday_unit.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/workday.py tests/test_workday_unit.py
git commit -m "feat(workday): cxs URL translation + payload parsing"
```

---

### Task 3: Exceptions + status/challenge classification (pure)

**Files:**
- Modify: `src/jobmaxxing/enrichment/workday.py`
- Test: `tests/test_workday_unit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workday_unit.py`:

```python
import pytest

from jobmaxxing.enrichment.workday import (
    WorkdayBlocked, WorkdayNotFound, WorkdayTransient,
    _classify_status, _looks_like_challenge,
)


def test_classify_status_ok_returns_none():
    assert _classify_status(200) is None


@pytest.mark.parametrize("code", [403, 429, 503])
def test_classify_status_blocked(code):
    with pytest.raises(WorkdayBlocked):
        _classify_status(code)


@pytest.mark.parametrize("code", [404, 410])
def test_classify_status_not_found(code):
    with pytest.raises(WorkdayNotFound):
        _classify_status(code)


def test_classify_status_other_is_transient():
    with pytest.raises(WorkdayTransient):
        _classify_status(500)


def test_looks_like_challenge():
    assert _looks_like_challenge("Just a moment...") is True
    assert _looks_like_challenge("Attention Required! | Cloudflare") is True
    assert _looks_like_challenge("Software Engineer Intern - Micron") is False
    assert _looks_like_challenge("") is False
```

- [ ] **Step 2: Run — fails (names missing)**

Run: `uv run pytest tests/test_workday_unit.py -k "classify_status or challenge" -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement exceptions + classifiers**

Append to `src/jobmaxxing/enrichment/workday.py`:

```python
class WorkdayBlocked(Exception):
    """Cloudflare/anti-bot blocked this fetch (403/429/503/challenge). Escalate a tier;
    if blocked at every tier, classify transient (a later run/tier may succeed)."""


class WorkdayNotFound(Exception):
    """Posting gone (404/410, or the rendered careers app fired no job cxs call). Permanent."""


class WorkdayTransient(Exception):
    """Timeout, connection error, or browser crash. Retry next run until the cap."""


def _classify_status(status: int):
    if status == 200:
        return None
    if status in (403, 429, 503):
        raise WorkdayBlocked(f"status {status}")
    if status in (404, 410):
        raise WorkdayNotFound(f"status {status}")
    raise WorkdayTransient(f"status {status}")


# Cloudflare interstitial titles ("Just a moment...", "Attention Required!", etc.).
_CHALLENGE_MARKERS = ("just a moment", "attention required", "checking your browser", "cloudflare")


def _looks_like_challenge(page_title: str) -> bool:
    t = (page_title or "").lower()
    return any(marker in t for marker in _CHALLENGE_MARKERS)
```

- [ ] **Step 4: Run — passes**

Run: `uv run pytest tests/test_workday_unit.py -v`
Expected: PASS (all Task 2 + Task 3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/workday.py tests/test_workday_unit.py
git commit -m "feat(workday): typed fetch exceptions + status/challenge classifiers"
```

---

### Task 4: `WorkdayFetcher` protocol + `fetch_workday_one` tier dispatch (pure)

**Files:**
- Modify: `src/jobmaxxing/enrichment/workday.py`
- Test: `tests/test_workday_unit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workday_unit.py`:

```python
from jobmaxxing.enrichment.workday import fetch_workday_one

_PAYLOAD = {"jobPostingInfo": {"jobDescription": "<p>Real JD with enough words</p>"}}
_URL = "https://acme.wd5.myworkdayjobs.com/Careers/job/NYC/Intern_R1"


class FakeFetcher:
    """Drives each tier with a queued behavior: a dict -> returned, an Exception -> raised."""
    def __init__(self, plain=None, context=None, render=None):
        self._plan = {"plain": plain, "context": context, "render": render}
        self.calls = []

    def _do(self, tier):
        self.calls.append(tier)
        b = self._plan[tier]
        if isinstance(b, Exception):
            raise b
        if b is None:
            raise AssertionError(f"tier {tier} unexpectedly called")
        return b

    def fetch_plain(self, cxs_url):
        return self._do("plain")

    def fetch_via_context(self, host, cxs_url):
        return self._do("context")

    def fetch_via_render(self, job_url):
        return self._do("render")


def test_tier0_success_skips_other_tiers():
    f = FakeFetcher(plain=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert out.description == "<p>Real JD with enough words</p>"
    assert f.calls == ["plain"]


def test_escalates_to_context_on_block():
    f = FakeFetcher(plain=WorkdayBlocked("403"), context=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert f.calls == ["plain", "context"]


def test_escalates_to_render_on_block():
    f = FakeFetcher(plain=WorkdayBlocked("403"), context=WorkdayBlocked("403"), render=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert f.calls == ["plain", "context", "render"]


def test_blocked_all_tiers_is_transient():
    f = FakeFetcher(plain=WorkdayBlocked("x"), context=WorkdayBlocked("x"), render=WorkdayBlocked("x"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "transient"
    assert f.calls == ["plain", "context", "render"]


def test_not_found_stops_immediately_permanent():
    f = FakeFetcher(plain=WorkdayNotFound("404"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "permanent"
    assert f.calls == ["plain"]


def test_transient_stops_immediately():
    f = FakeFetcher(plain=WorkdayTransient("timeout"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "transient"
    assert f.calls == ["plain"]


def test_payload_without_description_is_permanent():
    f = FakeFetcher(plain={"jobPostingInfo": {}})
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "permanent"


def test_unrecognized_url_is_permanent_without_fetching():
    f = FakeFetcher()
    out = fetch_workday_one("j1", "https://x.greenhouse.io/y", f)
    assert out.kind == "permanent"
    assert f.calls == []
```

- [ ] **Step 2: Run — fails**

Run: `uv run pytest tests/test_workday_unit.py -k "tier or escalat or blocked or found or payload or unrecognized" -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_workday_one'`.

- [ ] **Step 3: Implement the protocol + dispatch**

Append to `src/jobmaxxing/enrichment/workday.py` (add `from typing import Protocol` to the imports at the top, and `from .enrich import Outcome`):

```python
class WorkdayFetcher(Protocol):
    def fetch_plain(self, cxs_url: str) -> dict: ...           # Tier 0 (no browser)
    def fetch_via_context(self, host: str, cxs_url: str) -> dict: ...  # Tier 1
    def fetch_via_render(self, job_url: str) -> dict: ...      # Tier 2


def _outcome_from_payload(job_id, payload) -> Outcome:
    desc = parse_workday(payload)
    if not desc:
        return Outcome(job_id, "permanent", None, "no description in workday payload")
    return Outcome(job_id, "enriched", desc, None)


def fetch_workday_one(job_id, url: str, fetcher: WorkdayFetcher) -> Outcome:
    """Plain -> headless-context -> headless-render, classifying as it escalates.
    Pure w.r.t. the DB and the browser; all errors isolate into an Outcome."""
    cxs = workday_cxs_url(url)
    if cxs is None:
        return Outcome(job_id, "permanent", None, f"unrecognized workday url: {url}")
    host = workday_host(url)
    for tier in (
        lambda: fetcher.fetch_plain(cxs),
        lambda: fetcher.fetch_via_context(host, cxs),
        lambda: fetcher.fetch_via_render(url),
    ):
        try:
            return _outcome_from_payload(job_id, tier())
        except WorkdayNotFound as exc:
            return Outcome(job_id, "permanent", None, str(exc))
        except WorkdayTransient as exc:
            return Outcome(job_id, "transient", None, str(exc))
        except WorkdayBlocked:
            continue
    return Outcome(job_id, "transient", None, "blocked at all tiers (cloudflare unsolved)")
```

- [ ] **Step 4: Run — passes**

Run: `uv run pytest tests/test_workday_unit.py -v`
Expected: PASS (all unit tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/workday.py tests/test_workday_unit.py
git commit -m "feat(workday): WorkdayFetcher protocol + tiered fetch_workday_one dispatch"
```

---

### Task 5: `enrich_workday` worker (candidate query, sharding, batched write)

**Files:**
- Modify: `src/jobmaxxing/enrichment/workday.py`
- Test: `tests/test_workday_db.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workday_db.py`:

```python
import psycopg
import pytest

from jobmaxxing.enrichment.workday import enrich_workday
from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


_WD = "https://{tenant}.wd5.myworkdayjobs.com/Careers/job/NYC/Intern_R{n}"
_PAYLOAD = {"jobPostingInfo": {"jobDescription": "<p>A real Workday JD with enough words</p>"}}


def _insert(conn, *, dedupe_key, url, description="", attempts=0, route_method=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "enrich_attempts, route_method) values (%s,'github:simplify','Acme','Intern',%s,%s,%s,%s)",
        (dedupe_key, url, description, attempts, route_method),
    )
    conn.commit()


class _OkFetcher:
    """Tier-0 success for every job; records hosts to prove per-tenant handling."""
    def __init__(self):
        self.hosts = []
    def fetch_plain(self, cxs_url):
        self.hosts.append(cxs_url)
        return _PAYLOAD
    def fetch_via_context(self, host, cxs_url):  # pragma: no cover - not reached on Tier-0 success
        return _PAYLOAD
    def fetch_via_render(self, job_url):  # pragma: no cover
        return _PAYLOAD


def test_enrich_workday_fills_descriptions(conn):
    _insert(conn, dedupe_key="w1", url=_WD.format(tenant="acme", n=1))
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher)
    assert counts == {"enriched": 1, "permanent_failed": 0, "transient_failed": 0, "candidates": 1}
    row = conn.execute("select description, enriched_at from jobs where dedupe_key='w1'").fetchone()
    assert row[0] == "<p>A real Workday JD with enough words</p>"
    assert row[1] is not None


def test_enrich_workday_selects_only_eligible_rows(conn):
    _insert(conn, dedupe_key="wd_ok", url=_WD.format(tenant="acme", n=2))
    _insert(conn, dedupe_key="gh", url="https://job-boards.greenhouse.io/acme/jobs/9")   # non-workday
    _insert(conn, dedupe_key="manual", url=_WD.format(tenant="acme", n=3), route_method="manual")
    _insert(conn, dedupe_key="hasdesc", url=_WD.format(tenant="acme", n=4), description="already")
    _insert(conn, dedupe_key="capped", url=_WD.format(tenant="acme", n=5), attempts=3)
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher, cap=3)
    assert counts["candidates"] == 1          # only wd_ok
    assert counts["enriched"] == 1


def test_enrich_workday_transient_then_permanent_classification(conn):
    from jobmaxxing.enrichment.workday import WorkdayBlocked, WorkdayNotFound
    _insert(conn, dedupe_key="blocked", url=_WD.format(tenant="hard", n=6))
    _insert(conn, dedupe_key="gone", url=_WD.format(tenant="dead", n=7))

    class _MixedFetcher:
        def fetch_plain(self, cxs_url):
            if "hard" in cxs_url:
                raise WorkdayBlocked("403")
            raise WorkdayNotFound("404")
        def fetch_via_context(self, host, cxs_url):
            raise WorkdayBlocked("403")
        def fetch_via_render(self, job_url):
            raise WorkdayBlocked("403")

    counts = enrich_workday(conn, fetcher_factory=_MixedFetcher, cap=3)
    assert counts["transient_failed"] == 1    # hard -> blocked all tiers -> transient
    assert counts["permanent_failed"] == 1    # dead -> 404 -> permanent
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='blocked'").fetchone()[0] == 1
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='gone'").fetchone()[0] == 3


def test_enrich_workday_one_fetcher_per_tenant_shard(conn):
    # Two tenants, two jobs each -> 2 fetcher instances (one per tenant shard).
    for n in (10, 11):
        _insert(conn, dedupe_key=f"a{n}", url=_WD.format(tenant="alpha", n=n))
        _insert(conn, dedupe_key=f"b{n}", url=_WD.format(tenant="beta", n=n))
    made = []

    class _CountingFetcher(_OkFetcher):
        def __init__(self):
            super().__init__()
            made.append(self)

    counts = enrich_workday(conn, fetcher_factory=_CountingFetcher, max_workers=2)
    assert counts["enriched"] == 4
    assert len(made) == 2                      # one fetcher per tenant shard, not per job


def test_enrich_workday_respects_max_jobs(conn):
    for n in range(5):
        _insert(conn, dedupe_key=f"m{n}", url=_WD.format(tenant="acme", n=20 + n))
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher, max_jobs=2)
    assert counts["candidates"] == 2


def test_workday_enriched_description_survives_reingest(conn):
    # merge-no-clobber for a Workday row: a re-ingested empty-description row must not wipe
    # the enriched description or the tracking columns (spec §8 invariant).
    from jobmaxxing.models import JobRecord
    from jobmaxxing.store import upsert_jobs
    url = _WD.format(tenant="acme", n=99)
    _insert(conn, dedupe_key="wd|keep", url=url)
    enrich_workday(conn, fetcher_factory=_OkFetcher)
    before = conn.execute("select description, enriched_at from jobs where dedupe_key='wd|keep'").fetchone()
    assert before[0]
    rec = JobRecord(dedupe_key="wd|keep", source="github:simplify", company="Acme",
                    title="Intern", url=url, description=None)
    upsert_jobs(conn, [rec])
    after = conn.execute("select description, enriched_at from jobs where dedupe_key='wd|keep'").fetchone()
    assert after[0] == before[0]      # description preserved
    assert after[1] == before[1]      # enriched_at untouched
```

- [ ] **Step 2: Run — fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_workday_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'enrich_workday'`.

- [ ] **Step 3: Implement the worker**

Append to `src/jobmaxxing/enrichment/workday.py` (add to the top imports: `import logging`, `from concurrent.futures import ThreadPoolExecutor, as_completed`, `import psycopg`, `from ..config import load_settings`, `from .enrich import _apply_outcomes`):

```python
logger = logging.getLogger(__name__)


def _default_fetcher_factory():
    from .playwright_fetcher import PlaywrightFetcher  # lazy: CI never imports playwright
    return PlaywrightFetcher()


def enrich_workday(conn, *, max_jobs=300, max_workers=3, cap=3, fetcher_factory=_default_fetcher_factory):
    """Local worker: enrich description-less Workday rows via the tiered headless fetch.

    Jobs are sharded by tenant host so each shard runs on one thread-local fetcher and reuses
    that tenant's Cloudflare clearance. Returns {enriched, permanent_failed, transient_failed,
    candidates}."""
    rows = conn.execute(
        "select id, url from jobs "
        "where coalesce(description, '') = '' "
        "and route_method is distinct from 'manual' "
        "and enrich_attempts < %s "
        "and url ~* 'myworkdayjobs\\.com' "
        "order by scraped_at desc "
        "limit %s",
        (cap, max_jobs),
    ).fetchall()
    if not rows:
        return {"enriched": 0, "permanent_failed": 0, "transient_failed": 0, "candidates": 0}

    shards: dict[str, list] = {}
    for job_id, url in rows:
        shards.setdefault(workday_host(url) or "", []).append((job_id, url))

    def run_shard(jobs):
        fetcher = fetcher_factory()
        try:
            return [fetch_workday_one(jid, url, fetcher) for jid, url in jobs]
        finally:
            close = getattr(fetcher, "close", None)
            if close:
                close()

    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_shard, jobs) for jobs in shards.values()]
        for future in as_completed(futures):
            outcomes.extend(future.result())

    counts = _apply_outcomes(conn, outcomes, cap=cap)
    counts["candidates"] = len(rows)
    logger.info("enrich_workday summary: %s", counts)
    return counts
```

- [ ] **Step 4: Run — passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_workday_db.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/workday.py tests/test_workday_db.py
git commit -m "feat(workday): enrich_workday worker (candidate query, tenant sharding, batched write)"
```

---

### Task 6: CLI entrypoint

**Files:**
- Modify: `src/jobmaxxing/enrichment/workday.py` (add `main`)
- Create: `src/jobmaxxing/enrich_workday.py`
- Test: `tests/test_workday_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workday_db.py`:

```python
def test_cli_shim_exposes_main():
    import jobmaxxing.enrich_workday as cli
    from jobmaxxing.enrichment.workday import main
    assert cli.main is main
```

- [ ] **Step 2: Run — fails**

Run: `uv run pytest tests/test_workday_db.py::test_cli_shim_exposes_main -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.enrich_workday`.

- [ ] **Step 3: Implement `main` + shim**

Append to `src/jobmaxxing/enrichment/workday.py`:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"workday enriched: {enrich_workday(conn)}")
```

Create `src/jobmaxxing/enrich_workday.py`:

```python
"""CLI shim: `python -m jobmaxxing.enrich_workday` (run LOCALLY; needs the `headless` extra)."""

from .enrichment.workday import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run — passes**

Run: `uv run pytest tests/test_workday_db.py::test_cli_shim_exposes_main -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/enrichment/workday.py src/jobmaxxing/enrich_workday.py tests/test_workday_db.py
git commit -m "feat(workday): python -m jobmaxxing.enrich_workday CLI entrypoint"
```

---

# STREAM 2 — Browser fetcher, packaging, e2e, docs

> **Stream 2 note:** `playwright_fetcher.py` imports `WorkdayBlocked`, `WorkdayNotFound`, `WorkdayTransient`, `_classify_status`, `_looks_like_challenge`, and `workday_host` from `enrichment/workday.py` (built in Stream 1). On this branch those don't exist yet, so the module won't import and the e2e test (the only test here, and skipped unless `JOBMAXXING_E2E=1`) cannot run standalone — that is expected. Implement against the interface contract at the top of this plan; it integrates and is validated when Stream 1 merges. Verify your Python parses (`uv run python -c "import ast; ast.parse(open('src/jobmaxxing/enrichment/playwright_fetcher.py').read())"`) and commit per task.

### Task 7: Playwright optional dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Add the optional-dependency group**

In `pyproject.toml`, add (creating the table if absent):

```toml
[project.optional-dependencies]
headless = ["playwright>=1.40"]
```

- [ ] **Step 2: Lock without installing into the default env**

Run: `uv lock`
Expected: `uv.lock` updates to include `playwright` under the `headless` extra; the default sync is unaffected.

- [ ] **Step 3: Verify the default (CI) sync still excludes playwright**

Run: `uv sync --frozen --no-dev && uv run python -c "import importlib.util; assert importlib.util.find_spec('playwright') is None; print('playwright absent from default env: OK')"`
Expected: prints OK (CI env stays browser-free).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(workday): add playwright as a local-only 'headless' optional dependency"
```

---

### Task 8: `PlaywrightFetcher` (the browser boundary)

**Files:**
- Create: `src/jobmaxxing/enrichment/playwright_fetcher.py`

> No unit test — this class is the un-unit-testable browser boundary (like pdflatex compile in tailoring). It is exercised by the spike (Task 9) and the e2e test (Task 9), and validated by the operator's first real run. Keep it thin: it only drives Playwright and delegates all classification to `workday.py`'s pure helpers.

- [ ] **Step 1: Implement the fetcher**

Create `src/jobmaxxing/enrichment/playwright_fetcher.py`:

```python
"""The real WorkdayFetcher: headless Chromium with per-host Cloudflare-clearance reuse.

Imported lazily by the worker so CI (no playwright installed) never loads it. All status
and challenge classification is delegated to enrichment.workday's pure helpers.
"""

import httpx

from .workday import (
    WorkdayBlocked, WorkdayNotFound, WorkdayTransient,
    _classify_status, _looks_like_challenge, workday_host,
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


class PlaywrightFetcher:
    """One browser + a per-host Cloudflare-cleared context cache. NOT thread-safe (Playwright
    sync objects belong to their creating thread); the worker gives each pool thread its own
    instance and shards jobs by tenant so a tenant's clearance is established once and reused."""

    def __init__(self, *, headless: bool = True, settle_ms: int = 5000, nav_timeout_ms: int = 45000):
        from playwright.sync_api import sync_playwright  # lazy
        self._settle_ms, self._nav_timeout_ms = settle_ms, nav_timeout_ms
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._contexts: dict[str, object] = {}
        self._http = httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    def fetch_plain(self, cxs_url: str) -> dict:
        try:
            r = self._http.get(cxs_url)
        except httpx.HTTPError as exc:
            raise WorkdayTransient(f"plain: {exc}") from exc
        _classify_status(r.status_code)
        return r.json()

    def _cleared_context(self, host: str):
        if host not in self._contexts:
            ctx = self._browser.new_context(user_agent=_UA, locale="en-US")
            page = ctx.new_page()
            try:
                page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                page.wait_for_timeout(self._settle_ms)  # let the CF JS challenge resolve
            finally:
                page.close()
            self._contexts[host] = ctx
        return self._contexts[host]

    def fetch_via_context(self, host: str, cxs_url: str) -> dict:
        ctx = self._cleared_context(host)
        r = ctx.request.get(cxs_url, headers={"Accept": "application/json"})
        _classify_status(r.status)
        return r.json()

    def fetch_via_render(self, job_url: str) -> dict:
        ctx = self._cleared_context(workday_host(job_url))
        page = ctx.new_page()
        captured: dict = {}

        def on_response(resp):
            if "/wday/cxs/" in resp.url and "/job/" in resp.url and resp.status == 200:
                try:
                    captured["payload"] = resp.json()
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        title = ""
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
            page.wait_for_timeout(self._settle_ms)
            title = page.title() or ""
        except Exception as exc:  # noqa: BLE001 - navigation failure
            raise WorkdayTransient(f"render: {exc}") from exc
        finally:
            page.close()
        if "payload" in captured:
            return captured["payload"]
        if _looks_like_challenge(title):
            raise WorkdayBlocked("render blocked by cloudflare challenge")
        raise WorkdayNotFound("no cxs job payload from rendered page")

    def close(self):
        self._http.close()
        self._browser.close()
        self._pw.stop()
```

- [ ] **Step 2: Verify it parses**

Run: `uv run python -c "import ast; ast.parse(open('src/jobmaxxing/enrichment/playwright_fetcher.py').read()); print('parse OK')"`
Expected: `parse OK`.

- [ ] **Step 3: Commit**

```bash
git add src/jobmaxxing/enrichment/playwright_fetcher.py
git commit -m "feat(workday): PlaywrightFetcher (tiered headless fetch + per-host CF clearance)"
```

---

### Task 9: Validation spike script + e2e test

**Files:**
- Create: `scripts/spike_workday.py`
- Create: `tests/test_workday_e2e.py`

- [ ] **Step 1: Write the spike script**

Create `scripts/spike_workday.py` (operator runs it to measure real-world tier yield before/while building; it imports the real fetcher + dispatch):

```python
"""Spike: measure Workday tiered-fetch yield against live jobs from the DB.

Run locally with the headless extra installed:
    uv run --extra headless python scripts/spike_workday.py [N]
Prints a per-outcome tally so we can confirm the headless approach's real hit rate.
"""

import os
import sys
from collections import Counter

import psycopg
from dotenv import load_dotenv

from jobmaxxing.enrichment.playwright_fetcher import PlaywrightFetcher
from jobmaxxing.enrichment.workday import fetch_workday_one


def main(n: int) -> None:
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "select id, url from jobs where url ~* 'myworkdayjobs\\.com' "
            "and coalesce(description,'')='' order by scraped_at desc limit %s",
            (n,),
        ).fetchall()
    fetcher = PlaywrightFetcher()
    tally: Counter = Counter()
    try:
        for job_id, url in rows:
            out = fetch_workday_one(job_id, url, fetcher)
            tally[out.kind] += 1
            print(f"{out.kind:9} {url[:70]}")
    finally:
        fetcher.close()
    print("\nyield:", dict(tally), f"(of {len(rows)})")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
```

- [ ] **Step 2: Write the e2e test (skipped unless `JOBMAXXING_E2E=1`)**

Create `tests/test_workday_e2e.py`:

```python
"""End-to-end: real Playwright vs. live Workday. Skipped unless JOBMAXXING_E2E=1 (like the
pdflatex tests) — never runs in CI/normal pytest. Run locally with the headless extra."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1",
    reason="set JOBMAXXING_E2E=1 (and install the headless extra) to run live Workday e2e",
)

# A Tier-0-reachable tenant (edu/gov often serve cxs directly). Replace if it goes stale.
_TIER0_URL = "https://psu.wd1.myworkdayjobs.com/PSU_Staff/job/Penn-State-Berks/Part-Time---Physics---Internship_REQ_0000068406-1"


def test_live_workday_tier0_enriches():
    from jobmaxxing.enrichment.playwright_fetcher import PlaywrightFetcher
    from jobmaxxing.enrichment.workday import fetch_workday_one

    fetcher = PlaywrightFetcher()
    try:
        out = fetch_workday_one("e2e", _TIER0_URL, fetcher)
    finally:
        fetcher.close()
    # A live posting enriches with a non-trivial HTML description; a stale one is permanent.
    assert out.kind in {"enriched", "permanent"}
    if out.kind == "enriched":
        assert out.description and len(out.description) > 200
```

- [ ] **Step 3: Verify the e2e test is collected-but-skipped in a normal run**

Run: `uv run pytest tests/test_workday_e2e.py -v`
Expected: `1 skipped` (the skipif guard fires when `JOBMAXXING_E2E` is unset).

- [ ] **Step 4: Commit**

```bash
git add scripts/spike_workday.py tests/test_workday_e2e.py
git commit -m "feat(workday): validation spike script + skip-by-default live e2e test"
```

---

### Task 10: README — local Workday enrichment usage

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document setup + usage**

Add a section to `README.md` (place it near any existing enrichment/operator notes):

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(workday): local enrichment setup + usage"
```

---

## Done criteria

- **Stream 1:** `uv run pytest tests/test_workday_unit.py tests/test_workday_db.py tests/test_enrich_db.py -q` green; `_apply_outcomes` refactor leaves all pre-existing enrich tests green.
- **Stream 2 (post-integration):** `playwright_fetcher.py` imports cleanly with Stream 1 merged; `uv run pytest tests/test_workday_e2e.py` reports `1 skipped` normally; default `uv sync --frozen --no-dev` env has no playwright.
- **Whole feature:** `uv run pytest -q` green (Workday unit + db tests added; e2e skipped; 2 pdflatex skipped). `python -m jobmaxxing.enrich_workday` runs locally; CI pipeline unchanged.
- **Operator validation:** `scripts/spike_workday.py` reports a non-trivial `enriched` yield against live jobs (confirms the headless approach); first real run drains a batch and routing picks up the newly-JD-bearing Workday rows.
