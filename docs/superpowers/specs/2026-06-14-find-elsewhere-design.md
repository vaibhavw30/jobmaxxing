# Spec — Find-elsewhere JD recovery (Workday backup sub-project 2)

**Type:** New feature — Workday-backup sub-project 2 of 3 (see `2026-06-14-workday-backup-roadmap.md`)
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** sub-project 1 (title triage — provides the relevance signal: `resume_type` set on JD-less Workday jobs) and the enrichment merge/write patterns. Honors the roadmap **reset contract**.

---

## 1. Problem & rationale

~50% of Workday jobs can't be enriched (Cloudflare-gated cxs API even in a headless browser). But the same job's description very often lives elsewhere as `schema.org/JobPosting` JSON-LD on an aggregator or the company's own careers page — which is **not** Cloudflare-gated. Probed against the real backlog, a free DuckDuckGo search surfaced recoverable JDs (e.g. Chegg → Glassdoor 6,044 chars / jobgether 5,939; Trimble → 3,000 chars). This sub-project recovers those JDs so the relevant internships can be routed-with-JD and tailored.

**Constraints chosen during brainstorming:**
- **Free search** (DuckDuckGo HTML, no API key, no cost).
- **Local, residential IP** — DDG/aggregators block datacenter IPs (same reason the Workday worker is local); operator-run, cron-able.
- **Match guard = deterministic + LLM-confirm** — a wrong match writes a wrong JD → mis-tailoring, so accept only on a hard signal (req-id / back-link) or an LLM-confirmed fuzzy match; flag every recovered JD for the operator's nightly spot-check.

### Scope
**In:** a `recovery/` package (search, JSON-LD extraction, match decision, the worker) + a `recover_jd` local CLI; migration 0005 (`jd_source`, `recover_attempts`, `recover_error`); unit/integration/e2e tests. Targets relevant (`resume_type` set), JD-less, Workday jobs.
**Out:** the nightly operator queue (sub-project 3); paid search APIs; non-Workday sources (they already enrich via Phase 5a); changing the router or the Workday worker.

## 2. Architecture

A new local worker, run on a residential IP:

```
python -m jobmaxxing.recover_jd
  └─ select relevant JD-less Workday rows (resume_type set, description empty, recover_attempts<cap)
       └─ per job:  search (DDG) → fetch candidates → extract JobPosting JSON-LD → MATCH
            ├─ accept → write description, jd_source='recovered', RESET resume_type/route_method=NULL
            └─ no match → recover_attempts += 1 (give up at cap)
  └─ batched write
  (next CI route_new re-routes the now-JD-bearing job confidently with rules/llm)
```

New package `src/jobmaxxing/recovery/` (mirrors `enrichment/`'s split — pure logic behind injectable network):
- **`search.py`** — `ddg_search(query, *, fetch_text) -> list[str]`: query DDG HTML, return candidate result URLs (skip Workday/the original host). `build_query(company, title) -> str`.
- **`extract.py`** — `extract_job_posting(html_text) -> JobPosting | None`: pull the `JobPosting` JSON-LD (walks `@graph`/lists/nesting), defensively normalizing `description`/`title`/`hiringOrganization`/`identifier`/`url`. `workday_req_id(url) -> str | None` (the slug's trailing `_<reqid>`).
- **`match.py`** — `match_job(job, candidate, *, llm_confirm) -> MatchResult`: the pure decision (deterministic req-id/back-link → accept; else fuzzy company+title → LLM-confirm). Fully unit-testable with a fake `llm_confirm`.
- **`recover.py`** — `recover_new(conn, *, max_jobs, cap, searcher, fetcher, llm_confirm)`: candidate query → per-job pipeline → batched write/reset. `main()`.
- **`src/jobmaxxing/recover_jd.py`** — CLI shim (`python -m jobmaxxing.recover_jd`), mirrors `enrich_workday.py`.

The network (DDG search, page fetch) and the LLM sit behind injected `searcher`/`fetcher`/`llm_confirm` callables so `match.py`, `extract.py`, and the worker logic are tested with fakes — no network (the Workday `WorkdayFetcher` pattern).

## 3. Data types (with code)

```python
from dataclasses import dataclass

@dataclass
class JobPosting:
    """The fields we pull from a candidate's schema.org JobPosting JSON-LD."""
    description: str                 # HTML
    title: str | None = None
    company: str | None = None       # hiringOrganization.name (or the string form)
    identifier: str | None = None    # identifier.value, or the string form
    url: str | None = None           # canonical posting URL
    source_url: str | None = None    # the page we fetched it from

@dataclass
class MatchResult:
    accepted: bool
    reason: str                      # "reqid" | "backlink" | "llm_confirmed" | "rejected:<why>"
```

## 4. Search & extraction (with code, grounded in the working probe)

```python
# search.py
import re, urllib.parse

_RESULT = re.compile(r'class="result__a"[^>]+href="([^"]+)"')

def build_query(company: str | None, title: str | None) -> str:
    return " ".join(p for p in (company, title) if p).strip()

def ddg_search(query: str, *, fetch_text, max_results: int = 6) -> list[str]:
    """DuckDuckGo HTML search -> candidate result URLs (Workday hosts excluded)."""
    body = fetch_text("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    urls = []
    for href in _RESULT.findall(body):
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        if "myworkdayjobs.com" in url:        # the gated source we're routing around
            continue
        urls.append(url)
        if len(urls) >= max_results:
            break
    return urls
```

```python
# extract.py
import json, re

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL)
_REQID = re.compile(r"_([A-Za-z]*[-_]?\d[\w-]*)$")   # trailing slug token, e.g. _JR012226 / _R-1289

def workday_req_id(url: str) -> str | None:
    m = _REQID.search(url.split("?")[0].rstrip("/"))
    return m.group(1) if m else None

def _text(v):                      # hiringOrganization / identifier may be str OR object
    if isinstance(v, dict):
        return v.get("name") or v.get("value")
    return v if isinstance(v, str) else None

def extract_job_posting(html_text: str, *, source_url=None) -> "JobPosting | None":
    for block in _LD.findall(html_text):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(n, dict) and n.get("@type") == "JobPosting" and n.get("description"):
                return JobPosting(
                    description=n["description"],
                    title=n.get("title"),
                    company=_text(n.get("hiringOrganization")),
                    identifier=_text(n.get("identifier")),
                    url=n.get("url"),
                    source_url=source_url,
                )
    return None
```

> The defensive `_text` (string-or-object) and the `@graph`/list handling are pinned against **captured real fixtures** during TDD; the trailing-token req-id regex is validated against real Workday slugs (`…_JR012226`, `…_R-1289`, `…_REQ-4012`).

## 5. The match guard (with code)

```python
# match.py
import re

def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def _company_matches(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    at, bt = set(a.split()), set(b.split())
    return bool(at & bt) and (a in b or b in a or len(at & bt) / max(len(at), len(bt)) >= 0.5)

def _title_similar(a, b):
    at, bt = set(_norm(a).split()), set(_norm(b).split())
    return bool(at) and len(at & bt) / len(at | bt) >= 0.4    # Jaccard

def match_job(job: dict, cand: "JobPosting", *, llm_confirm) -> "MatchResult":
    """job = {company, title, url, req_id}. Deterministic accept on req-id / back-link;
    else fuzzy company+title -> one LLM 'same posting?' confirm. Returns MatchResult."""
    hay = " ".join(filter(None, [cand.description, cand.identifier, cand.url, cand.source_url, cand.title]))
    req = job.get("req_id")
    if req and req.lower() in hay.lower():
        return MatchResult(True, "reqid")
    if job.get("url") and job["url"] in hay:
        return MatchResult(True, "backlink")
    if not _company_matches(job.get("company"), cand.company):
        return MatchResult(False, "rejected:company")
    if not _title_similar(job.get("title"), cand.title):
        return MatchResult(False, "rejected:title")
    if llm_confirm(job, cand):                       # cheap "same posting? yes/no"
        return MatchResult(True, "llm_confirmed")
    return MatchResult(False, "rejected:llm")
```

`llm_confirm(job, cand)` builds a tiny schema-gated prompt (our title+company vs the candidate's title+company + a description snippet → strict `{"same": true|false}`) on the `route` task (Haiku / claude-cli subscription), parsed leniently (any failure → `False`, i.e. reject — prefer a safe miss over a wrong JD). It lives in `recover.py` (the one place that touches the LLM client) and is injected into `match_job`.

## 6. The worker (`recover.py`)

```python
def recover_new(conn, *, max_jobs=100, cap=2, searcher=ddg_search, fetcher=..., llm_confirm=...) -> dict:
    rows = conn.execute(
        "select id, company, title, url from jobs "
        "where coalesce(description,'')='' "
        "and resume_type is not null "                 # relevant (title-routed by sub-project 1)
        "and route_method is distinct from 'manual' "
        "and recover_attempts < %s "
        "and url ilike '%%myworkdayjobs%%' "
        "order by scraped_at desc limit %s",
        (cap, max_jobs),
    ).fetchall()
    ...
    # per job (bounded concurrency, gentle on DDG):
    #   q = build_query(company, title); results = searcher(q, fetch_text=fetcher)
    #   for url in results: cand = extract_job_posting(fetcher(url), source_url=url)
    #       if cand and match_job({company,title,url,req_id=workday_req_id(url)}, cand, llm_confirm=...).accepted:
    #           -> outcome recovered(description=cand.description); break
    #   else -> outcome missed
    # batched write in one transaction:
    #   recovered -> set description=%s, jd_source='recovered', resume_type=null, route_method=null
    #   missed    -> set recover_attempts = recover_attempts + 1, recover_error=%s
    return {"recovered": ..., "missed": ..., "candidates": len(rows)}
```

- **Accept write** sets `description` + `jd_source='recovered'` and **resets `resume_type=NULL, route_method=NULL`** (the roadmap reset contract) so the next CI `route_new` re-routes with the real JD.
- **Politeness:** modest concurrency (e.g. 3) + small delays; DDG is rate-limited and unofficial. `max_jobs` bounds a run; resumable via `recover_attempts < cap`.
- A low `cap` (default **2**) — repeated free-search misses rarely flip; the misses are sub-project 3's job.

## 7. Schema — migration 0005

```sql
alter table jobs add column if not exists jd_source       text;     -- 'ats' | 'recovered' | 'manual'
alter table jobs add column if not exists recover_attempts int not null default 0;
alter table jobs add column if not exists recover_error    text;
```

`jd_source` flags how a description was obtained; `'recovered'` rows are exactly what the operator spot-checks (sub-project 3 surfaces them). (Existing ATS/enrichment descriptions leave `jd_source` NULL — that's fine; only recovery sets it. A future cleanup could backfill `'ats'`, out of scope here.)

## 8. Invariants & risks

| Invariant / risk | Handling |
| --- | --- |
| **Wrong-match corruption** (writes a wrong JD) | Deterministic req-id/back-link first; fuzzy only with LLM confirm; `llm_confirm` failure → reject; every recovery flagged `jd_source='recovered'` for spot-check; reset-on-better-data via the reroute contract. |
| Re-routes confidently once a JD lands | Accept resets `resume_type`/`route_method` → CI `route_new` re-routes with the JD. |
| Doesn't re-search forever | `recover_attempts < cap` (default 2) excludes exhausted rows. |
| Manual rows untouched | Candidate query excludes `route_method='manual'`. |
| Only relevant jobs searched | Candidate query requires `resume_type is not null` (sub-project 1's relevance). |
| CI unaffected / no browser | Local CLI only; CI pipeline and `uv sync --no-dev` unchanged (no new runtime dep — stdlib `urllib`/`re`/`json`; or `httpx` which is already a dep). |
| DDG fragility (HTML drift, rate limits, IP block) | Local residential IP; per-job graceful failure (a fetch/parse error = that candidate skipped, job → missed, retried next run); politeness delays. |
| Partial coverage | Misses fall to sub-project 3 (nightly human). |

## 9. Testing — pyramid

- **Unit (no network/LLM/DB):**
  - `workday_req_id`: `…_JR012226`→`JR012226`, `…_R-1289`→`R-1289`, `…_REQ-4012`→`REQ-4012`; non-Workday → None.
  - `extract_job_posting`: real-shaped fixtures — `@graph` form, top-level form, `hiringOrganization`/`identifier` as string AND as object, no-JobPosting → None, JobPosting without description → None.
  - `ddg_search`: a captured DDG HTML fixture → expected result URLs, Workday hosts filtered.
  - `match_job`: req-id present → `reqid` accept (no LLM); back-link → `backlink`; company mismatch → reject (no LLM); title dissimilar → reject; fuzzy + `llm_confirm`→True → `llm_confirmed`; fuzzy + `llm_confirm`→False → reject.
- **Integration (pytest-postgresql + fake searcher/fetcher/llm_confirm):**
  - candidate selection: only `resume_type`-set, JD-less, Workday, under-cap rows (excludes manual, has-desc, non-Workday, capped).
  - accept → `description` written, `jd_source='recovered'`, `resume_type`/`route_method` reset to NULL.
  - miss → `recover_attempts += 1`, reselected until cap then excluded.
  - `max_jobs` bound; counts `{recovered, missed, candidates}`.
- **E2E (skip-by-default, `JOBMAXXING_E2E=1`):** real DDG + extraction against a seeded known-recoverable job; asserts a non-trivial description (or a clean miss) — operator-run on a residential IP.

## 10. Deliverables

- `src/jobmaxxing/recovery/{__init__,search,extract,match,recover}.py`; `src/jobmaxxing/recover_jd.py` CLI.
- `migrations/0005_recovery.sql`.
- Captured fixtures under `tests/fixtures/recovery/`; unit + integration + skip-by-default e2e tests.
- README note: local `python -m jobmaxxing.recover_jd` on a residential IP; recovered JDs are flagged for review.
- No CI workflow change; no new runtime dependency (uses `httpx` already in deps).

## 11. Open items & risks (resolve during implementation, not blocking)

- Pin the exact `JobPosting` `identifier`/`hiringOrganization` shapes against captured fixtures (handled defensively as string-or-object).
- Tune `_company_matches`/`_title_similar` thresholds against real recovered candidates; if the LLM-confirm rejects too many valid matches (or accepts wrong ones), adjust the prompt / thresholds — measured against a first real run.
- DDG may need a fallback result-parse if its HTML changes; the `searcher` interface makes swapping the source (or adding a paid fallback later) a localized change.
- If a single aggregator dominates and rate-limits, add per-host backoff (start without it; the politeness delay + low `max_jobs` likely suffice).
