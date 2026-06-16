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
- **Filter at ingest**: drop postings that are *purely* off-window (don't store them at all).
- **Keep co-ops and untagged**: keep a posting if **any** of its terms is in-window, **or** if it is
  untagged (`terms` empty/missing or only `"N/A"`). Co-ops fall in naturally (they carry an in-window
  term or are untagged).
- **In-window set = {Spring 2026, Summer 2026, Fall 2026, Winter 2026}** (the whole 2026 cycle).
- **Existing off-window rows**: don't delete — push them to the **bottom** of triage ("I don't want to
  see them"). No backfill.

This last point requires a per-row term signal (a pure ingest-filter can't sort rows it already
dropped), so the design **stores** a small `term` marker — a minor adjustment to the pure-filter idea,
which also makes the sort clean.

## Goal
Stop ingesting off-window Simplify postings, keep in-window + untagged ones (incl. co-ops), and pin any
already-stored off-window / legacy rows to the bottom of the triage table — without touching the
hand-curated ATS/watchlist boards (which carry no term data).

## Design

### 1. In-window term set (config constant)
A single clearly-marked, case-insensitive constant — bumped once per recruiting cycle:
```python
# normalize.py
INTERNSHIP_TERMS = frozenset({"spring 2026", "summer 2026", "fall 2026", "winter 2026"})
```
Lives next to the other ingest-normalization constants in `normalize.py` and is exported for the
source adapter. Comparison is against `normalize_text`-style lowering + whitespace-collapse so
`" Summer 2026 "` / `"Summer  2026"` match (consistent with the whitespace fix merged in `0007`).

### 2. Ingest filter + classification (Simplify source only)
`sources/github_lists.py` parses the previously-discarded `terms` list and classifies each entry:
```
raw_terms  = [t for t in entry.get("terms", []) if isinstance(t, str)]
clean      = [normalize_text(t) for t in raw_terms]            # lowered, ws-collapsed
informative = [t for t in clean if t and t != "n/a"]           # real, non-"N/A" terms
in_window  = [t for t in informative if t in INTERNSHIP_TERMS]

if informative and not in_window:   -> DROP   (purely off-window: Summer 2027, Winter 2025, …)
elif in_window:                     -> KEEP, term = joined original in-window terms
else:                               -> KEEP, term = "N/A"   (untagged / only "N/A")
```
- A drop is a `continue` in the parse loop (same fail-soft style as the existing required-field
  skips), so the row is never constructed or stored.
- `term` is stored as the **original-cased** matched term string(s), comma-joined for multi-term
  (e.g. `"Summer 2026, Fall 2026"`); untagged kept rows store the literal `"N/A"`.
- The ATS adapters (`sources/ats.py`) are **untouched** — Greenhouse/Lever/Ashby payloads have no
  `terms`, so their records leave `term = None`.

### 3. `JobRecord.term` + storage
- Add `term: str | None = None` to `JobRecord` (`models.py`). `__post_init__` strips it like
  company/title (reuse the same isinstance-guarded strip).
- Migration `0008_add_term.sql`: `alter table jobs add column if not exists term text;` (nullable, no
  backfill — per operator decision). Idempotent, fits the always-re-run migration runner.
- `store.py`: add `term` to `_INSERT_COLS` / `_record_values`. `term` **must also** be in
  `_UPDATE_SQL` + `_update_values` (unlike company/title) so it refreshes on re-ingest — a legacy
  `NULL`-term row must get tagged when the active feed re-sees it. Also add `term` to the
  existing-rows SELECT-back columns + `_row_to_record` so the merge round-trips it.
- `merge.py`: add `term` to the `JobRecord` built by `merge_records`, using the **fill-when-null**
  rule (`primary.term or secondary.term`, like `description`/`location`). In the re-ingest path
  (`merge_records(_row_to_record(existing_db_row), fresh_rec)`) the existing DB row is primary; a
  legacy row's `term` is `NULL`, so the fresh record's term fills it → the row gets tagged. An
  ATS-promotion keeps the original listing's term (secondary) rather than nulling it.

### 4. Triage sort — demote legacy/off-window to the bottom (all sorts)
Add a leading demotion key to **every** ordering in `web/triage.py` `_order_by` (default *and*
whitelisted column sorts), because hiding off-window rows is a visibility rule, not a sort column:
```sql
order by (source like 'github:%' and term is null) asc, <existing order…>, id asc
```
- `source like 'github:%'` guards the demotion to Simplify rows only, so **ATS/watchlist rows
  (term NULL by nature) are NOT demoted** — they stay in the normal tier.
- New ingested Simplify rows always have a non-NULL `term` (in-window or `"N/A"`) → not demoted.
- Legacy Simplify rows (ingested before this change) have `term = NULL` → sink to the bottom.
- Implemented as a shared `_DEMOTE = "(source like 'github:%' and term is null) asc"` prefix prepended
  in `_order_by` for both branches, so default and column sorts can't drift.

### 5. Self-healing of existing rows (no backfill)
Immediately after deploy, *all* existing Simplify rows are `term = NULL` → at the bottom. The next
pipeline run re-ingests the **active** feed: in-window rows are re-tagged (`term` set → they rise),
purely-off-window rows are skipped at ingest (their stored row is never updated → stays NULL → stays
at the bottom). Rows no longer in the active feed are never re-tagged and remain sunk — which is the
desired "stale stuff stays out of sight" behavior. Documented as expected; no migration backfill.

## Components & data flow
- `normalize.py` — add `INTERNSHIP_TERMS` constant (+ reuse `normalize_text` for matching).
- `sources/github_lists.py` — parse `terms`, classify (drop / keep+term), set `JobRecord.term`.
- `models.py` — `JobRecord.term` field + strip in `__post_init__`.
- `migrations/0008_add_term.sql` — add nullable `term` column.
- `store.py` — thread `term` through insert, update, select-back, `_row_to_record`.
- `merge.py` — carry `term` in `merge_records` (incoming-wins, fallback to existing).
- `web/triage.py` — `_DEMOTE` prefix in `_order_by` (default + all whitelisted sorts).

## Testing (TDD, per layer)
- **`tests/test_github_lists.py`**:
  - keep + tag in-window (`terms=["Summer 2026"]` → `term == "Summer 2026"`).
  - keep + tag multi in-window (`["Fall 2026","Spring 2026"]` → joined; off-window member excluded).
  - drop purely off-window (`["Summer 2027"]` → no record); drop past (`["Winter 2025"]`).
  - keep untagged: `terms` missing, `[]`, `["N/A"]` → kept, `term == "N/A"`.
  - mixed off-window + N/A (`["Summer 2027","N/A"]`) → dropped (has a real off-window term).
  - whitespace/case-insensitive match (`[" summer 2026 "]` → kept).
- **`tests/test_models.py`**: `term` defaults None; surrounding-whitespace `term` stored trimmed.
- **`tests/test_store.py`**: insert persists `term`; **re-ingest updates `term`** on an existing row
  (NULL → "Summer 2026"); migration adds the column (`select term from jobs` works).
- **`tests/test_merge.py`**: `merge_records` fills `term` from primary, else secondary — a legacy
  `term=None` existing (primary) row takes the fresh record's term; a set existing term is preserved.
- **`tests/test_web_triage.py`**:
  - `test_legacy_github_rows_demoted`: a recent high-confidence Simplify row with `term IS NULL` ranks
    **below** an older one with `term='Summer 2026'`.
  - `test_ats_rows_not_demoted`: an ATS row (`source='greenhouse'`, `term NULL`) is **not** demoted.
  - `test_demotion_applies_to_all_sorts`: with `sort=company`, a `term NULL` github row still sorts last.

## Out of scope
No `term` column in the triage UI table or new filter dropdown (operator chose filter+sort, not
display); no backfill / cleanup migration for existing rows (self-heal instead); no term parsing for
ATS sources (no `terms` data); no change to `within_age_cutoff`, routing, enrichment, or the dormant
Sheets sync. The `GITHUB_LISTS` source URLs in `run.py` are unchanged (the repos already carry all
terms — the fix is term-aware ingestion, not new sources).

## Execution
Isolated git worktree off `main` (`worktree-term-window-filtering`); subagent-driven strict TDD
(failing test → minimal impl → green → small commit); two-stage review (spec compliance → code
quality); merge to `main`. Run `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before pytest
(`uv run pytest`).
