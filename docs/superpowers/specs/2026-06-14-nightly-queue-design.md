# Spec — Nightly operator queue (Workday backup sub-project 3)

**Type:** New feature — Workday-backup sub-project 3 of 3 (see `2026-06-14-workday-backup-roadmap.md`)
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** sub-projects 1 (title triage → relevance) and 2 (find-elsewhere → `jd_source`/`recover_attempts`), and the Phase-4 MCP (`mcp/tools.py` + `mcp/server.py`) + funnel views (migration 0003). Honors the roadmap reset contract.

---

## 1. Problem & rationale

The headless Workday worker (Phase 5b) and find-elsewhere (sub-project 2) recover JDs automatically, but a residual set of **relevant** Workday internships still has no JD — and tailoring needs one. The fix is the human: a **real browser is not bot-blocked**, so the operator can simply open those jobs and read the JD. This sub-project gives the operator a nightly worklist (the jobs both automated paths gave up on) and a frictionless way to feed a JD back in — plus a spot-check surface for the `jd_source='recovered'` JDs from sub-project 2. It builds on the existing MCP so the operator does it conversationally ("show me tonight's queue", "here's the JD for job X").

### Scope
**In:** a `nightly_queue` SQL view (migration 0006); three additions to `mcp/tools.py` — `nightly_queue` (list the worklist), `set_description` (ingest an operator-provided JD), `reject_recovered` (discard a wrong recovered JD) — plus a `jd_source` filter on the existing `query_jobs`; registration in `mcp/server.py`; tests.
**Out:** browser-automation code (the operator may paste OR drive Claude-in-Chrome at runtime — the ingest is mechanism-agnostic); auto-tailoring (the human stays the gate); any new pipeline stage.

## 2. The `nightly_queue` view (migration 0006)

The worklist = relevant, still-JD-less jobs that **both** the headless worker and find-elsewhere exhausted (manual is the last resort):

```sql
create or replace view nightly_queue as
  select id, company, title, url, resume_type, route_confidence, scraped_at
  from jobs
  where coalesce(description, '') = ''            -- still no JD
    and resume_type is not null                   -- relevant (title-routed by sub-project 1; excludes not_target)
    and route_method is distinct from 'manual'
    and recover_attempts >= 2                      -- find-elsewhere also gave up (= recover_jd's default cap)
  order by scraped_at desc;
```

`recover_attempts >= 2` mirrors `recover_new`'s default `cap=2`; documented in the migration so the two stay in sync. Migrations run in filename order, so `recover_attempts` (migration 0005) exists before this view. `create or replace view` is idempotent (re-runnable by `apply_migrations`).

## 3. MCP tools (with code, mirroring `mcp/tools.py` style)

All are plain `(conn, …)` functions returning JSON-safe dicts/lists, writes wrapped in `conn.transaction()`, raising `ValueError` on bad input — consistent with the existing tools.

### 3.1 `nightly_queue` — the worklist

```python
_QUEUE_COLS = ["id", "company", "title", "url", "resume_type", "route_confidence", "scraped_at"]


def nightly_queue(conn, *, limit=50) -> list[dict]:
    """The operator's manual-capture worklist: relevant, still-JD-less jobs that both the headless
    worker and find-elsewhere gave up on (from the nightly_queue view). limit hard-capped at 200."""
    capped = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"select {', '.join(_QUEUE_COLS)} from nightly_queue limit %s", (capped,)
    ).fetchall()
    return [{c: _json_safe(v) for c, v in zip(_QUEUE_COLS, row)} for row in rows]
```

### 3.2 `set_description` — ingest an operator-provided JD

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

It does NOT route inline — it resets routing and lets the normal `route_new` (next CI poll, or a local `python -m jobmaxxing.route`) re-route with the JD. This keeps the tool simple and consistent with how sub-projects 1/2 hand jobs back to the router.

### 3.3 `reject_recovered` — discard a wrong recovered JD

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

### 3.4 `query_jobs` — add a `jd_source` filter

So the operator can list the recovered JDs to spot-check (`query_jobs(jd_source='recovered')`). Add the parameter and a clause:

```python
def query_jobs(conn, *, status=None, resume_type=None, company=None,
               jd_source=None, since_days=None, limit=50) -> list[dict]:
    ...
    if jd_source is not None:
        clauses.append("jd_source = %s")
        params.append(jd_source)
    ...
```

(Everything else in `query_jobs` is unchanged.)

## 4. Registration (`mcp/server.py`)

Register `nightly_queue`, `set_description`, and `reject_recovered` as MCP tools the same way the existing tools (`query_jobs`, `approve`, `set_status`, …) are wired — each opens a per-call DB connection and delegates to the `tools.py` function. The `query_jobs` MCP tool gains the optional `jd_source` argument. No other server change.

## 5. Operator flow (nightly, conversational via the MCP)

1. "Show me tonight's queue" → `nightly_queue` lists the relevant, JD-less stragglers (company, title, URL).
2. For each, the operator gets the JD from their **real, non-bot browser** — pastes it, or has Claude-in-Chrome open the URL and extract it — then `set_description(job_id, text)`.
3. "Show me the recovered JDs" → `query_jobs(jd_source='recovered')`; for any wrong one, `reject_recovered(job_id)` (it returns to the queue for manual capture).
4. The normal pipeline takes over: `route_new` re-routes the now-JD-bearing jobs (next poll / local run) → `approve` → `tailor` — the human stays the gate throughout.

## 6. Invariants & error handling

| Invariant | How |
| --- | --- |
| Manual is the last resort | `nightly_queue` requires `recover_attempts >= 2` (both automated paths exhausted). |
| Ingested JD re-routes confidently | `set_description` resets `resume_type`/`route_method` (reset contract) → `route_new` re-routes with the JD. |
| Can't wipe a good JD by mistake | `reject_recovered` is guarded to `jd_source='recovered'`; a non-recovered id → `ValueError`. |
| Rejected JD won't be auto-re-grabbed | `reject_recovered` caps `recover_attempts`, and keeps `resume_type` so it re-enters `nightly_queue`. |
| Bad input rejected | empty `set_description` text → `ValueError`; unknown id → `ValueError` (rowcount 0). |
| Human stays the gate | No auto-tailoring; the operator drives every step via the MCP. |
| Consistent with existing tools | Same `(conn, …) -> dict` shape, `_json_safe`, transactions, `ValueError`; no new dependency. |

## 7. Testing — pyramid (pytest-postgresql, the existing MCP test style)

- **`nightly_queue` view/tool:** seed (a) relevant + no-JD + `recover_attempts=2` → appears; (b) `recover_attempts=1` (not yet exhausted) → excluded; (c) has-description → excluded; (d) `resume_type` NULL (not_target / unrouted) → excluded; (e) `route_method='manual'` → excluded. Assert the tool returns only (a), JSON-safe, capped.
- **`set_description`:** writes `description`, `jd_source='manual'`, and resets `resume_type`/`route_method` to NULL; empty/whitespace text → `ValueError`; unknown id → `ValueError`.
- **`reject_recovered`:** a `jd_source='recovered'` row → `description` cleared, `recover_attempts` raised to the cap, `resume_type` kept, `jd_source` cleared, and it now satisfies the `nightly_queue` predicate; a non-recovered (`jd_source` NULL / 'manual') id → `ValueError` and no change.
- **`query_jobs` `jd_source` filter:** returns only the matching rows; `None` (default) unchanged behavior.
- All existing MCP tests stay green (only an additive `query_jobs` param + new tools).

## 8. Deliverables

- `migrations/0006_nightly_queue.sql` (the view).
- `mcp/tools.py`: `nightly_queue`, `set_description`, `reject_recovered`, `_QUEUE_COLS`, `_RECOVER_CAP`; `jd_source` param on `query_jobs`.
- `mcp/server.py`: register the three tools; add `jd_source` to the `query_jobs` MCP tool.
- Tests (extend `tests/test_mcp_routing_tools.py` or a new `tests/test_mcp_queue_tools.py`).
- README note: the nightly operator flow.
- No new pipeline stage, no new dependency, no CI change.

## 9. Open items & risks (named, accepted)

- The `recover_attempts >= 2` threshold is duplicated between the view and `recover_new`'s `cap`. Documented in both; if `cap` ever changes, update the view. (A config-driven threshold is possible but over-engineered for a single hardcoded default.)
- If the operator never runs the headless/recovery workers, `nightly_queue` stays empty (nothing is "exhausted" yet) — intended: manual is the last resort, not the first.
- `set_description` trusts the operator's text (no validation beyond non-empty). Acceptable — the operator is the source of truth, and a bad paste is caught at review/tailoring.
