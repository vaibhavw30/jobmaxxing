# Term-window filtering — design

## Context
The Simplify-format GitHub feeds (`run.py` `GITHUB_LISTS` — the three `*/Summer2026-Internships`
repos) are **general aggregators, not summer-only**. A read of the live `SimplifyJobs` `listings.json`
(16,713 entries; 1,367 active) shows `terms` spanning every cycle:

| Term | Active |
|------|-------:|
| Summer 2026 | 768 |
| Fall 2026 | 341 |
| N/A (untagged) | 179 |
| Spring 2026 | 91 |
| Winter 2026 | 70 |
| Summer 2027 | 37 |
| Winter 2025 (past) | 32 |
| … 2027/2028/2029, old 2016 | rest |

The pipeline currently ingests **all** of them — `sources/github_lists.py` parses the payload but
**discards the `terms` field**, and the only volume filter is `normalize.within_age_cutoff`
(`MAX_AGE_DAYS = 243`, on `posted_at`). So the operator's triage table is padded with off-window
postings (future cycles like Summer 2027, past cycles like Winter 2025) that waste enrichment /
routing / triage attention. `category` is the *domain* (AI/ML, Software, Hardware, Quant, Product) —
**not** an internship-type signal; co-ops are identified by their `terms` (or title). An entry may
carry multiple terms (1,102 do — e.g. a Fall+Spring co-op).

Operator decisions (brainstorm):
- **Filter at ingest**: drop postings that are *purely* off-window (never store them).
- **Keep co-ops and untagged**: keep a posting if **any** of its terms is in-window, **or** if it is
  untagged (`terms` empty/missing or only `"N/A"` / unparseable). Co-ops fall in naturally.
- **Window derived from today's date** — *not* a hand-maintained list (operator pushed back on
  bumping a constant each cycle). It auto-rolls.
- **Existing off-window / legacy rows**: don't delete — push them to the **bottom** of triage. No
  backfill.
- **Show `term` in the UI**: a Term filter dropdown **and** a per-row column.

Storing `term` (needed for both the sort-to-bottom and the UI filter) is what turns the original
"pure ingest-filter" into "filter + store" — a deliberate, minor adjustment.

## Goal
Ingest only in-window Simplify postings (in-window terms + untagged, incl. co-ops), drop purely
off-window ones, store each posting's matched term(s), let the operator filter/see term in triage, and
pin already-stored legacy/off-window rows to the bottom — without touching the hand-curated
ATS/watchlist boards (which carry no term data) and with **zero manual maintenance** of the window.

## Design

### 1. Date-derived in-window set (no manual bumping)
A pure function in `normalize.py` maps "today" → the set of in-window **years**:
```python
def current_cycle_years(today: date) -> set[int]:
    """Years considered 'in window'. The current calendar year, plus next year once we're
    in H2 (month >= CYCLE_LOOKAHEAD_MONTH = 7) — by late summer, next year's cycle is open
    for applications, so we start surfacing it without dropping the current fall."""
    years = {today.year}
    if today.month >= CYCLE_LOOKAHEAD_MONTH:   # 7 == July
        years.add(today.year + 1)
    return years
```
- As of **2026-06-16** (month 6) → `{2026}` → keeps Spring/Summer/Fall/Winter 2026 exactly (matches
  the operator's list); drops 2025 and 2027+. From July 2026 → `{2026, 2027}` (next cycle opens).
- Year-based, not per-season — `within_age_cutoff` (243 days) already removes genuinely stale
  postings, so we don't also prune past *seasons* of the current year.
- `CYCLE_LOOKAHEAD_MONTH = 7` is a documented constant (the one knob), but no per-cycle edits.

### 2. Term parsing + classification (Simplify source only)
A pure helper parses a `terms` entry into a `(season, year)`:
```python
_TERM_RE = re.compile(r"\b(spring|summer|fall|winter)\s+(\d{4})\b")   # on normalize_text(term)
```
`parse_term(s) -> (season:str, year:int) | None` (None for `"N/A"`, blank, or unparseable). In
`parse_simplify_format`, given `allowed_years: set[int] | None`:
```
parsed   = [pt for t in raw_terms if (pt := parse_term(t))]      # [(season, year, original), …]
in_window = [orig for (s, y, orig) in parsed if y in allowed_years]   # only when allowed_years given

if allowed_years is not None and parsed and not in_window: -> DROP   (purely off-window)
elif in_window:  -> KEEP, term = unique in-window original strings (list)
else:            -> KEEP (untagged/co-op-no-term), term = []   (empty list)
```
- A drop is a `continue` (same fail-soft style as the existing required-field skips).
- `allowed_years=None` ⇒ **no drop filter** (term is still parsed/stored). Default for the bare
  signature so existing callers/tests keep working; production always passes a real set.
- **Wiring**: `run.py main()` already builds `now`; compute `allowed_years =
  current_cycle_years(now.date())` there and thread it `build_sources(allowed_years=…)` →
  `_github_list_source(source, url, allowed_years)` → `parse_simplify_format(..., allowed_years=…)`.
  The drop lives in the parser (that's where the raw `terms` payload is); `pipeline.ingest_records`
  is untouched.
- `term` stores the **original-cased** in-window term strings, e.g. `["Summer 2026", "Fall 2026"]`;
  an off-window member of a multi-term posting is excluded from the stored list.
- ATS adapters (`sources/ats.py`) untouched → their records leave `term = None`.

### 3. `JobRecord.term` (array) + storage
- Add `term: list[str] | None = None` to `JobRecord` (`models.py`). Three-state by design:
  `None` = unprocessed/legacy or ATS (no term concept); `[]` = processed-but-untagged (kept N/A /
  co-op); non-empty = matched in-window terms. (`__post_init__` is unchanged — `term` elements are
  produced clean by the parser; only the existing company/title string-strip stays.)
- Migration `0008_add_term.sql`: `alter table jobs add column if not exists term text[];` (nullable,
  no default, no backfill). Idempotent; fits the always-re-run migration runner. Mirrors the existing
  `alt_urls text[]` column, so psycopg adapts a Python `list` ↔ `text[]` automatically.
- `store.py`: add `term` to `_INSERT_COLS` / `_record_values`. `term` **must also** be in `_UPDATE_SQL`
  + `_update_values` (unlike company/title) so it refreshes on re-ingest — a legacy `NULL`-term row
  gets tagged when the active feed re-sees it. Add `term` to the existing-rows SELECT-back columns +
  `_row_to_record` (`term = row["term"]`, preserving `None` vs `[]`) so the merge round-trips it.
- `merge.py`: set `term` on the merged `JobRecord` with a **None-aware** fill (NOT truthiness —
  `[]` is a real "processed untagged" value): `term = primary.term if primary.term is not None else
  secondary.term`. In the re-ingest path (`merge_records(_row_to_record(existing_db_row), fresh_rec)`),
  a legacy row's `term is None` so the fresh record's term fills it → the row gets tagged; an
  ATS-promotion keeps the original listing's term instead of nulling it.

### 4. Triage — demote legacy/off-window to the bottom (all sorts)
A leading demotion key in **every** ordering in `web/triage.py` `_order_by` (default *and* whitelisted
column sorts), since hiding off-window rows is a visibility rule, not a sort column:
```sql
order by (source like 'github:%' and term is null) asc, <existing order…>, id asc
```
- `source like 'github:%'` guards the demotion to Simplify rows, so **ATS/watchlist rows (`term`
  NULL by nature) are NOT demoted**.
- New ingested Simplify rows have non-NULL `term` (in-window list or `[]`) → not demoted.
- Legacy Simplify rows (pre-change) have `term = NULL` → sink to the bottom.
- Shared `_DEMOTE` prefix string prepended in both `_order_by` branches so they can't drift.

### 5. Triage — Term filter + column
- **Column**: append `"term"` to `_DISPLAY_COLS` in `triage.py` (alongside `route_confidence`; leaves
  `funnel.TRIAGE_COLUMNS` / dormant Sheets sync untouched). `server.py` renders a "Term" cell —
  the array joined `", "`, or `—` for `[]`/`NULL`.
- **Filter** (`web/server.py` + `triage._build_where`): a `term` query param, composed like the
  existing status/category filters:
  - a specific term string → `%s = any(term)` (so a Fall+Spring co-op matches under either).
  - sentinel `__untagged__` → `term is not null and cardinality(term) = 0` (kept N/A rows).
  - `All`/absent → no clause.
  - Dropdown options: `select distinct unnest(term) from jobs where term is not null and
    cardinality(term) > 0 order by 1`, plus `All` and `Untagged`. (ATS/legacy `NULL`-term rows show
    only under `All` — they have no term to filter on; documented.)
- Term filter **composes** with status/category/sort and is preserved across header-sort clicks and
  the other filter forms (same query-string plumbing as the existing filters).

### 6. Self-healing of existing rows (no backfill)
Right after deploy, all existing Simplify rows are `term = NULL` → bottom. The next pipeline run
re-ingests the **active** feed: in-window rows are re-tagged (`term` set → they rise), purely
off-window rows are skipped at ingest (stored row never updated → stays NULL → stays at the bottom).
Rows no longer in the active feed are never re-tagged and remain sunk — the desired "stale stuff out
of sight" behavior. Documented as expected; no migration backfill.

## Components & data flow
- `normalize.py` — `CYCLE_LOOKAHEAD_MONTH`, `current_cycle_years(today)`, `parse_term(s)`, `_TERM_RE`.
- `run.py` — `main()` computes `current_cycle_years(now.date())` and threads it through
  `build_sources(allowed_years=…)` → `_github_list_source(source, url, allowed_years)` → the parser.
- `sources/github_lists.py` — parse + classify `terms`, drop purely off-window, set `JobRecord.term`.
- `models.py` — `JobRecord.term: list[str] | None`.
- `migrations/0008_add_term.sql` — `term text[]` (nullable).
- `store.py` — thread `term` through insert, update, select-back, `_row_to_record`.
- `merge.py` — None-aware `term` carry in `merge_records`.
- `web/triage.py` — `_DEMOTE` prefix in `_order_by`; `term` in `_DISPLAY_COLS`; `term` filter in
  `_build_where`.
- `web/server.py` — Term `<select>` dropdown + "Term" column + query-string preservation.

## Testing (TDD, per layer)
- **`tests/test_normalize.py`**:
  - `current_cycle_years`: month 6 → `{Y}`; month 7/12 → `{Y, Y+1}`; year-boundary cases.
  - `parse_term`: "Summer 2026"→(summer,2026); " fall  2026 "→(fall,2026); "N/A"/""/"intern"→None.
- **`tests/test_github_lists.py`** (pass explicit `allowed_years={2026}`):
  - keep + tag in-window (`["Summer 2026"]` → `term == ["Summer 2026"]`).
  - multi in-window (`["Fall 2026","Spring 2026"]` → both; off-window member excluded).
  - drop purely off-window (`["Summer 2027"]`, `["Winter 2025"]` → no record).
  - keep untagged: `terms` missing / `[]` / `["N/A"]` / unparseable → kept, `term == []`.
  - mixed real off-window + N/A (`["Summer 2027","N/A"]`) → dropped.
  - case/whitespace-insensitive match (`[" summer 2026 "]` → kept).
  - `allowed_years=None` → nothing dropped, `term` still populated.
- **`tests/test_models.py`**: `term` defaults `None`.
- **`tests/test_store.py`**: insert persists `term` array; **re-ingest updates `term`** on an existing
  row (`NULL` → `["Summer 2026"]`); `[]` round-trips distinct from `NULL`; migration adds the column.
- **`tests/test_merge.py`**: None-aware fill — legacy `term=None` (primary) takes the fresh record's
  term; an existing `[]` is preserved (not overwritten via truthiness); set term stays.
- **`tests/test_web_triage.py`**:
  - `test_legacy_github_rows_demoted`: recent `term IS NULL` Simplify row ranks **below** an older
    `term='{Summer 2026}'` one.
  - `test_ats_rows_not_demoted`: `source='greenhouse'`, `term NULL` is **not** demoted.
  - `test_demotion_applies_to_all_sorts`: `sort=company` still pins the `NULL`-term github row last.
  - `test_filter_by_term` (`term='Summer 2026'` → only matching, incl. a multi-term co-op);
    `test_filter_untagged` (cardinality-0 rows); `test_term_filter_composes_with_status`.
- **`tests/test_web_server.py`**:
  - `test_term_column_rendered` (array joined; `—` for empty/NULL).
  - `test_term_dropdown_options` (distinct terms + All + Untagged).
  - `test_term_filter_preserved_in_sort_links` / `test_sort_preserved_in_term_filter`.

## Out of scope
No backfill / cleanup migration for existing rows (self-heal instead); no term parsing for ATS sources
(no `terms` data); no change to `within_age_cutoff`, routing, enrichment, or the dormant Sheets sync;
no new source URLs in `run.py` (the repos already carry all terms — the fix is term-aware ingestion).
Term semantics are year-based for the in-window decision; per-season pruning of the current year is
explicitly not done (age cutoff covers it).

## Execution
Isolated git worktree off `main` (`worktree-term-window-filtering`); subagent-driven strict TDD
(failing test → minimal impl → green → small commit); two-stage review (spec compliance → code
quality); merge to `main`. Run `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before pytest
(`uv run pytest`).
