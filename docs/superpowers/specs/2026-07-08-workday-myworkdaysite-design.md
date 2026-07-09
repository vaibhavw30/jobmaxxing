# Workday `myworkdaysite.com` coverage — design

## Context
`enrichment/workday.py` (Phase 5b, `docs/superpowers/specs/2026-06-14-workday-enrichment-design.md`)
recognizes exactly one Workday public-URL shape:

```
https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
```

Live data (2026-07-08 query) shows a second, structurally different but functionally equivalent Workday
public domain is completely invisible to it:

```
https://{wd}.myworkdaysite.com/[xx-XX/]recruiting/{tenant}/{site}/job/{rest}
```

**126 live jobs** sit on this domain (Magna, Snap, Microchip Technology, Western Alliance, Parexel,
others) and **100% are description-less** — `enrich_workday`'s candidate query never selects them, so
they will never enrich no matter how many times the worker runs.

Verified empirically (live curl, 2026-07-08) against three different tenants: the derived
`{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{rest}` cxs endpoint returns the same
`{"errorCode":"S22","httpStatus":403,"message":"permission denied"}` shape that a normal, already-covered
`myworkdayjobs.com` tenant returns from a non-cleared context — i.e. this is the *same* Cloudflare gate
the existing tiered fetcher (`fetch_workday_one`: plain → cleared-context → render) is already built to
climb, not a different/unreachable endpoint. A 404 would have meant "wrong derivation"; getting the same
403 shape across three unrelated tenants (Magna, Snap, Microchip Technology) confirms the tenant-from-path
extraction is reliable, not a one-off coincidence.

## Goal
Teach `enrichment/workday.py` to recognize `myworkdaysite.com` URLs, derive their `(tenant, wd, site,
rest)` identity, and feed them through the **existing, unmodified** tiered fetch pipeline — no new fetch
tier, no new worker, no new dependency. This is a coverage extension to already-proven code, not a new
subsystem.

## Design

### Pattern recognition (`enrichment/workday.py`)
Add a second regex alongside the existing `_WORKDAY_RE`:
```python
# https://{wd}.myworkdaysite.com/[xx-XX/]recruiting/{tenant}/{site}/job/{rest}
_WORKDAY_SITE_RE = re.compile(
    r"https://(?P<wd>wd\d+)\.myworkdaysite\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"                 # optional locale prefix, stripped (same as _WORKDAY_RE)
    r"recruiting/(?P<tenant>[^/]+)/(?P<site>[^/]+)/job/(?P<rest>.+)$"
)
```
Both regexes expose the same four named groups (`tenant`, `wd`, `site`, `rest`) — just extracted from
different positions (tenant lives in the hostname for `myworkdayjobs.com`, in the path for
`myworkdaysite.com`). `workday_host()` and `workday_cxs_url()` are refactored to try `_WORKDAY_RE` first
(the common case), fall back to `_WORKDAY_SITE_RE`, and build the host string / cxs URL from whichever
matched via **one shared code path** — no duplicated cxs-URL-construction logic between the two shapes.

### Everything downstream is unchanged
`fetch_workday_one` (tiered plain → cleared-context → render fetch, Cloudflare-block / not-found /
transient classification), `_apply_outcomes` (the shared enrichment-outcome writer), retry caps — none of
it changes. A `myworkdaysite.com` row simply now produces a `workday_host`/cxs URL identical in shape to a
native `myworkdayjobs.com` row's, so it flows through the exact same pipeline.

### Candidate selection
`enrich_workday()`'s SQL candidate query currently filters `url ~* 'myworkdayjobs\\.com'`. It gains a
second alternation: `url ~* 'myworkdayjobs\\.com|myworkdaysite\\.com'`. This is the only change that makes
these 126 rows selectable at all — without it, the new regex is unreachable code.

### Sharding / Cloudflare-clearance reuse
`workday_host(url)` is used to shard candidates so each shard runs on one thread-local fetcher and reuses
that tenant's Cloudflare clearance (`enrich_workday`'s `shards` dict). For a `myworkdaysite.com` row,
`workday_host` now returns the **canonical `tenant.wd.myworkdayjobs.com` identity**, not a
`myworkdaysite.com`-shaped key. This means if a tenant somehow has postings under both domain shapes (not
observed in current data, but not ruled out), they shard together and share one clearance-cache entry —
consistent with the existing per-tenant-identity design, not a new invariant.

### Stored URL — unchanged (explicit decision)
The `jobs.url` column is **never rewritten**. The operator always sees the exact link the company
published (a `myworkdaysite.com` URL stays a `myworkdaysite.com` URL in triage / "Open posting"). Only the
internal enrichment fetch target is derived, in-memory, for the duration of one fetch call. This matches
how `canonicalize_url` behaves today (generic hygiene — lowercase/trim/query-stripping — never
host-rewriting for any source) and keeps dedupe/canonicalization semantics untouched: no new code path can
alter what URL identifies a row.

## Error handling / robustness
No new error paths. A `myworkdaysite.com` URL that doesn't match `_WORKDAY_SITE_RE` (malformed, a future
unrelated Workday domain variant, etc.) falls through both regexes → `workday_cxs_url` returns `None` →
`fetch_workday_one`'s existing `if cxs is None: return Outcome(job_id, "permanent", None, f"unrecognized
workday url: {url}")` branch handles it exactly as an unrecognized `myworkdayjobs.com` URL would today.

## Testing (pyramid)
- **Unit — pattern recognition:** `_WORKDAY_SITE_RE` (via `workday_host`/`workday_cxs_url`) against the
  three empirically-verified real shapes (Magna: no locale prefix; Parexel: `en-US/` locale prefix;
  Snap/Microchip as additional cases) — assert the derived tenant/wd/site/rest and the resulting cxs URL.
- **Unit — cross-shape equivalence:** a `myworkdaysite.com` URL and its hand-constructed equivalent
  `myworkdayjobs.com` URL (same tenant/wd/site/rest) produce the **identical** `workday_host()` and
  `workday_cxs_url()` — the sharding/clearance-reuse invariant.
- **Unit — non-match:** an unrelated URL (or a `myworkdaysite.com` URL missing `recruiting/`) still returns
  `None` from both functions (no regression to the "unrecognized" path).
- **Integration — candidate selection:** `enrich_workday()`'s SQL candidate query selects a description-less
  `myworkdaysite.com` row (real DB, `pytest-postgresql` + `apply_migrations`, mirroring the existing
  `enrich_workday` integration tests) alongside an existing `myworkdayjobs.com` row.
- **No regression:** existing `enrichment/workday.py` tests (`fetch_workday_one` tiering, `_apply_outcomes`
  interplay, existing `_WORKDAY_RE` cases) stay green — this only adds a second recognized shape.

## Out of scope
Any other Workday-adjacent domain variant not yet observed in the data (add it the same way, if/when it
shows up); the broader long-tail of ~4,300 description-less jobs on ~673 non-Workday, non-clean-API-ATS
hosts (Oracle Cloud/Taleo, Jobvite, Workable, Rippling, custom company career pages) — a much larger,
lower-ROI-per-host problem deliberately not pursued here; rewriting the stored URL (explicit operator
decision, 2026-07-08); changing anything in `fetch_workday_one`, `_apply_outcomes`, or the CI
pollers/enrich/route pipeline.

## Execution
Isolated git worktree off `main`; subagent-driven TDD (single task — this is small enough not to need
decomposition into multiple reviewed tasks, though the implementer still follows red/green/commit
discipline per step); one review pass (spec + quality); merge to `main`; push (gh `vaibhavw30`).
