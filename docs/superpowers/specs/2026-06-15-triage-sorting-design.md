# Triage table sorting & filtering — design

## Context
The local web triage table (`python -m jobmaxxing.web`, merged 2026-06-15) currently orders rows by
`scraped_at desc`. Because the entire feed was ingested during a one-time cold-start backlog import,
every row's `scraped_at` is clustered on 2026-06-14/15 — so the ordering is effectively random with
respect to how *recently the job was posted*. The operator sees stale postings at the top.

Data check (read-only, against live Supabase): **`posted_at` is populated for 100% of the 3,641
routed jobs** (range 2025-10-14 → now), so true posting-date ordering is fully feasible. Of the
undecided (`new`/`routed`) jobs, only 129 were posted in the last 7 days, 288 in 8–30 days, and 2,445
are >90 days old — so recency ordering matters a lot. Relevance signals available: `route_confidence`
(min 0.05, avg 0.70, max 1.0) and `route_method` (`rules`=3,088 authoritative, `llm_title`=305
provisional title-only guesses with confidence capped at 0.4, `llm`=248 JD-based).

Operator decisions (brainstorm): **relevance = routing confidence**; **categories = filter + sort**;
**default leads with posted date (newest first)**; **raise the row cap and show a count indicator
rather than building full pagination now**.

## Goal
Let the operator order the triage table by **posting recency (default), company (A–Z), category
(resume type), and routing confidence**, filter by **category and status**, and see **how many rows
match** — so recent, confidently-relevant jobs surface at the top.

## Design

### 1. Default ordering — "recent + relevant"
```sql
order by (coalesce(route_confidence, 1.0) < 0.4) asc, posted_at desc nulls last
```
- Primary key demotes the low-confidence tier: `coalesce(route_confidence, 1.0) < RELEVANCE_FLOOR`
  (a named constant `RELEVANCE_FLOOR = 0.4`). In Postgres a boolean `false` (0) sorts before `true`
  (1) under `asc`, so **high-confidence jobs come first**. `0.4` aligns with the `llm_title`
  provisional cap, so the 305 title-only guesses naturally sink to the bottom while staying visible.
  The `coalesce(..., 1.0)` treats an unknown/NULL `route_confidence` (e.g. a manual override) as
  high-trust so it is **not** demoted — and, critically, avoids the NULL-sorts-first trap that a bare
  `route_confidence < 0.4` would hit (NULL is neither `<0.4` true nor false, and sorts NULLS-FIRST
  under `asc`, which would wrongly float unknown-confidence rows to the very top).
- Secondary key `posted_at desc` → **within the (dominant) high-confidence tier, newest posting
  first**. `nulls last` is defensive (today `posted_at` is 100% populated, but a future source might
  not be).
- This is the ordering when no explicit `sort` is requested.

### 2. Sortable columns (clickable headers, server-side)
When the operator clicks a column header, that column becomes the sort key; clicking the **same**
column again **toggles asc/desc**. Sort keys are a fixed whitelist (NO user input interpolated into
SQL) mapping a key name → a fixed ORDER BY expression:

| Header | `sort` key | Expression | Default dir | Notes |
|--------|-----------|------------|-------------|-------|
| Posted | `posted` | `posted_at` | `desc` | newest first |
| Company | `company` | `lower(company)` | `asc` | A→Z, case-insensitive |
| Type | `type` | `resume_type` | `asc` | groups same categories; **secondary `posted_at desc`** so within a type it's newest-first |
| Confidence | `conf` | `route_confidence` | `desc` | most relevant first |

- The active sort column shows a `↑`/`↓` arrow.
- Any explicit column sort appends a stable final tiebreaker `id asc` for deterministic paging/repeat
  loads.
- The implicit default (no `sort` param) uses the §1 expression (not in the table above); selecting
  the "Posted" header gives a *pure* `posted_at` sort without the confidence tier (so the operator can
  see strict recency including low-confidence rows when they want).

### 3. Filters (kept + improved)
- **Status** dropdown: `Undecided` (default → `status in ('new','routed')`), `All`, and each
  individual status. Reuses the existing `statuses=`/`status=` plumbing in `fetch_triage_rows`.
- **Category** dropdown: `All` + each resume type (`swe, mle, ai, fdse, robotics, quant-dev,
  quant-trader, av`). Reuses the existing `resume_type=` filter.
- Filters and sort **compose**: e.g. Category=`quant-trader` (52 rows) + sort by Company.
- Clicking a sort header **preserves** the active filters (carried as query params); changing a filter
  preserves the active sort.

### 4. Row cap + count indicator
- Raise the cap from 200 to **`DEFAULT_LIMIT = 500`** (still clamped; `MAX_LIMIT = 500`).
- Add a **"showing N of M matching"** indicator: `N` = rows rendered, `M` = total rows matching the
  current filters (ignoring the limit). When `M > N`, show a hint to narrow via a category filter.
- Real pagination is explicitly **deferred** (documented as out of scope); the cap + indicator + the
  category filter cover the operator's deep-dive needs (the largest single undecided category, `swe`,
  is 2,057 — beyond the cap — so the indicator must make truncation visible).

## Components & data flow
- **`src/jobmaxxing/web/triage.py`** (pure DB logic, no Flask):
  - `fetch_triage_rows(conn, *, status=None, statuses=None, resume_type=None, sort=None,
    direction=None, limit=DEFAULT_LIMIT)`:
    - Builds the SELECT from `TRIAGE_COLUMNS` (unchanged), applies the existing
      status/statuses/resume_type filters, then an ORDER BY from a **whitelist**:
      - `sort=None` → the §1 default expression.
      - `sort` in the whitelist → that column's expression; `direction` in `{"asc","desc"}` (falls
        back to the column's default dir on anything else); append column-specific secondary +
        `id asc` tiebreaker.
      - An unknown `sort` value → fall back to the §1 default (never error, never interpolate).
    - `limit` clamped to `[1, MAX_LIMIT]`.
    - Returns `list[dict]` (unchanged shape; `description` still `plain_text`-ed).
  - `count_triage(conn, *, status=None, statuses=None, resume_type=None) -> int`: same WHERE as
    `fetch_triage_rows`, `select count(*)`, no order/limit. (Shared WHERE-builder helper so the two
    can't drift.)
  - Constants `RELEVANCE_FLOOR = 0.4`, `DEFAULT_LIMIT = 500`, `MAX_LIMIT = 500`, and a `_SORTS` dict
    (key → (expression, default_direction, secondary)).
- **`src/jobmaxxing/web/server.py`** (thin Flask):
  - `GET /` reads `request.args`: `sort`, `dir`, `status`, `resume_type`. Computes the effective
    statuses (default undecided), calls `fetch_triage_rows(...)` + `count_triage(...)`, renders
    `INDEX_HTML` with: the rows, the total count, the active sort/dir, and the filter values.
  - `INDEX_HTML` (inline): header cells become `<a>` links built by a small Jinja helper that sets
    `sort=<key>` and toggles `dir` if that column is already active, **carrying current
    `status`/`resume_type`** in the query string; the active column renders its `↑`/`↓`. Two
    `<select>` filter dropdowns (status, category) in a `GET` `<form>` that **preserves the current
    `sort`/`dir`** via hidden inputs. A "showing N of M" line. The existing per-row decision controls
    (Interested/Not/Applied/reset) and the badge-on-change JS are unchanged.
  - `POST /decide` and `/reset` are unchanged.

## Testing
- **`tests/test_web_triage.py`**:
  - Update `test_fetch_orders_newest_first` to seed explicit `posted_at` (not `scraped_at`) and assert
    `posted_at desc` is the default order within one confidence tier.
  - `test_default_demotes_low_confidence`: seed a *recent* low-confidence job (`route_confidence=0.2`)
    and an *older* high-confidence job; assert the older high-confidence one ranks first (tier beats
    recency in the default).
  - `test_sort_company_asc_desc`, `test_sort_posted_pure` (recency including low-conf, no tier),
    `test_sort_type_groups_then_recency`, `test_sort_confidence_desc` — one per sort key incl.
    direction toggle.
  - `test_sort_unknown_key_falls_back_to_default` (no error, no injection).
  - `test_limit_clamped_to_max` and `test_count_triage_matches_filters` (count ignores limit, honors
    filters).
- **`tests/test_web_server.py`**:
  - `test_get_sort_param_orders_rows` (e.g. `?sort=company` reflects in row order in the HTML).
  - `test_get_header_links_toggle_direction` (active column link flips `dir`; arrow rendered).
  - `test_get_filters_preserved_in_sort_links` and `test_sort_preserved_in_filter_form` (query-string
    round-trip).
  - `test_get_shows_count_indicator` ("of M" present, M = total matching).
  - `test_get_default_excludes_decided` still passes (default status filter unchanged).

## Out of scope
Real pagination / infinite scroll (cap + count indicator + category filter instead); free-text search;
multi-column sort UI; saved views; per-operator category-priority weighting (relevance is
`route_confidence` only); any change to `POST /decide` `/reset`, the funnel logic, or the dormant
Sheets sync. `ROUTED_JOBS_SQL` in `funnel.py` (used only by the dormant Sheets sync) keeps its
`scraped_at desc` order — unchanged.

## Execution
Isolated git worktree off `main`; subagent-driven TDD; two-stage review (spec → quality) per stream;
merge to `main`; push (gh `vaibhavw30`).
