# PRD — Internship Recruiting Pipeline

**Owner:** Vaibhav
**Window:** July 2026 → end of recruiting season (~3–4 months of active use)
**Status:** Draft v1

---

## 1. Problem

Recruiting season is a high-volume, time-sensitive funnel. The bottleneck isn't deciding _whether_ to apply — it's (a) finding relevant postings before they fill, and (b) the per-application cost of tailoring a resume well. Both are mechanical enough to automate up to the point of human judgment, and both are currently done by hand, slowly, and inconsistently.

This system removes the mechanical cost from discovery and tailoring so the only thing left for a human is the parts that actually need a human: deciding which jobs are worth applying to, and hitting submit.

## 2. Goals

1. **Continuous discovery.** New SWE / ML / quant / FDSE / AI / robotics / AV internship postings land in one queryable place automatically, deduped across sources, without manual searching.
2. **Routing, not ranking.** Each posting is classified into one of a fixed set of resume _types_. The type selects which base resume gets used. No fuzzy relevance scoring needed.
3. **High-quality automated tailoring.** Given a posting and its routed base resume, produce a tailored one-page resume via a two-pass adversarial loop, with a _quantified_ before/after improvement score.
4. **Human stays the gate.** The system produces review-ready tailored applications. A human approves and submits. No autonomous submission in v1.
5. **Cost-bounded.** Must run the full season on ~$2,000 OpenAI + ~$500 Anthropic credits plus negligible AWS spend.
6. **Model-agnostic.** Any step that calls an LLM must work across providers/models via a thin abstraction, so cost or availability can be re-routed without rewrites.

## 3. Non-goals (v1)

- **No autonomous submission.** Form-fill on approved jobs is an explicit _later_ phase, gated behind a human approval, never for discovery or decision.
- **No LinkedIn scraping of our own account.** LinkedIn data comes only through tolerated channels (aggregator libraries, email alerts). We do not automate the logged-in LinkedIn session — the ban risk is not worth it.
- **No semantic match engine.** Routing is keyword/title + a cheap LLM tiebreaker. We are explicitly _not_ building an embedding reranker; matching is a router by design.
- **No ML model training.** Everything is rules + hosted LLM calls.
- **No multi-user / SaaS.** Single user, single operator.

## 4. Users

One: the operator (Vaibhav). Acts as both the consumer of the job feed and the approver in the loop. There is no second persona.

## 5. Core user stories

- _As the operator,_ I want new relevant postings to appear in a queryable list automatically, so I never have to refresh LinkedIn or job boards manually.
- _As the operator,_ I want to see each posting already labeled with which of my base resumes applies, so I can skip the classification step.
- _As the operator,_ I want to mark a posting "tailor this" and get back a tailored `.tex` + compiled `.pdf` + a before/after score report, so I can decide whether to use it in seconds.
- _As the operator,_ I want the tailoring to tell me the 3 biggest weaknesses and the missing ATS keywords it found and fixed, so I trust the output and learn from it.
- _As the operator,_ I want to drive the whole thing conversationally (query jobs, trigger tailoring, mark status) from Claude Code / a chat surface, so I don't context-switch into a dashboard.
- _As the operator,_ I want every stage to fail soft — if one source or one model is down, the rest keeps working — so the pipeline survives a 3-month season unattended.

## 6. Functional requirements

### 6.1 Discovery / ingestion

- Poll a fixed set of structured sources on a schedule: curated GitHub internship lists (Simplify, vanshb03, Pitt CSC, SWE List), and direct ATS endpoints (Greenhouse, Lever, Ashby) for a watch-list of target companies.
- Poll a broader aggregator (JobSpy) covering Indeed / Glassdoor / Google Jobs / LinkedIn-via-library on a slower schedule.
- Parse LinkedIn Premium saved-search email alerts (via Gmail) to extract posting URLs — the only LinkedIn channel we touch.
- Each source is independent. One failing source must not block others.

### 6.2 Storage / dedupe

- All postings normalized into one table.
- Dedupe across sources on a normalized key (company + title + canonical URL).
- Each posting carries a lifecycle `status` and a routed resume `type`.

### 6.3 Routing

- Classify each new posting into exactly one resume type from the fixed set: `quant-trader, quant-dev, mle, swe, fdse, ai, robotics, av`.
- Primary: deterministic title/JD keyword rules.
- Fallback: a single cheap LLM call (Haiku-class / GPT-mini-class) only when rules are ambiguous.
- Routing must be overridable by the operator (manual re-label).

### 6.4 Tailoring (core)

- Input: a posting (JD text) + the base `.tex`/`.pdf` for its routed type.
- **Pass 1:** produce a tailored `.tex` — surgical edits only, one-page constraint, no fabrication.
- **Pass 2:** adversarial review in two personas (senior engineer: 3 biggest weaknesses; hiring-manager/ATS: missing keywords), then apply fixes.
- Score before _and_ after on a fixed 5-axis rubric whose weights and keyword dictionary vary by type. At least one axis (keyword coverage) is computed deterministically, not self-reported by the LLM.
- Compile to PDF. Enforce one-page; if it overflows, send back to the model to cut. Never trust the model's self-report that it fit.
- Output artifacts per job: `tailored.tex`, `tailored.pdf`, `review.json` (before/after scores, weaknesses, missing-keyword list), `diff.txt`.

### 6.5 Interface

- An MCP server exposing the pipeline as tools: query jobs, preview routing, trigger tailoring, fetch a review, set status.
- A minimal review surface (a DB view / lightweight kanban) showing the funnel: `new → routed → approved_for_tailoring → tailored → reviewed → applied/rejected`.

### 6.6 Human gate

- Nothing is submitted automatically. Transition from `tailored` to `applied` is always a manual human action in v1.

## 7. Non-functional requirements

- **Reliability over completeness.** Missing a few postings is acceptable; the pipeline crashing is not. Every external dependency has a fallback or degrades gracefully.
- **Cost ceiling.** Per-job tailoring cost must stay low enough that the season fits the credit budget (see budget model in the tech plan). Discovery/routing must be near-free.
- **Idempotency.** Re-running any poller or re-tailoring any job must not create duplicates or corrupt state.
- **Observability.** Every run logs what it ingested, what it skipped, and why. Failures are visible, not silent.
- **Portability.** No hard dependency on a single LLM vendor. Swapping the tailoring model is a config change.

## 8. Success metrics

- **Coverage:** ≥ 90% of postings the operator would have found manually show up in the feed within a few hours, without manual searching.
- **Tailoring quality:** measured improvement delta (after − before composite score) is positive and meaningful on the majority of tailored jobs; the deterministic keyword-coverage axis improves on essentially all of them.
- **Operator time:** time from "this job looks good" to "review-ready tailored PDF in hand" drops to seconds of human attention.
- **Survival:** pipeline runs the full season with no more than occasional, recoverable manual intervention.
- **Budget:** total LLM spend stays within the available credits with margin to spare.

## 9. Risks & mitigations

| Risk                                                          | Mitigation                                                                                                                                       |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| LinkedIn / aggregator scraping breaks or gets blocked         | Treat GitHub lists + ATS APIs as the _reliable core_; aggregators and LinkedIn-email are _additive_. Pipeline is fully useful without them.      |
| LLM cost overruns budget                                      | Cheap models for routing; prompt-cache the base resume on tailoring; cap tailoring to operator-approved jobs only, never auto-tailor everything. |
| Model produces a resume that overflows one page or fabricates | Deterministic compile + page check; explicit no-fabrication constraint; human reviews diff before use.                                           |
| A single source/model outage stalls the pipeline              | Every stage isolated; per-source try/except; provider-agnostic LLM layer with fallback model.                                                    |
| Self-graded scores are meaningless                            | One axis is deterministic and grounds the rest; before/after use the identical scorer.                                                           |
| Scope creep into auto-submit                                  | Explicit non-goal; human gate is a hard architectural boundary in v1.                                                                            |

## 10. Phasing

1. **Core feed:** schema + GitHub-list + ATS pollers → queryable deduped table. _Useful on its own._
2. **Routing:** keyword router + LLM tiebreaker.
3. **Tailoring service:** two-pass loop + deterministic scorer + compile/one-page guard. _The piece that matters most._
4. **Interface:** MCP server + minimal review surface.
5. **Broader discovery:** JobSpy + Gmail/LinkedIn-email ingestion.
6. **(Optional, later):** form-fill assist on approved jobs only, human-gated.
