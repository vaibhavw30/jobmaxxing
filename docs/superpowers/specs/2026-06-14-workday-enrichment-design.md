# Spec — Workday JD Enrichment (self-hosted headless)

**Type:** New feature (Phase 5b) — a local, operator-run enrichment worker
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** JD enrichment (Phase 5a, merged) — reuses its `enrich_*` schema columns, attempt-cap, and permanent/transient classification. Adds a separate **local** worker; the CI pipeline is untouched.

---

## 1. Problem & rationale

The clean-API enrichment (Phase 5a) deliberately skipped Workday because its job-description API is Cloudflare-gated. Workday is the single largest unreachable source: **6,922 description-less rows across 995 distinct tenants** (a long tail — the biggest tenant is 172 rows). Until these get descriptions, the LLM router defers all of them.

### Measured reachability (against the live backlog, 2026-06-14)

Every Workday job exposes a JSON endpoint — given a job URL
`https://{tenant}.{wd}.myworkdayjobs.com/[{locale}/]{site}/job/{path}`
the description lives at
`https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}` → `jobPostingInfo.jobDescription` (HTML).

But access is **tiered**, confirmed by probing real tenants:

| Tier | Method | Result | Approx. population |
| --- | --- | --- | --- |
| 0 | Plain HTTP GET of the cxs URL (browser-like headers) | `200` for a minority (e.g. PSU: 6,109-char JD); **`403` Cloudflare for ~85%** | ~15% (mostly edu/gov) |
| — | Chrome TLS-impersonation (`curl_cffi`) | Still `403` (26/30) — Cloudflare runs an **active JS challenge**, not passive fingerprinting | 0 extra |
| 1 | Headless Chromium clears Cloudflare on the tenant domain, then calls cxs through the cleared context | `200` for many gated tenants (e.g. **leidos: 200, 4,369-char JD**) | a large chunk of the 85% |
| 2 | Headless loads the actual job page; Workday's SPA fires its own cxs call; intercept that `200` response | Handles the stubbornest tenants (e.g. micron, which `403`s even Tier 1) | most of the remainder |
| — | Hard tail | A few tenants may resist even Tier 2 | accepted as unreachable, attempt-capped |

**Conclusion:** plain HTTP and TLS tricks are insufficient; the gated majority needs a JavaScript-executing browser. A **self-hosted headless worker on a residential IP** is the right tool — residential IPs are far less aggressively challenged by Cloudflare than datacenter IPs (GitHub runners), and it incurs no per-request cost.

### Goal
A local CLI worker, `python -m jobmaxxing.enrich_workday`, that fills `description` for Workday rows using a per-job **tiered fetch** (plain → headless-context → headless-render), reusing the existing enrichment schema and write/classification machinery. The CI pipeline does not change. Coverage is best-effort and additive; the reliable core is unaffected.

### Scope
**In:** a new `enrichment/workday.py` module (URL→cxs translation, payload parsing, tier dispatch), an injectable `WorkdayFetcher` interface with a real `PlaywrightFetcher` implementation (per-host Cloudflare-clearance reuse + bounded concurrency), the `enrich_workday(conn, ...)` worker, a CLI entrypoint, Playwright as a **local-only optional dependency**, a full test pyramid, and a validation spike.
**Out:** any change to the CI pollers/enrich/route pipeline; iCIMS and bespoke long-tail sources; paid CAPTCHA/scraping services; adding Workday to the shared `ADAPTERS`/`SUPPORTED_HOSTS_SQL` (deliberately — see §4).

## 2. Why Workday is owned entirely by the local worker (not the CI adapter registry)

A tempting shortcut — add a `WorkdayAdapter` to the CI enrich step for the cheap ~15% — has a **silent correctness bug**: the gated 85% would return `403`, which the CI engine classifies as **permanent**, setting `enrich_attempts = cap`. Those rows would then be permanently excluded from *every* candidate query — so the headless worker would **never see them**. We would lock ourselves out of the majority to grab the minority.

Therefore Workday is handled **only** by the local worker, which does the cheap Tier-0 attempt *itself* (capturing the ~15% with no browser) before escalating. The CI `enrich_new` and its `SUPPORTED_HOSTS_SQL` regex are left exactly as they are; CI never selects a Workday row, never installs a browser, and the reliable core stays decoupled from the operator's machine being online.

## 3. Architecture

```
                 (CI, unchanged)                         (local, operator-run)
 poll → enrich_new (GH/Lever/Ashby/SR) → route        python -m jobmaxxing.enrich_workday
                      │                                         │
                      └── writes description/enrich_* ──────────┴──> same Supabase `jobs` table
```

New module `src/jobmaxxing/enrichment/workday.py` — pure, browser-free logic:
- `workday_cxs_url(url) -> str | None` — translate a job URL to its cxs endpoint (strip optional locale).
- `workday_host(url) -> str | None` — the `{tenant}.{wd}.myworkdayjobs.com` host (the Cloudflare-clearance key).
- `parse_workday(payload) -> str | None` — pull `jobPostingInfo.jobDescription`, or `None`.
- `WorkdayFetcher` (Protocol) + typed exceptions `WorkdayBlocked` / `WorkdayNotFound` / `WorkdayTransient`.
- `fetch_workday_one(job_id, url, fetcher) -> Outcome` — the **pure tier-dispatch** (Tier 0 → 1 → 2 → classify). Fetcher-agnostic, fully unit-testable.

New module `src/jobmaxxing/enrichment/playwright_fetcher.py` — the real `WorkdayFetcher`:
- Lazy `import playwright` (so importing the package in CI, where Playwright is absent, never fails).
- `fetch_plain` (Tier 0, httpx, no browser), `fetch_via_context` (Tier 1), `fetch_via_render` (Tier 2).
- A per-host **cleared-context cache** (clear Cloudflare once per tenant host, reuse for all that tenant's jobs) — the Workday analog of the Ashby board cache.

New module/function `enrich_workday(conn, *, max_jobs, max_workers, cap, fetcher_factory)` — the worker: candidate query → group by tenant → bounded-concurrent tier dispatch → batched write. Lives in `workday.py` (or `enrichment/workday_runner.py` if `workday.py` grows large; the plan decides at file-size review).

New CLI shim `src/jobmaxxing/enrich_workday.py` — mirrors `src/jobmaxxing/route.py`.

Shared-write refactor: extract `_apply_outcomes(conn, outcomes, *, cap)` from the existing `enrich_new` so both the CI engine and the Workday worker write outcomes identically (DRY). Behavior-preserving; covered by characterization tests.

### 3.1 `workday.py` — pure logic (with code)

```python
import re
from typing import Protocol

# https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
_WORKDAY_RE = re.compile(
    r"https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"            # optional locale prefix (e.g. en-US/), stripped
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


def parse_workday(payload: dict) -> str | None:
    """Extract the (HTML) job description from a cxs payload, or None if absent."""
    jd = (payload or {}).get("jobPostingInfo", {}).get("jobDescription")
    return jd or None


class WorkdayBlocked(Exception):
    """Cloudflare/anti-bot blocked this fetch (403/challenge). Escalate a tier; if all
    tiers are blocked, classify transient (a future run / tenant change may succeed)."""


class WorkdayNotFound(Exception):
    """The posting is gone (404, or the SPA never fired a job cxs call). Permanent."""


class WorkdayTransient(Exception):
    """Timeout, connection error, or browser crash. Retry next run until the cap."""


class WorkdayFetcher(Protocol):
    def fetch_plain(self, cxs_url: str) -> dict: ...          # Tier 0 (no browser)
    def fetch_via_context(self, host: str, cxs_url: str) -> dict: ...  # Tier 1
    def fetch_via_render(self, job_url: str) -> dict: ...     # Tier 2
```

### 3.2 Tier dispatch — pure, testable (with code)

Reuses the `Outcome` dataclass from `enrichment/enrich.py` (`Outcome(job_id, kind, description, error)`).

```python
from .enrich import Outcome


def _outcome_from_payload(job_id, payload) -> Outcome:
    desc = parse_workday(payload)
    if not desc:
        return Outcome(job_id, "permanent", None, "no description in workday payload")
    return Outcome(job_id, "enriched", desc, None)


def fetch_workday_one(job_id, url: str, fetcher: WorkdayFetcher) -> Outcome:
    """Plain -> headless-context -> headless-render, classifying as it escalates.
    Pure w.r.t. the DB and the browser; all errors are isolated into an Outcome."""
    cxs = workday_cxs_url(url)
    if cxs is None:
        return Outcome(job_id, "permanent", None, f"unrecognized workday url: {url}")
    host = workday_host(url)

    for tier in (
        lambda: fetcher.fetch_plain(cxs),
        lambda: fetcher.fetch_via_context(host, cxs),
        lambda: fetcher.fetch_via_render(url),
    ):
        try:
            return _outcome_from_payload(job_id, tier())
        except WorkdayNotFound as exc:
            return Outcome(job_id, "permanent", None, str(exc))   # posting gone -> stop, permanent
        except WorkdayTransient as exc:
            return Outcome(job_id, "transient", None, str(exc))   # network/timeout -> stop, retry later
        except WorkdayBlocked:
            continue                                              # escalate to the next tier
    return Outcome(job_id, "transient", None, "blocked at all tiers (cloudflare unsolved)")
```

The dispatch is a plain function over a `WorkdayFetcher` — a `FakeFetcher` raising the typed exceptions in any pattern drives every branch in unit tests, with zero browser or network.

### 3.3 `PlaywrightFetcher` — the real fetcher (with code)

```python
import httpx
from .workday import WorkdayBlocked, WorkdayNotFound, WorkdayTransient, workday_host

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _classify_status(status: int):
    if status == 200:
        return None
    if status in (403, 429, 503):
        raise WorkdayBlocked(f"status {status}")
    if status in (404, 410):
        raise WorkdayNotFound(f"status {status}")
    raise WorkdayTransient(f"status {status}")


# Cloudflare interstitial titles ("Just a moment...", "Attention Required!", etc.).
_CHALLENGE_MARKERS = ("just a moment", "attention required", "checking your browser", "cloudflare")


def _looks_like_challenge(page_title: str) -> bool:
    t = (page_title or "").lower()
    return any(marker in t for marker in _CHALLENGE_MARKERS)


class PlaywrightFetcher:
    """One browser instance + a per-host Cloudflare-cleared context cache.

    NOT thread-safe (Playwright sync objects belong to their creating thread). The worker
    gives each pool thread its OWN PlaywrightFetcher via fetcher_factory; jobs are sharded
    by tenant so one tenant's clearance is established once and reused within that thread.
    """

    def __init__(self, *, headless: bool = True, settle_ms: int = 5000, nav_timeout_ms: int = 45000):
        from playwright.sync_api import sync_playwright  # lazy: CI never imports playwright
        self._settle_ms, self._nav_timeout_ms = settle_ms, nav_timeout_ms
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._contexts: dict[str, object] = {}   # host -> CF-cleared BrowserContext
        self._http = httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    # Tier 0 -------------------------------------------------------------
    def fetch_plain(self, cxs_url: str) -> dict:
        try:
            r = self._http.get(cxs_url)
        except httpx.HTTPError as exc:
            raise WorkdayTransient(f"plain: {exc}") from exc
        _classify_status(r.status_code)
        return r.json()

    # Cloudflare clearance (memoized per host) ---------------------------
    def _cleared_context(self, host: str):
        if host not in self._contexts:
            ctx = self._browser.new_context(user_agent=_UA, locale="en-US")
            page = ctx.new_page()
            try:
                page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                page.wait_for_timeout(self._settle_ms)  # let the CF JS challenge resolve
            finally:
                page.close()
            self._contexts[host] = ctx
        return self._contexts[host]

    # Tier 1 -------------------------------------------------------------
    def fetch_via_context(self, host: str, cxs_url: str) -> dict:
        ctx = self._cleared_context(host)
        r = ctx.request.get(cxs_url, headers={"Accept": "application/json"})
        _classify_status(r.status)
        return r.json()

    # Tier 2 -------------------------------------------------------------
    def fetch_via_render(self, job_url: str) -> dict:
        ctx = self._cleared_context(workday_host(job_url))
        page = ctx.new_page()
        captured: dict = {}

        def on_response(resp):
            if "/wday/cxs/" in resp.url and "/job/" in resp.url and resp.status == 200:
                try:
                    captured["payload"] = resp.json()
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        title = ""
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
            page.wait_for_timeout(self._settle_ms)
            title = (page.title() or "")
        except Exception as exc:  # noqa: BLE001 - navigation failure
            raise WorkdayTransient(f"render: {exc}") from exc
        finally:
            page.close()
        if "payload" in captured:
            return captured["payload"]
        # No job cxs fired. Distinguish a Cloudflare challenge page (retryable) from a
        # genuinely-stale posting (the careers app loaded but the job is gone).
        if _looks_like_challenge(title):
            raise WorkdayBlocked("render blocked by cloudflare challenge")  # -> transient at top level
        raise WorkdayNotFound("no cxs job payload from rendered page")       # -> permanent (stale/closed)

    def close(self):
        self._http.close()
        self._browser.close()
        self._pw.stop()
```

### 3.4 `enrich_workday` worker (with code)

```python
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

from ..config import load_settings
from .enrich import Outcome, _apply_outcomes  # shared batched-write helper (refactored out of enrich_new)
from .workday import fetch_workday_one, workday_host

logger = logging.getLogger(__name__)


def _default_fetcher_factory():
    from .playwright_fetcher import PlaywrightFetcher
    return PlaywrightFetcher()


def enrich_workday(conn, *, max_jobs=300, max_workers=3, cap=3, fetcher_factory=_default_fetcher_factory):
    """Local worker: enrich description-less Workday rows via the tiered headless fetch.

    Jobs are sharded by tenant host so each shard runs on one thread-local fetcher and
    reuses that tenant's Cloudflare clearance. Returns {enriched, permanent_failed,
    transient_failed, candidates}.
    """
    rows = conn.execute(
        "select id, url from jobs "
        "where coalesce(description, '') = '' "
        "and route_method is distinct from 'manual' "
        "and enrich_attempts < %s "
        "and url ~* 'myworkdayjobs\\.com' "
        "order by scraped_at desc "
        "limit %s",
        (cap, max_jobs),
    ).fetchall()
    if not rows:
        return {"enriched": 0, "permanent_failed": 0, "transient_failed": 0, "candidates": 0}

    # Shard by tenant host so clearance is reused; each shard handled by one thread/fetcher.
    shards: dict[str, list] = {}
    for job_id, url in rows:
        shards.setdefault(workday_host(url) or "", []).append((job_id, url))

    def run_shard(jobs):
        fetcher = fetcher_factory()
        try:
            return [fetch_workday_one(jid, url, fetcher) for jid, url in jobs]
        finally:
            close = getattr(fetcher, "close", None)
            if close:
                close()

    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_shard, jobs) for jobs in shards.values()]
        for future in as_completed(futures):
            outcomes.extend(future.result())

    counts = _apply_outcomes(conn, outcomes, cap=cap)   # same write/classify as enrich_new
    counts["candidates"] = len(rows)
    logger.info("enrich_workday summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"workday enriched: {enrich_workday(conn)}")
```

`_apply_outcomes(conn, outcomes, *, cap)` is extracted verbatim from the current `enrich_new` write block (enriched → `description`/`enriched_at`/clear error; permanent → `enrich_attempts=cap`/error; transient → `+1`/error; returns the counts dict without `candidates`). `enrich_new` is refactored to call it — behavior-preserving.

CLI shim `src/jobmaxxing/enrich_workday.py`:

```python
"""CLI shim: `python -m jobmaxxing.enrich_workday` (run LOCALLY; needs the `headless` extra)."""
from .enrichment.workday import main

if __name__ == "__main__":
    main()
```

## 4. Failure model & classification

Per job, the first tier to produce a definite signal wins:

| Signal (any tier) | Class | DB effect |
| --- | --- | --- |
| `200` + non-empty `jobPostingInfo.jobDescription` | **enriched** | `description`, `enriched_at=now()`, clear error |
| `404`/`410`, or rendered page loads the careers app but fires no job cxs call (stale/closed) | **permanent** | `enrich_attempts = cap` (never reselected) |
| `403`/`429`/`503` at every tier, **or** the rendered page is a Cloudflare challenge (`_looks_like_challenge`) | **transient** | `enrich_attempts += 1` (retry to cap, then give up) |
| timeout / connection error / browser crash | **transient** | `enrich_attempts += 1` |

The render tier distinguishes *stale* (careers app loaded, no job → permanent) from *blocked* (Cloudflare interstitial title → transient) so a hard-blocked tenant is retried up to the cap rather than permanently killed on its first render.

The key Workday-specific nuance vs. the CI engine: **a `403` is transient here, not permanent.** In the CI clean-API path a `403` means a dead/blocked job (won't improve), but for Workday a `403` means "this tier couldn't solve Cloudflare" — a different tier, a warmed context, or a later run may succeed. The attempt-cap (`cap=3`) bounds wasted effort on the hard tail; after `cap` blocked runs a tenant is left alone. This reuses the existing columns and `_apply_outcomes` exactly — only the *classification* of `403` differs, and that lives in `_classify_status`/the tier dispatch, not in the shared writer.

## 5. Concurrency, politeness, resumability

- **Tenant sharding + per-host clearance reuse:** all of a tenant's jobs run on one shard/thread, so Cloudflare is cleared once per tenant per run (Tier 0 needs no browser; Tier 1/2 reuse the cleared context). This is the Ashby-board-cache idea applied to `cf_clearance`.
- **Bounded concurrency:** `max_workers` (default **3**) thread-local `PlaywrightFetcher`s. Playwright sync objects are thread-confined, so each thread owns its own browser — no cross-thread sharing. Three browsers is gentle on memory and on any single tenant (different tenants per shard).
- **`max_jobs` per run (default 300)** bounds a run; the candidate query + attempt-cap make runs **resumable** — enriched rows drop out (non-empty description), capped rows drop out, so re-running simply continues. Cold-start (6,922 rows) drains over repeated runs; steady-state handles the trickle of new Workday rows.
- **Politeness:** modest concurrency + per-tenant grouping avoid hammering a single host; `settle_ms` waits for the challenge rather than busy-retrying.

## 6. Dependencies & packaging

- Playwright is a **local-only optional dependency**, isolated so CI's `uv sync --frozen --no-dev` never installs Chromium:
  ```toml
  [project.optional-dependencies]
  headless = ["playwright>=1.40"]
  ```
- All Playwright imports are **lazy** (inside `_default_fetcher_factory` / `PlaywrightFetcher.__init__`), so `import jobmaxxing.enrichment.workday` succeeds in CI without Playwright. The pure logic and the worker (with an injected fake fetcher) import cleanly everywhere.
- Operator setup (documented in README): `uv sync --extra headless && uv run playwright install chromium`.

## 7. Testing — full pyramid

### 7.1 Unit (no DB, no browser, no network) — `tests/test_workday_unit.py`
- `workday_cxs_url`: job URL → cxs URL; **locale-prefix stripped** (`/en-US/` form); `wd1`/`wd3`/`wd5` variants; non-Workday URL → `None`.
- `workday_host`: returns `{tenant}.{wd}.myworkdayjobs.com`; `None` for non-match.
- `parse_workday`: real-shaped payload → HTML JD; missing `jobPostingInfo`/empty description → `None`.
- `_looks_like_challenge`: Cloudflare titles ("Just a moment…", "Attention Required!") → `True`; a real job title / empty → `False`.
- `fetch_workday_one` tier dispatch with a `FakeFetcher` (configurable per-tier behavior):
  - Tier 0 returns payload → `enriched`, Tiers 1/2 never called.
  - Tier 0 `WorkdayBlocked`, Tier 1 returns payload → `enriched` (escalated once).
  - All tiers `WorkdayBlocked` → `transient` ("blocked at all tiers").
  - Any tier `WorkdayNotFound` → `permanent`, no further tiers.
  - Any tier `WorkdayTransient` → `transient`, no further tiers.
  - Payload present but no description → `permanent`.
  - Unrecognized URL → `permanent`, fetcher never called.

### 7.2 Integration (pytest-postgresql + fake fetcher, no browser) — `tests/test_workday_db.py`
- **Candidate query:** seeds Workday + non-Workday + manual + has-description + attempt-capped rows; asserts only the eligible Workday rows are selected (others excluded). Reuses the `conn` fixture pattern from `test_enrich_db.py`.
- **Write transitions via `_apply_outcomes`:** enriched fills `description`/`enriched_at`; permanent sets `enrich_attempts=cap` and is not reselected on a second run; transient increments and is reselected until capped.
- **Counts:** `{enriched, permanent_failed, transient_failed, candidates}` exact.
- **Tenant sharding + clearance reuse:** a fake fetcher that counts `_cleared_context`-equivalent calls (e.g. records hosts seen) proves one clearance per tenant even with several same-tenant jobs; two tenants → two clearances.
- **`max_jobs` bound** respected.
- **`_apply_outcomes` characterization:** a test asserting the refactored `enrich_new` still produces identical results (guards the extraction).

### 7.3 End-to-end (real Playwright vs. live Workday) — `tests/test_workday_e2e.py`
- Marked `@pytest.mark.e2e` and **skipped unless `JOBMAXXING_E2E=1`** (the pattern the 2 pdflatex tests already use — never runs in CI/normal `pytest`). Run by the operator.
- Constructs a real `PlaywrightFetcher`, runs `fetch_workday_one` against a small seeded set of **known-live** Workday job URLs spanning a Tier-0 tenant (e.g. an edu/gov), a Tier-1 tenant (leidos-class), and a Tier-2 tenant; asserts `enriched` outcomes with non-empty HTML descriptions and that the tier escalation actually engaged.
- Documents that hard-tail tenants may yield `transient`; the test asserts a **minimum yield** (e.g. ≥ Tier-0 and Tier-1 succeed), not 100%.

### 7.4 Validation spike (Task 1 — a script, not a committed test)
Before building the worker, a throwaway script runs the three tiers against ~30 **fresh** Workday jobs sampled across tenants and prints the per-tier yield (enriched / blocked / not-found). This confirms the headless approach's real-world hit-rate (the leidos `200` proves Tier 1; micron's residual `403` is the open question Tier 2 must answer) and surfaces per-tier timings to tune `max_workers`/`max_jobs`. If yield is unexpectedly low, we revisit (stealth args, longer settle, etc.) **before** investing in the full pipeline.

## 8. Invariants preserved

| Invariant | How |
| --- | --- |
| CI pipeline unchanged; reliable core stays browser-free | Workday is not in `ADAPTERS`/`SUPPORTED_HOSTS_SQL`; no workflow edit; Playwright is an opt-in extra. |
| No gated rows perma-killed before headless sees them | The CI engine never selects Workday rows; only the local worker touches them, and it classifies `403` transient. |
| Enriched JD survives re-ingest (merge-no-clobber) | Unchanged `merge_records`/`_UPDATE_SQL` (empty desc is falsy; tracking columns untouched by upsert). Already covered by Phase 5a's durability test; a Workday-row variant is added. |
| Idempotent / resumable | Success removes a row (non-empty description); permanent/capped rows excluded by `enrich_attempts < cap`. |
| Manual rows untouched | Candidate query excludes `route_method='manual'`. |
| Importable in CI without Playwright | Lazy imports; pure logic + worker (fake fetcher) need no browser. |

## 9. Deliverables

- `src/jobmaxxing/enrichment/workday.py` (pure logic + `fetch_workday_one` + worker `enrich_workday` + `main`).
- `src/jobmaxxing/enrichment/playwright_fetcher.py` (`PlaywrightFetcher`).
- `src/jobmaxxing/enrich_workday.py` (CLI shim).
- `_apply_outcomes` extracted from `enrich_new` (shared writer) + characterization test.
- `pyproject.toml`: `headless` optional-dependency group; `uv.lock` updated.
- Tests: `tests/test_workday_unit.py`, `tests/test_workday_db.py`, `tests/test_workday_e2e.py` (skipped by default).
- README: local setup + `python -m jobmaxxing.enrich_workday` usage; note it runs locally on a residential IP.
- No migration (reuses the Phase 5a `enrich_*` columns), no CI workflow change.

## 10. Open items & risks (named, accepted)

- **Headless-beats-Cloudflare on fresh micron-class jobs is unproven** (Tier 1 confirmed on leidos; Tier 2 on stubborn tenants is the open question). → Task 1 validation spike de-risks before the full build; hard-tail tenants are attempt-capped and we quantify yield after the first real run.
- **Concurrency model:** sync Playwright is thread-confined → each thread gets its own browser (memory cost ~ `max_workers` browsers). Default 3 is conservative; tune from spike timings. If memory/throughput demands it later, an async-Playwright single-event-loop variant is a future option (not v1).
- **Fragility:** Cloudflare/Workday changes can break tiers. This is best-effort and additive — the core pipeline is unaffected, and breakage degrades to `transient`/capped, never corruption.
- **JD is HTML:** stored as-is (router/scorer tolerate raw text), consistent with Greenhouse/SmartRecruiters. HTML-stripping deferred unless it measurably helps routing.
- **Stale links:** same reality as Phase 5a — closed Workday postings render to the careers home / fire no job cxs → permanent. Expected, not a bug.
