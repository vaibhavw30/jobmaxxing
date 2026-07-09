# Workday `myworkdaysite.com` coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the existing Workday enrichment worker to also recognize `myworkdaysite.com` job URLs (a second, functionally-equivalent Workday public domain), so the 126 live jobs currently sitting on it — 100% description-less today — become enrichable through the existing, unmodified tiered fetch pipeline.

**Architecture:** Add a second regex (`_WORKDAY_SITE_RE`) alongside the existing `_WORKDAY_RE`, both feeding a single shared match helper that extracts the same `(tenant, wd, site, rest)` identity regardless of which URL shape matched. `workday_host`/`workday_cxs_url` build off that shared helper — no duplicated cxs-URL logic. The SQL candidate query gains one additional host alternation. Everything downstream (tiered fetch, classification, outcome writer) is untouched.

**Tech Stack:** Python 3.12, stdlib `re`. No new dependency.

## Global Constraints
- Python 3.12. Run pytest with the Postgres binary on PATH for the integration test:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- **The stored `jobs.url` column is NEVER rewritten.** A `myworkdaysite.com` row keeps its exact scraped
  URL forever; only the in-memory fetch target (the derived cxs URL) changes. This is an explicit,
  already-made decision — do not introduce any code path that writes a different URL back to the row.
- **`fetch_workday_one`, `_apply_outcomes`, the tiered fetch (plain → cleared-context → render), and the
  Cloudflare-block/not-found/transient classification are UNCHANGED.** This task only extends URL
  *recognition* (which strings map to which cxs endpoint); it does not touch fetch or classification logic.
- **No duplicated cxs-URL-construction logic** between the two recognized shapes — one shared code path
  builds the host string / cxs URL from whichever regex matched.
- A `myworkdaysite.com` URL and its equivalent `myworkdayjobs.com` URL (same tenant/wd/site/rest) MUST
  produce the identical `workday_host()`/`workday_cxs_url()` output — this is the sharding/Cloudflare-
  clearance-reuse invariant from the spec.
- Worktree cwd discipline: pin every subagent's cwd to the feature worktree; verify
  `git rev-parse --show-toplevel` before any commit. Push with the `vaibhavw30` gh account.

---

## Task 1: Recognize `myworkdaysite.com` URLs + extend candidate selection

**Files:**
- Modify: `src/jobmaxxing/enrichment/workday.py`
- Test: `tests/test_workday_unit.py`, `tests/test_workday_db.py`

**Interfaces:**
- Produces: `workday_host(url: str) -> str | None` and `workday_cxs_url(url: str) -> str | None` now
  recognize BOTH URL shapes (unchanged signatures, unchanged output for existing `myworkdayjobs.com`
  inputs). `enrich_workday()`'s candidate SQL now also selects `myworkdaysite.com` rows. No new public
  functions are required by callers outside this module — `fetch_workday_one`/`enrich_workday` call
  `workday_host`/`workday_cxs_url` exactly as they do today.

- [ ] **Step 1: Write failing unit tests for the new URL shape.** Append to `tests/test_workday_unit.py`
(these use two real, empirically-verified live postings — Magna with no locale prefix, Parexel with an
`en-US` locale prefix — plus a cross-shape equivalence check and a non-match check):
```python
def test_myworkdaysite_cxs_url_basic():
    u = "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    assert workday_cxs_url(u) == (
        "https://magna.wd3.myworkdayjobs.com/wday/cxs/magna/Magna/job/"
        "Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    )

def test_myworkdaysite_cxs_url_strips_locale_prefix():
    u = ("https://wd1.myworkdaysite.com/en-US/recruiting/parexel/Parexel_External_Careers/"
         "job/United-Kingdom-Sheffield-Remote/Intern_R0000038395-1")
    assert workday_cxs_url(u) == (
        "https://parexel.wd1.myworkdayjobs.com/wday/cxs/parexel/Parexel_External_Careers/"
        "job/United-Kingdom-Sheffield-Remote/Intern_R0000038395-1"
    )

def test_myworkdaysite_host():
    u = "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    assert workday_host(u) == "magna.wd3.myworkdayjobs.com"

def test_myworkdaysite_and_myworkdayjobs_same_identity_are_equivalent():
    # Same tenant/wd/site/rest via the two different public-domain shapes -> identical
    # host/cxs output. This is the sharding + Cloudflare-clearance-reuse invariant.
    site_url = "https://wd2.myworkdaysite.com/recruiting/acme/Careers/job/NYC/Intern_R1"
    jobs_url = "https://acme.wd2.myworkdayjobs.com/Careers/job/NYC/Intern_R1"
    assert workday_host(site_url) == workday_host(jobs_url) == "acme.wd2.myworkdayjobs.com"
    assert workday_cxs_url(site_url) == workday_cxs_url(jobs_url) == (
        "https://acme.wd2.myworkdayjobs.com/wday/cxs/acme/Careers/job/NYC/Intern_R1"
    )

def test_myworkdaysite_missing_recruiting_segment_is_none():
    # Not the recognized shape (no "recruiting/" path segment) -> unrecognized, not mangled.
    u = "https://wd2.myworkdaysite.com/acme/Careers/job/NYC/Intern_R1"
    assert workday_host(u) is None
    assert workday_cxs_url(u) is None
```
Run: `uv run pytest tests/test_workday_unit.py -q` → FAIL (the new shape isn't recognized yet — the two
"myworkdaysite" cxs/host tests fail, the equivalence test fails, the missing-recruiting test already
passes trivially since it's unrecognized either way, but keep it as a locked-in regression guard).

- [ ] **Step 2: Implement.** In `src/jobmaxxing/enrichment/workday.py`, replace the current block:
```python
# https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
_WORKDAY_RE = re.compile(
    r"https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"            # optional locale prefix, stripped
    r"(?P<site>[^/]+)/job/(?P<rest>.+)$"
)


def workday_host(url: str) -> str | None:
    m = _WORKDAY_RE.match(url)
    return f"{m.group('tenant')}.{m.group('wd')}.myworkdayjobs.com" if m else None


def workday_cxs_url(url: str) -> str | None:
    """Translate a Workday job URL to its cxs JSON endpoint, or None if unrecognized."""
    m = _WORKDAY_RE.match(url)
    if not m:
        return None
    t, wd, site, rest = m.group("tenant"), m.group("wd"), m.group("site"), m.group("rest")
    return f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/job/{rest}"
```
with:
```python
# https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
_WORKDAY_RE = re.compile(
    r"https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"            # optional locale prefix, stripped
    r"(?P<site>[^/]+)/job/(?P<rest>.+)$"
)

# https://{wd}.myworkdaysite.com/[xx-XX/]recruiting/{tenant}/{site}/job/{rest}
# A second, functionally-equivalent Workday public domain: same cxs API, tenant lives in the
# path instead of the hostname. Verified live (2026-07-08): the derived cxs URL for real
# myworkdaysite.com postings (Magna, Snap, Microchip Technology) returns the identical
# Cloudflare-gate error shape a normal myworkdayjobs.com tenant returns from an uncleared
# context -- i.e. the SAME endpoint, reachable via the SAME tiered fetch below.
_WORKDAY_SITE_RE = re.compile(
    r"https://(?P<wd>wd\d+)\.myworkdaysite\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"             # optional locale prefix, stripped
    r"recruiting/(?P<tenant>[^/]+)/(?P<site>[^/]+)/job/(?P<rest>.+)$"
)


def _match_workday(url: str) -> dict | None:
    """Try both recognized Workday URL shapes; return {tenant, wd, site, rest} from whichever
    matches, or None. Both shapes resolve to the identical downstream identity/cxs URL."""
    m = _WORKDAY_RE.match(url)
    if m:
        return m.groupdict()
    m = _WORKDAY_SITE_RE.match(url)
    return m.groupdict() if m else None


def workday_host(url: str) -> str | None:
    g = _match_workday(url)
    return f"{g['tenant']}.{g['wd']}.myworkdayjobs.com" if g else None


def workday_cxs_url(url: str) -> str | None:
    """Translate a Workday job URL (either recognized public-domain shape) to its cxs JSON
    endpoint, or None if unrecognized."""
    g = _match_workday(url)
    if not g:
        return None
    return (f"https://{g['tenant']}.{g['wd']}.myworkdayjobs.com/wday/cxs/"
            f"{g['tenant']}/{g['site']}/job/{g['rest']}")
```
Note: this is a straight superset — every existing `_WORKDAY_RE` match produces byte-identical output to
before (same group names, same f-string), so no existing test can regress from this change alone.

- [ ] **Step 3: Run the unit tests** to confirm GREEN, plus the full existing unit file (no regression):
Run: `uv run pytest tests/test_workday_unit.py -q` → PASS (all, including every pre-existing test).

- [ ] **Step 4: Write a failing integration test for candidate selection.** Append to
`tests/test_workday_db.py` (reuses the file's existing `conn` fixture, `_insert` helper, `_OkFetcher`):
```python
def test_enrich_workday_selects_myworkdaysite_candidates(conn):
    _insert(conn, dedupe_key="wd_jobs", url=_WD.format(tenant="acme", n=30))
    _insert(conn, dedupe_key="wd_site",
            url="https://wd3.myworkdaysite.com/recruiting/acme/Careers/job/NYC/Intern_R31")
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher)
    assert counts["candidates"] == 2
    assert counts["enriched"] == 2
    row = conn.execute("select description from jobs where dedupe_key='wd_site'").fetchone()
    assert row[0] == "<p>A real Workday JD with enough words</p>"
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_workday_db.py -q`
→ FAIL (the `myworkdaysite.com` row isn't selected by the candidate SQL yet — `candidates` is 1, not 2).

- [ ] **Step 5: Extend the candidate SQL.** In `src/jobmaxxing/enrichment/workday.py`, inside
`enrich_workday()`, change the query's URL filter line from:
```python
        "and url ~* 'myworkdayjobs\\.com' "
```
to:
```python
        "and url ~* 'myworkdayjobs\\.com|myworkdaysite\\.com' "
```
(the rest of the query string is unchanged).

- [ ] **Step 6: Run the DB test file + the full suite** (no regression):
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_workday_db.py tests/test_workday_unit.py -q` → PASS.
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → PASS (full suite,
baseline 576 passed + 6 new tests = 582 passed, 8 skipped; no regressions).

- [ ] **Step 7: Commit** (verify worktree cwd first — `git rev-parse --show-toplevel`):
```bash
git add src/jobmaxxing/enrichment/workday.py tests/test_workday_unit.py tests/test_workday_db.py
git commit -m "enrich(workday): recognize myworkdaysite.com URLs alongside myworkdayjobs.com

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green,
   582 passed, 8 skipped.
2. `workday_cxs_url`/`workday_host` produce identical output for a `myworkdaysite.com` URL and its
   hand-constructed `myworkdayjobs.com` equivalent (Task 1 Step 1's equivalence test — the
   sharding/clearance invariant).
3. `enrich_workday()`'s candidate query selects both URL shapes (Task 1 Step 4's integration test).
4. Optional live check (operator, residential IP, real DB, needs the `headless` extra): run
   `python -m jobmaxxing.enrich_workday` and confirm previously-0%-enriched `myworkdaysite.com` rows
   (e.g. the Magna/Snap/Microchip Technology/Western Alliance/Parexel postings) start enriching.

## Risks & notes
- **No new fetch/classification code** — this task is pure URL-recognition surface area. If a
  `myworkdaysite.com` posting fails to enrich, the failure mode is identical to any `myworkdayjobs.com`
  posting failing (Cloudflare-block escalation exhausted, 404, timeout) — nothing new to debug.
- **Coverage is bounded to the one shape observed live** (`.../recruiting/{tenant}/{site}/job/{rest}`,
  with or without a locale prefix). If Workday exposes yet another public-domain variant later, add it the
  same way (a third regex feeding the same `_match_workday` helper) — out of scope here.
- **Stored URL identity is untouched** — no dedupe/canonicalization behavior changes; this was an explicit
  operator decision (2026-07-08).

## Execution
Isolated git worktree off `main`; subagent-driven TDD (single task — small and cohesive enough not to
need decomposition, per the spec's own Execution note); one review pass (spec + quality); full-suite
green; merge to `main`; push (gh `vaibhavw30`).
