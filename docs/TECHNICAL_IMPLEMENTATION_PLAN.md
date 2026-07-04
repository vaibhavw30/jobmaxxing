# Technical Implementation Plan — Internship Recruiting Pipeline

Companion to `PRD.md`. This is the build spec. Design principle throughout: **the reliable core is cheap, structured, and boring; everything fancy is additive and fails soft.**

---

## 1. Architecture at a glance

Three decoupled stages plus an interface. Nothing is real-time; cron + a status column is the only orchestration primitive needed. No WebSockets, no message broker, no Kafka — that would be overengineering for a single-user, low-throughput pipeline.

```
  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
  │  INGESTION  │ ──▶ │  STORE +     │ ──▶ │  ROUTE + TAILOR  │
  │  (pollers)  │     │  DEDUPE (PG) │     │  (LLM service)   │
  └─────────────┘     └──────────────┘     └──────────────────┘
        │                    │                      │
   GitHub lists         Postgres            base resumes (S3)
   Greenhouse/Lever/    (Supabase)          tailored artifacts (S3)
   Ashby                status column        deterministic scorer
   JobSpy (additive)         │              provider-agnostic LLM
   Gmail alerts (additive)   │
                             ▼
                      ┌──────────────┐
                      │ MCP SERVER + │  ◀── operator drives from
                      │ review view  │       Claude Code / chat
                      └──────────────┘
```

State machine (single `status` column):
`new → routed → approved_for_tailoring → tailored → reviewed → applied | rejected`

The operator is the only thing that moves a job past `tailored`.

---

## 2. Stack decisions (and why each is the cheap/boring choice)

| Concern                    | Choice                                                      | Why                                                                                      |
| -------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| DB                         | Supabase Postgres (free tier)                               | Already in use; free; SQL is enough; no pgvector needed since routing isn't semantic     |
| Reliable pollers           | GitHub Actions scheduled workflows                          | Free compute, free cron, no server to keep alive                                         |
| Aggregator poller (JobSpy) | Scheduled AWS Lambda (or Fargate task)                      | Needs to run from a non-datacenter-flagged context occasionally; cheap, intermittent     |
| Tailoring compute          | AWS Lambda (container image w/ LaTeX) or small Fargate task | Pay-per-invoke; only runs on approved jobs                                               |
| Artifact storage           | S3                                                          | Cheap, durable, keyed by job_id                                                          |
| Base resumes               | S3 (canonical), Overleaf (editing surface)                  | Overleaf git remote adds auth fragility for read-only use; just `aws s3 cp` after export |
| LLM access                 | Thin provider-agnostic wrapper                              | Budget portability; swap model via config                                                |
| Interface                  | MCP server (Python)                                         | Drive conversationally from Claude Code; no dashboard to build first                     |
| Review surface             | Supabase view / minimal kanban                              | Don't build a UI until the pipeline works                                                |

**Explicitly rejected as overengineering:** WebSockets (nothing bidirectional/real-time), Redis/Celery (cron + status column suffices at this volume), embeddings/pgvector (routing is keyword-based), a custom scraper for logged-in LinkedIn (ban risk >> value).

---

## 3. Data model

One primary table. Keep it flat.

```sql
create table jobs (
  id              uuid primary key default gen_random_uuid(),
  source          text not null,            -- 'github:simplify', 'greenhouse', 'lever', 'jobspy', 'gmail-alert', ...
  company         text not null,
  title           text not null,
  location        text,
  url             text not null,
  description     text,                      -- full JD when we have it; null if we only have a link
  posted_at       timestamptz,
  scraped_at      timestamptz default now(),
  dedupe_key      text not null,            -- normalized(company || title || canonical_url)
  resume_type     text,                      -- one of the 8 types; null until routed
  route_method    text,                      -- 'rules' | 'llm' | 'manual'
  route_confidence real,
  status          text not null default 'new',
  artifact_prefix text,                      -- s3://.../tailored/{id}/ once tailored
  score_before    jsonb,                     -- composite + per-axis
  score_after     jsonb,
  notes           text,
  unique (dedupe_key)
);

create index on jobs (status);
create index on jobs (resume_type);
create index on jobs (scraped_at desc);
```

Dedupe is enforced at the DB level (`unique (dedupe_key)`) so every poller can blindly upsert with `on conflict (dedupe_key) do nothing` (or `do update` to enrich a link-only row with a full JD when a richer source later finds the same job). This makes all pollers idempotent for free — a core reliability requirement.

`dedupe_key` normalization: lowercase, strip whitespace/punctuation from company+title, canonicalize URL (strip query params, tracking, trailing slash). Same job from 5 sources collapses to one row.

---

## 4. Stage 1 — Ingestion

Each poller is an independent script. Contract: read a source, normalize to the `jobs` schema, upsert. Wrap the whole body in try/except that logs and exits 0 on failure of any single source — **one source failing never fails the run.**

### 4.1 Reliable core (free, GitHub Actions)

**GitHub list pollers.** The curated lists publish structured `listings.json` (or parseable README tables). Fetch raw, parse, upsert. These are the goldmine: structured, maintained, and they _want_ to be read. Cadence: every 2–4 hours.

**ATS direct pollers.** For a watch-list of target companies, hit the public board APIs:

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: public board endpoint per company.

These return full JDs (so routing + tailoring have real text), are free, and are stable. Maintain the company→token mapping in a config file. Cadence: every 4–6 hours.

> The watch-list is where your target firms (SIG, TransMarket, Base Power, NVIDIA, etc.) go — most post on Greenhouse/Lever/Ashby, so you get their full JDs directly, no aggregator needed.

### 4.2 Additive sources (fail-soft, not on the critical path)

**JobSpy (aggregator).** Self-hosted [JobSpy] pulls Indeed/Glassdoor/Google Jobs/LinkedIn-via-library into a dataframe. This is the _only_ clean way to touch LinkedIn-derived data without running a logged-in scraper. Flaky from datacenter IPs → run as an intermittent scheduled Lambda/Fargate task, accept partial failures, upsert whatever comes back. Cadence: 1–2×/day.

**Gmail alert parser.** Set up LinkedIn Premium saved searches → email alerts. A Gmail-API poller extracts posting URLs from those emails and upserts link-only rows (description null; enriched later if an ATS poller finds the same job). This is low-tech and low-ban-risk — we read our own inbox, we never automate the LinkedIn session.

**Reliability stance:** the pipeline is fully useful with _only_ §4.1. §4.2 widens coverage but nothing depends on it. If JobSpy breaks for a week (it will, periodically), the core feed is unaffected.

---

## 5. Stage 2 — Routing

Turn a posting into a `resume_type`. This is a router, not a ranker.

1. **Rules pass (deterministic, free).** Title + JD keyword match against per-type signal dictionaries (see §7). Produces a type + confidence. Most postings resolve here — "Quantitative Trader Intern" → `quant-trader` needs no LLM.
2. **LLM tiebreaker (only when ambiguous).** When rules are low-confidence or two types tie (e.g. "ML Infrastructure Engineer" straddles `mle`/`swe`), one cheap call to a Haiku-class / GPT-mini-class model returns the type. This is the _only_ LLM cost in discovery, and it's bounded to ambiguous cases.
3. **Manual override.** Operator can re-label any job via the MCP tool; sets `route_method='manual'`.

Record `route_method` and `route_confidence` so you can audit how routing performed and tune the dictionaries.

---

## 6. Stage 3 — Tailoring (the core)

Triggered when a job hits `approved_for_tailoring`. This is the piece worth getting right; everything else is plumbing.

### 6.1 Inputs

- JD text (from the job row).
- Base `main.tex` + `resume.pdf` for the routed type, pulled from `s3://<bucket>/base/{type}/`.
- The type's rubric: `rubrics/{type}.json` = `{ weights, keyword_dict, aliases }` (see §7).

### 6.2 The two-pass loop

**Pass 0 — Score the base (before).** Run the deterministic keyword-coverage scorer + an LLM qualitative scoring call against the rubric. Persist as `score_before`. Both static-dictionary and JD-extracted-dynamic coverage are computed here (see §7.3).

**Pass 1 — Build the tailored copy.** LLM call: base `.tex` + JD → tailored `.tex`. Constraints in the prompt: surgical edits only, one-page, **no fabrication**, preserve the template structure. The base `.tex` is **prompt-cached** — it's identical across every tailoring call, so caching is a large cost saver over the season.

**Pass 2 — Adversarial review + patch.** One structured call with two personas:

- _Senior engineer:_ the 3 biggest weaknesses of the resume as written against this JD.
- _Hiring manager / ATS:_ the missing keywords/phrases a parser screening thousands of resumes would flag as absent.
  Then the model applies the fixes and emits the patched `.tex`.

**Pass 3 — Compile + one-page guard (deterministic).** `pdflatex` the patched `.tex`. If it overflows one page, send it back: "you exceeded one page, cut to fit," and recompile. **Never trust the model's self-report that it fit** — the page count is measured from the compiled PDF. Cap retries (e.g. 3) then fall back to the last fitting version.

**Pass 4 — Score the result (after).** Re-run the _identical_ scorer from Pass 0 on the final `.tex`. Persist `score_after`. The improvement delta is `after − before` on the same axes — real, because the scorer is fixed and one axis is deterministic.

### 6.3 Outputs (to `s3://<bucket>/tailored/{job_id}/`)

- `tailored.tex`, `tailored.pdf`
- `review.json` — `{ score_before, score_after, delta, weaknesses[3], missing_keywords[], applied_fixes[] }`
- `diff.txt` — unified diff base→tailored, so the operator sees exactly what changed before trusting it.

### 6.4 Why this shape

- Deterministic compile + page check means the model can't hand you an invalid or overflowing resume.
- A fixed rubric scored before _and_ after, with a grounded keyword axis, means the "improvement out of 10" is meaningful rather than the model grading its own homework optimistically.
- The diff keeps the human gate honest: you approve based on what actually changed.

---

## 7. The rubric system

Structure is constant across all types (so before/after and cross-type comparisons are coherent); **weights and keyword dictionary vary by type.**

### 7.1 The five axes (each 0–10)

1. **JD keyword coverage** — _deterministic._ Extracted JD terms matched against resume text (exact + alias table). The grounded number.
2. **Technical depth match** — does the resume show the _level_ the role wants, not just the noun.
3. **Impact quantification** — bullets carrying real numbers/outcomes.
4. **ATS parseability** — clean structure, standard headers, no parser-breaking formatting.
5. **Relevance ordering** — most JD-relevant experience surfaced first.

Composite = weighted sum of axes using the type's weight vector.

### 7.2 Per-type weighting & dictionary emphasis

| Type         | Heaviest axes                       | Dictionary emphasis                                                           |
| ------------ | ----------------------------------- | ----------------------------------------------------------------------------- |
| quant-trader | keyword coverage + impact           | probability, EV, market-making, PnL, options, mental math, game theory        |
| quant-dev    | technical depth + keyword coverage  | low-latency, C++, market data, backtesting, time-series, systems              |
| mle          | technical depth + impact            | training/inference, XGBoost/transformers, feature eng, model eval, data scale |
| swe          | keyword coverage + ATS parseability | language/framework breadth, distributed, APIs, scale, CI/CD                   |
| fdse         | impact + relevance ordering         | customer-facing, deployment, data integration, Java, ontology/Foundry         |
| ai           | technical depth + relevance         | LLMs, agents, RAG, fine-tuning, eval, inference infra                         |
| robotics     | technical depth + keyword coverage  | control, perception, ROS, state estimation, sim, RL                           |
| av           | technical depth + keyword coverage  | perception, sensor fusion, planning, SLAM, real-time, safety                  |

Each type gets a `rubrics/{type}.json`:

```json
{
  "weights": {
    "keyword_coverage": 0.3,
    "technical_depth": 0.3,
    "impact": 0.2,
    "ats": 0.1,
    "relevance_order": 0.1
  },
  "keyword_dict": ["low-latency", "c++", "market data", "backtesting", "..."],
  "aliases": { "c++": ["cpp", "c plus plus"], "ml": ["machine learning"] }
}
```

### 7.3 Static vs dynamic keyword coverage

- **Static (per type):** the dictionary above — does the resume read as the right _kind_ of candidate for this track?
- **Dynamic (per JD):** terms extracted from _this specific posting_ (their exact stack/phrasing — "Polars" not "pandas," "Temporal" not "queue") — will it survive _this_ company's ATS filter?

Coverage is computed and reported against both, separately, so the review tells you "strong generic SWE profile, but missing 4 of their specific stack terms." The deterministic scorer reads `keyword_dict` + `aliases`; the LLM qualitative call is handed the _same_ weights + dictionary so its scoring is anchored to identical terms.

---

## 8. Model-agnostic LLM layer

A thin wrapper is the only thing that touches a provider SDK. Every LLM step (routing tiebreaker, tailoring passes, qualitative scoring) calls this, never a vendor SDK directly.

```
llm.complete(task, messages, *, cache=None, max_tokens=...) -> text
```

- `task` maps to a model tier via config, not hardcoded: `route` → cheap tier, `tailor` → mid tier, `review` → mid/strong tier.
- Config holds, per tier, an ordered list of `(provider, model)` candidates. The wrapper tries the first; on error/timeout/rate-limit it falls through to the next. This is the provider-agnostic fallback the PRD requires.
- Prompt caching is requested via the `cache` arg (base resume on tailoring calls) where the provider supports it; the wrapper no-ops it where unsupported.

### Cost routing strategy (fits the budget)

- **Routing tiebreaker:** cheapest tier (Haiku-class / GPT-mini-class). Bounded to ambiguous postings only.
- **Tailoring + review:** mid tier, base resume prompt-cached. This is where most spend goes; caching + running only on _approved_ jobs keeps it bounded.
- **Never auto-tailor the whole feed.** Tailoring is gated behind operator approval. This single rule is the main cost control.

### Budget sketch (order-of-magnitude, validate before relying on it)

- Routing: pennies/day; effectively free over the season.
- Tailoring: a handful of cents per job with caching; even hundreds of tailored jobs across the season sits comfortably inside $2,000 OpenAI + $500 Anthropic.
- The credits last because the _expensive_ operation is gated and cached, and the _frequent_ operations (discovery, routing) are free or near-free.

> Split usage to stretch credits: route the cheap/high-frequency tiebreaker and one tailoring pass to whichever provider you have more credit headroom on; keep the other as the fallback model in the same tier. The wrapper makes this a config change.

---

## 9. Interface

### 9.1 MCP server

Exposes the pipeline as tools so the operator drives it from Claude Code / a chat surface — no dashboard required first:

- `query_jobs(filters)` — by status, type, company, recency.
- `preview_route(job_id)` — show routed type + method + confidence; allow override.
- `approve(job_id)` — set `approved_for_tailoring`.
- `tailor_job(job_id)` — run the Stage-3 loop; returns the review summary.
- `get_review(job_id)` — fetch `review.json` + diff.
- `set_status(job_id, status)` — move through the funnel (incl. `applied`/`rejected`).

### 9.2 Review surface

A Supabase view or a minimal kanban over the `status` column: `new → routed → approved_for_tailoring → tailored → reviewed → applied/rejected`. Build this only after the pipeline produces artifacts worth reviewing.

---

## 10. Reliability & redundancy (how it survives 3–4 months unattended)

- **Source isolation:** each poller try/excepts its whole body, logs, exits 0. One dead source never blocks the run.
- **DB-level idempotency:** `unique (dedupe_key)` + upsert means re-runs and overlapping sources can't duplicate or corrupt.
- **Tiered source reliability:** GitHub lists + ATS APIs are the reliable core; aggregators + email are additive and expected to be flaky.
- **LLM fallback:** tier-based candidate lists; automatic fall-through on provider failure/rate-limit.
- **Deterministic guards around the model:** compile + one-page check are code, not trust.
- **Cost guardrail:** tailoring gated behind human approval; cheap models for high-frequency steps; caching on the repeated payload.
- **Observability:** every run logs ingested/skipped/failed counts with reasons. Failures are visible.
- **Re-tailor safety:** tailoring writes to a job-keyed S3 prefix and overwrites cleanly; re-running is safe.

---

## 11. Build order (maps to PRD phasing)

1. **Schema + reliable pollers** (GitHub lists, Greenhouse/Lever/Ashby) on GitHub Actions → queryable deduped feed. _Useful immediately, zero LLM cost._
2. **Router** — keyword rules + LLM tiebreaker + manual override.
3. **Tailoring service** — two-pass loop + deterministic keyword scorer + compile/one-page guard + artifacts. _Nail this._
4. **MCP server + review view.**
5. **Additive discovery** — JobSpy (Lambda) + Gmail alert parser.
6. **(Optional, later)** form-fill assist on approved jobs only, human-gated.

Each phase is independently useful; you can stop after any phase and still have something better than manual.

---

## 12. Open items to decide before/while building

- Final company watch-list for the ATS pollers (which Greenhouse/Lever/Ashby tokens).
- Exact `keyword_dict` contents per type (start from §7.2, refine against real JDs you route).
- Which provider gets the bulk tailoring load vs. fallback, based on live credit burn.
- Whether the review surface is a Supabase view (fastest) or a small kanban (nicer) — defer until Phase 4.
