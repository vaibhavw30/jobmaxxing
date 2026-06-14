# Nightly Operator Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a nightly worklist of relevant, JD-less Workday jobs both automated paths gave up on, plus MCP tools to ingest a JD they provide and to reject a wrong recovered JD.

**Architecture:** A `nightly_queue` SQL view (migration 0006) + three new functions in `mcp/tools.py` (`nightly_queue`, `set_description`, `reject_recovered`) and a `jd_source` filter on `query_jobs`, all registered in `mcp/server.py`. Plain `(conn, …) -> dict/list` tools mirroring the existing MCP pattern; `set_description` resets routing (the reset contract) so the JD flows on to tailoring.

**Tech Stack:** Python 3.12, psycopg3, FastMCP (`mcp` SDK), pytest + pytest-postgresql.

**Spec:** `docs/superpowers/specs/2026-06-14-nightly-queue-design.md`

---

## File structure

- Create `migrations/0006_nightly_queue.sql` — the `nightly_queue` view.
- Modify `src/jobmaxxing/mcp/tools.py` — `_QUEUE_COLS`, `nightly_queue`, `set_description`, `_RECOVER_CAP`, `reject_recovered`; `jd_source` param on `query_jobs`.
- Modify `src/jobmaxxing/mcp/server.py` — register the three new tools; add `jd_source` to the `query_jobs` MCP tool.
- Create `tests/test_mcp_queue_tools.py` — the `conn` fixture + `_insert` helper + all tests.
- Modify `README.md` — the nightly operator flow.

All tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

### Task 1: `nightly_queue` view + tool

**Files:**
- Create: `migrations/0006_nightly_queue.sql`
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_queue_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_queue_tools.py`:

```python
import uuid

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import nightly_queue


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, description="", resume_type="swe", route_method="rules",
            recover_attempts=0, jd_source=None, company="Acme", title="SWE Intern"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, recover_attempts, jd_source) "
        "values (%s,'github:simplify',%s,%s,%s,%s,%s,%s,%s,%s)",
        (dedupe_key, company, title, f"https://x/{dedupe_key}", description, resume_type,
         route_method, recover_attempts, jd_source),
    )
    conn.commit()
    return conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0]


def test_nightly_queue_selects_only_exhausted_relevant_jdless(conn):
    _insert(conn, dedupe_key="q|ok", recover_attempts=2)                                 # appears
    _insert(conn, dedupe_key="q|fresh", recover_attempts=1)                              # not exhausted
    _insert(conn, dedupe_key="q|hasdesc", description="already", recover_attempts=2)     # has JD
    _insert(conn, dedupe_key="q|norel", resume_type=None, route_method=None, recover_attempts=2)  # not relevant
    _insert(conn, dedupe_key="q|manual", route_method="manual", recover_attempts=2)      # manual
    rows = nightly_queue(conn)
    assert {r["title"] for r in rows} == {"SWE Intern"}
    assert len(rows) == 1
    r = rows[0]
    assert r["resume_type"] == "swe" and r["url"].endswith("q|ok")
    assert isinstance(r["id"], str)        # JSON-safe (uuid -> str)


def test_nightly_queue_caps_limit(conn):
    for i in range(3):
        _insert(conn, dedupe_key=f"q|c{i}", recover_attempts=2)
    assert len(nightly_queue(conn, limit=2)) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'nightly_queue'` (and the view doesn't exist).

- [ ] **Step 3: Create the view + the tool**

Create `migrations/0006_nightly_queue.sql`:

```sql
-- The operator's nightly manual-capture worklist: relevant, still-JD-less jobs that BOTH the
-- headless worker and find-elsewhere exhausted. recover_attempts >= 2 mirrors recover_jd's
-- default cap (keep in sync). Idempotent (create or replace).
create or replace view nightly_queue as
  select id, company, title, url, resume_type, route_confidence, scraped_at
  from jobs
  where coalesce(description, '') = ''
    and resume_type is not null
    and route_method is distinct from 'manual'
    and recover_attempts >= 2
  order by scraped_at desc;
```

In `src/jobmaxxing/mcp/tools.py`, add (near `_QUERY_COLS`):

```python
_QUEUE_COLS = ["id", "company", "title", "url", "resume_type", "route_confidence", "scraped_at"]


def nightly_queue(conn, *, limit=50) -> list[dict]:
    """The operator's manual-capture worklist: relevant, still-JD-less jobs both the headless
    worker and find-elsewhere gave up on (from the nightly_queue view). limit hard-capped at 200."""
    capped = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"select {', '.join(_QUEUE_COLS)} from nightly_queue limit %s", (capped,)
    ).fetchall()
    return [{c: _json_safe(v) for c, v in zip(_QUEUE_COLS, row)} for row in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add migrations/0006_nightly_queue.sql src/jobmaxxing/mcp/tools.py tests/test_mcp_queue_tools.py
git commit -m "feat(mcp): nightly_queue view + tool (relevant JD-less exhausted worklist)"
```

---

### Task 2: `set_description` tool

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_queue_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_queue_tools.py`:

```python
from jobmaxxing.mcp.tools import set_description


def test_set_description_writes_jd_and_resets_routing(conn):
    jid = _insert(conn, dedupe_key="sd|1", resume_type="mle", route_method="llm_title", recover_attempts=2)
    out = set_description(conn, jid, "  A real pasted job description with words.  ")
    assert out["jd_source"] == "manual" and out["chars"] == len("A real pasted job description with words.")
    row = conn.execute(
        "select description, jd_source, resume_type, route_method from jobs where id=%s", (jid,)
    ).fetchone()
    assert row[0] == "A real pasted job description with words."
    assert row[1] == "manual" and row[2] is None and row[3] is None     # reset so route_new re-routes


def test_set_description_rejects_empty(conn):
    jid = _insert(conn, dedupe_key="sd|2", recover_attempts=2)
    with pytest.raises(ValueError, match="empty"):
        set_description(conn, jid, "   ")


def test_set_description_unknown_id_raises(conn):
    with pytest.raises(ValueError, match="no job"):
        set_description(conn, uuid.uuid4(), "some jd")
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -k set_description -v`
Expected: FAIL — `ImportError: cannot import name 'set_description'`.

- [ ] **Step 3: Implement `set_description`**

In `src/jobmaxxing/mcp/tools.py`, add:

```python
def set_description(conn, job_id, text) -> dict:
    """Ingest a JD the operator obtained (pasted, or fetched by Claude-in-Chrome). Writes the
    description, marks jd_source='manual', and resets resume_type/route_method to NULL (the reset
    contract) so the next route_new re-routes it with the JD — then it can be approved + tailored."""
    text = (text or "").strip()
    if not text:
        raise ValueError("description text is empty")
    with conn.transaction():
        cur = conn.execute(
            "update jobs set description=%s, jd_source='manual', resume_type=null, route_method=null "
            "where id=%s",
            (text, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")
    return {"job_id": str(job_id), "jd_source": "manual", "chars": len(text)}
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -k set_description -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_queue_tools.py
git commit -m "feat(mcp): set_description — ingest an operator JD + reset routing"
```

---

### Task 3: `reject_recovered` tool

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_queue_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_queue_tools.py`:

```python
from jobmaxxing.mcp.tools import reject_recovered


def _jid_in(rows, jid):
    return any(r["id"] == str(jid) for r in rows)


def test_reject_recovered_clears_jd_and_returns_to_queue(conn):
    jid = _insert(conn, dedupe_key="rr|1", description="wrong recovered jd", jd_source="recovered",
                  resume_type="swe", route_method="rules", recover_attempts=0)
    out = reject_recovered(conn, jid)
    assert out["status"] == "rejected_recovered"
    row = conn.execute(
        "select description, jd_source, resume_type, recover_attempts from jobs where id=%s", (jid,)
    ).fetchone()
    assert row[0] is None and row[1] is None and row[2] == "swe" and row[3] >= 2  # JD cleared, type kept, capped
    # and it now reappears in the nightly worklist for manual capture
    assert _jid_in(nightly_queue(conn), jid)


def test_reject_recovered_only_touches_recovered_rows(conn):
    jid = _insert(conn, dedupe_key="rr|2", description="an ATS jd", jd_source=None, recover_attempts=0)
    with pytest.raises(ValueError, match="no recovered job"):
        reject_recovered(conn, jid)
    # unchanged
    assert conn.execute("select description from jobs where id=%s", (jid,)).fetchone()[0] == "an ATS jd"
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -k reject_recovered -v`
Expected: FAIL — `ImportError: cannot import name 'reject_recovered'`.

- [ ] **Step 3: Implement `reject_recovered`**

In `src/jobmaxxing/mcp/tools.py`, add:

```python
_RECOVER_CAP = 2   # must match recover_jd's cap so find-elsewhere won't re-grab a rejected JD


def reject_recovered(conn, job_id) -> dict:
    """Reject a wrong recovered JD: clear the description, cap recover_attempts so find-elsewhere
    won't re-grab it, keep resume_type so the job drops back into nightly_queue for manual capture.
    Guarded to jd_source='recovered' so it can't accidentally wipe an ATS/manual JD."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set description=null, jd_source=null, "
            "recover_attempts=greatest(recover_attempts, %s) "
            "where id=%s and jd_source='recovered'",
            (_RECOVER_CAP, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no recovered job with id {job_id}")
    return {"job_id": str(job_id), "status": "rejected_recovered"}
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py -k reject_recovered -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_queue_tools.py
git commit -m "feat(mcp): reject_recovered — discard a wrong recovered JD back to the queue"
```

---

### Task 4: `query_jobs` `jd_source` filter

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_queue_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_queue_tools.py`:

```python
from jobmaxxing.mcp.tools import query_jobs


def test_query_jobs_filters_by_jd_source(conn):
    _insert(conn, dedupe_key="js|rec", description="d", jd_source="recovered")
    _insert(conn, dedupe_key="js|ats", description="d", jd_source=None)
    rows = query_jobs(conn, jd_source="recovered")
    assert len(rows) == 1 and rows[0]["url"].endswith("js|rec")
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py::test_query_jobs_filters_by_jd_source -v`
Expected: FAIL — `query_jobs() got an unexpected keyword argument 'jd_source'`.

- [ ] **Step 3: Add the `jd_source` parameter + clause**

In `src/jobmaxxing/mcp/tools.py`, change the `query_jobs` signature and add the clause. The signature becomes:

```python
def query_jobs(conn, *, status=None, resume_type=None, company=None,
               jd_source=None, since_days=None, limit=50) -> list[dict]:
```

And add this clause alongside the existing ones (e.g. after the `company` clause, before `since_days`):

```python
    if jd_source is not None:
        clauses.append("jd_source = %s")
        params.append(jd_source)
```

(Leave the rest of `query_jobs` exactly as-is.)

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_queue_tools.py::test_query_jobs_filters_by_jd_source -v`
Expected: PASS. Also run the existing MCP tests to confirm the additive param didn't break them: `uv run pytest tests/test_mcp_routing_tools.py -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_queue_tools.py
git commit -m "feat(mcp): query_jobs jd_source filter (list recovered JDs to spot-check)"
```

---

### Task 5: Register the tools in the MCP server + README

**Files:**
- Modify: `src/jobmaxxing/mcp/server.py`
- Modify: `README.md`

- [ ] **Step 1: Register the three new tools + the `jd_source` arg**

In `src/jobmaxxing/mcp/server.py`, update the `query_jobs` MCP tool to accept `jd_source` and add the three new tools (mirroring the existing `@mcp.tool()` wrappers that open `_conn()` and delegate to `tools.*`):

```python
@mcp.tool()
def query_jobs(status: str | None = None, resume_type: str | None = None,
               company: str | None = None, jd_source: str | None = None,
               since_days: int | None = None, limit: int = 50) -> list[dict]:
    """List postings from the feed, newest first, filtered by status/resume_type/company/jd_source/recency.
    company is a case-insensitive substring; jd_source filters how the JD was obtained ('recovered' for
    find-elsewhere JDs to spot-check); since_days bounds scraped_at. limit defaults to 50, max 200."""
    with _conn() as conn:
        return tools.query_jobs(conn, status=status, resume_type=resume_type, company=company,
                                jd_source=jd_source, since_days=since_days, limit=limit)


@mcp.tool()
def nightly_queue(limit: int = 50) -> list[dict]:
    """Tonight's manual-capture worklist: relevant, still-JD-less jobs that both the headless Workday
    worker and find-elsewhere gave up on. Open each in your own browser to read the JD, then call
    set_description. limit defaults to 50, max 200."""
    with _conn() as conn:
        return tools.nightly_queue(conn, limit=limit)


@mcp.tool()
def set_description(job_id: str, text: str) -> dict:
    """Ingest a job description you obtained for a job_id (e.g. pasted from the posting page). Stores
    it (jd_source='manual') and resets routing so the next routing run classifies it with the JD."""
    with _conn() as conn:
        return tools.set_description(conn, job_id, text)


@mcp.tool()
def reject_recovered(job_id: str) -> dict:
    """Discard a wrong auto-recovered JD (jd_source='recovered'): clears it and returns the job to the
    nightly_queue for manual capture. Errors if the job's JD was not auto-recovered."""
    with _conn() as conn:
        return tools.reject_recovered(conn, job_id)
```

(Replace the existing `query_jobs` wrapper with the version above; add the other three.)

- [ ] **Step 2: Verify the server module imports cleanly (tools registered)**

Run: `uv run python -c "import jobmaxxing.mcp.server as s; print('tools registered: ok')"`
Expected: prints `tools registered: ok` (no import/registration error).

- [ ] **Step 3: Document the nightly flow in the README**

Add to `README.md`, near the recovery/MCP sections:

```markdown
### Nightly operator queue (MCP)

For Workday jobs neither the headless worker nor find-elsewhere could enrich, the MCP surfaces a
nightly worklist:

- `nightly_queue` — relevant, still-JD-less jobs to grab by hand. Open each in your own (non-bot)
  browser, read the JD, then `set_description(job_id, "<the JD text>")` — it stores the JD and
  resets routing so the next poll classifies it with the JD, ready to approve + tailor.
- `query_jobs(jd_source="recovered")` — list the auto-recovered JDs to spot-check; `reject_recovered(job_id)`
  discards a wrong one and returns the job to the nightly queue.
```

- [ ] **Step 4: Run the full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass (new queue tests + every pre-existing test; the 5 e2e/pdflatex tests skip by design).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/server.py README.md
git commit -m "feat(mcp): register nightly_queue/set_description/reject_recovered tools + README"
```

---

## Done criteria

- `uv run pytest -q` green: `nightly_queue` (selects only exhausted-relevant-JD-less rows, capped), `set_description` (writes JD + `jd_source='manual'` + resets routing; errors on empty/unknown), `reject_recovered` (clears JD + caps attempts + keeps resume_type + guarded to recovered rows), `query_jobs` `jd_source` filter; all pre-existing tests still green.
- The MCP server imports cleanly with the four tools registered (`query_jobs` gains `jd_source`; `nightly_queue`/`set_description`/`reject_recovered` added).
- Operator can, via the MCP: pull tonight's queue, ingest pasted/fetched JDs (which then route → approve → tailor), and spot-check/reject recovered JDs. Human stays the gate.
- This completes the Workday-backup roadmap (sub-projects 1–3 all merged).
