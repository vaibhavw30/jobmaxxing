# MCP Interface (Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the pipeline as MCP tools (FastMCP over stdio) so the operator drives the whole funnel conversationally from Claude Code, plus read-only SQL funnel views.

**Architecture:** A testable `mcp/tools.py` (plain logic functions, all boundaries injected) wrapped by a thin `mcp/server.py` (FastMCP `@mcp.tool()` decorators that open a per-call DB connection and inject the real `S3Store`/`llm.complete`/`compile_pdf`). `python -m jobmaxxing.mcp` runs it. Every tool wraps an existing Phase 1–3 function; the only new logic is a `get_artifact` storage read and the funnel views.

**Tech Stack:** Python 3.12, uv, the official `mcp` SDK (FastMCP at `mcp.server.fastmcp`), psycopg3, pytest + pytest-postgresql. Spec: `docs/superpowers/specs/2026-06-12-mcp-interface-design.md`.

**Conventions (match Phases 1–3):**
- Work in an isolated worktree off `main`; strict TDD per task; small commits.
- ENV for tests: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before `uv run pytest` (pytest-postgresql needs the local Postgres binary).
- Logic in plain functions with injected boundaries; tested against `pytest-postgresql` + `InMemoryStore` + mocked LLM/compile. The FastMCP/stdio transport is the thin untested shell (like `run.py`'s `main`).
- `uv.lock` is committed; after any dependency change run `uv lock` and commit it.
- **Namespacing:** our package is `jobmaxxing.mcp`; the SDK is top-level `mcp`. `from mcp.server.fastmcp import FastMCP` inside `jobmaxxing/mcp/server.py` resolves to the SDK (absolute import; `jobmaxxing.mcp` is never bare `mcp`). This works — do not rename.

---

### Task 1: Add the mcp SDK dep + package skeleton

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/jobmaxxing/mcp/__init__.py`, `tests/test_mcp_skeleton.py`

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_skeleton.py`:

```python
import importlib


def test_mcp_package_imports():
    assert importlib.import_module("jobmaxxing.mcp")


def test_mcp_sdk_available():
    assert importlib.import_module("mcp.server.fastmcp")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_skeleton.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.mcp'` and/or `No module named 'mcp'`.

- [ ] **Step 3: Add the dep and the package init**

In `pyproject.toml`, add `"mcp>=1.2"` to the `dependencies` list (keep all existing entries):
```toml
    "mcp>=1.2",
```

Create `src/jobmaxxing/mcp/__init__.py`:
```python
"""MCP server exposing the pipeline as conversational tools."""
```

- [ ] **Step 4: Sync deps and run the test**

Run:
```bash
uv lock
uv sync
export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"
uv run pytest tests/test_mcp_skeleton.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/jobmaxxing/mcp/__init__.py tests/test_mcp_skeleton.py
git commit -m "chore: add mcp SDK dep and mcp package skeleton"
```

---

### Task 2: Storage extension — `get_artifact` + `ArtifactMissing`

**Files:**
- Modify: `src/jobmaxxing/tailoring/storage.py`
- Test: `tests/test_storage_get_artifact.py`

**Context:** `get_review` (Task 7) must read artifacts back; the Phase-3 store only writes them. Add a read method to the Protocol and both implementations, raising `ArtifactMissing` when absent (S3 narrows to NoSuchKey/404 only, like `get_base_resume`).

- [ ] **Step 1: Write the failing test** — `tests/test_storage_get_artifact.py`:

```python
import pytest

from jobmaxxing.tailoring.storage import ArtifactMissing, InMemoryStore, S3Store


def test_in_memory_get_artifact_roundtrip():
    store = InMemoryStore()
    store.put_artifact("job1", "review.json", b'{"x": 1}')
    assert store.get_artifact("job1", "review.json") == b'{"x": 1}'


def test_in_memory_get_artifact_missing_raises():
    store = InMemoryStore()
    with pytest.raises(ArtifactMissing):
        store.get_artifact("job1", "review.json")


class _FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_s3_get_artifact_reads_key():
    client = _FakeS3(objects={"tailored/job1/review.json": b"DATA"})
    store = S3Store("b", client=client)
    assert store.get_artifact("job1", "review.json") == b"DATA"


def test_s3_get_artifact_missing_raises():
    store = S3Store("b", client=_FakeS3())
    with pytest.raises(ArtifactMissing):
        store.get_artifact("job1", "review.json")


def test_s3_get_artifact_other_error_propagates():
    from botocore.exceptions import ClientError

    class _Denied:
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    store = S3Store("b", client=_Denied())
    with pytest.raises(ClientError):
        store.get_artifact("job1", "review.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_get_artifact.py -v`
Expected: FAIL — `ImportError: cannot import name 'ArtifactMissing'`.

- [ ] **Step 3: Implement** — in `src/jobmaxxing/tailoring/storage.py`:

Add the exception near `BaseResumeMissing`:
```python
class ArtifactMissing(RuntimeError):
    """Raised when a requested artifact does not exist."""
```

Add to the `ArtifactStore` Protocol (after `artifact_prefix`):
```python
    def get_artifact(self, job_id, name: str) -> bytes: ...
```

Add to `InMemoryStore`:
```python
    def get_artifact(self, job_id, name: str) -> bytes:
        key = (str(job_id), name)
        if key not in self.artifacts:
            raise ArtifactMissing(f"no artifact {name!r} for job {job_id}")
        return self.artifacts[key]
```

Add to `S3Store`:
```python
    def get_artifact(self, job_id, name: str) -> bytes:
        key = f"tailored/{str(job_id)}/{name}"
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ArtifactMissing(f"no artifact at s3://{self.bucket}/{key}") from exc
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_get_artifact.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/tailoring/storage.py tests/test_storage_get_artifact.py
git commit -m "feat: add get_artifact + ArtifactMissing to the storage layer"
```

---

### Task 3: Funnel views migration

**Files:**
- Create: `migrations/0003_funnel_views.sql`
- Test: `tests/test_funnel_views.py`

- [ ] **Step 1: Write the failing test** — `tests/test_funnel_views.py`:

```python
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


def test_funnel_counts_view_queryable(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', 'tailored')"
    )
    conn.commit()
    rows = dict(conn.execute("select status, n from funnel_counts").fetchall())
    assert rows["tailored"] == 1


def test_review_queue_lists_tailored(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status, resume_type) "
        "values ('a|y', 'github:simplify', 'Acme', 'SWE', 'https://y', 'tailored', 'swe')"
    )
    conn.commit()
    row = conn.execute("select company, resume_type from review_queue").fetchone()
    assert row == ("Acme", "swe")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_funnel_views.py -v`
Expected: FAIL — `psycopg.errors.UndefinedTable: relation "funnel_counts" does not exist`.

- [ ] **Step 3: Implement** — `migrations/0003_funnel_views.sql`:

```sql
-- Count of jobs in each funnel stage.
create or replace view funnel_counts as
  select status, count(*) as n
  from jobs
  group by status
  order by status;

-- Tailored jobs awaiting the operator's review/decision, with their scores.
create or replace view review_queue as
  select id, company, title, resume_type, score_before, score_after, artifact_prefix, scraped_at
  from jobs
  where status = 'tailored'
  order by scraped_at desc;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_funnel_views.py -v`
Expected: 2 passed. (The migrate runner applies `0003_funnel_views.sql` after the existing migrations; `create or replace view` keeps re-runs idempotent.)

- [ ] **Step 5: Commit**

```bash
git add migrations/0003_funnel_views.sql tests/test_funnel_views.py
git commit -m "feat: add funnel_counts and review_queue SQL views"
```

---

### Task 4: tools.py — `_json_safe` + `query_jobs`

**Files:**
- Create: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_query.py`

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_query.py`:

```python
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import query_jobs


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, company="Acme", status="new", resume_type=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status, resume_type) "
        "values (%s, 'github:simplify', %s, 'SWE Intern', %s, %s, %s)",
        (dedupe_key, company, f"https://x/{dedupe_key}", status, resume_type),
    )
    conn.commit()


def test_query_returns_json_safe_rows(conn):
    _insert(conn, dedupe_key="a|1")
    rows = query_jobs(conn)
    assert len(rows) == 1
    assert isinstance(rows[0]["id"], str)            # uuid stringified
    assert rows[0]["company"] == "Acme" and rows[0]["status"] == "new"


def test_query_filters_by_status_and_type(conn):
    _insert(conn, dedupe_key="a|1", status="routed", resume_type="swe")
    _insert(conn, dedupe_key="a|2", status="new")
    assert len(query_jobs(conn, status="routed")) == 1
    assert query_jobs(conn, resume_type="swe")[0]["resume_type"] == "swe"


def test_query_company_is_case_insensitive_substring(conn):
    _insert(conn, dedupe_key="a|1", company="Acme Corp")
    assert len(query_jobs(conn, company="acme")) == 1


def test_query_limit_is_capped(conn):
    for i in range(5):
        _insert(conn, dedupe_key=f"a|{i}")
    assert len(query_jobs(conn, limit=2)) == 2
    assert len(query_jobs(conn, limit=10_000)) == 5      # hard cap doesn't error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.mcp.tools'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/mcp/tools.py`:

```python
import uuid
from datetime import datetime, timedelta, timezone

VALID_STATUSES = {
    "new", "routed", "approved_for_tailoring", "tailored", "reviewed", "applied", "rejected",
}
_QUERY_COLS = ["id", "company", "title", "status", "resume_type", "route_confidence", "url", "posted_at"]


def _json_safe(value):
    """uuid/datetime -> str so tool results are JSON-serializable for MCP."""
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    return value


def query_jobs(conn, *, status=None, resume_type=None, company=None,
               since_days=None, limit=50) -> list[dict]:
    """Filtered, capped view of the feed (newest first). limit hard-capped at 200."""
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if resume_type:
        clauses.append("resume_type = %s")
        params.append(resume_type)
    if company:
        clauses.append("company ilike %s")
        params.append(f"%{company}%")
    if since_days is not None:
        clauses.append("scraped_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(days=int(since_days)))
    where = (" where " + " and ".join(clauses)) if clauses else ""
    capped = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"select {', '.join(_QUERY_COLS)} from jobs{where} order by scraped_at desc limit %s",
        (*params, capped),
    ).fetchall()
    return [{c: _json_safe(v) for c, v in zip(_QUERY_COLS, row)} for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_query.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_query.py
git commit -m "feat: add query_jobs mcp tool"
```

---

### Task 5: tools.py — `preview_route` + `set_route`

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_routing_tools.py`

**Context:** `preview_route` reads the stored route and, with `rerun=True`, runs the Phase-2 `route_one` live (mocked LLM in tests) without persisting. `set_route` wraps `routing.route.set_manual`.

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_routing_tools.py`:

```python
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import preview_route, set_route


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"min_top_score": 1.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5},
    "types": {
        "swe": {"definition": "x", "title_signals": ["software engineer"], "jd_signals": ["api"], "exclude_signals": []},
    },
}


def _insert(conn, *, title="Software Engineer Intern", description="api work",
            resume_type="swe", route_method="rules", route_confidence=0.8):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "resume_type, route_method, route_confidence) "
        "values ('a|x', 'github:simplify', 'Acme', %s, 'https://x', %s, %s, %s, %s)",
        (title, description, resume_type, route_method, route_confidence),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def test_preview_route_returns_stored(conn):
    job_id = _insert(conn)
    out = preview_route(conn, job_id)
    assert out["stored"] == {"resume_type": "swe", "route_method": "rules", "route_confidence": 0.8}
    assert "rerun" not in out


def test_preview_route_rerun_returns_live_decision_without_persisting(conn):
    job_id = _insert(conn, resume_type=None, route_method=None, route_confidence=None)

    def llm_never(*a, **k):
        raise AssertionError("clear title should not call the LLM")

    out = preview_route(conn, job_id, rerun=True, config=CONFIG, llm_complete=llm_never)
    assert out["rerun"]["resume_type"] == "swe" and out["rerun"]["method"] == "rules"
    # not persisted: the stored value is still null
    assert conn.execute("select resume_type from jobs where id=%s", (job_id,)).fetchone()[0] is None


def test_preview_route_missing_job_raises(conn):
    with pytest.raises(ValueError):
        preview_route(conn, "00000000-0000-0000-0000-000000000000")


def test_set_route_overrides_and_marks_manual(conn):
    job_id = _insert(conn, resume_type="swe")
    out = set_route(conn, job_id, "mle")
    assert out["resume_type"] == "mle" and out["route_method"] == "manual"
    row = conn.execute("select resume_type, route_method from jobs where id=%s", (job_id,)).fetchone()
    assert row == ("mle", "manual")


def test_set_route_invalid_type_raises(conn):
    job_id = _insert(conn)
    with pytest.raises(ValueError):
        set_route(conn, job_id, "not-a-type")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_routing_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'preview_route'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/mcp/tools.py` (add imports at the TOP of the file with the existing ones):

```python
from ..llm.client import complete as default_complete
from ..routing.config import load_routing_config
from ..routing.route import route_one, set_manual
from ..routing.types import Budget
```

```python
def preview_route(conn, job_id, *, rerun=False, config=None, llm_complete=None) -> dict:
    """The stored route; with rerun=True, also what the router WOULD assign now (not persisted)."""
    row = conn.execute(
        "select title, description, resume_type, route_method, route_confidence from jobs where id=%s",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    title, description, rtype, method, conf = row
    out = {"stored": {"resume_type": rtype, "route_method": method, "route_confidence": conf}}
    if rerun:
        cfg = config if config is not None else load_routing_config()
        do_llm = llm_complete if llm_complete is not None else default_complete
        decision = route_one(title, description, cfg, llm_complete=do_llm, budget=Budget(remaining=1))
        out["rerun"] = {
            "resume_type": decision.resume_type,
            "method": decision.method,
            "confidence": decision.confidence,
        }
    return out


def set_route(conn, job_id, resume_type) -> dict:
    """Manual routing override (route_method='manual'; never auto-re-routed)."""
    set_manual(conn, job_id, resume_type)
    return {"job_id": str(job_id), "resume_type": resume_type, "route_method": "manual"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_routing_tools.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_routing_tools.py
git commit -m "feat: add preview_route and set_route mcp tools"
```

---

### Task 6: tools.py — `approve` + `set_status`

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_status_tools.py`

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_status_tools.py`:

```python
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import approve, set_status


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, status="routed"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', %s)",
        (status,),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def test_approve_sets_status(conn):
    job_id = _insert(conn)
    out = approve(conn, job_id)
    assert out["status"] == "approved_for_tailoring"
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "approved_for_tailoring"


def test_set_status_valid_transition(conn):
    job_id = _insert(conn, status="tailored")
    out = set_status(conn, job_id, "applied")
    assert out["status"] == "applied"
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "applied"


def test_set_status_rejects_unknown_status(conn):
    job_id = _insert(conn)
    with pytest.raises(ValueError):
        set_status(conn, job_id, "bogus")


def test_set_status_missing_job_raises(conn):
    with pytest.raises(ValueError):
        set_status(conn, "00000000-0000-0000-0000-000000000000", "applied")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_status_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'approve'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/mcp/tools.py` (add the import at the TOP with the others):

```python
from ..tailoring.tailor import approve as _tailoring_approve
```

```python
def approve(conn, job_id) -> dict:
    """Gate a job for tailoring (status -> approved_for_tailoring)."""
    _tailoring_approve(conn, job_id)
    return {"job_id": str(job_id), "status": "approved_for_tailoring"}


def set_status(conn, job_id, status) -> dict:
    """Move a job through the funnel (incl. applied/rejected — the human gate)."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}")
    with conn.transaction():
        cur = conn.execute("update jobs set status=%s where id=%s", (status, job_id))
    if cur.rowcount == 0:
        raise ValueError(f"no job with id {job_id}")
    return {"job_id": str(job_id), "status": status}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_status_tools.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_status_tools.py
git commit -m "feat: add approve and set_status mcp tools"
```

---

### Task 7: tools.py — `tailor` + `get_review`

**Files:**
- Modify: `src/jobmaxxing/mcp/tools.py`
- Test: `tests/test_mcp_tailor_tools.py`

**Context:** `tailor` wraps `tailor_job` (injected store/complete/compile_fn — reuse the Phase-3 harness). `get_review` reads `review.json` + `diff.txt` via the Task-2 `get_artifact`.

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_tailor_tools.py`:

```python
import json

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import get_review, tailor
from jobmaxxing.tailoring.latex import CompileResult
from jobmaxxing.tailoring.storage import ArtifactMissing, InMemoryStore


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


RUBRIC = {"keyword_dict": ["python"], "aliases": {}}


def _insert_approved(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', 'needs python', 'swe', "
        "'approved_for_tailoring')"
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def _fake_complete(task, messages, **kw):
    if task == "review" and kw.get("response_format"):
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["kafka"]}'
    return r"\documentclass{article}\begin{document} python \end{document}"


def _fake_compile(tex):
    return CompileResult(pdf_bytes=b"%PDF fake", page_count=1, log="")


def test_tailor_runs_loop_and_returns_review(conn, monkeypatch):
    import jobmaxxing.mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "load_rubric", lambda t: RUBRIC)
    job_id = _insert_approved(conn)
    store = InMemoryStore(base_resumes={"swe": r"\documentclass{article} base"})

    review = tailor(conn, job_id, store=store, complete=_fake_complete, compile_fn=_fake_compile)
    assert review["missing_keywords"] == ["kafka"]
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "tailored"


def test_get_review_roundtrips_artifacts():
    store = InMemoryStore()
    store.put_artifact("job1", "review.json", json.dumps({"score_after": {"static": 1.0}}).encode())
    store.put_artifact("job1", "diff.txt", b"--- base\n+++ tailored\n")
    out = get_review(store, "job1")
    assert out["review"]["score_after"]["static"] == 1.0
    assert out["diff"].startswith("--- base")


def test_get_review_missing_raises():
    with pytest.raises(ArtifactMissing):
        get_review(InMemoryStore(), "nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_mcp_tailor_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'tailor'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/mcp/tools.py` (add the imports at the TOP with the others):

```python
import json

from ..tailoring.rubric import load_rubric
from ..tailoring.tailor import tailor_job
```

```python
def tailor(conn, job_id, *, store, complete, compile_fn) -> dict:
    """Run the Phase-3 tailoring loop for an approved job; return the review summary."""
    return tailor_job(conn, job_id, store=store, complete=complete, compile_fn=compile_fn,
                      rubric_loader=load_rubric)


def get_review(store, job_id) -> dict:
    """Fetch review.json + diff.txt from storage and return both inline."""
    review = json.loads(store.get_artifact(job_id, "review.json").decode("utf-8"))
    diff = store.get_artifact(job_id, "diff.txt").decode("utf-8")
    return {"review": review, "diff": diff}
```

Note: `tailor` passes `rubric_loader=load_rubric` explicitly so the test's `monkeypatch.setattr(tools_mod, "load_rubric", ...)` reroutes it. (`tailor_job`'s own default is also `load_rubric`, but passing the module-level reference makes the rubric loader injectable from this layer for testing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_tailor_tools.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/tools.py tests/test_mcp_tailor_tools.py
git commit -m "feat: add tailor and get_review mcp tools"
```

---

### Task 8: server.py — FastMCP wrappers + entrypoint

**Files:**
- Create: `src/jobmaxxing/mcp/server.py`, `src/jobmaxxing/mcp/__main__.py`
- Test: `tests/test_mcp_server.py`

**Context:** Thin FastMCP layer. Each `@mcp.tool()` opens a per-call DB connection and injects the real boundaries, then calls the `tools.py` function. `@mcp.tool()` returns the wrapped function unchanged, so the names remain importable for the smoke test. Importing `server` must NOT connect to anything (only define + register).

- [ ] **Step 1: Write the failing test** — `tests/test_mcp_server.py`:

```python
def test_server_module_imports_without_connecting():
    import jobmaxxing.mcp.server as server

    assert server.mcp is not None
    assert callable(server.main)


def test_all_seven_tools_registered():
    import jobmaxxing.mcp.server as server

    for name in ("query_jobs", "preview_route", "set_route", "approve",
                 "tailor_job", "get_review", "set_status"):
        assert callable(getattr(server, name)), f"missing tool wrapper: {name}"


def test_entrypoint_module_resolves():
    import importlib

    assert importlib.import_module("jobmaxxing.mcp.__main__")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.mcp.server'`.

- [ ] **Step 3: Implement**

`src/jobmaxxing/mcp/server.py`:
```python
import logging
import os

import psycopg
from mcp.server.fastmcp import FastMCP

from ..config import load_settings
from ..llm.client import complete as llm_complete
from ..tailoring.latex import compile_pdf
from ..tailoring.storage import S3Store
from . import tools

logger = logging.getLogger(__name__)
mcp = FastMCP("jobmaxxing")


def _conn():
    return psycopg.connect(load_settings().database_url)


def _store() -> S3Store:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set (see README / .env.example)")
    return S3Store(bucket)


@mcp.tool()
def query_jobs(status: str | None = None, resume_type: str | None = None,
               company: str | None = None, since_days: int | None = None,
               limit: int = 50) -> list[dict]:
    """List postings from the feed (newest first), filtered by status/type/company/recency. limit capped at 200."""
    with _conn() as conn:
        return tools.query_jobs(conn, status=status, resume_type=resume_type,
                                company=company, since_days=since_days, limit=limit)


@mcp.tool()
def preview_route(job_id: str, rerun: bool = False) -> dict:
    """Show a job's stored route; with rerun=true, also what the router would assign now (not saved)."""
    with _conn() as conn:
        return tools.preview_route(conn, job_id, rerun=rerun)


@mcp.tool()
def set_route(job_id: str, resume_type: str) -> dict:
    """Manually override a job's resume type (route_method=manual; never auto-re-routed)."""
    with _conn() as conn:
        return tools.set_route(conn, job_id, resume_type)


@mcp.tool()
def approve(job_id: str) -> dict:
    """Approve a job for tailoring (status -> approved_for_tailoring)."""
    with _conn() as conn:
        return tools.approve(conn, job_id)


@mcp.tool()
def tailor_job(job_id: str) -> dict:
    """Run the full tailoring loop for an approved job (slow: LLM + pdflatex). Returns the review summary."""
    store = _store()
    with _conn() as conn:
        return tools.tailor(conn, job_id, store=store, complete=llm_complete, compile_fn=compile_pdf)


@mcp.tool()
def get_review(job_id: str) -> dict:
    """Fetch a tailored job's review.json + diff.txt from storage."""
    return tools.get_review(_store(), job_id)


@mcp.tool()
def set_status(job_id: str, status: str) -> dict:
    """Move a job through the funnel (new/routed/approved_for_tailoring/tailored/reviewed/applied/rejected)."""
    with _conn() as conn:
        return tools.set_status(conn, job_id, status)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
```

`src/jobmaxxing/mcp/__main__.py`:
```python
from .server import main

main()
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_mcp_server.py -v
```
Expected: 3 passed.

Also confirm the entrypoint resolves (it will start the stdio server and block, so send EOF immediately):
```bash
echo "" | DATABASE_URL= S3_BUCKET= uv run python -m jobmaxxing.mcp 2>&1 | tail -2 || true
```
Expected: it starts (no `ModuleNotFoundError`); it exits/EOFs without a traceback about a missing module. (A FastMCP stdio server reads MCP protocol on stdin; an empty line ends it. The point of this check is that the module RESOLVES and the server boots.)

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/mcp/server.py src/jobmaxxing/mcp/__main__.py tests/test_mcp_server.py
git commit -m "feat: add FastMCP server wiring the 7 tools over stdio"
```

---

### Task 9: `.mcp.json` registration + README

**Files:**
- Create: `.mcp.json`
- Modify: `.env.example`, `README.md`

- [ ] **Step 1: Create `.mcp.json`** at the repo root:

```json
{
  "mcpServers": {
    "jobmaxxing": {
      "command": "uv",
      "args": ["run", "python", "-m", "jobmaxxing.mcp"]
    }
  }
}
```

- [ ] **Step 2: Validate it parses**

Run:
```bash
uv run python -c "import json; assert 'jobmaxxing' in json.load(open('.mcp.json'))['mcpServers']; print('.mcp.json ok')"
```
Expected: `.mcp.json ok`.

- [ ] **Step 3: Update `.env.example`** — confirm it already has `DATABASE_URL`, the LLM keys, and `S3_BUCKET` (added in Phase 3). No new vars are required for the MCP server (it reuses them). Append a clarifying comment after the `S3_BUCKET` block:

```
# The MCP server (python -m jobmaxxing.mcp) reads all of the above from the environment / .env.
```

- [ ] **Step 4: Add a section to `README.md`** (insert BEFORE "## Status & open items"):

```markdown
## Conversational interface (MCP)

Drive the whole pipeline from Claude Code via an MCP server — no dashboard.

- Register it: the repo ships `.mcp.json` (runs `uv run python -m jobmaxxing.mcp`); point Claude
  Code at this project so it launches the server. It reads `DATABASE_URL`, `S3_BUCKET`, `AWS_*`,
  and the LLM keys from the environment / `.env`. The `tailor_job` tool needs `pdflatex` locally.
- Tools: `query_jobs` (filter by status/type/company/recency), `preview_route` (stored route, or
  `rerun` to preview live), `set_route` (manual override), `approve` (gate for tailoring),
  `tailor_job` (run the loop — slow, ~1–2 min), `get_review` (fetch review.json + diff), and
  `set_status` (move through the funnel incl. `applied`/`rejected`).
- Typical flow in chat: `query_jobs(status="routed")` → `approve(<id>)` → `tailor_job(<id>)` →
  `get_review(<id>)` → review the diff → `set_status(<id>, "applied")`.
- Funnel at a glance (Supabase SQL editor): `select * from funnel_counts;` and
  `select * from review_queue;`.
```

- [ ] **Step 5: Cross-check + commit**

Confirm the referenced names match the code: the 7 tool names match `server.py`; `funnel_counts`/`review_queue` match `0003_funnel_views.sql`; `python -m jobmaxxing.mcp` resolves (Task 8). Then:
```bash
export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q   # full suite stays green
git add .mcp.json .env.example README.md
git commit -m "docs: register MCP server and document the conversational interface"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- §2 architecture (tools.py/server.py split, per-call conn, namespacing) → Tasks 4–8. §3 the 7 tools → Tasks 4 (query_jobs), 5 (preview_route, set_route), 6 (approve, set_status), 7 (tailor, get_review), wired in Task 8. §4 storage extension (get_artifact + ArtifactMissing) → Task 2. §5 funnel views → Task 3. §6 .mcp.json + env → Task 9. §7 data model (no schema change; views) → Task 3 (no migration to columns). §8 testing → every task is TDD; server smoke test in Task 8. §9 deliverables → all covered. §1 dep `mcp` → Task 1.

**Type/signature consistency:** `query_jobs`, `preview_route`, `set_route`, `approve`, `tailor`, `get_review`, `set_status`, `_json_safe`, `VALID_STATUSES` (tools.py) used consistently; server.py wraps with matching params; `get_artifact`/`ArtifactMissing` (Task 2) consumed by `get_review` (Task 7); `tailor` injects `rubric_loader=load_rubric`; wrapped existing fns verified against the codebase (`set_manual(conn, job_id, resume_type)`, `route_one(title, description, config, *, llm_complete, budget)`, `Budget`, `tailor_job(conn, job_id, *, store, complete, compile_fn, rubric_loader)`, `tailoring.approve(conn, job_id)`, `complete(task, messages, *, max_tokens, ...)`, `load_settings().database_url`, `apply_migrations`).

**No placeholders:** every step has real code/SQL/JSON; the funnel views and `.mcp.json` ship concrete content; the server smoke test avoids version-fragile FastMCP introspection by asserting the wrapper functions are importable (the `@mcp.tool()` decorator returns the function unchanged).
