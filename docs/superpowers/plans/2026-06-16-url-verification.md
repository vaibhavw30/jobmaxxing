# URL Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local `verify_url` stage that confirms each triaged job's posting URL resolves, finds + stores a working alternative when it doesn't, and flags/demotes dead-unrecoverable links in triage.

**Architecture:** A new `verification/` package: `liveness.py` (HTTP status → alive/dead/transient), `find.py` (reuse the recovery DuckDuckGo→schema.org→match engine to locate an alternative URL), and `verify.py` (orchestrator mirroring `recovery.recover_new`'s candidate-query + attempt-cap + batched-write pattern). New columns via migration `0010`. Triage demotes/flag dead rows. Local-only CLI; AWS-portable by construction.

**Tech Stack:** Python 3.12, httpx, psycopg3, PostgreSQL (pytest-postgresql), Flask, pytest, `uv`.

**Before every `pytest`:** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` and use `uv run pytest`.

---

## File Structure

- `src/jobmaxxing/verification/__init__.py` — **create** (empty package marker).
- `src/jobmaxxing/verification/liveness.py` — **create**: `Liveness`, `check_liveness`, `default_fetcher`.
- `src/jobmaxxing/verification/find.py` — **create**: `find_alternative_url` (reuses `recovery.*`).
- `src/jobmaxxing/verification/verify.py` — **create**: `verify_urls` orchestrator + `main`.
- `src/jobmaxxing/verify_url.py` — **create**: CLI shim (`from .verification.verify import main`).
- `migrations/0010_url_verification.sql` — **create**: 4 columns.
- `src/jobmaxxing/web/triage.py` — **modify**: `url_status` in `_DISPLAY_COLS`; dead disjunct in `_demote_clause`.
- `src/jobmaxxing/web/server.py` — **modify**: "⚠ dead link" marker in the row template.
- `README.md` — **modify**: a "URL verification (local)" subsection.

Tasks are sequential on branch `worktree-url-verification`.

---

### Task 1: Liveness check (`verification/liveness.py`)

**Files:**
- Create: `src/jobmaxxing/verification/__init__.py`, `src/jobmaxxing/verification/liveness.py`
- Test: `tests/test_verify_liveness.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_liveness.py`:

```python
from jobmaxxing.verification.liveness import check_liveness


def _fixed(status):
    return lambda url: status


def test_2xx_and_3xx_final_are_alive():
    assert check_liveness("https://x", fetcher=_fixed(200)).kind == "alive"
    assert check_liveness("https://x", fetcher=_fixed(301)).kind == "alive"


def test_404_and_410_are_dead():
    assert check_liveness("https://x", fetcher=_fixed(404)).kind == "dead"
    assert check_liveness("https://x", fetcher=_fixed(410)).kind == "dead"


def test_other_statuses_are_transient():
    for s in (403, 429, 500, 503):
        assert check_liveness("https://x", fetcher=_fixed(s)).kind == "transient"


def test_fetcher_exception_is_transient():
    def boom(url):
        raise RuntimeError("timeout")
    result = check_liveness("https://x", fetcher=boom)
    assert result.kind == "transient"
    assert result.status is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_liveness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.verification'`.

- [ ] **Step 3: Implement**

Create `src/jobmaxxing/verification/__init__.py` (empty file).

Create `src/jobmaxxing/verification/liveness.py`:

```python
"""HTTP liveness check for a job posting URL."""

from dataclasses import dataclass

import httpx

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
_TIMEOUT = 15.0


@dataclass
class Liveness:
    kind: str                 # 'alive' | 'dead' | 'transient'
    status: int | None = None
    error: str | None = None


def default_fetcher(url: str) -> int:
    """GET the URL (following redirects) and return the final HTTP status code. GET not HEAD —
    many ATS boards reject or mis-handle HEAD."""
    return httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True).status_code


def check_liveness(url, *, fetcher=default_fetcher) -> Liveness:
    """Classify a URL: 2xx/3xx-final -> alive; 404/410 -> dead; everything else (incl. a fetch
    exception) -> transient (retry later)."""
    try:
        status = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - network/timeout is just a transient miss
        return Liveness("transient", None, type(exc).__name__)
    if 200 <= status < 400:
        return Liveness("alive", status)
    if status in (404, 410):
        return Liveness("dead", status)
    return Liveness("transient", status)
```

- [ ] **Step 4: Run to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_liveness.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/verification/__init__.py src/jobmaxxing/verification/liveness.py tests/test_verify_liveness.py
git commit -m "verify(liveness): classify a job URL as alive/dead/transient"
```

---

### Task 2: Find-alternative URL (`verification/find.py`)

**Files:**
- Create: `src/jobmaxxing/verification/find.py`
- Test: `tests/test_verify_find.py`

Context: reuses the recovery engine. Signatures (confirmed): `recovery.search.build_query(company, title)`, `recovery.search.ddg_search(query, *, fetch_text, max_results=6)`, `recovery.extract.extract_job_posting(html, *, source_url=None) -> JobPosting|None` (fields `.url`, `.source_url`, `.company`, `.title`), `recovery.extract.workday_req_id(url)`, `recovery.match.match_job(job_dict, cand, *, llm_confirm) -> MatchResult(.accepted)`. `recovery.recover._default_fetcher(url)->html` and `recovery.recover._default_llm_confirm(job, cand)->bool` are reused as defaults so behavior matches `recover_jd`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_find.py`:

```python
import types

import jobmaxxing.verification.find as find
from jobmaxxing.verification.find import find_alternative_url


def _cand(url):
    return types.SimpleNamespace(url=url, source_url=url, company="Acme", title="SWE Intern")


def test_returns_first_confidently_matched_url(monkeypatch):
    monkeypatch.setattr(find, "extract_job_posting", lambda html, source_url=None: _cand(source_url))
    monkeypatch.setattr(find, "match_job",
                        lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=cand.url == "https://r2"))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://r1", "https://r2"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out == "https://r2"


def test_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(find, "extract_job_posting", lambda html, source_url=None: _cand(source_url))
    monkeypatch.setattr(find, "match_job", lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=False))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://r1"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out is None


def test_skips_unparseable_candidate(monkeypatch):
    def extract(html, source_url=None):
        if source_url == "https://bad":
            return None
        return _cand(source_url)
    monkeypatch.setattr(find, "extract_job_posting", extract)
    monkeypatch.setattr(find, "match_job", lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=True))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://bad", "https://good"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out == "https://good"


def test_search_failure_returns_none(monkeypatch):
    def boom(q, *, fetch_text):
        raise RuntimeError("ddg blocked")
    out = find_alternative_url("Acme", "SWE Intern", "https://dead",
                               searcher=boom, fetcher=lambda url: "<html>", llm_confirm=lambda j, c: True)
    assert out is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_find.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.verification.find'`.

- [ ] **Step 3: Implement**

Create `src/jobmaxxing/verification/find.py`:

```python
"""Find a working alternative posting URL by reusing the recovery search/extract/match engine.

When a job's primary URL is dead, search the web for the same posting elsewhere (LinkedIn / job
board / ATS) and return a candidate URL ONLY if it is confidently matched to this job (recovery's
req-id / backlink / LLM check). The caller liveness-checks the returned URL before trusting it.
"""

from ..recovery.extract import extract_job_posting, workday_req_id
from ..recovery.match import match_job
from ..recovery.recover import _default_fetcher, _default_llm_confirm
from ..recovery.search import build_query, ddg_search


def find_alternative_url(company, title, original_url, *,
                         searcher=ddg_search, fetcher=_default_fetcher,
                         llm_confirm=_default_llm_confirm) -> str | None:
    job = {"company": company, "title": title, "url": original_url,
           "req_id": workday_req_id(original_url)}
    try:
        results = searcher(build_query(company, title), fetch_text=fetcher)
    except Exception:  # noqa: BLE001 - a search failure just means no alternative this round
        return None
    for result_url in results:
        try:
            cand = extract_job_posting(fetcher(result_url), source_url=result_url)
        except Exception:  # noqa: BLE001 - skip an unfetchable/unparseable candidate
            continue
        if cand and match_job(job, cand, llm_confirm=llm_confirm).accepted:
            return cand.url or cand.source_url or result_url
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_find.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/verification/find.py tests/test_verify_find.py
git commit -m "verify(find): locate a confidently-matched alternative URL via recovery engine"
```

---

### Task 3: Migration + orchestrator (`verification/verify.py`, `verify_url.py`)

**Files:**
- Create: `migrations/0010_url_verification.sql`, `src/jobmaxxing/verification/verify.py`, `src/jobmaxxing/verify_url.py`
- Test: `tests/test_verify_db.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_db.py`:

```python
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.normalize import in_window_term_labels
from jobmaxxing.verification.verify import verify_urls

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _win_term():
    return sorted(in_window_term_labels(NOW.date()))[0]


def _ins(conn, *, dedupe_key, url, alt_urls=None, status="routed", resume_type="swe",
         verify_attempts=0, verified_at=None, source="github:simplify"):
    cols = ["dedupe_key", "source", "company", "title", "url", "resume_type", "status",
            "term", "verify_attempts"]
    vals = [dedupe_key, source, "Acme", "SWE Intern", url, resume_type, status,
            [_win_term()], verify_attempts]
    if alt_urls is not None:
        cols.append("alt_urls"); vals.append(alt_urls)
    if verified_at is not None:
        cols.append("verified_at"); vals.append(verified_at)
    ph = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({ph})", vals)
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])


def _status_fetcher(status_map):
    """A liveness fetcher: returns the mapped status, or raises (transient) for unknown URLs."""
    def fetch(url):
        if url in status_map:
            return status_map[url]
        raise RuntimeError("unreachable")
    return fetch


def _row(conn, jid):
    return conn.execute(
        "select url, alt_urls, url_status, verify_attempts, verified_at from jobs where id=%s",
        (jid,)).fetchone()


def test_alive_url_marked_alive(conn):
    jid = _ins(conn, dedupe_key="v|alive", url="https://live")
    verify_urls(conn, now=NOW, liveness_fetcher=_status_fetcher({"https://live": 200}),
                find_alt=lambda c, t, u: None)
    url, alts, status, attempts, verified_at = _row(conn, jid)
    assert status == "alive" and url == "https://live" and verified_at is not None


def test_dead_url_promotes_live_alt(conn):
    jid = _ins(conn, dedupe_key="v|alt", url="https://dead", alt_urls=["https://alt"])
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://alt": 200}),
                find_alt=lambda c, t, u: None)
    url, alts, status, _, _ = _row(conn, jid)
    assert status == "alive" and url == "https://alt" and "https://dead" in alts


def test_dead_url_promotes_found_alternative(conn):
    jid = _ins(conn, dedupe_key="v|found", url="https://dead")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://found": 200}),
                find_alt=lambda c, t, u: "https://found")
    url, alts, status, _, _ = _row(conn, jid)
    assert status == "alive" and url == "https://found" and "https://dead" in alts


def test_found_alternative_that_is_dead_is_not_promoted(conn):
    jid = _ins(conn, dedupe_key="v|founddead", url="https://dead")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://found": 410}),
                find_alt=lambda c, t, u: "https://found")
    url, alts, status, attempts, _ = _row(conn, jid)
    assert status == "dead" and url == "https://dead" and attempts == 3   # default cap


def test_dead_with_no_alternative_marked_dead_and_capped(conn):
    jid = _ins(conn, dedupe_key="v|dead", url="https://dead")
    verify_urls(conn, now=NOW, cap=3,
                liveness_fetcher=_status_fetcher({"https://dead": 404}),
                find_alt=lambda c, t, u: None)
    _, _, status, attempts, _ = _row(conn, jid)
    assert status == "dead" and attempts == 3


def test_transient_only_increments_attempts(conn):
    jid = _ins(conn, dedupe_key="v|trans", url="https://flaky")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://flaky": 503}),
                find_alt=lambda c, t, u: None)
    _, _, status, attempts, verified_at = _row(conn, jid)
    assert status is None and attempts == 1 and verified_at is None


def test_candidate_query_excludes_offwindow_decided_capped_and_fresh(conn):
    # off-window (Summer 2016 tag): not a candidate
    conn.execute("insert into jobs (dedupe_key, source, company, title, url, resume_type, status, term, verify_attempts) "
                 "values ('x|off','github:simplify','C','T','https://o','swe','routed',%s,0)",
                 (["Summer 2016"],)); conn.commit()
    # decided
    _ins(conn, dedupe_key="x|dec", url="https://d", status="applied")
    # capped
    _ins(conn, dedupe_key="x|cap", url="https://c", verify_attempts=3)
    # recently verified (within reverify window)
    _ins(conn, dedupe_key="x|fresh", url="https://f", verified_at=NOW - timedelta(days=1))
    counts = verify_urls(conn, now=NOW, cap=3, reverify_days=14,
                         liveness_fetcher=_status_fetcher({}), find_alt=lambda c, t, u: None)
    assert counts["candidates"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_db.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.verification.verify'` (and the column doesn't exist).

- [ ] **Step 3a: Create the migration**

Create `migrations/0010_url_verification.sql`:

```sql
-- URL verification: whether a posting's link still resolves, with the same attempt-cap shape as
-- enrich/recover. url_status: NULL=unverified, 'alive', 'dead'. A 'dead' row has verify_attempts
-- set to the cap so it isn't reselected until the operator bumps the cap. No backfill: every row
-- starts unverified.
alter table jobs add column if not exists url_status      text;
alter table jobs add column if not exists verify_attempts int not null default 0;
alter table jobs add column if not exists verified_at      timestamptz;
alter table jobs add column if not exists verify_error     text;
```

- [ ] **Step 3b: Create the orchestrator**

Create `src/jobmaxxing/verification/verify.py`:

```python
"""URL verification stage. Run LOCALLY: python -m jobmaxxing.verify_url

For the in-window triage backlog, confirm each posting URL resolves; when dead, promote a working
alternative (existing alt_url, else a confidently-matched search result); else mark it dead. Mirrors
recovery.recover_new's candidate-query + attempt-cap + batched-write shape.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import load_settings
from ..normalize import in_window_term_labels, off_window_sql
from .find import find_alternative_url
from .liveness import check_liveness, default_fetcher

logger = logging.getLogger(__name__)

DEFAULT_CAP = 3
DEFAULT_MAX_JOBS = 200
DEFAULT_REVERIFY_DAYS = 14
DEFAULT_MAX_WORKERS = 5


@dataclass
class _Outcome:
    job_id: object
    kind: str                              # 'alive' | 'dead' | 'transient'
    new_url: str | None = None             # set when a working alternative is promoted
    new_alt_urls: list = field(default_factory=list)
    error: str | None = None


def _fold_alts(new_primary, old_url, alt_urls):
    """Old primary + existing alts, order-preserving dedup, excluding the new primary."""
    seen = [old_url, *(alt_urls or [])]
    return [u for u in dict.fromkeys(seen) if u != new_primary]


def _verify_one(job_id, url, alt_urls, company, title, *, liveness_fetcher, find_alt) -> _Outcome:
    live = check_liveness(url, fetcher=liveness_fetcher)
    if live.kind == "alive":
        return _Outcome(job_id, "alive")
    if live.kind == "transient":
        return _Outcome(job_id, "transient", error=str(live.status or live.error))
    # dead: try existing alts, then a confidently-matched search result; promote only if it resolves.
    for alt in alt_urls:
        if check_liveness(alt, fetcher=liveness_fetcher).kind == "alive":
            return _Outcome(job_id, "alive", new_url=alt, new_alt_urls=_fold_alts(alt, url, alt_urls))
    found = find_alt(company, title, url)
    if found and check_liveness(found, fetcher=liveness_fetcher).kind == "alive":
        return _Outcome(job_id, "alive", new_url=found, new_alt_urls=_fold_alts(found, url, alt_urls))
    return _Outcome(job_id, "dead", error=f"dead:{live.status}")


def verify_urls(conn: psycopg.Connection, *, now=None, cap=DEFAULT_CAP, max_jobs=DEFAULT_MAX_JOBS,
                reverify_days=DEFAULT_REVERIFY_DAYS, max_workers=DEFAULT_MAX_WORKERS,
                liveness_fetcher=default_fetcher, find_alt=find_alternative_url) -> dict:
    now = now or datetime.now(timezone.utc)
    labels = in_window_term_labels(now.date())
    cutoff = now - timedelta(days=reverify_days)
    rows = conn.execute(
        f"select id, url, alt_urls, company, title from jobs "
        f"where resume_type is not null and status in ('new', 'routed') "
        f"and not ({off_window_sql(labels)}) "
        f"and verify_attempts < %s and (verified_at is null or verified_at < %s) "
        f"order by verified_at asc nulls first, scraped_at desc limit %s",
        (cap, cutoff, max_jobs),
    ).fetchall()
    if not rows:
        return {"alive": 0, "promoted": 0, "dead": 0, "transient": 0, "candidates": 0}

    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_verify_one, jid, url, list(alts or []), co, t,
                               liveness_fetcher=liveness_fetcher, find_alt=find_alt)
                   for jid, url, alts, co, t in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    alive_plain = [(now, o.job_id) for o in outcomes if o.kind == "alive" and o.new_url is None]
    promoted = [(o.new_url, o.new_alt_urls, now, o.job_id)
                for o in outcomes if o.kind == "alive" and o.new_url is not None]
    dead = [(cap, now, o.error, o.job_id) for o in outcomes if o.kind == "dead"]
    transient = [(o.error, o.job_id) for o in outcomes if o.kind == "transient"]

    with conn.transaction(), conn.cursor() as cur:
        if alive_plain:
            cur.executemany(
                "update jobs set url_status='alive', verified_at=%s, verify_error=null where id=%s",
                alive_plain)
        if promoted:
            cur.executemany(
                "update jobs set url=%s, alt_urls=%s, url_status='alive', verified_at=%s, "
                "verify_error=null where id=%s", promoted)
        if dead:
            cur.executemany(
                "update jobs set url_status='dead', verify_attempts=%s, verified_at=%s, "
                "verify_error=%s where id=%s", dead)
        if transient:
            cur.executemany(
                "update jobs set verify_attempts=verify_attempts+1, verify_error=%s where id=%s",
                transient)

    counts = {"alive": len(alive_plain), "promoted": len(promoted), "dead": len(dead),
              "transient": len(transient), "candidates": len(rows)}
    logger.info("verify summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"verify: {verify_urls(conn)}")
```

- [ ] **Step 3c: Create the CLI shim**

Create `src/jobmaxxing/verify_url.py`:

```python
"""CLI entrypoint: python -m jobmaxxing.verify_url (run LOCALLY)."""

from .verification.verify import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_verify_db.py -q`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add migrations/0010_url_verification.sql src/jobmaxxing/verification/verify.py src/jobmaxxing/verify_url.py tests/test_verify_db.py
git commit -m "verify(stage): url_status schema + verify_urls orchestrator + CLI"
```

---

### Task 4: Triage demotion + url_status display (`web/triage.py`)

**Files:**
- Modify: `src/jobmaxxing/web/triage.py`
- Test: `tests/test_web_triage.py`

- [ ] **Step 1: Write the failing tests**

The `_insert` helper in `tests/test_web_triage.py` already accepts `source` and `term`. Append these tests (they pass `in_window_labels` so the demotion is active; the helper's default `term=None` rows are non-github-agnostic — seed `term` in-window to avoid off-window demotion interfering):

```python
def test_dead_url_row_demoted(conn):
    alive = _insert(conn, dedupe_key="u|alive", term=["Summer 2026"], posted_at="2026-01-01",
                    route_confidence=0.9)
    dead = _insert(conn, dedupe_key="u|dead", term=["Summer 2026"], posted_at="2026-06-01",
                   route_confidence=0.9)
    conn.execute("update jobs set url_status='dead' where id=%s", (dead,)); conn.commit()
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, in_window_labels=["Summer 2026"])]
    assert ids.index(alive) < ids.index(dead)  # dead link sinks despite being newer


def test_dead_demotion_applies_to_all_sorts(conn):
    a_dead = _insert(conn, dedupe_key="u|a", company="Aardvark", term=["Summer 2026"])
    conn.execute("update jobs set url_status='dead' where id=%s", (a_dead,)); conn.commit()
    z_alive = _insert(conn, dedupe_key="u|z", company="Zzz", term=["Summer 2026"])
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc",
                                                   in_window_labels=["Summer 2026"])]
    assert ids.index(z_alive) < ids.index(a_dead)  # Zzz(alive) before Aardvark(dead)


def test_url_status_in_rows(conn):
    jid = _insert(conn, dedupe_key="u|s", term=["Summer 2026"])
    conn.execute("update jobs set url_status='alive' where id=%s", (jid,)); conn.commit()
    rows = fetch_triage_rows(conn, in_window_labels=["Summer 2026"])
    assert rows[0]["url_status"] == "alive"
```

- [ ] **Step 2: Run to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_triage.py -k "dead or url_status" -q`
Expected: FAIL — `url_status` not in returned rows (KeyError) and dead rows not demoted.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/web/triage.py`:

Add `url_status` to the display columns:

```python
_DISPLAY_COLS = (*TRIAGE_COLUMNS, "route_confidence", "term", "url_status")
```

Extend `_demote_clause` so dead rows (any source) sink with the off-window tier:

```python
def _demote_clause(in_window_labels) -> str:
    """ORDER BY key that sinks rows the operator shouldn't act on to the bottom of EVERY sort:
    off-window github rows (date-aware, shared via ``normalize.off_window_sql``) AND rows whose link
    is confirmed dead (``url_status='dead'``, any source). Hiding these is a visibility rule, not a
    sort column."""
    return f"({off_window_sql(in_window_labels)} or url_status = 'dead') asc"
```

- [ ] **Step 4: Run to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_triage.py -q`
Expected: PASS (new + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/web/triage.py tests/test_web_triage.py
git commit -m "web(triage): demote dead-link rows + expose url_status"
```

---

### Task 5: Dead-link marker in the table (`web/server.py`)

**Files:**
- Modify: `src/jobmaxxing/web/server.py`
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Write the failing tests**

The `_insert` helper in `tests/test_web_server.py` already accepts `term`. Append:

```python
def test_dead_link_marker_rendered(client, conn):
    jid = _insert(conn, dedupe_key="m|dead", term=["Summer 2026"], company="DeadCo")
    conn.execute("update jobs set url_status='dead' where id=%s", (jid,)); conn.commit()
    html = client.get("/").get_data(as_text=True)
    assert "dead link" in html.lower()


def test_no_marker_for_alive_link(client, conn):
    jid = _insert(conn, dedupe_key="m|ok", term=["Summer 2026"], company="OkCo")
    conn.execute("update jobs set url_status='alive' where id=%s", (jid,)); conn.commit()
    html = client.get("/").get_data(as_text=True)
    assert "OkCo" in html and "dead link" not in html.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_server.py -k "dead_link or alive_link" -q`
Expected: FAIL — no "dead link" text in the HTML.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/web/server.py` `INDEX_HTML`, find the posting-link cell:

```html
    <td>
      <a class="posting-link" href="{{ row.url }}" target="_blank" rel="noopener">Open posting</a>
    </td>
```

Replace it with one that appends the marker when the link is dead:

```html
    <td>
      <a class="posting-link" href="{{ row.url }}" target="_blank" rel="noopener">Open posting</a>
      {% if row.url_status == 'dead' %}<span class="dead-link" title="link did not resolve; no working alternative found">&#9888; dead link</span>{% endif %}
    </td>
```

And add a style rule inside the existing `<style>` block (next to the other `.badge`/link rules):

```css
  .dead-link { color: #b91c1c; font-size: 11px; font-weight: 600; margin-left: 6px; white-space: nowrap; }
```

- [ ] **Step 4: Run to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_server.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/web/server.py tests/test_web_server.py
git commit -m "web(server): dead-link marker on the posting link"
```

---

### Task 6: README + full-suite regression

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the stage**

In `README.md`, under the local operator stages (near the `recover_jd` / `enrich_workday` docs), add:

```markdown
### URL verification (local)

`uv run python -m jobmaxxing.verify_url` checks that the in-window triaged jobs' posting URLs still
resolve. When a URL is dead (404/410), it tries the job's other known URLs, then searches the web for
the same posting (reusing the recovery engine) and promotes a confidently-matched, working link to the
primary URL (folding the dead one into `alt_urls`). When nothing resolves, it marks `url_status='dead'`
— the triage table shows a "⚠ dead link" marker and sinks the row to the bottom.

Run LOCALLY (DuckDuckGo rate-limits datacenter IPs), like `recover_jd`. Re-checks each job every ~14
days; a dead row stays dead (verify_attempts hits the cap) until re-run with a higher cap.
```

- [ ] **Step 2: Commit the docs**

```bash
git add README.md
git commit -m "docs: URL verification (local) stage"
```

- [ ] **Step 3: Full-suite regression**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass (pre-existing skips OK). If anything fails, STOP and fix before review/merge.

---

## Self-Review

**Spec coverage:**
- Schema (migration 0010) → Task 3. ✓
- Liveness (status-code) → Task 1. ✓
- Find-alternative (reuse recovery, confident match) → Task 2. ✓
- Orchestrator: candidate query (in-window/routed/undecided/not-fresh/under-cap), promote alt/found, dead-cap, transient-increment, batched writes → Task 3. ✓
- Dead & unrecoverable: `url_status='dead'`, demote + marker (is_active untouched) → Tasks 4, 5. ✓
- Local CLI + README → Tasks 3, 6. ✓

**Type consistency:** `Liveness.kind` ∈ {alive,dead,transient} used in Task 1 + Task 3's `_verify_one`. `find_alternative_url(company, title, original_url, *, searcher, fetcher, llm_confirm)` defined Task 2, called as `find_alt(company, title, url)` in Task 3 (the injected default is `find_alternative_url`; the 3 positional args match). `verify_urls(conn, *, now, cap, max_jobs, reverify_days, max_workers, liveness_fetcher, find_alt)` consistent between Task 3 def and its tests. `_DISPLAY_COLS` adds `url_status` (Task 4) → consumed by `row.url_status` in template (Task 5) and tests. `_demote_clause` disjunct uses `url_status = 'dead'` consistently.

**Placeholder scan:** every code step is complete; commands have expected output. ✓

---

## Execution Handoff

Subagent-driven TDD, sequential, two-stage review (spec compliance → code quality) after each task, then a final review + merge to `main`. A separate small spec/plan follows for the **Dockerfile** AWS-portability groundwork.
