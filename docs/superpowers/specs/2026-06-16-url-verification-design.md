# URL verification — design

## Context
Some job postings carry a URL that no longer resolves (the role was pulled, the ATS link expired).
Today nothing checks this: `is_active` comes purely from the source feed's own `active` flag
(`sources/github_lists.py`, `sources/ats.py`, `merge.py`) — never from whether the link actually
works. The triage table's "Open posting" link can therefore point at a dead page.

There IS reusable machinery. The recovery module ([recovery/](../../../src/jobmaxxing/recovery))
already, for gated Workday rows, searches DuckDuckGo and extracts schema.org `JobPosting` data to
recover **JD text** — but it never stores an alternative **URL**. Its pieces are directly reusable to
*find a posting elsewhere*:
- `recovery.search.ddg_search(query, *, fetch_text)` → candidate result URLs;
- `recovery.search.build_query(company, title)`;
- `recovery.extract.extract_job_posting(html, *, source_url)` → `JobPosting` (`.url`, `.source_url`,
  `.company`, `.title`, `.identifier`);
- `recovery.match.match_job(job, cand, *, llm_confirm)` → `MatchResult.accepted` (req-id / backlink /
  LLM confirmation — so we only trust a candidate that's provably the *same* job);
- `recovery.extract.workday_req_id(url)`.

The retry-cap pattern (`enrich_attempts`/`recover_attempts` + a candidate query gated on
`attempts < cap`, with permanent/transient/success batched writes) is the template to mirror.

## Goal
A `verify_url` stage that, for the jobs you actually triage, confirms the posting URL resolves; when
it doesn't, finds and stores a working alternative posting URL (LinkedIn / job board / ATS) — and when
no alternative can be confidently found, marks the link dead and flags + demotes it in triage. Built as
a clean, idempotent, env-configured CLI stage so it ports to AWS unchanged.

## Decisions (from brainstorm)
- **Scope:** the in-window, routed, undecided jobs (the triage backlog) — not the whole table.
- **Find-alternative:** reuse the recovery engine; only store a found URL if it's confidently matched
  AND itself resolves.
- **Dead & unrecoverable:** keep `is_active` untouched; record `url_status='dead'`, show a "⚠ dead
  link" marker in triage and demote the row to the bottom (joining the off-window tier) — kept visible.
- **Detection depth (v1):** status-code only — `404`/`410` (and unresolvable) are dead; `403/429/5xx`/
  timeouts are transient retries. "200-but-says-closed" content detection is an explicit fast-follow.
- **Execution:** runs locally like `recover_jd` (DuckDuckGo rate-limits datacenter IPs). Still a clean
  CLI stage; liveness-only-in-CI is a possible future split.

## Design

### 1. Schema (migration `0010_url_verification.sql`)
```sql
alter table jobs add column if not exists url_status     text;        -- null=unverified, 'alive', 'dead'
alter table jobs add column if not exists verify_attempts int not null default 0;
alter table jobs add column if not exists verified_at     timestamptz;
alter table jobs add column if not exists verify_error    text;
```
Idempotent (`add column if not exists`), no backfill — every row starts unverified (`url_status` NULL).

### 2. Liveness check (`verification/liveness.py`)
```python
@dataclass
class Liveness:
    kind: str           # 'alive' | 'dead' | 'transient'
    status: int | None
    error: str | None

def check_liveness(url, *, fetcher) -> Liveness
```
- `fetcher(url) -> int` performs a GET with `follow_redirects=True` and returns the final status code
  (raises on network/timeout). Injectable; the default uses `httpx` (GET, not HEAD — many ATS reject
  HEAD), a browser-ish User-Agent (reuse recovery's `_HEADERS`), and a ~15s timeout.
- Classify: `200 ≤ s < 400` → **alive**; `404` or `410` → **dead**; anything else (`403/429/5xx`/
  raise) → **transient**.

### 3. Find-alternative (`verification/find.py`)
Mirrors `recovery._recover_one` but returns a *URL* instead of a description:
```python
def find_alternative_url(company, title, original_url, *, searcher, fetcher, llm_confirm) -> str | None:
    job = {"company": company, "title": title, "url": original_url,
           "req_id": workday_req_id(original_url)}
    for result_url in searcher(build_query(company, title), fetch_text=fetcher):
        try:
            cand = extract_job_posting(fetcher(result_url), source_url=result_url)
        except Exception:
            continue
        if cand and match_job(job, cand, llm_confirm=llm_confirm).accepted:
            return cand.url or cand.source_url or result_url
    return None
```
The returned URL is then liveness-checked before it's trusted (a matched-but-dead candidate is
rejected). `searcher`/`fetcher`/`llm_confirm` default to the recovery implementations and are injected
in tests.

### 4. Orchestrator (`verification/verify.py`)
`verify_urls(conn, *, now, cap=3, max_jobs=200, reverify_days=14, liveness_fetcher, find_alt) -> dict`

- **Candidate query** — the triage backlog, stalest-first, re-checked every `reverify_days` since links
  rot:
  ```sql
  select id, url, alt_urls, company, title from jobs
  where resume_type is not null
    and status in ('new','routed')
    and not ({off_window_sql(in_window_term_labels(now.date()))})
    and verify_attempts < %s
    and (verified_at is null or verified_at < %s)        -- now - reverify_days
  order by verified_at asc nulls first, scraped_at desc
  limit %s
  ```
- **Per candidate** (thread-pooled like recovery):
  1. `check_liveness(url)`.
  2. **alive** → outcome `alive` (no URL change).
  3. **dead** → check each `alt_url` in turn; the first **alive** one is promoted. If none, call
     `find_alt(company, title, url)`; if it returns a URL that is itself **alive**, promote it.
     Promotion = new `url` = the working link, and the old `url` (+ any other alts) folded into
     `alt_urls` (order-preserving dedup, excluding the new primary — same rule as `merge.alt_urls`).
     If nothing resolves → outcome `dead`.
  4. **transient** → outcome `transient`.
- **Batched writes** (one transaction, mirroring enrich/recover):
  - alive (no promotion): `set url_status='alive', verified_at=%s, verify_error=null`.
  - alive (promotion): `set url=%s, alt_urls=%s, url_status='alive', verified_at=%s, verify_error=null`.
  - dead: `set url_status='dead', verify_attempts=%s (cap), verified_at=%s, verify_error=%s` (capped so
    it isn't reselected until the next operator decides to bump the cap).
  - transient: `set verify_attempts=verify_attempts+1, verify_error=%s` (retried until cap).
- Returns `{alive, promoted, dead, transient, candidates}`; logs a summary.

### 5. CLI (`verify_url.py` shim) + execution
`python -m jobmaxxing.verify_url` → `verification.verify.main` (loads settings, `now=utcnow`, connects,
calls `verify_urls`, prints counts). **Local-only** operator stage — documented in the README next to
`recover_jd`/`enrich_workday`; *not* added to `pollers.yml` (DuckDuckGo blocks datacenter IPs).

### 6. Triage surfacing (`web/triage.py`, `web/server.py`)
- `verify.url_status` joins `_DISPLAY_COLS`.
- **Demotion**: extend `_demote_clause` so dead rows sink with the off-window tier:
  `({off_window_sql(labels)} or url_status = 'dead') asc`. (Dead applies to all sources, so it's a
  separate disjunct from the github-only off-window predicate.)
- **Marker**: in the row template, when `url_status='dead'` render a "⚠ dead link" badge next to the
  "Open posting" link (which still points at the best URL we have). No new filter in v1.

## Components & data flow
- `migrations/0010_url_verification.sql` — the four columns.
- `src/jobmaxxing/verification/__init__.py`
- `src/jobmaxxing/verification/liveness.py` — `check_liveness`, default httpx fetcher.
- `src/jobmaxxing/verification/find.py` — `find_alternative_url` (reuses recovery search/extract/match).
- `src/jobmaxxing/verification/verify.py` — `verify_urls` orchestrator + `main`.
- `src/jobmaxxing/verify_url.py` — CLI shim (`from .verification.verify import main`).
- `src/jobmaxxing/web/triage.py` — `url_status` in display cols + demotion disjunct.
- `src/jobmaxxing/web/server.py` — dead-link marker in the row template.
- `README.md` — a "URL verification (local)" subsection.

## Testing (TDD)
- `tests/test_verify_liveness.py`: `check_liveness` classifies 200/301→alive, 404/410→dead,
  403/429/500/raise→transient, with a fake fetcher.
- `tests/test_verify_find.py`: `find_alternative_url` returns the matched candidate's URL with injected
  `searcher`/`fetcher`/`llm_confirm`; returns None when nothing matches; skips unparseable candidates.
- `tests/test_verify_db.py` (pytest-postgresql), injecting a fake liveness fetcher + fake `find_alt`:
  - alive URL → `url_status='alive'`, `verified_at` set, URL unchanged.
  - dead URL with a live `alt_url` → alt promoted to `url`, old URL in `alt_urls`, status alive.
  - dead URL, no live alt, `find_alt` returns a matched **alive** URL → promoted; status alive.
  - dead URL, `find_alt` returns a URL that is itself dead → not promoted; `url_status='dead'`.
  - dead URL, `find_alt` returns None → `url_status='dead'`, `verify_attempts=cap`.
  - transient → `verify_attempts` incremented only; no status/url change.
  - candidate query excludes off-window, decided, recently-verified (`verified_at` fresh), and
    `verify_attempts >= cap` rows.
- `tests/test_web_triage.py`: a `url_status='dead'` row is demoted below an alive in-window row, in all
  sorts; an alive row is unaffected.
- `tests/test_web_server.py`: the "⚠ dead link" marker renders for a dead row and not for an alive one.

## Out of scope
"200-but-says-closed" content detection (fast-follow); verifying off-window / decided / non-triaged
rows; running verification in CI (local-only for now); changing `is_active`; any new triage filter for
url_status; the Dockerfile / AWS migration (separate spec). `recover_jd` is unchanged — verification and
JD-recovery stay separate stages (different candidate sets, different writes).

## Execution
Isolated worktree off `main` (`worktree-url-verification`); strict TDD; final review; merge to `main`.
`export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before `uv run pytest`. After this lands, a
separate small spec covers the **Dockerfile** groundwork (containerize the core pipeline with config
baked in) so July's AWS move is drop-in.
