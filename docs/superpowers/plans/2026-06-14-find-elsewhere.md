# Find-elsewhere JD Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local CLI worker that recovers job descriptions for relevant, JD-less Workday jobs via a free DuckDuckGo search + `schema.org/JobPosting` JSON-LD extraction, with a deterministic+LLM match guard, sidestepping Cloudflare.

**Architecture:** New `recovery/` package mirroring `enrichment/`'s split — pure logic (`search`, `extract`, `match`) behind injectable `searcher`/`fetcher`/`llm_confirm` callables, plus a `recover_new` worker and CLI. Recovered JDs are written with `jd_source='recovered'` and reset `resume_type`/`route_method` so CI re-routes confidently (the roadmap reset contract).

**Tech Stack:** Python 3.12, psycopg3, httpx (already a dep), stdlib `re`/`json`/`urllib.parse`; pytest + pytest-postgresql.

**Spec:** `docs/superpowers/specs/2026-06-14-find-elsewhere-design.md`

---

## File structure

- Create `src/jobmaxxing/recovery/__init__.py`, `recovery/extract.py` (`JobPosting`, `workday_req_id`, `extract_job_posting`), `recovery/search.py` (`build_query`, `ddg_search`), `recovery/match.py` (`MatchResult`, `match_job`), `recovery/recover.py` (`recover_new`, `_recover_one`, `_default_fetcher`, `_default_llm_confirm`, `main`).
- Create `src/jobmaxxing/recover_jd.py` (CLI shim).
- Create `migrations/0005_recovery.sql`.
- Create tests: `tests/test_recovery_extract.py`, `tests/test_recovery_search.py`, `tests/test_recovery_match.py`, `tests/test_recover_db.py`, `tests/test_recover_e2e.py`.

Dependency order of the modules: `extract` (defines `JobPosting`) → `match` (imports `JobPosting`) → `recover` (imports all). Build in that order.

All DB tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

### Task 1: Migration 0005 — recovery tracking columns

**Files:**
- Create: `migrations/0005_recovery.sql`
- Test: `tests/test_recover_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_recover_db.py`:

```python
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_migration_adds_recovery_columns(conn):
    cols = {r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_name='jobs'"
    ).fetchall()}
    assert {"jd_source", "recover_attempts", "recover_error"} <= cols
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_recover_db.py::test_migration_adds_recovery_columns -v`
Expected: FAIL — columns missing.

- [ ] **Step 3: Write the migration**

Create `migrations/0005_recovery.sql` (idempotent — `apply_migrations` re-runs every file):

```sql
-- JD recovery (find-elsewhere): how a description was obtained, and recovery retry tracking.
alter table jobs add column if not exists jd_source        text;    -- 'ats' | 'recovered' | 'manual'
alter table jobs add column if not exists recover_attempts int not null default 0;
alter table jobs add column if not exists recover_error    text;
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_recover_db.py::test_migration_adds_recovery_columns -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/0005_recovery.sql tests/test_recover_db.py
git commit -m "feat(recovery): migration 0005 adds jd_source/recover_attempts/recover_error"
```

---

### Task 2: `extract.py` — JobPosting + req-id + JSON-LD extraction

**Files:**
- Create: `src/jobmaxxing/recovery/__init__.py`, `src/jobmaxxing/recovery/extract.py`
- Test: `tests/test_recovery_extract.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery_extract.py`:

```python
from jobmaxxing.recovery.extract import JobPosting, extract_job_posting, workday_req_id


def test_workday_req_id():
    assert workday_req_id("https://x.wd1.myworkdayjobs.com/Ext/job/NYC/SW-Intern_JR012226") == "JR012226"
    assert workday_req_id("https://x.wd3.myworkdayjobs.com/C/job/Loc/Renewable-Eng_R-1289") == "R-1289"
    assert workday_req_id("https://x.wd1.myworkdayjobs.com/C/job/Loc/CV-Intern_REQ-4012") == "REQ-4012"
    assert workday_req_id("https://job-boards.greenhouse.io/acme/jobs/123") is None


_HTML = """<html><head>
<script type="application/ld+json">
{"@type":"JobPosting","title":"ML Intern","description":"<p>Build models</p>",
 "hiringOrganization":{"@type":"Organization","name":"Chegg"},
 "identifier":{"@type":"PropertyValue","name":"req","value":"JR012226"},
 "url":"https://glassdoor.com/job/123"}
</script></head><body>x</body></html>"""


def test_extract_top_level_job_posting():
    jp = extract_job_posting(_HTML, source_url="https://glassdoor.com/job/123")
    assert isinstance(jp, JobPosting)
    assert jp.description == "<p>Build models</p>"
    assert jp.title == "ML Intern"
    assert jp.company == "Chegg"               # hiringOrganization object -> name
    assert jp.identifier == "JR012226"         # identifier object -> value
    assert jp.url == "https://glassdoor.com/job/123"
    assert jp.source_url == "https://glassdoor.com/job/123"


def test_extract_graph_and_string_forms():
    html = ('<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebPage"},'
            '{"@type":"JobPosting","description":"d","hiringOrganization":"Acme Corp","identifier":"R-9"}]}'
            '</script>')
    jp = extract_job_posting(html)
    assert jp.description == "d" and jp.company == "Acme Corp" and jp.identifier == "R-9"


def test_extract_returns_none_when_absent_or_no_description():
    assert extract_job_posting("<html>no ld json</html>") is None
    assert extract_job_posting('<script type="application/ld+json">{"@type":"JobPosting"}</script>') is None
    assert extract_job_posting('<script type="application/ld+json">not json</script>') is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_recovery_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.recovery.extract`.

- [ ] **Step 3: Implement `extract.py`**

Create `src/jobmaxxing/recovery/__init__.py` (empty file). Create `src/jobmaxxing/recovery/extract.py`:

```python
"""Extract a schema.org JobPosting (and the Workday req-id) for find-elsewhere recovery."""

import json
import re
from dataclasses import dataclass

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL)
# Trailing slug token after the last '_': e.g. _JR012226, _R-1289, _REQ_0000068406-1
_REQID = re.compile(r"_([A-Za-z]*[-_]?\d[\w-]*)$")


@dataclass
class JobPosting:
    description: str                 # HTML
    title: str | None = None
    company: str | None = None       # hiringOrganization.name (or the string form)
    identifier: str | None = None    # identifier.value (or the string form)
    url: str | None = None           # canonical posting URL
    source_url: str | None = None    # the page we fetched it from


def workday_req_id(url: str) -> str | None:
    m = _REQID.search(url.split("?")[0].rstrip("/"))
    return m.group(1) if m else None


def _text(v):
    """hiringOrganization / identifier may be a string OR an object — normalize to a string."""
    if isinstance(v, dict):
        return v.get("name") or v.get("value")
    return v if isinstance(v, str) else None


def extract_job_posting(html_text: str, *, source_url: str | None = None) -> JobPosting | None:
    """Return the first JSON-LD JobPosting with a non-empty description, or None."""
    for block in _LD.findall(html_text):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(n, dict) and n.get("@type") == "JobPosting" and n.get("description"):
                return JobPosting(
                    description=n["description"],
                    title=n.get("title"),
                    company=_text(n.get("hiringOrganization")),
                    identifier=_text(n.get("identifier")),
                    url=n.get("url"),
                    source_url=source_url,
                )
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_recovery_extract.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/recovery/__init__.py src/jobmaxxing/recovery/extract.py tests/test_recovery_extract.py
git commit -m "feat(recovery): JobPosting JSON-LD extraction + workday_req_id"
```

---

### Task 3: `search.py` — DuckDuckGo search

**Files:**
- Create: `src/jobmaxxing/recovery/search.py`
- Test: `tests/test_recovery_search.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery_search.py`:

```python
from jobmaxxing.recovery.search import build_query, ddg_search


def test_build_query():
    assert build_query("Chegg", "Computational Linguist") == "Chegg Computational Linguist"
    assert build_query(None, "ML Intern") == "ML Intern"


_DDG_HTML = """
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fglassdoor.com%2Fjob%2F1&rut=x">Glassdoor</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftrimble.wd1.myworkdayjobs.com%2Fx&rut=y">Workday</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjobgether.com%2Fp%2F2&rut=z">Jobgether</a>
"""


def test_ddg_search_unwraps_and_filters_workday():
    seen = {}
    def fake_fetch(url):
        seen["url"] = url
        return _DDG_HTML
    results = ddg_search("Chegg Computational Linguist", fetch_text=fake_fetch)
    assert "html.duckduckgo.com" in seen["url"]
    assert results == ["https://glassdoor.com/job/1", "https://jobgether.com/p/2"]   # workday filtered out


def test_ddg_search_respects_max_results():
    def fake_fetch(url):
        return _DDG_HTML
    assert len(ddg_search("q", fetch_text=fake_fetch, max_results=1)) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_recovery_search.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.recovery.search`.

- [ ] **Step 3: Implement `search.py`**

Create `src/jobmaxxing/recovery/search.py`:

```python
"""Free DuckDuckGo HTML search -> candidate result URLs (find-elsewhere recovery)."""

import re
import urllib.parse

_RESULT = re.compile(r'class="result__a"[^>]+href="([^"]+)"')


def build_query(company: str | None, title: str | None) -> str:
    return " ".join(p for p in (company, title) if p).strip()


def ddg_search(query: str, *, fetch_text, max_results: int = 6) -> list[str]:
    """Query DuckDuckGo's HTML endpoint and return candidate result URLs, unwrapping the
    `uddg=` redirector and excluding Workday hosts (the gated source we're routing around)."""
    body = fetch_text("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    urls: list[str] = []
    for href in _RESULT.findall(body):
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        if "myworkdayjobs.com" in url:
            continue
        urls.append(url)
        if len(urls) >= max_results:
            break
    return urls
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_recovery_search.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/recovery/search.py tests/test_recovery_search.py
git commit -m "feat(recovery): DuckDuckGo search with uddg unwrap + Workday filter"
```

---

### Task 4: `match.py` — the match guard

**Files:**
- Create: `src/jobmaxxing/recovery/match.py`
- Test: `tests/test_recovery_match.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery_match.py`:

```python
from jobmaxxing.recovery.extract import JobPosting
from jobmaxxing.recovery.match import MatchResult, match_job


def _job(**kw):
    base = {"company": "Chegg", "title": "Computational Linguist", "url": "https://x.wd1.myworkdayjobs.com/j", "req_id": "JR012226"}
    base.update(kw)
    return base


def _llm_never(job, cand):
    raise AssertionError("llm_confirm must not be called")


def test_accept_on_reqid_without_llm():
    cand = JobPosting(description="d", title="Comp Linguist", company="Chegg", identifier="JR012226")
    r = match_job(_job(), cand, llm_confirm=_llm_never)
    assert r.accepted and r.reason == "reqid"


def test_accept_on_backlink_without_llm():
    cand = JobPosting(description="d", company="Chegg", url="https://x.wd1.myworkdayjobs.com/j")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert r.accepted and r.reason == "backlink"


def test_reject_company_mismatch_without_llm():
    cand = JobPosting(description="d", title="Computational Linguist", company="TotallyOther Inc", identifier="ZZ")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert not r.accepted and r.reason == "rejected:company"


def test_fuzzy_company_title_then_llm_confirm():
    cand = JobPosting(description="d", title="Computational Linguist (FTC)", company="Chegg Inc", identifier="ZZ")
    assert match_job(_job(req_id=None), cand, llm_confirm=lambda j, c: True).reason == "llm_confirmed"
    assert not match_job(_job(req_id=None), cand, llm_confirm=lambda j, c: False).accepted


def test_reject_title_dissimilar_without_llm():
    cand = JobPosting(description="d", title="Warehouse Forklift Operator", company="Chegg", identifier="ZZ")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert not r.accepted and r.reason == "rejected:title"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_recovery_match.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.recovery.match`.

- [ ] **Step 3: Implement `match.py`**

Create `src/jobmaxxing/recovery/match.py`:

```python
"""Decide whether a recovered JobPosting is the SAME job — deterministic first, LLM-confirm fuzzy."""

import re
from dataclasses import dataclass

from .extract import JobPosting


@dataclass
class MatchResult:
    accepted: bool
    reason: str   # "reqid" | "backlink" | "llm_confirmed" | "rejected:<why>"


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _company_matches(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    at, bt = set(a.split()), set(b.split())
    return bool(at & bt) and (a in b or b in a or len(at & bt) / max(len(at), len(bt)) >= 0.5)


def _title_similar(a, b) -> bool:
    at, bt = set(_norm(a).split()), set(_norm(b).split())
    return bool(at) and bool(bt) and len(at & bt) / len(at | bt) >= 0.4   # Jaccard


def match_job(job: dict, cand: JobPosting, *, llm_confirm) -> MatchResult:
    """job = {company, title, url, req_id}. Accept deterministically on req-id/back-link; else
    require fuzzy company+title and an LLM 'same posting?' confirm. Prefer a safe miss."""
    hay = " ".join(filter(None, [cand.description, cand.identifier, cand.url, cand.source_url, cand.title]))
    req = job.get("req_id")
    if req and req.lower() in hay.lower():
        return MatchResult(True, "reqid")
    if job.get("url") and job["url"] in hay:
        return MatchResult(True, "backlink")
    if not _company_matches(job.get("company"), cand.company):
        return MatchResult(False, "rejected:company")
    if not _title_similar(job.get("title"), cand.title):
        return MatchResult(False, "rejected:title")
    if llm_confirm(job, cand):
        return MatchResult(True, "llm_confirmed")
    return MatchResult(False, "rejected:llm")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_recovery_match.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/recovery/match.py tests/test_recovery_match.py
git commit -m "feat(recovery): match guard — deterministic reqid/backlink + fuzzy LLM-confirm"
```

---

### Task 5: `recover.py` — the worker

**Files:**
- Create: `src/jobmaxxing/recovery/recover.py`
- Test: `tests/test_recover_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recover_db.py`:

```python
from jobmaxxing.recovery.recover import recover_new

_WD = "https://acme.wd1.myworkdayjobs.com/Ext/job/NYC/ML-Intern_JR{n}"
_CAND_HTML = (
    '<script type="application/ld+json">'
    '{{"@type":"JobPosting","title":"ML Intern","description":"<p>Recovered JD {n}</p>",'
    '"hiringOrganization":"Acme","identifier":"JR{n}"}}</script>'
)


def _insert(conn, *, dedupe_key, url, description="", resume_type="mle", route_method="llm_title",
            recover_attempts=0, company="Acme", title="ML Intern"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, recover_attempts) values (%s,'github:simplify',%s,%s,%s,%s,%s,%s,%s)",
        (dedupe_key, company, title, url, description, resume_type, route_method, recover_attempts),
    )
    conn.commit()


def _searcher_ok(query, *, fetch_text):      # one candidate result URL
    return ["https://glassdoor.com/job/1"]


def _llm_never(job, cand):
    raise AssertionError("llm_confirm should not be needed (req-id matches)")


def test_recover_writes_jd_and_resets_routing(conn):
    _insert(conn, dedupe_key="r1", url=_WD.format(n="012226"))

    def fetcher(url):
        return _CAND_HTML.format(n="012226")   # carries identifier JR012226 == the job's req-id

    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=_llm_never)
    assert counts == {"recovered": 1, "missed": 0, "candidates": 1}
    row = conn.execute(
        "select description, jd_source, resume_type, route_method from jobs where dedupe_key='r1'"
    ).fetchone()
    assert row[0] == "<p>Recovered JD 012226</p>" and row[1] == "recovered"
    assert row[2] is None and row[3] is None    # reset so CI re-routes with the JD


def test_recover_miss_bumps_attempts_and_caps(conn):
    _insert(conn, dedupe_key="r2", url=_WD.format(n="999"))

    def fetcher(url):
        return _CAND_HTML.format(n="DIFFERENT")   # identifier mismatch, company 'Acme' but...

    # company matches (Acme) + title similar (ML Intern) -> falls to llm_confirm; force a reject
    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)
    assert counts["missed"] == 1
    assert conn.execute("select recover_attempts from jobs where dedupe_key='r2'").fetchone()[0] == 1
    # bump to the cap -> no longer selected
    recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)
    assert recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)["candidates"] == 0


def test_recover_selects_only_relevant_jdless_workday(conn):
    _insert(conn, dedupe_key="ok", url=_WD.format(n="1"))                                   # selected
    _insert(conn, dedupe_key="hasdesc", url=_WD.format(n="2"), description="already")        # has JD
    _insert(conn, dedupe_key="norel", url=_WD.format(n="3"), resume_type=None, route_method=None)  # not relevant
    _insert(conn, dedupe_key="manual", url=_WD.format(n="4"), route_method="manual")         # manual
    _insert(conn, dedupe_key="gh", url="https://job-boards.greenhouse.io/acme/jobs/9")       # non-workday

    def fetcher(url):
        return _CAND_HTML.format(n="1")

    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=_llm_never)
    assert counts["candidates"] == 1 and counts["recovered"] == 1


def test_recover_cli_shim_exposes_main():
    import jobmaxxing.recover_jd as cli
    from jobmaxxing.recovery.recover import main
    assert cli.main is main
```

> Note: `_insert` here is local to the recovery tests (distinct from `test_route_db.py`'s). The `route_method='manual'` row also sets `resume_type='mle'` via the default, but the candidate query excludes it by `route_method='manual'`; the non-Workday row is excluded by the `url ilike` filter; the not-relevant row by `resume_type is not null`.

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_recover_db.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.recovery.recover` (and the CLI import).

- [ ] **Step 3: Implement `recover.py`**

Create `src/jobmaxxing/recovery/recover.py`:

```python
"""Find-elsewhere JD recovery worker. Run LOCALLY: python -m jobmaxxing.recover_jd"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
import psycopg

from ..config import load_settings
from .extract import extract_job_posting, workday_req_id
from .match import match_job
from .search import build_query, ddg_search

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}


def _default_fetcher(url: str) -> str:
    r = httpx.get(url, headers=_HEADERS, timeout=20.0, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _default_llm_confirm(job: dict, cand) -> bool:
    """One cheap schema-gated 'same posting?' check. Any failure -> False (prefer a safe miss)."""
    from ..llm.client import LLMUnavailable, complete
    messages = [
        {"role": "system", "content": ("Decide whether two postings are the SAME job at the SAME company. "
                                        'Respond with STRICT JSON only: {"same": true|false}. No prose.')},
        {"role": "user", "content": (f"A) {job.get('company')} — {job.get('title')}\n"
                                      f"B) {cand.company} — {cand.title}\n"
                                      f"B description:\n{(cand.description or '')[:600]}")},
    ]
    try:
        text = complete("route", messages, max_tokens=50, response_format={"type": "json_object"})
    except LLMUnavailable:
        return False
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return False
    try:
        return json.loads(m.group(0)).get("same") is True
    except (ValueError, TypeError):
        return False


@dataclass
class _Outcome:
    job_id: object
    description: str | None     # set when recovered
    error: str | None           # set when missed


def _recover_one(job_id, company, title, url, *, searcher, fetcher, llm_confirm) -> _Outcome:
    job = {"company": company, "title": title, "url": url, "req_id": workday_req_id(url)}
    try:
        results = searcher(build_query(company, title), fetch_text=fetcher)
    except Exception as exc:  # noqa: BLE001 - a search failure just misses this job
        return _Outcome(job_id, None, f"search: {type(exc).__name__}")
    for result_url in results:
        try:
            cand = extract_job_posting(fetcher(result_url), source_url=result_url)
        except Exception:  # noqa: BLE001 - skip an unfetchable/unparseable candidate
            continue
        if cand and match_job(job, cand, llm_confirm=llm_confirm).accepted:
            return _Outcome(job_id, cand.description, None)
    return _Outcome(job_id, None, "no match")


def recover_new(conn, *, max_jobs=100, max_workers=3, cap=2,
                searcher=ddg_search, fetcher=_default_fetcher, llm_confirm=_default_llm_confirm) -> dict:
    """Recover JDs for relevant, JD-less Workday rows. Returns {recovered, missed, candidates}."""
    rows = conn.execute(
        "select id, company, title, url from jobs "
        "where coalesce(description,'')='' "
        "and resume_type is not null "
        "and route_method is distinct from 'manual' "
        "and recover_attempts < %s "
        "and url ilike '%%myworkdayjobs%%' "
        "order by scraped_at desc limit %s",
        (cap, max_jobs),
    ).fetchall()
    if not rows:
        return {"recovered": 0, "missed": 0, "candidates": 0}

    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_recover_one, jid, co, t, u,
                               searcher=searcher, fetcher=fetcher, llm_confirm=llm_confirm)
                   for jid, co, t, u in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    recovered = [(o.description, o.job_id) for o in outcomes if o.description]
    missed = [(o.error, o.job_id) for o in outcomes if not o.description]
    if recovered or missed:
        with conn.transaction(), conn.cursor() as cur:
            if recovered:
                cur.executemany(
                    "update jobs set description=%s, jd_source='recovered', "
                    "resume_type=null, route_method=null where id=%s",
                    recovered,
                )
            if missed:
                cur.executemany(
                    "update jobs set recover_attempts=recover_attempts+1, recover_error=%s where id=%s",
                    missed,
                )
    counts = {"recovered": len(recovered), "missed": len(missed), "candidates": len(rows)}
    logger.info("recover summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"recovered: {recover_new(conn)}")
```

Create `src/jobmaxxing/recover_jd.py`:

```python
"""CLI shim: `python -m jobmaxxing.recover_jd` (run LOCALLY on a residential IP)."""

from .recovery.recover import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_recover_db.py -v`
Expected: PASS (migration + 4 recover tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/recovery/recover.py src/jobmaxxing/recover_jd.py tests/test_recover_db.py
git commit -m "feat(recovery): recover_new worker + python -m jobmaxxing.recover_jd CLI"
```

---

### Task 6: Skip-by-default e2e + README

**Files:**
- Create: `tests/test_recover_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Write the e2e test (skipped unless `JOBMAXXING_E2E=1`)**

Create `tests/test_recover_e2e.py`:

```python
"""Real DuckDuckGo + JSON-LD recovery against a live posting. Skipped unless JOBMAXXING_E2E=1
(mirrors the Workday/claude-cli e2e skips). Run on a residential IP:
JOBMAXXING_E2E=1 uv run pytest tests/test_recover_e2e.py -v"""

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("JOBMAXXING_E2E") != "1",
                                reason="set JOBMAXXING_E2E=1 to run the live find-elsewhere e2e")


def test_live_recovery_finds_a_jobposting():
    from jobmaxxing.recovery.extract import extract_job_posting
    from jobmaxxing.recovery.recover import _default_fetcher
    from jobmaxxing.recovery.search import ddg_search

    results = ddg_search("Chegg Computational Linguist job", fetch_text=_default_fetcher)
    found = False
    for url in results:
        try:
            if extract_job_posting(_default_fetcher(url), source_url=url):
                found = True
                break
        except httpx.HTTPError:
            continue
    assert found, "expected at least one JobPosting JSON-LD among DDG results"
```

- [ ] **Step 2: Verify it is collected-but-skipped in a normal run**

Run: `uv run pytest tests/test_recover_e2e.py -v`
Expected: `1 skipped`.

- [ ] **Step 3: Document the local worker in the README**

Add to `README.md`, near the Workday-enrichment section:

```markdown
### JD recovery (find-elsewhere, local)

For Workday jobs that can't be enriched, run the recovery worker LOCALLY (residential IP — it
free-searches DuckDuckGo and reads `JobPosting` JSON-LD from aggregators/company sites):

    uv run python -m jobmaxxing.recover_jd

It targets relevant (title-routed), description-less Workday rows, accepts a JD only on a
req-id/back-link match or an LLM-confirmed fuzzy match, writes it with `jd_source='recovered'`
(flagged for review), and resets routing so the next poll re-routes it with the real JD.
Optional live check: `JOBMAXXING_E2E=1 uv run pytest tests/test_recover_e2e.py -v`.
```

- [ ] **Step 4: Run the full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass; the e2e test reports skipped (alongside the existing pdflatex / Workday-e2e / claude-cli-e2e skips).

- [ ] **Step 5: Commit**

```bash
git add tests/test_recover_e2e.py README.md
git commit -m "feat(recovery): skip-by-default live e2e + README local-worker note"
```

---

## Done criteria

- `uv run pytest -q` green: unit (`extract`/`search`/`match`), integration (`recover_new` recovers + resets routing, miss bumps attempts + caps, candidate selection), all pre-existing tests; e2e skipped by default.
- `python -m jobmaxxing.recover_jd` runs locally: searches DDG, extracts JSON-LD, writes recovered JDs with `jd_source='recovered'` and resets `resume_type`/`route_method` so CI re-routes them with the JD.
- No CI workflow change; no new runtime dependency (httpx already in deps).
- Operator-validated: a real run on a residential IP recovers a non-trivial number of JDs; the next `route_new` shows those rows re-routing with `rules`/`llm` (not `llm_title`). Reminder: sub-project 3 (nightly queue) surfaces the remaining misses + the `jd_source='recovered'` rows for spot-check.
