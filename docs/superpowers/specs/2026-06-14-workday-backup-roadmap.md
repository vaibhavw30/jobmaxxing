# Roadmap — Workday JD-acquisition backup (3 sub-projects)

**Type:** Multi-spec roadmap (overview only — each sub-project gets its own spec → plan → build)
**Date:** 2026-06-14
**Status:** Decomposed; sub-project 1 being specced first.

---

## Why

Workday is the largest unreached enrichment source (~6,900 description-less rows). The self-hosted headless worker (Phase 5b, merged) reaches ~43% on a residential IP — the rest are Cloudflare-gated at the cxs **API** level even inside a real headless browser, so we can't get their JDs that way. The pipeline still needs JDs (tailoring requires them) for the internships the operator would actually apply to. This roadmap is the backup for the un-enrichable remainder.

**Key facts that shape the design:**
- The 403s are **Cloudflare bot-detection** on the public cxs API — NOT a login/2FA wall. Viewing a public JD needs no account; 2FA only appears at *apply* time. (So "give the headless browser email/2FA creds" was rejected — wrong diagnosis + a security risk.)
- A **real human browser is not bot-blocked** — the operator viewing a Workday job in their own browser trivially sees the JD. This is what makes the human-in-the-loop fallback (sub-project 3) work.
- Many Workday jobs are **also posted elsewhere** (company careers site, LinkedIn, Indeed, Google Jobs) as plain HTML / `schema.org/JobPosting` JSON-LD that isn't Cloudflare-gated. This is what makes auto-recovery (sub-project 2) work.

## The three sub-projects (build order 1 → 2 → 3)

### Sub-project 1 — Title triage (the relevance engine) — FIRST, being specced now
Route the **enrichment-exhausted, no-JD** jobs on their **title/slug alone** (e.g. `…/Machine-Learning-Engineer-Co-op`) — rules first, LLM for the ambiguous ones — flagged as **title-only / low-confidence** so it's distinguishable from full-JD routing and the operator stays the gate. General (any ATS job that exhausts enrichment, not just Workday).
- **Why first:** it produces **relevance** — labels each stuck job with a résumé-type, so we know which are the target-type intern roles worth chasing (sub-projects 2 & 3 both consume this; without it the nightly queue would be thousands of jobs, not the relevant dozen).
- **Where:** the existing router (`routing/route.py`: `route_one`/`route_new`). Trigger = `enrich_attempts >= cap AND description empty`. Output = `resume_type` set + a title-only marker (route_method / confidence) + status moves out of "stuck."
- **Cost:** small; reuses the LLM router (and the claude-cli/Haiku providers).

### Sub-project 2 — Auto find-elsewhere (automated JD recovery)
For the *relevant* stuck jobs, **search external sources** (Google Jobs / company careers site / aggregators) and parse `JobPosting` JSON-LD or readable HTML to recover the real JD, sidestepping Cloudflare. Recovered JDs flow into normal routing + tailoring.
- **Hard parts:** choosing a search source (API vs scrape); HTML/JSON-LD extraction; a **matching guard** so we never write the wrong company's/role's JD (same corruption risk fixed in the Workday render tier — match on company + title/req-id, not just "a JobPosting").
- **Where it runs:** likely HTTP-only (no browser) → could run in CI if the sources aren't gated, else local. TBD in its spec.

### Sub-project 3 — Nightly operator queue + capture
Whatever's relevant but *still* has no JD after #2 lands in a queue the operator works in a nightly (~8pm) session, surfaced through the existing **MCP** ("show me tonight's relevant un-enriched jobs"). The operator opens each in their **real (non-bot) browser** and feeds the JD back in; ingesting it as the job's `description` triggers normal routing + tailoring.
- **Where:** builds on the Phase-4 MCP + the funnel/review views. Needs a "relevant + no-JD + exhausted" queue query and a low-friction JD-capture path (paste, or a browser helper in the operator's authenticated session).
- **Human stays the gate** — consistent with the no-autonomous-submission rule.

## Reset contract (sub-projects 2 & 3 MUST honor)
When sub-project 2 or 3 writes a real JD (`description`) to a job that sub-project 1 had title-routed (`route_method='llm_title'`) or marked `not_target`, it MUST also **reset `resume_type=NULL, route_method=NULL`** on that row, so the next `route_new` re-routes it from scratch with the full JD (a confident `rules`/`llm` decision replacing the provisional title-only one). See `2026-06-14-title-triage-design.md` §6.

## Dependencies & sequencing
1 (relevance) → 2 (auto-recover, prioritized by relevance) → 3 (human fallback for what 2 can't get). Each is independently shippable and testable. Build 1 now; revisit this roadmap for 2 and 3.
