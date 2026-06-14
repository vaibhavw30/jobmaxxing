# Batched DB Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Batch the per-row DB commits in `routing.route_new` and `store.upsert_jobs` so a run over a remote pooled connection issues a handful of round-trips instead of thousands.

**Architecture:** `route_new` collects all routing decisions in Python (logic isolation unchanged) then writes them with a single pipelined `executemany` in one transaction. `upsert_jobs` partitions a batch into new-vs-existing (one existence query), bulk-inserts the new rows and bulk-updates the merged existing rows (reusing `merge_records`), all in one transaction. Behavior-preserving — all existing idempotency/merge/fail-soft invariants hold.

**Tech Stack:** Python 3.12, psycopg3 (executemany pipelining), pytest + pytest-postgresql. Spec: `docs/superpowers/specs/2026-06-14-batch-writes-design.md`.

**Conventions (match the codebase):**
- Work in the isolated worktree off `main`; ENV `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before `uv run pytest` (pytest-postgresql needs the local Postgres binary).
- **These are refactors of correct code.** The new tests *characterize* the batch behavior — they PASS against the current per-row implementation too. The discipline is: add the characterization tests (green now), rewrite, confirm everything still green. The existing `tests/test_store.py` + `tests/test_route_db.py` are the primary safety net and MUST stay green.

---

### Task 1: Batch `route_new` writes

**Files:**
- Modify: `src/jobmaxxing/routing/route.py` (the `route_new` function only)
- Test: `tests/test_route_db.py` (add one batch test)

**Step 1: Add the characterization test** — append to `tests/test_route_db.py` (the `conn` fixture, `_insert`, `route_new`, `CONFIG` already exist at the top of that file):

```python
def test_route_new_batches_mixed_rows(conn):
    # clear-title -> rules; title-only-ambiguous -> deferred; manual -> skipped
    _insert(conn, title="Software Engineer Intern", description="api work", dedupe_key="b|swe")
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None, dedupe_key="b|titleonly")
    _insert(conn, title="Quant Intern", description="api", dedupe_key="b|manual",
            resume_type="quant-trader", route_method="manual")

    counts = route_new(conn, config=CONFIG, llm_complete=lambda *a, **k: "{}")
    assert counts["rules"] == 1
    assert counts["deferred"] == 1
    assert counts["manual_skipped"] == 1
    # the routable row is written; the deferred row stays new; the manual row untouched
    rows = dict(conn.execute("select dedupe_key, status from jobs").fetchall())
    assert rows["b|swe"] == "routed"
    assert rows["b|titleonly"] == "new"
    by_method = dict(conn.execute("select dedupe_key, route_method from jobs").fetchall())
    assert by_method["b|swe"] == "rules"
    assert by_method["b|manual"] == "manual"
```

**Step 2: Run it (passes against current per-row code)**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py::test_route_new_batches_mixed_rows -v`
Expected: PASS (the current implementation is already correct; this pins the behavior the refactor must preserve).

**Step 3: Rewrite `route_new` to batch the writes.** Replace the body of `route_new` in `src/jobmaxxing/routing/route.py` (the function currently does a per-row `with conn.transaction(): conn.execute(UPDATE...)`). The new body collects updates and issues one `executemany`:

```python
def route_new(conn: psycopg.Connection, *, config=None, llm_complete=None, max_llm_calls=None, reroute=False) -> dict:
    """Route unrouted, non-manual rows. With reroute=True, re-route all non-manual rows.
    Returns counts {rules, llm, deferred, manual_skipped}.

    Decisions are computed per row (so one bad row never aborts the run); the resulting
    UPDATEs are batched into a single pipelined executemany in one transaction, which
    collapses thousands of remote round-trips into one commit.
    """
    cfg = config if config is not None else load_routing_config()
    do_llm = llm_complete if llm_complete is not None else llm_complete_default
    cap = max_llm_calls if max_llm_calls is not None else cfg.get("thresholds", {}).get("max_llm_calls_per_run", 200)
    budget = Budget(remaining=cap)

    if reroute:
        where = "route_method is distinct from 'manual'"
    else:
        where = "resume_type is null and route_method is distinct from 'manual'"

    rows = conn.execute(f"select id, title, description from jobs where {where}").fetchall()
    counts = {"rules": 0, "llm": 0, "deferred": 0, "manual_skipped": 0}
    updates: list[tuple] = []

    for job_id, title, description in rows:
        try:
            decision = route_one(title, description, cfg, llm_complete=do_llm, budget=budget)
        except Exception as exc:  # noqa: BLE001 - one bad row never aborts the run
            logger.warning("route: job %s failed: %s", job_id, exc)
            counts["deferred"] += 1
            continue
        if decision.method is None:
            counts["deferred"] += 1
            continue
        updates.append((decision.resume_type, decision.method, decision.confidence, job_id))
        counts[decision.method] += 1

    if updates:
        with conn.transaction():
            conn.cursor().executemany(
                "update jobs set resume_type=%s, route_method=%s, route_confidence=%s, status='routed' where id=%s",
                updates,
            )
    counts["manual_skipped"] = conn.execute(
        "select count(*) from jobs where route_method = 'manual'"
    ).fetchone()[0]
    logger.info("route summary: %s (budget left=%d)", counts, budget.remaining)
    return counts
```

**Step 4: Run the route tests (new + existing all green)**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py -v`
Expected: all pass (the existing single-row tests + the new batch test). Then run the full suite: `uv run pytest -q` — all green.

**Step 5: Commit**

```bash
git add src/jobmaxxing/routing/route.py tests/test_route_db.py
git commit -m "perf: batch route_new writes into a single executemany"
```

---

### Task 2: Batch `store.upsert_jobs` writes

**Files:**
- Modify: `src/jobmaxxing/store.py` (the `upsert_jobs` function + add a `_update_values` helper)
- Test: `tests/test_store.py` (add batch tests)

**Step 1: Add the characterization tests** — append to `tests/test_store.py` (the `conn` fixture, `apply_migrations`, `_rec`, `upsert_jobs`, `pytest` already exist at the top of that file):

```python
def test_upsert_inserts_a_batch_of_new_rows(conn):
    apply_migrations(conn)
    recs = [_rec(dedupe_key=f"acme|swe {i}", title=f"SWE Intern {i}") for i in range(5)]
    counts = upsert_jobs(conn, recs)
    assert counts == {"inserted": 5, "merged": 0}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 5


def test_upsert_mixed_new_and_existing_batch(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(dedupe_key="acme|swe intern", description=None)])
    # batch: one existing key (enriched) + one brand-new key
    counts = upsert_jobs(conn, [
        _rec(dedupe_key="acme|swe intern", source="greenhouse",
             url="https://boards.greenhouse.io/acme/jobs/1", description="full JD"),
        _rec(dedupe_key="acme|ml intern", title="ML Intern"),
    ])
    assert counts == {"inserted": 1, "merged": 1}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 2
    desc = conn.execute("select description from jobs where dedupe_key='acme|swe intern'").fetchone()[0]
    assert desc == "full JD"                      # existing row enriched


def test_upsert_collapses_intra_batch_duplicate_keys(conn):
    apply_migrations(conn)
    # two records with the SAME dedupe_key in ONE batch collapse to one row
    counts = upsert_jobs(conn, [
        _rec(dedupe_key="acme|swe intern", description=None),
        _rec(dedupe_key="acme|swe intern", source="greenhouse",
             url="https://boards.greenhouse.io/acme/jobs/1", description="full JD"),
    ])
    assert counts["inserted"] == 1
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
    desc = conn.execute("select description from jobs").fetchone()[0]
    assert desc == "full JD"                      # the two folded together


def test_upsert_batch_is_idempotent(conn):
    apply_migrations(conn)
    recs = [_rec(dedupe_key=f"acme|swe {i}", title=f"SWE Intern {i}") for i in range(3)]
    upsert_jobs(conn, recs)
    counts = upsert_jobs(conn, recs)
    assert counts == {"inserted": 0, "merged": 3}     # all already exist -> merged, no new rows
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 3
```

**Step 2: Run them.** Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_store.py -v`. The new tests should PASS against the current per-row implementation (it is already correct). This pins the batch behavior the refactor must preserve. (If `test_upsert_collapses_intra_batch_duplicate_keys` does NOT pass against the current code — the current per-row path handles intra-batch dups by merging the second into the first via the conflict path — confirm it does; if not, report it, do not silently change the test.)

**Step 3: Add the `_update_values` helper** to `src/jobmaxxing/store.py` (next to `_record_values`, so the merge-UPDATE value tuple is DRY and matches `_UPDATE_SQL`'s 9 placeholders):

```python
def _update_values(rec: JobRecord) -> tuple:
    """Values for _UPDATE_SQL (the enrichable columns + the dedupe_key WHERE)."""
    return (
        rec.source,
        rec.external_id,
        rec.location,
        rec.url,
        rec.alt_urls,
        rec.description,
        rec.posted_at,
        rec.is_active,
        rec.dedupe_key,
    )
```

**Step 4: Rewrite `upsert_jobs`** in `src/jobmaxxing/store.py` to partition + bulk insert + bulk merge-update (replace the per-row loop body; keep the up-front validation exactly as-is):

```python
def upsert_jobs(conn: psycopg.Connection, records: list[JobRecord]) -> dict[str, int]:
    """Insert new rows; on dedupe_key conflict, merge and update. Batched.

    Validates the whole batch up front (empty dedupe_key raises before any write). Then,
    in one transaction: bulk-INSERT the rows whose dedupe_key does not yet exist (ON CONFLICT
    DO NOTHING as a race safety net) and bulk-UPDATE the existing ones with their merge.
    `executemany` pipelines the statements, so a 6k-row batch is a handful of round-trips,
    not thousands. Intra-batch duplicate dedupe_keys are folded with merge_records first.
    """
    for rec in records:
        if not rec.dedupe_key:
            raise ValueError(
                f"refusing to upsert record with empty dedupe_key: "
                f"{rec.company!r} / {rec.title!r} ({rec.source})"
            )
    if not records:
        return {"inserted": 0, "merged": 0}

    # Fold intra-batch duplicates so each dedupe_key appears once (later records enrich earlier).
    folded: dict[str, JobRecord] = {}
    for rec in records:
        folded[rec.dedupe_key] = (
            merge_records(folded[rec.dedupe_key], rec) if rec.dedupe_key in folded else rec
        )

    keys = list(folded)
    cur = conn.cursor(row_factory=dict_row)
    existing = {
        row["dedupe_key"]: row
        for row in cur.execute("select * from jobs where dedupe_key = any(%s)", (keys,)).fetchall()
    }

    to_insert = [_record_values(rec) for key, rec in folded.items() if key not in existing]
    to_update = [
        _update_values(merge_records(_row_to_record(existing[key]), rec))
        for key, rec in folded.items()
        if key in existing
    ]

    with conn.transaction():
        if to_insert:
            cur.executemany(_INSERT_SQL, to_insert)
        if to_update:
            cur.executemany(_UPDATE_SQL, to_update)

    return {"inserted": len(to_insert), "merged": len(to_update)}
```

**Step 5: Run the store tests (new + existing all green)**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_store.py -v`
Expected: ALL pass — the existing tests (`test_upsert_inserts_new_row`, `test_upsert_merges_duplicate_and_enriches`, `test_upsert_is_idempotent`, `test_upsert_rejects_empty_dedupe_key`, `test_upsert_validates_batch_before_writing`, `test_upsert_refreshes_scraped_at_on_merge`) AND the 4 new batch tests. Then the full suite: `uv run pytest -q` — all green (the pipeline/run tests that call upsert_jobs must stay green too).

**Step 6: Commit**

```bash
git add src/jobmaxxing/store.py tests/test_store.py
git commit -m "perf: batch upsert_jobs into bulk insert + bulk merge-update"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** §2.1 route_new batching → Task 1. §2.2 upsert_jobs partition+bulk → Task 2 (incl. the `_update_values` DRY helper). §3 invariants → preserved by keeping validation up front, the WHERE clauses, `merge_records`, the `_UPDATE_SQL` columns, and per-row decision isolation; verified by the existing tests staying green (Tasks 1 & 2 step "run full suite"). §5 testing → the new characterization tests (batch new, mixed, intra-batch dup, batch idempotent for store; mixed batch for routing) plus all existing tests.

**Type/signature consistency:** `route_new(conn, *, config, llm_complete, max_llm_calls, reroute)` and `upsert_jobs(conn, records) -> dict[str,int]` signatures unchanged (callers untouched). `_record_values`, `_row_to_record`, `_update_values`, `_INSERT_SQL`, `_UPDATE_SQL`, `merge_records`, `route_one`, `Budget` referenced consistently with their existing definitions.

**No placeholders:** every step shows real code/tests/commands. The "characterization (passes against current code)" framing is explicit because these are behavior-preserving refactors, not fail-first features.
