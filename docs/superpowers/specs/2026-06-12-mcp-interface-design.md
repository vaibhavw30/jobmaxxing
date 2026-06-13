# Spec — Phase 4: MCP Server + Review Surface

**Sprint:** Phase 4 of the Internship Recruiting Pipeline (`docs/PRD.md` §6.5/§10.4, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md` §9)
**Author:** Vaibhav
**Date:** 2026-06-12
**Status:** Approved for planning
**Builds on:** Phases 1–3 (all merged). Wraps the existing functions; no new pipeline logic.

---

## 1. Goal & rationale

Let the operator drive the entire pipeline **conversationally from Claude Code** (or any MCP chat surface) — query the feed, sanity-check routing, approve a job, run tailoring, read the review, and move a job through the funnel — without a dashboard. The PRD's interface requirement (§6.5) and the conversational-first design (tech plan §9, "no dashboard to build first") are satisfied by an MCP server that exposes the pipeline as tools, plus a couple of read-only SQL views for an at-a-glance funnel in the Supabase console.

Everything the tools need already exists as **injectable Python functions** from Phases 1–3 (`routing.route_new`/`route_one`/`set_manual`, `tailoring.approve`/`tailor_job`, `tailoring.storage`, the LLM wrapper). Phase 4 is a thin, well-tested wrapper plus the connection/credential wiring and one small storage extension.

### Scope
**In:** an MCP server (FastMCP over stdio) exposing 7 tools; a `get_artifact` extension to the storage interface (so reviews can be read back); a migration adding read-only funnel SQL views; `.mcp.json` registration; docs.
**Out (deferred):** any web/kanban UI (tech plan §9.2 — "don't build a UI until the pipeline works"); auth (local single-user stdio); JobSpy/Gmail (Phase 5); form-fill (Phase 6). No schema changes — the migration only adds views.

## 2. Architecture

Two layers, mirroring the codebase's injectable-boundary pattern (so the logic is unit-testable and the transport is a thin shell):

```
Claude Code ──stdio──▶  jobmaxxing/mcp/server.py   (FastMCP @mcp.tool wrappers)
                                │  opens per-call DB conn; injects S3Store / llm.complete / compile_pdf
                                ▼
                         jobmaxxing/mcp/tools.py    (plain functions: the logic, boundaries injected)
                                │
                                ▼
        existing: routing/* , tailoring/* , llm/* , Postgres jobs table , S3
```

- **`src/jobmaxxing/mcp/tools.py`** — plain functions holding all logic. Each takes a live `conn` (and injected `store`/`complete`/`compile_fn` where needed) so it is testable against `pytest-postgresql` + `InMemoryStore` + mocked LLM/compile, exactly like `tailor_job`. No FastMCP, no global state, no I/O beyond the injected boundaries.
- **`src/jobmaxxing/mcp/server.py`** — the FastMCP wrapper. Creates `mcp = FastMCP("jobmaxxing")`, defines one `@mcp.tool()` per tool that opens a **per-call DB connection** (`psycopg.connect(load_settings().database_url)`), injects the real `S3Store`/`llm.complete`/`compile_pdf`, calls the matching `tools.py` function, and returns the (JSON-serializable) result. `main()` calls `mcp.run()` (stdio). This is the untested transport/wiring layer, analogous to `run.py`'s `main`.
- **`src/jobmaxxing/mcp/__main__.py`** — `from .server import main; main()`, so `python -m jobmaxxing.mcp` launches the server. (A package with `__main__.py` is the entrypoint — no separate top-level shim needed.)

**Namespacing note (non-obvious, must hold):** our package is `jobmaxxing.mcp`; the SDK is the top-level `mcp`. Inside `jobmaxxing/mcp/server.py`, `from mcp.server.fastmcp import FastMCP` resolves to the **installed SDK** (absolute imports look up top-level names; `jobmaxxing.mcp` is never registered as bare `mcp`). No collision. New dependency: `mcp` (the official Python SDK; FastMCP lives at `mcp.server.fastmcp`).

**Per-call connections:** the server is long-lived (Claude Code keeps the subprocess alive across a session). Opening a fresh `psycopg` connection per tool call (via a `with psycopg.connect(...) as conn:` context manager) avoids stale/broken-connection bugs and keeps each tool atomic. At single-user, one-call-at-a-time volume this is free.

**Credentials:** `config.load_settings()` already calls `load_dotenv()`, so the server picks up `DATABASE_URL`, `S3_BUCKET`, `AWS_*`, and the LLM keys from `.env`/the environment — same as the CLIs. No secrets in `.mcp.json`.

## 3. The 7 tools

Signatures below are the **`tools.py`** logic functions (the `server.py` `@mcp.tool()` wrappers have the same parameters minus the injected `conn`/`store`/`complete`/`compile_fn`). All returns are JSON-serializable (uuid/datetime rendered as strings via a `_json_safe` helper).

### 3.1 `query_jobs`
```python
def query_jobs(conn, *, status=None, resume_type=None, company=None,
               since_days=None, limit=50) -> list[dict]:
    """Filtered, capped view of the feed (newest first)."""
    clauses, params = [], []
    if status:       clauses.append("status = %s");        params.append(status)
    if resume_type:  clauses.append("resume_type = %s");   params.append(resume_type)
    if company:      clauses.append("company ilike %s");   params.append(f"%{company}%")
    if since_days is not None:
        clauses.append("scraped_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(days=since_days))
    where = (" where " + " and ".join(clauses)) if clauses else ""
    capped = max(1, min(int(limit), 200))  # hard cap protects the chat context
    rows = conn.execute(
        f"select id, company, title, status, resume_type, route_confidence, url, posted_at "
        f"from jobs{where} order by scraped_at desc limit %s",
        (*params, capped),
    ).fetchall()
    cols = ["id", "company", "title", "status", "resume_type", "route_confidence", "url", "posted_at"]
    return [{c: _json_safe(v) for c, v in zip(cols, r)} for r in rows]
```
- `_json_safe(v)`: `str(v)` for `uuid.UUID`/`datetime`, else `v`.
- Default `limit=50`, hard cap 200, so a broad query never dumps thousands of rows into the model's context. Documented in the tool docstring (the MCP tool description the model sees).

### 3.2 `preview_route`
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
        out["rerun"] = {"resume_type": decision.resume_type, "method": decision.method,
                        "confidence": decision.confidence}
    return out
```
- Reuses Phase-2 `route_one` with a one-call `Budget`. The rerun result is **not persisted** — it's a what-if for comparison after dictionary tuning.

### 3.3 `set_route`
```python
def set_route(conn, job_id, resume_type) -> dict:
    """Manual routing override (sets route_method='manual'; never re-routed automatically)."""
    set_manual(conn, job_id, resume_type)   # routing.route.set_manual (validates type, rowcount)
    return {"job_id": str(job_id), "resume_type": resume_type, "route_method": "manual"}
```
- Wraps `routing.route.set_manual` (validates `resume_type ∈ VALID_TYPES`, raises on unknown id). The override the tech plan folded into `preview_route` is an explicit, single-purpose tool here.

### 3.4 `approve`
```python
def approve(conn, job_id) -> dict:
    """Gate a job for tailoring (status -> approved_for_tailoring)."""
    tailoring_approve(conn, job_id)          # tailoring.tailor.approve
    return {"job_id": str(job_id), "status": "approved_for_tailoring"}
```

### 3.5 `tailor` (the slow one — synchronous)
```python
def tailor(conn, job_id, *, store, complete, compile_fn) -> dict:
    """Run the Phase-3 tailoring loop for an approved job; return the review summary."""
    return tailor_job(conn, job_id, store=store, complete=complete, compile_fn=compile_fn)
```
- The `server.py` wrapper injects the real `S3Store`, `llm.complete`, `compile_pdf`. Runs synchronously (several LLM calls + a `pdflatex` compile, ~30s–2min); the tool returns the review dict (scores, delta, weaknesses, missing_keywords, page_count, fit) when finished. Requires `pdflatex` + S3 + LLM keys on the machine running the server. Raises surface cleanly: `BaseResumeMissing`, `LatexError`, `RubricMissing`, or the `ValueError` if the job isn't `approved_for_tailoring`.

### 3.6 `get_review`
```python
def get_review(store, job_id) -> dict:
    """Fetch review.json + diff.txt from storage and return both inline."""
    review = json.loads(store.get_artifact(job_id, "review.json").decode("utf-8"))
    diff = store.get_artifact(job_id, "diff.txt").decode("utf-8")
    return {"review": review, "diff": diff}
```
- Requires the **storage extension** (§4): a `get_artifact(job_id, name) -> bytes` on the `ArtifactStore` interface + both implementations, raising `ArtifactMissing` when absent. The diff is one-page-bounded so returning it inline is safe.

### 3.7 `set_status`
```python
VALID_STATUSES = {"new", "routed", "approved_for_tailoring", "tailored",
                  "reviewed", "applied", "rejected"}

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
- Validates against the known funnel set (rejects typos), allows any legal status (operator override). This is how `tailored → applied|rejected` happens — the human gate stays a manual action.

## 4. Storage extension (`tailoring/storage.py`)

`get_review` needs to read artifacts back; the Phase-3 interface only writes them. Add:
```python
class ArtifactMissing(RuntimeError):
    """Raised when a requested artifact does not exist."""

# ArtifactStore Protocol gains:
def get_artifact(self, job_id, name: str) -> bytes: ...

# InMemoryStore:
def get_artifact(self, job_id, name: str) -> bytes:
    key = (str(job_id), name)
    if key not in self.artifacts:
        raise ArtifactMissing(f"no artifact {name!r} for job {job_id}")
    return self.artifacts[key]

# S3Store:
def get_artifact(self, job_id, name: str) -> bytes:
    key = f"tailored/{str(job_id)}/{name}"
    try:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise ArtifactMissing(f"no artifact at s3://{self.bucket}/{key}") from exc
        raise
```
(Same NoSuchKey-only narrowing as `get_base_resume`.)

## 5. SQL funnel views (`migrations/0003_funnel_views.sql`)

Read-only, glanceable in the Supabase console; applied by the existing idempotent migrate runner (`apply_migrations` runs every `migrations/*.sql` in order, `create or replace view` so re-apply is safe):
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

## 6. Running it (`.mcp.json`, env)

Commit `.mcp.json` at the repo root (the repo is public; it carries no secrets):
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
Claude Code launches the server as a stdio subprocess with the project as cwd. The server reads `DATABASE_URL`, `S3_BUCKET`, `AWS_*`, and the LLM keys from the environment / `.env` (loaded by `python-dotenv` in `config.py`). The operator then drives the funnel in chat: `query_jobs(status="routed")` → `approve(<id>)` → `tailor_job(<id>)` → `get_review(<id>)` → `set_status(<id>, "applied")`.

## 7. Data model

No schema change. Tools read/write only existing columns; `0003_funnel_views.sql` adds two views. The funnel state machine `new → routed → approved_for_tailoring → tailored → reviewed → applied|rejected` is honored, with `set_status` enforcing the valid set.

## 8. Testing

- **`tools.py`** (against `pytest-postgresql` + `InMemoryStore` + mocked LLM/compile — reuse the Phase-3 `tailor_job` harness):
  - `query_jobs`: filters (status/type/company ilike/since_days), newest-first ordering, the limit cap, JSON-safe rendering of uuid/datetime.
  - `preview_route`: stored-only; `rerun=True` returns a live decision (mocked `llm_complete`) without persisting; missing id raises.
  - `set_route`: sets `route_method='manual'` + the type; invalid type / missing id raise (via `set_manual`).
  - `approve`: status → `approved_for_tailoring`.
  - `tailor`: end-to-end via injected `InMemoryStore` + mocked complete/compile → review returned, artifacts written, status `tailored`; refuses unapproved.
  - `get_review`: round-trips `review.json` + `diff.txt` from `InMemoryStore`; `ArtifactMissing` when absent.
  - `set_status`: valid transition updates; invalid status raises; missing id raises.
- **Storage extension:** `get_artifact` round-trip (InMemoryStore) + `ArtifactMissing`; `S3Store.get_artifact` with the fake boto3 client (key hit + NoSuchKey→ArtifactMissing + other ClientError propagates).
- **`server.py`:** one smoke test — import `jobmaxxing.mcp.server`, assert the `FastMCP` instance exists and the 7 tools are registered (e.g. via the SDK's tool listing), and `main` is callable. The stdio transport itself is the framework's concern, not unit-tested.
- **Migration:** extend the migrate test to assert `funnel_counts` and `review_queue` are queryable after `apply_migrations`.

## 9. Deliverables

- `src/jobmaxxing/mcp/` — `tools.py` (the 7 logic functions + `_json_safe` + `VALID_STATUSES`), `server.py` (FastMCP wrappers + `main`), `__main__.py`, `__init__.py`.
- `tailoring/storage.py` — `ArtifactMissing` + `get_artifact` on the Protocol, `InMemoryStore`, `S3Store`.
- `migrations/0003_funnel_views.sql`.
- `.mcp.json` at repo root.
- New dep: `mcp`.
- Tests for every `tools.py` function + the storage extension + the migration views + the server smoke test.
- README "Conversational interface (MCP)" section: register the server in Claude Code, the env it needs, the tool list, and the typical funnel walkthrough.

## 10. Open items (resolve during implementation, not blocking)

- Exact `mcp` SDK version pin and the precise FastMCP tool-registration introspection API used by the server smoke test (verify against the installed package).
- Whether `query_jobs` should also expose an `order_by` / offset for pagination — start without (newest-first + limit is enough for a single operator).
- Whether `get_review` should also return the `tailored.pdf` bytes (base64) or just the review+diff — start with review+diff (the operator opens the PDF from S3); revisit if useful.
- Final default/cap for `query_jobs` limit (start 50 / cap 200).
