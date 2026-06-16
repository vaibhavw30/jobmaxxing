# Term-Window Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest only in-window Simplify postings (terms in the date-derived window, plus untagged/co-op), drop purely off-window ones, store each posting's matched `term`, surface it as a triage column + filter, and pin legacy/off-window github rows to the bottom of all sorts — without touching ATS sources.

**Architecture:** A date-derived year window (`current_cycle_years(today)`) computed in `run.main()` is threaded into the Simplify parser, which classifies each posting by its `terms` field (drop / keep+tag). `term` is a nullable `text[]` column threaded through `JobRecord` → `store` → `merge`. The triage layer demotes `github` rows with `term IS NULL` and adds a `term` filter + column.

**Tech Stack:** Python 3.12, psycopg3, PostgreSQL (pytest-postgresql), Flask (triage UI), pytest, `uv`.

**Before every `pytest` run:** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` and use `uv run pytest`.

---

## File Structure

- `src/jobmaxxing/normalize.py` — **modify**: add `CYCLE_LOOKAHEAD_MONTH`, `current_cycle_years(today)`, `_TERM_RE`, `parse_term(value)`.
- `src/jobmaxxing/models.py` — **modify**: add `JobRecord.term: list[str] | None = None`.
- `migrations/0008_add_term.sql` — **create**: `alter table jobs add column if not exists term text[];`.
- `src/jobmaxxing/store.py` — **modify**: thread `term` through insert / update / select-back / `_row_to_record`.
- `src/jobmaxxing/merge.py` — **modify**: None-aware `term` carry in `merge_records`.
- `src/jobmaxxing/sources/github_lists.py` — **modify**: parse + classify `terms`, drop off-window, set `term`.
- `src/jobmaxxing/run.py` — **modify**: compute + thread `allowed_years`.
- `src/jobmaxxing/web/triage.py` — **modify**: `_DEMOTE` prefix in `_order_by`; `term` in `_DISPLAY_COLS`; `term` filter in `_build_where` / `fetch_triage_rows` / `count_triage`.
- `src/jobmaxxing/web/server.py` — **modify**: Term `<select>` + Term column + query-string preservation.

Tasks are sequential (coupled) on the single branch `worktree-term-window-filtering`.

---

### Task 1: Date-derived window + term parser (`normalize.py`)

**Files:**
- Modify: `src/jobmaxxing/normalize.py`
- Test: `tests/test_normalize.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py` (add `from datetime import date` if not already imported, and extend the existing `from jobmaxxing.normalize import (...)`):

```python
from datetime import date

from jobmaxxing.normalize import current_cycle_years, parse_term


def test_current_cycle_years_first_half_is_current_year_only():
    assert current_cycle_years(date(2026, 6, 16)) == {2026}
    assert current_cycle_years(date(2026, 1, 1)) == {2026}


def test_current_cycle_years_h2_adds_next_year():
    assert current_cycle_years(date(2026, 7, 1)) == {2026, 2027}
    assert current_cycle_years(date(2026, 12, 31)) == {2026, 2027}


def test_parse_term_basic():
    assert parse_term("Summer 2026") == ("summer", 2026)
    assert parse_term("Fall 2026") == ("fall", 2026)


def test_parse_term_whitespace_and_case_insensitive():
    assert parse_term("  SUMMER   2026 ") == ("summer", 2026)


def test_parse_term_returns_none_for_untagged_or_junk():
    assert parse_term("N/A") is None
    assert parse_term("") is None
    assert parse_term("intern") is None
    assert parse_term(None) is None
    assert parse_term(["Summer 2026"]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_normalize.py -k "current_cycle_years or parse_term" -q`
Expected: FAIL — `ImportError: cannot import name 'current_cycle_years'`.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/normalize.py`, change the datetime import line to include `date`:

```python
from datetime import date, datetime, timedelta, timezone
```

Then add, after the `_WS` regex definition near the top:

```python
# Term filtering: a posting's recruiting term(s) are "in window" if their year is in the
# current cycle. The window is derived from the run date (no hand-maintained list): the current
# calendar year, plus next year once we're in H2 — by late summer next year's cycle is open for
# applications, so we surface it without dropping the current fall.
CYCLE_LOOKAHEAD_MONTH = 7  # July

_TERM_RE = re.compile(r"\b(spring|summer|fall|winter)\s+(\d{4})\b")


def current_cycle_years(today: date) -> set[int]:
    """In-window years for term filtering, derived from ``today``."""
    years = {today.year}
    if today.month >= CYCLE_LOOKAHEAD_MONTH:
        years.add(today.year + 1)
    return years


def parse_term(value) -> tuple[str, int] | None:
    """Parse a Simplify ``terms`` entry like 'Summer 2026' -> ('summer', 2026).

    Returns None for 'N/A', blanks, non-strings, or anything without a season+year. Matching runs
    on ``normalize_text`` output, so case and surrounding/extra whitespace don't matter."""
    if not isinstance(value, str):
        return None
    m = _TERM_RE.search(normalize_text(value))
    if not m:
        return None
    return m.group(1), int(m.group(2))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_normalize.py -q`
Expected: PASS (all, including pre-existing normalize tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/normalize.py tests/test_normalize.py
git commit -m "ingest(normalize): date-derived term window + parse_term helper"
```

---

### Task 2: `JobRecord.term` field (`models.py`)

**Files:**
- Modify: `src/jobmaxxing/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_jobrecord_term_defaults_none():
    rec = JobRecord(source="github:simplify", company="Acme", title="SWE Intern", url="https://x/y")
    assert rec.term is None


def test_jobrecord_accepts_term_list():
    rec = JobRecord(source="github:simplify", company="Acme", title="SWE Intern",
                    url="https://x/y", term=["Summer 2026", "Fall 2026"])
    assert rec.term == ["Summer 2026", "Fall 2026"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_models.py -k term -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'term'`.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/models.py`, add the field after `dedupe_key`:

```python
    alt_urls: list[str] = field(default_factory=list)
    dedupe_key: str = ""
    # Recruiting term(s) for github-list postings, e.g. ["Summer 2026"]. Three-state:
    # None = legacy/unprocessed or ATS (no term); [] = processed-but-untagged (kept N/A / co-op);
    # non-empty = matched in-window terms.
    term: list[str] | None = None
```

(`__post_init__` is unchanged — `term` elements are produced clean by the parser.)

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/models.py tests/test_models.py
git commit -m "models: add JobRecord.term (nullable list)"
```

---

### Task 3: `term` column + storage (`migrations/0008`, `store.py`)

**Files:**
- Create: `migrations/0008_add_term.sql`
- Modify: `src/jobmaxxing/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py` (the `_rec` helper and `conn` fixture already exist):

```python
def test_upsert_persists_term(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(term=["Summer 2026", "Fall 2026"])])
    row = conn.execute("select term from jobs where dedupe_key='acme|swe intern'").fetchone()
    assert row[0] == ["Summer 2026", "Fall 2026"]


def test_term_empty_list_and_null_persist_distinctly(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(dedupe_key="a|1", term=[]), _rec(dedupe_key="a|2", term=None)])
    got = dict(conn.execute("select dedupe_key, term from jobs").fetchall())
    assert got["a|1"] == []
    assert got["a|2"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_store.py -k "term" -q`
Expected: FAIL — `psycopg.errors.UndefinedColumn: column "term" does not exist`.

- [ ] **Step 3a: Create the migration**

Create `migrations/0008_add_term.sql`:

```sql
-- Per-posting recruiting term(s), e.g. {'Summer 2026','Fall 2026'}, parsed from the Simplify
-- feed's `terms` field. Nullable text[]: NULL = legacy/unprocessed or ATS (no term concept);
-- '{}' = processed but untagged (kept N/A / co-op-with-no-term); non-empty = matched in-window
-- terms. Used to demote off-window/legacy github rows in triage and to filter by term.
-- No backfill: existing rows stay NULL and self-heal on the next term-aware ingest.
alter table jobs add column if not exists term text[];
```

- [ ] **Step 3b: Thread `term` through `store.py`**

In `src/jobmaxxing/store.py`:

Replace `_INSERT_COLS` / `_INSERT_SQL`:

```python
_INSERT_COLS = (
    "dedupe_key, source, external_id, company, title, location, "
    "url, alt_urls, description, posted_at, is_active, term"
)
_INSERT_SQL = (
    f"insert into jobs ({_INSERT_COLS}) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "on conflict (dedupe_key) do nothing"
)
```

Replace `_UPDATE_SQL` (add `term=%s` before `scraped_at=now()`):

```python
_UPDATE_SQL = (
    "update jobs set source=%s, external_id=%s, location=%s, url=%s, "
    "alt_urls=%s, description=%s, posted_at=%s, is_active=%s, term=%s, scraped_at=now() "
    "where dedupe_key=%s"
)
```

In `_record_values`, add `rec.term` as the last value (matching the column order):

```python
def _record_values(rec: JobRecord) -> tuple:
    return (
        rec.dedupe_key,
        rec.source,
        rec.external_id,
        rec.company,
        rec.title,
        rec.location,
        rec.url,
        rec.alt_urls,
        rec.description,
        rec.posted_at,
        rec.is_active,
        rec.term,
    )
```

In `_update_values`, add `rec.term` immediately before `rec.dedupe_key`:

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
        rec.term,
        rec.dedupe_key,
    )
```

In `_row_to_record`, add `term=row["term"]` (preserves None vs []):

```python
def _row_to_record(row: dict) -> JobRecord:
    return JobRecord(
        source=row["source"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        external_id=row["external_id"],
        location=row["location"],
        description=row["description"],
        posted_at=row["posted_at"],
        is_active=row["is_active"],
        alt_urls=list(row["alt_urls"]),
        dedupe_key=row["dedupe_key"],
        term=row["term"],
    )
```

In `upsert_jobs`, add `term` to the existing-rows SELECT column list:

```python
            for row in cur.execute(
                "select dedupe_key, source, external_id, company, title, location, "
                "url, alt_urls, description, posted_at, is_active, term "
                "from jobs where dedupe_key = any(%s)",
                (keys,),
            ).fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_store.py -q`
Expected: PASS (new term tests + all pre-existing store tests).

- [ ] **Step 5: Commit**

```bash
git add migrations/0008_add_term.sql src/jobmaxxing/store.py tests/test_store.py
git commit -m "ingest(store): add term column + thread through upsert"
```

---

### Task 4: None-aware `term` carry in merge (`merge.py`)

**Files:**
- Modify: `src/jobmaxxing/merge.py`
- Test: `tests/test_merge.py`, `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_merge.py` (uses the existing `_rec` helper):

```python
def test_merge_keeps_primary_term():
    existing = _rec(term=["Summer 2026"])
    incoming = _rec(source="github:vanshb03", url="https://other", term=["Fall 2026"])
    merged = merge_records(existing, incoming)
    assert merged.term == ["Summer 2026"]  # existing stays primary (neither is ATS)


def test_merge_fills_term_from_secondary_when_primary_none():
    existing = _rec(term=None)
    incoming = _rec(source="github:vanshb03", url="https://other", term=["Summer 2026"])
    merged = merge_records(existing, incoming)
    assert merged.term == ["Summer 2026"]


def test_merge_preserves_empty_term_not_truthiness():
    # [] is a real "processed untagged" value, NOT missing -> must not fall through to secondary.
    existing = _rec(term=[])
    incoming = _rec(source="github:vanshb03", url="https://other", term=["Summer 2026"])
    merged = merge_records(existing, incoming)
    assert merged.term == []
```

Append to `tests/test_store.py` (integration: re-ingest tags a legacy NULL-term row via merge):

```python
def test_reingest_tags_legacy_null_term_row(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(term=None)])             # legacy row, no term
    upsert_jobs(conn, [_rec(term=["Summer 2026"])])  # re-seen with a term -> tagged via merge
    row = conn.execute("select term from jobs where dedupe_key='acme|swe intern'").fetchone()
    assert row[0] == ["Summer 2026"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_merge.py -k term tests/test_store.py::test_reingest_tags_legacy_null_term_row -q`
Expected: FAIL — merged `term` is `None` (merge_records doesn't pass it yet), so assertions fail.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/merge.py`, add `term` to the returned `JobRecord` (after `dedupe_key`), using a None-aware fill (NOT truthiness, so `[]` is preserved):

```python
    return JobRecord(
        source=primary.source,
        company=primary.company,
        title=primary.title,
        url=primary.url,
        external_id=primary.external_id or secondary.external_id,
        location=primary.location or secondary.location,
        description=primary.description or secondary.description,
        posted_at=primary.posted_at or secondary.posted_at,
        is_active=incoming.is_active,
        alt_urls=alt_urls,
        dedupe_key=existing.dedupe_key or incoming.dedupe_key,
        # term: primary's value, else secondary's. None-aware (not `or`) so an empty list — a real
        # "processed untagged" marker — is preserved, and a legacy NULL gets the fresh term.
        term=primary.term if primary.term is not None else secondary.term,
    )
```

Also add to the docstring's field list (after the `dedupe_key` line):

```python
    - term: primary's value, else secondary's (None-aware fill; preserves an empty-list marker).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_merge.py tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/merge.py tests/test_merge.py tests/test_store.py
git commit -m "ingest(merge): carry term (None-aware) so re-ingest tags legacy rows"
```

---

### Task 5: Parse + classify terms in the Simplify source (`github_lists.py`)

**Files:**
- Modify: `src/jobmaxxing/sources/github_lists.py`
- Test: `tests/test_github_lists.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_github_lists.py`:

```python
def _entry(**kw):
    base = {"company_name": "Acme", "title": "SWE", "url": "https://x"}
    base.update(kw)
    return base


def test_keeps_and_tags_in_window_term():
    rec = parse_simplify_format([_entry(terms=["Summer 2026"])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["Summer 2026"]


def test_keeps_multi_term_excludes_off_window_member():
    rec = parse_simplify_format([_entry(terms=["Fall 2026", "Summer 2027", "Spring 2026"])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["Fall 2026", "Spring 2026"]  # 2027 dropped from the stored list


def test_drops_purely_off_window():
    assert parse_simplify_format([_entry(terms=["Summer 2027"])],
                                 source="github:simplify", allowed_years={2026}) == []
    assert parse_simplify_format([_entry(terms=["Winter 2025"])],
                                 source="github:simplify", allowed_years={2026}) == []


def test_keeps_untagged_with_empty_term():
    for terms in (None, [], ["N/A"], ["totally bogus"]):
        recs = parse_simplify_format([_entry(terms=terms)],
                                     source="github:simplify", allowed_years={2026})
        assert len(recs) == 1 and recs[0].term == []


def test_drops_mixed_real_offwindow_plus_na():
    assert parse_simplify_format([_entry(terms=["Summer 2027", "N/A"])],
                                 source="github:simplify", allowed_years={2026}) == []


def test_term_match_is_case_and_whitespace_insensitive():
    rec = parse_simplify_format([_entry(terms=[" summer  2026 "])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["summer  2026"]  # original (stripped) string stored; matched on normalize


def test_allowed_years_none_keeps_all_and_tags():
    recs = parse_simplify_format([_entry(terms=["Summer 2027"])], source="github:simplify")
    assert len(recs) == 1 and recs[0].term == ["Summer 2027"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_github_lists.py -k "term or window or untagged or allowed_years" -q`
Expected: FAIL — `parse_simplify_format() got an unexpected keyword argument 'allowed_years'`.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/sources/github_lists.py`, update the import:

```python
from ..normalize import make_dedupe_key, parse_term
```

Change the function signature and add the term-classification block. Replace the function header and the per-entry body up to the `JobRecord(...)` append:

```python
def parse_simplify_format(payload: list[dict], source: str,
                          allowed_years: set[int] | None = None) -> list[JobRecord]:
    """Parse a Simplify-format listings.json (Simplify / vanshb03 / pitt-csc forks).

    Defensive by design: a single malformed entry is skipped rather than aborting the
    whole feed (fail-soft). Skips entries that are not dicts or are missing
    company_name, title, or url; tolerates dirty `locations` (nulls/non-strings) and
    non-numeric `date_posted`. URLs are stored as-is here; canonicalization happens once
    in the pipeline (the single chokepoint before storage), not per adapter.

    Term filtering: when `allowed_years` is given, an entry whose `terms` are ALL outside the
    window (every parsed term's year not in `allowed_years`) is dropped; in-window terms are stored
    on `JobRecord.term`. Untagged entries (no parseable term) are kept with `term=[]`. When
    `allowed_years` is None, nothing is dropped and every parsed term is stored.
    """
    records: list[JobRecord] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        company = _clean_str(entry.get("company_name"))
        title = _clean_str(entry.get("title"))
        url = entry.get("url")
        if not company or not title or not url:
            continue

        raw_terms = entry.get("terms")
        raw_terms = raw_terms if isinstance(raw_terms, list) else []
        parsed = []  # (year, original_string) for each parseable term
        for t in raw_terms:
            pt = parse_term(t)
            if pt:
                parsed.append((pt[1], t.strip()))
        if allowed_years is None:
            in_window = [orig for (_year, orig) in parsed]
        else:
            in_window = [orig for (year, orig) in parsed if year in allowed_years]
            if parsed and not in_window:
                continue  # purely off-window: never stored
        term = list(dict.fromkeys(in_window))  # order-preserving de-dup; [] when untagged

        locations = entry.get("locations")
        if isinstance(locations, list):
            location = ", ".join(str(x) for x in locations if x is not None) or None
        else:
            location = None

        posted_at = None
        epoch = entry.get("date_posted")
        # bool is a subclass of int; exclude it. Require a positive numeric epoch.
        if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and epoch > 0:
            posted_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        active_val = entry.get("active")
        # absent key -> active by default; explicit null -> treat as unknown -> active.
        is_active = bool(active_val) if active_val is not None else True

        records.append(
            JobRecord(
                source=source,
                company=company,
                title=title,
                url=url,
                external_id=entry.get("id"),
                location=location,
                posted_at=posted_at,
                is_active=is_active,
                dedupe_key=make_dedupe_key(company, title),
                term=term,
            )
        )
    return records
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_github_lists.py -q`
Expected: PASS (new term tests + all pre-existing simplify tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/sources/github_lists.py tests/test_github_lists.py
git commit -m "ingest(simplify): parse terms, drop off-window, tag JobRecord.term"
```

---

### Task 6: Thread `allowed_years` from the run entrypoint (`run.py`)

**Files:**
- Modify: `src/jobmaxxing/run.py`
- Test: `tests/test_resilience.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resilience.py`:

```python
def test_build_sources_threads_allowed_years_to_simplify(monkeypatch):
    import jobmaxxing.run as run

    captured = {}
    monkeypatch.setattr(run, "fetch_json", lambda url: [])

    def fake_parse(payload, source, allowed_years=None):
        captured[source] = allowed_years
        return []

    monkeypatch.setattr(run, "parse_simplify_format", fake_parse)

    sources = run.build_sources(watchlist=[], allowed_years={2026})
    for _name, fetch in sources:
        fetch()

    assert captured  # the github sources ran
    assert all(years == {2026} for years in captured.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_resilience.py::test_build_sources_threads_allowed_years_to_simplify -q`
Expected: FAIL — `build_sources() got an unexpected keyword argument 'allowed_years'`.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/run.py`, update the import line to add `current_cycle_years`:

```python
from .normalize import current_cycle_years
```

(Place it with the other `from .` imports, e.g. right after the `from .models import JobRecord` line.)

Update `_github_list_source` to accept and pass `allowed_years`:

```python
def _github_list_source(source: str, url: str, allowed_years):
    def fetch():
        return parse_simplify_format(fetch_json(url), source=source, allowed_years=allowed_years)
    return fetch
```

Update `build_sources` signature and the github loop:

```python
def build_sources(watchlist: list[dict] | None = None, *,
                  allowed_years: set[int] | None = None) -> list[tuple[str, object]]:
    """Assemble (name, fetch_callable) pairs: the GitHub lists plus valid watchlist ATS
    entries. Malformed watchlist entries (not a mapping, missing keys, or unknown ATS)
    are skipped with a warning so one bad config line can never abort the whole run.
    `watchlist` is injectable for testing; defaults to load_watchlist(). `allowed_years`
    is the term window passed through to the Simplify parser (None = no term filtering)."""
    sources: list[tuple[str, object]] = []
    for source, url in GITHUB_LISTS:
        sources.append((source, _github_list_source(source, url, allowed_years)))
```

(The rest of `build_sources` — the watchlist loop — is unchanged.)

In `main()`, compute and pass `allowed_years`:

```python
    now = datetime.now(timezone.utc)
    allowed_years = current_cycle_years(now.date())
    with psycopg.connect(settings.database_url) as conn:
        run_sources(conn, build_sources(allowed_years=allowed_years), now=now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_resilience.py tests/test_integration.py -q`
Expected: PASS (new test + pre-existing build_sources tests, which call `build_sources(watchlist=...)` and still work since `allowed_years` defaults to None).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/run.py tests/test_resilience.py
git commit -m "ingest(run): derive term window from run date, thread to parser"
```

---

### Task 7: Demote legacy rows + term filter (`web/triage.py`)

**Files:**
- Modify: `src/jobmaxxing/web/triage.py`
- Test: `tests/test_web_triage.py`

- [ ] **Step 1: Write the failing tests**

First extend the `_insert` helper at the top of `tests/test_web_triage.py` to accept `source` and `term` (add the two kwargs and the `("term", term)` pair; `term=[]` must still be inserted, so it rides the `is not None` loop):

```python
def _insert(conn, *, dedupe_key, resume_type="swe", status="routed", description="<p>jd</p>",
            company="Acme", title="SWE Intern", scraped_at=None, posted_at=None,
            route_confidence=None, route_method=None, source="github:simplify", term=None):
    cols = ["dedupe_key", "source", "company", "title", "url", "description", "resume_type", "status"]
    vals = [dedupe_key, source, company, title, f"https://x/{dedupe_key}",
            description, resume_type, status]
    for name, value in (("scraped_at", scraped_at), ("posted_at", posted_at),
                        ("route_confidence", route_confidence), ("route_method", route_method),
                        ("term", term)):
        if value is not None:
            cols.append(name)
            vals.append(value)
    placeholders = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({placeholders})", vals)
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])
```

Then append the tests:

```python
def test_legacy_github_rows_demoted(conn):
    older_tagged = _insert(conn, dedupe_key="d|tag", term=["Summer 2026"],
                           posted_at="2026-01-01", route_confidence=0.9)
    newer_legacy = _insert(conn, dedupe_key="d|leg", term=None,
                           posted_at="2026-06-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(older_tagged) < ids.index(newer_legacy)  # legacy sinks despite being newer


def test_ats_null_term_not_demoted_but_github_null_is(conn):
    ats = _insert(conn, dedupe_key="d|ats", source="greenhouse", term=None,
                  posted_at="2026-03-01", route_confidence=0.9)
    gh_legacy = _insert(conn, dedupe_key="d|ghleg", source="github:simplify", term=None,
                        posted_at="2026-03-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(ats) < ids.index(gh_legacy)  # source guard: only github-null demoted


def test_demotion_applies_to_all_sorts(conn):
    a_legacy = _insert(conn, dedupe_key="d|a", company="Aardvark", term=None)
    z_tagged = _insert(conn, dedupe_key="d|z", company="Zzz", term=["Summer 2026"])
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc")]
    assert ids.index(z_tagged) < ids.index(a_legacy)  # Zzz(tagged) before Aardvark(legacy)


def test_filter_by_term_matches_multi_term(conn):
    summer = _insert(conn, dedupe_key="t|s", term=["Summer 2026"])
    coop = _insert(conn, dedupe_key="t|c", term=["Fall 2026", "Spring 2026"])
    fall = _insert(conn, dedupe_key="t|f", term=["Fall 2026"])
    ids = {str(r["id"]) for r in fetch_triage_rows(conn, term="Fall 2026")}
    assert ids == {coop, fall}  # summer-only excluded; the multi-term co-op is included


def test_filter_untagged_matches_empty_array_only(conn):
    _insert(conn, dedupe_key="t|tag", term=["Summer 2026"])
    untagged = _insert(conn, dedupe_key="t|na", term=[])
    _insert(conn, dedupe_key="t|leg", term=None)
    ids = {str(r["id"]) for r in fetch_triage_rows(conn, term="__untagged__")}
    assert ids == {untagged}  # cardinality-0 only; NULL legacy excluded


def test_term_filter_composes_with_count(conn):
    _insert(conn, dedupe_key="t|s2", term=["Summer 2026"])
    _insert(conn, dedupe_key="t|f2", term=["Fall 2026"])
    assert count_triage(conn, term="Fall 2026") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_triage.py -k "demot or term or untagged or all_sorts" -q`
Expected: FAIL — `fetch_triage_rows() got an unexpected keyword argument 'term'` (and demotion not applied).

- [ ] **Step 3: Implement**

In `src/jobmaxxing/web/triage.py`:

Add `term` to the display columns:

```python
_DISPLAY_COLS = (*TRIAGE_COLUMNS, "route_confidence", "term")
```

Add the demotion constant after `RELEVANCE_FLOOR` and rewrite `_order_by`:

```python
# Legacy/off-window github rows (term not yet assigned) sink below everything in EVERY sort —
# hiding them is a visibility rule, not a sort column. ATS rows (term NULL by nature) are exempt
# via the source guard. Fixed string -> no SQL injection.
_DEMOTE = "(source like 'github:%' and term is null) asc"


def _order_by(sort, direction):
    """Build an ORDER BY from the whitelist. Unknown sort -> the 'recent + relevant' default.
    Every ordering is prefixed with _DEMOTE so legacy/off-window github rows stay at the bottom."""
    if sort in _SORTS:
        expr, default_dir, secondary = _SORTS[sort]
        d = direction if direction in ("asc", "desc") else default_dir
        return f"order by {_DEMOTE}, {expr} {d}{secondary}, id asc"
    return (f"order by {_DEMOTE},"
            f" (coalesce(route_confidence, 1.0) < {RELEVANCE_FLOOR}) asc,"
            f" posted_at desc nulls last, id asc")
```

Add the `term` filter to `_build_where` (new last param):

```python
def _build_where(status, statuses, resume_type, term=None):
    """Build the shared WHERE clause + params for fetch/count. Routed jobs only."""
    clauses = ["resume_type is not null"]
    params: list = []
    statuses_list = list(statuses) if statuses is not None else None
    if statuses is not None and not statuses_list:
        raise ValueError("statuses must be non-empty when provided")
    if statuses_list:
        placeholders = ", ".join(["%s"] * len(statuses_list))
        clauses.append(f"status in ({placeholders})")
        params.extend(statuses_list)
    elif status is not None:
        clauses.append("status = %s")
        params.append(status)
    if resume_type is not None:
        clauses.append("resume_type = %s")
        params.append(resume_type)
    if term is not None:
        if term == "__untagged__":
            clauses.append("(term is not null and cardinality(term) = 0)")
        else:
            clauses.append("%s = any(term)")
            params.append(term)
    return " and ".join(clauses), params
```

Thread `term` through both query functions:

```python
def fetch_triage_rows(conn, *, status=None, statuses=None, resume_type=None, term=None,
                      sort=None, direction=None, limit=DEFAULT_LIMIT) -> list[dict]:
    where, params = _build_where(status, statuses, resume_type, term)
    order = _order_by(sort, direction)
    capped = max(1, min(int(limit), MAX_LIMIT))
    sql = f"select {', '.join(_DISPLAY_COLS)} from jobs where {where} {order} limit %s"
    rows = conn.execute(sql, params + [capped]).fetchall()

    result = []
    for row in rows:
        d = dict(zip(_DISPLAY_COLS, row))
        d["description"] = plain_text(d["description"])
        result.append(d)
    return result


def count_triage(conn, *, status=None, statuses=None, resume_type=None, term=None) -> int:
    """Total rows matching the same filters as fetch_triage_rows, ignoring sort/limit."""
    where, params = _build_where(status, statuses, resume_type, term)
    return conn.execute(f"select count(*) from jobs where {where}", params).fetchone()[0]
```

(Update the `fetch_triage_rows` docstring's filter line to mention `term=`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_triage.py -q`
Expected: PASS (new tests + all pre-existing triage tests — the `_DEMOTE` prefix is inert for rows that already have a term or are non-github).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/web/triage.py tests/test_web_triage.py
git commit -m "web(triage): demote legacy github rows + term filter"
```

---

### Task 8: Term column + filter dropdown (`web/server.py`)

**Files:**
- Modify: `src/jobmaxxing/web/server.py`
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Write the failing tests**

First extend the `_insert` helper in `tests/test_web_server.py` exactly as in Task 7 (add `source="github:simplify"` and `term=None` kwargs and the `("term", term)` pair). Then append (the file already has an app/client fixture — reuse whatever the existing tests use; the pattern below assumes a `client` fixture built on `_KeepOpen`, matching the existing server tests):

```python
def test_term_column_rendered(client, conn):
    _insert(conn, dedupe_key="s|tag", term=["Summer 2026", "Fall 2026"])
    _insert(conn, dedupe_key="s|untag", term=[])
    html = client.get("/").get_data(as_text=True)
    assert "Summer 2026, Fall 2026" in html  # array joined in the cell
    assert ">Term<" in html                  # column header present


def test_term_dropdown_lists_distinct_terms_plus_untagged(client, conn):
    _insert(conn, dedupe_key="s|s", term=["Summer 2026"])
    _insert(conn, dedupe_key="s|f", term=["Fall 2026"])
    html = client.get("/").get_data(as_text=True)
    assert 'name="term"' in html
    assert ">Summer 2026<" in html and ">Fall 2026<" in html
    assert 'value="__untagged__"' in html


def test_term_filter_applied(client, conn):
    _insert(conn, dedupe_key="s|s2", term=["Summer 2026"], company="SummerCo")
    _insert(conn, dedupe_key="s|f2", term=["Fall 2026"], company="FallCo")
    html = client.get("/?term=Fall+2026").get_data(as_text=True)
    assert "FallCo" in html and "SummerCo" not in html


def test_term_preserved_in_sort_header_links(client, conn):
    _insert(conn, dedupe_key="s|p", term=["Summer 2026"])
    html = client.get("/?term=Summer+2026").get_data(as_text=True)
    assert "term=Summer+2026" in html  # sort links carry the active term filter
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_server.py -k term -q`
Expected: FAIL — no Term column/dropdown in the HTML; term filter not applied.

- [ ] **Step 3: Implement**

In `src/jobmaxxing/web/server.py`:

**(a)** In the filter `<form>` in `INDEX_HTML`, add a Term `<select>` after the Category `<label>` block (before the closing `</form>`):

```html
    <label>Term
      <select name="term" onchange="this.form.submit()">
        <option value="" {{ 'selected' if term_sel == '' else '' }}>all</option>
        <option value="__untagged__" {{ 'selected' if term_sel == '__untagged__' else '' }}>untagged</option>
        {% for t in term_options %}
        <option value="{{ t }}" {{ 'selected' if t == term_sel else '' }}>{{ t }}</option>
        {% endfor %}
      </select>
    </label>
```

**(b)** Add the Term cell in the row body, immediately after the `resume_type` cell:

```html
    <td>{{ row.resume_type or '' }}</td>
    <td>{{ row.term | join(', ') if row.term else '—' }}</td>
```

**(c)** Update the empty-state `colspan` from `9` to `10`:

```html
  <tr><td colspan="10" style="text-align:center;padding:20px;color:#9ca3af;">No jobs to triage.</td></tr>
```

**(d)** In `_build_headers`, add a `term_sel` param, carry it in `base`, and insert the Term column in the `order` list after `("type", None)`:

```python
def _build_headers(active_sort, active_dir, status_sel, resume_type_sel, term_sel=""):
    """Ordered <th> descriptors. Sortable columns get an href that toggles direction and
    preserves active filters; non-sortable columns get href=None. Order matches the row cells."""
    from urllib.parse import urlencode
    base = {}
    if status_sel:
        base["status"] = status_sel
    if resume_type_sel:
        base["resume_type"] = resume_type_sel
    if term_sel:
        base["term"] = term_sel
    sortable = {}
    for key, label, default_dir in _SORT_HEADERS:
        if active_sort == key:
            new_dir = "asc" if active_dir == "desc" else "desc"
            arrow = " ↑" if active_dir == "asc" else " ↓"
        else:
            new_dir, arrow = default_dir, ""
        sortable[key] = {"label": label,
                         "href": "/?" + urlencode({**base, "sort": key, "dir": new_dir}),
                         "arrow": arrow}
    order = [("company", None), (None, "Title"), ("type", None), (None, "Term"), ("conf", None),
             ("posted", None), (None, "Status"), (None, "JD"), (None, "Link"), (None, "Actions")]
    headers = []
    for key, plain_label in order:
        if key:
            h = sortable[key]
            headers.append({"label": h["label"], "href": h["href"], "arrow": h["arrow"]})
        else:
            headers.append({"label": plain_label, "href": None, "arrow": ""})
    return headers
```

**(e)** In the `index()` route, read `term`, fetch term options, pass everything through:

```python
        status_arg = request.args.get("status") or "undecided"
        resume_type_arg = request.args.get("resume_type") or None
        term_arg = request.args.get("term") or None
        sort_arg = request.args.get("sort") or None
        dir_arg = request.args.get("dir") or None

        status = None
        statuses = None
        if status_arg == "undecided":
            statuses = ("new", "routed")
        elif status_arg == "all":
            pass  # no status filter
        else:
            status = status_arg

        with conn_factory() as conn:
            rows = fetch_triage_rows(conn, status=status, statuses=statuses,
                                     resume_type=resume_type_arg, term=term_arg,
                                     sort=sort_arg, direction=dir_arg)
            total = count_triage(conn, status=status, statuses=statuses,
                                 resume_type=resume_type_arg, term=term_arg)
            cats = [r[0] for r in conn.execute(
                "select distinct resume_type from jobs where resume_type is not null order by 1"
            ).fetchall()]
            term_opts = [r[0] for r in conn.execute(
                "select distinct unnest(term) as t from jobs "
                "where term is not null and cardinality(term) > 0 order by t"
            ).fetchall()]

        for row in rows:
            row["id"] = str(row["id"])

        headers = _build_headers(sort_arg, dir_arg, status_arg, resume_type_arg or "",
                                 term_arg or "")
        return render_template_string(
            INDEX_HTML, rows=rows, headers=headers, total=total, shown=len(rows),
            status_options=_STATUS_OPTIONS, status_sel=status_arg,
            categories=cats, resume_type_sel=(resume_type_arg or ""),
            term_options=term_opts, term_sel=(term_arg or ""),
            active_sort=(sort_arg or ""), active_dir=(dir_arg or ""),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_web_server.py -q`
Expected: PASS (new term tests + all pre-existing server tests — note the empty-state colspan and header count changed, so confirm any header-count assertion in existing tests still holds; if an existing test counts headers it must expect 10).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/web/server.py tests/test_web_server.py
git commit -m "web(server): Term column + filter dropdown"
```

---

### Task 9: Full-suite regression + final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the entire test suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass (pre-existing skips for external-service tests are fine).

- [ ] **Step 2: Sanity-check the migration is idempotent**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_store.py::test_apply_migrations_creates_jobs_table -q`
Expected: PASS (migrations re-run cleanly; `add column if not exists` is a no-op on second run).

- [ ] **Step 3: Confirm no stray failures or warnings**

Review the summary line; if anything fails, STOP and fix before review/merge.

---

## Self-Review

**Spec coverage:**
- §1 date-derived window → Task 1 (`current_cycle_years`, `CYCLE_LOOKAHEAD_MONTH`). ✓
- §2 parse + classify + drop → Task 1 (`parse_term`) + Task 5 (classification/drop). ✓
- §3 `JobRecord.term` + migration + store + merge → Tasks 2, 3, 4. ✓
- §4 demote legacy/off-window in all sorts (ATS exempt) → Task 7. ✓
- §5 term column + filter → Tasks 7 (filter plumbing) + 8 (UI). ✓
- §6 self-heal / no backfill → no code (asserted by Task 4's re-ingest test + migration comment). ✓
- Wiring (run.main → build_sources → parser) → Task 6. ✓

**Type consistency:** `JobRecord.term: list[str] | None` used identically in models, store (`rec.term`), merge (`primary.term`/`secondary.term`), parser (`term=list(...)`), triage (`term is null` / `any(term)` / `cardinality(term)`), server (`row.term | join`). `allowed_years: set[int] | None` consistent across `current_cycle_years` return, `parse_simplify_format` param, `build_sources` param, `_github_list_source` param. `fetch_triage_rows`/`count_triage`/`_build_where` all take `term`. ✓

**Placeholder scan:** every code step shows full code; commands have expected output. ✓

---

## Execution Handoff

Subagent-driven TDD, sequential (tasks are coupled), two-stage review (spec compliance → code quality) after each task, then merge to `main`.
