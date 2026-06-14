# Spec — Batched DB Writes (ingestion + routing)

**Type:** Performance optimization (behavior-preserving)
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** Phases 1–4 (all merged). Touches `store.upsert_jobs` and `routing.route.route_new` only.

---

## 1. Problem & rationale

The first live poller run ingested the Simplify list's **6,389 current postings**, then had to route all of them. Both hot paths write **one row per transaction**, so each row is a separate commit + round-trip. Locally that's sub-millisecond; from a GitHub Actions runner to Supabase's session pooler it's ~50–150 ms per round-trip, so 6,389 rows × that = a multi-minute run.

This is not a one-time cost: every scheduled poll re-fetches the full list, so every run re-sees ~6,389 rows as **conflicts → merges** (ingestion) and routing re-scans the unrouted set. Batching the DB writes turns a multi-minute run into seconds.

**Goal:** batch the per-row writes in `upsert_jobs` and `route_new` while preserving every existing correctness invariant. No change to the funnel, the schema, or any caller's contract.

### Scope
**In:** rewrite the write path of `store.upsert_jobs` and `routing.route.route_new` to batch DB statements; add batch-specific tests. **Out:** no schema change; no change to the merge/route *logic* (reuse `merge_records`, `route_one`); no change to `tailor_job` (per-job, not a batch). Single-user assumption stands (one run at a time).

## 2. The two batched designs

### 2.1 `route_new` (routing) — collect-then-write

Decisions are already computed in Python (`route_one` is pure and isolates LLM errors internally). Collect every update, then write them in **one transaction with a single pipelined `executemany`** (psycopg3 ≥3.1 pipelines `executemany`, so the parameter sets share round-trips):

```python
def route_new(conn, *, config=None, llm_complete=None, max_llm_calls=None, reroute=False) -> dict:
    cfg = config if config is not None else load_routing_config()
    do_llm = llm_complete if llm_complete is not None else llm_complete_default
    cap = max_llm_calls if max_llm_calls is not None else cfg.get("thresholds", {}).get("max_llm_calls_per_run", 200)
    budget = Budget(remaining=cap)
    where = ("route_method is distinct from 'manual'" if reroute
             else "resume_type is null and route_method is distinct from 'manual'")
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
    counts["manual_skipped"] = conn.execute("select count(*) from jobs where route_method = 'manual'").fetchone()[0]
    logger.info("route summary: %s (budget left=%d)", counts, budget.remaining)
    return counts
```

6,389 commits → **1**; the UPDATEs are pipelined. Per-row *logic* isolation is unchanged (the `try/except` still skips a bad row to `deferred` before it ever reaches the batch).

### 2.2 `upsert_jobs` (ingestion) — partition + bulk insert + bulk merge-update

Per batch, in one transaction: find which keys already exist, bulk-insert the new ones, fetch + merge the existing ones in Python, bulk-update them. ~4 round-trips for the whole batch instead of ~2 per row.

```python
def upsert_jobs(conn, records) -> dict[str, int]:
    for rec in records:                       # validate up front: empty dedupe_key would collapse rows
        if not rec.dedupe_key:
            raise ValueError(f"refusing to upsert record with empty dedupe_key: "
                             f"{rec.company!r} / {rec.title!r} ({rec.source})")
    if not records:
        return {"inserted": 0, "merged": 0}

    # 1. Collapse intra-batch duplicates (same dedupe_key seen twice in one source) via merge_records.
    folded: dict[str, JobRecord] = {}
    for rec in records:
        folded[rec.dedupe_key] = merge_records(folded[rec.dedupe_key], rec) if rec.dedupe_key in folded else rec

    keys = list(folded)
    cur = conn.cursor(row_factory=dict_row)
    existing = {r["dedupe_key"]: r for r in
                cur.execute("select * from jobs where dedupe_key = any(%s)", (keys,)).fetchall()}

    to_insert = [_record_values(r) for k, r in folded.items() if k not in existing]
    to_update = [
        _update_values(merge_records(_row_to_record(existing[k]), r))
        for k, r in folded.items() if k in existing
    ]

    with conn.transaction():
        if to_insert:
            cur.executemany(_INSERT_SQL, to_insert)          # ON CONFLICT DO NOTHING (race safety net)
        if to_update:
            cur.executemany(_UPDATE_SQL, to_update)
    return {"inserted": len(to_insert), "merged": len(to_update)}
```

- `_INSERT_SQL` / `_UPDATE_SQL` are the existing statement constants (insert keeps `ON CONFLICT (dedupe_key) DO NOTHING`; update keeps `scraped_at = now()` and updates by `dedupe_key`).
- `_update_values(merged)` returns the tuple matching `_UPDATE_SQL`'s placeholders (source, external_id, location, url, alt_urls, description, posted_at, is_active, dedupe_key) — extract the existing inline tuple into a helper so insert/update value-shaping is DRY and testable.
- On a **cold run** (empty table) there are zero conflicts → it's a single bulk insert. On **steady state** (table full) almost all keys exist → one SELECT + one bulk UPDATE. Both collapse thousands of round-trips into a handful.

## 3. Correctness invariants (preserved, with how)

| Invariant (existing test) | How it still holds |
| --- | --- |
| Empty `dedupe_key` rejected before any write | Up-front validation loop runs before fetch/insert (unchanged). |
| Batch validation is atomic (`test_upsert_validates_batch_before_writing`) | Validation precedes the single `with conn.transaction()`, so a bad record raises with **zero** rows written. |
| Insert new row → `{inserted:1, merged:0}` | Key not in `existing` → `to_insert`. |
| Conflict → merge-enrich (ATS url promotion, alt_urls, scraped_at refresh) | `merge_records(_row_to_record(existing), incoming)` + the same `_UPDATE_SQL` columns (logic unchanged). |
| Idempotent re-run → still one row, no dup | Second call: key in `existing` → merge (no-op enrich) → UPDATE, never a second insert (also guarded by `ON CONFLICT DO NOTHING`). |
| Routing: manual rows untouched | WHERE clause excludes `route_method='manual'` (unchanged). |
| Routing: deferred rows stay `new` | `decision.method is None` → counted `deferred`, never added to `updates`. |
| Routing idempotent | Only unrouted/non-manual rows selected; re-run finds none. |
| Counts `{inserted, merged}` / `{rules, llm, deferred, manual_skipped}` | Counted exactly as before (per decision / per partition). |

## 4. The tradeoff (accepted)

Per-row transaction isolation becomes **per-batch**: a genuine *DB-level* error mid-batch rolls back that batch rather than a single row. Accepted because:
- It is caught at the source/step level — `run_sources` already wraps each source's `ingest_records` in `try/except` and logs; routing failures are isolated at the workflow-step level — and both paths are **idempotent**, so a failed batch simply re-runs cleanly next cycle. No partial corruption.
- *Logic*-level isolation is unchanged: a malformed record (ingestion validates up front) or an LLM hiccup (`route_one` catches it → `deferred`) is still handled per-row in Python **before** the batched write.
- Race-safety relaxes from per-row `SELECT FOR UPDATE` to check-then-insert, but the operator is single-user / one-run-at-a-time, and `INSERT ... ON CONFLICT DO NOTHING` still prevents duplicate-key errors if two runs ever overlap.

## 5. Testing

- **All existing `tests/test_store.py` + `tests/test_route_db.py` stay green** — they encode every invariant above and are the primary safety net.
- **New batch tests** (`test_store.py`):
  - upsert a batch of N new records in one call → `inserted == N`, N rows present.
  - upsert a batch where some keys exist and some are new → correct `{inserted, merged}` split; existing rows enriched, new rows added.
  - intra-batch duplicate dedupe_keys (two records, same key, in one call) → collapse to one row (merged), `inserted == 1`.
  - re-upsert the same batch → `inserted == 0, merged == N`, row count unchanged (idempotent at batch scale).
- **New batch test** (`test_route_db.py`): seed several unrouted rows of mixed kinds (clear-title, title-only-ambiguous, manual) → one `route_new` call → all routable rows updated in the batch, manual untouched, deferred left `new`, counts correct.
- A light assertion that the batched path issues far fewer statements is **not** required (psycopg pipelining is an implementation detail); correctness + the existing idempotency/merge tests are what matter.

## 6. Deliverables

- Rewritten `store.upsert_jobs` (partition + bulk insert + bulk merge-update) with a `_update_values` helper extracted for DRY value-shaping.
- Rewritten `routing.route.route_new` (collect decisions → single `executemany`).
- New batch tests in `tests/test_store.py` and `tests/test_route_db.py`; all existing tests green.
- No migration, no config, no README change (behavior-preserving).

## 7. Open items (resolve during implementation, not blocking)

- Whether to chunk very large batches (e.g. >5,000) into sub-batches to bound a single transaction's size/lock duration — start with one transaction per call (6,389 is fine for Postgres); add chunking only if a transaction-size limit shows up.
- Confirm psycopg3's `executemany` pipelines on the installed version (3.2); if not, the correctness is identical and only the round-trip win is smaller — measure against the live cold start.
