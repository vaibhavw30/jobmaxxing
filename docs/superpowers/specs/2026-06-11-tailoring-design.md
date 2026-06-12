# Spec — Phase 3: Tailoring

**Sprint:** Phase 3 of the Internship Recruiting Pipeline (`docs/PRD.md` §6.4/§10.3, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md` §6/§7)
**Author:** Vaibhav
**Date:** 2026-06-11
**Status:** Approved for planning
**Builds on:** Phase 1 (core feed) + Phase 2 (routing), both merged. The `jobs` table already has `score_before jsonb`, `score_after jsonb`, `artifact_prefix text`, `status text`, `resume_type text`. The Phase-2 LLM wrapper (`src/jobmaxxing/llm/`) is reused and extended with prompt caching.

---

## 1. Goal & rationale

Given a job that the operator has **approved for tailoring**, produce a review-ready tailored one-page résumé from the routed type's base résumé, with a **quantified, deterministic before/after improvement number** and actionable LLM feedback. The human approves and submits — no autonomous submission.

This is the highest-value phase ("the piece that matters most"). Two design anchors keep it trustworthy:
- **Deterministic guards around the model.** The improvement score (keyword coverage) and the one-page check are computed in code, never self-reported by the LLM.
- **Operator-gated.** Tailoring runs only on jobs the operator explicitly approves — the single most important cost control. It is NOT in the scheduled workflow.

### Scope
**In:** deterministic keyword-coverage scorer; `pdflatex` compile + one-page guard; the two LLM passes (build, adversarial review + patch); S3 storage for base résumés and artifacts; prompt-caching in the LLM wrapper; operator-gated orchestration + CLI.
**Out (deferred):** the 4 self-graded LLM scoring axes (technical depth / impact / ATS / relevance ordering); the MCP server (Phase 4); cloud/Lambda compile. No DB migration (reuses existing columns).

## 2. Architecture & data flow

Operator-triggered, runs locally (the operator's machine has `pdflatex`). Boundaries (S3, LLM, compile) are injected so the orchestrator is fully testable.

```
operator: python -m jobmaxxing.tailor approve <job_id>     → status = approved_for_tailoring
operator: python -m jobmaxxing.tailor <job_id>:
   load job row (description=JD, resume_type)               [requires status approved_for_tailoring]
   storage.get_base_resume(resume_type)  → base .tex (from s3://<bucket>/base/{type}/main.tex)
   load_rubric(resume_type)              → rubrics/{type}.json  (in-repo, versioned)

   Pass 0  score_before = coverage(base_tex, JD, rubric)                       [deterministic]
   Pass 1  tailored_tex = llm('tailor', JD, cache=base_tex)                    [build]
   Pass 2  critique = llm('review', tailored_tex, JD) → {weaknesses[3], missing_keywords[]}
           patched_tex = llm('review', apply critique to tailored_tex) → .tex
   Pass 3  final_tex, pdf, page_count = compile + one-page guard(patched_tex)  [deterministic]
   Pass 4  score_after = coverage(final_tex, JD, rubric)                       [deterministic]

   storage.put_artifact(job_id, "tailored.tex" / "tailored.pdf" / "review.json" / "diff.txt")
   update jobs: score_before, score_after, artifact_prefix, status='tailored'
```

The operator later moves `tailored → applied | rejected` (manual; MCP in Phase 4).

### New package layout (`src/jobmaxxing/tailoring/`)
- `rubric.py` — load `rubrics/{type}.json` (`{keyword_dict, aliases}`).
- `scorer.py` — deterministic keyword-coverage scoring (static + dynamic), alias/boundary-aware.
- `latex.py` — `compile_pdf` (pdflatex subprocess + page count via `pypdf`) and the one-page guard loop.
- `diffing.py` — unified diff base→tailored.
- `storage.py` — `ArtifactStore` interface, `S3Store` (boto3), `InMemoryStore` (tests).
- `passes.py` — the LLM passes: build prompt/call, critique prompt + strict parser, patch prompt/call.
- `tailor.py` — `tailor_job` orchestration, `approve`, CLI `main`.
- Top-level shim `src/jobmaxxing/tailor.py` re-exporting `main` so `python -m jobmaxxing.tailor` resolves (Phase-2 lesson: nested module + documented top-level command).
- New deps: `boto3`, `pypdf`. `rubrics/{type}.json` at repo root.

## 3. Deterministic scorer (`scorer.py`) — the trustworthy anchor

Rubric `rubrics/{type}.json`:
```json
{ "keyword_dict": ["low-latency", "c++", "market data", "backtesting"],
  "aliases": { "c++": ["cpp", "c plus plus"], "ml": ["machine learning"] } }
```

A dict term is **covered** by text if the term OR any of its aliases appears, matched **boundary-aware** (reuse the Phase-2 routing matcher idea: lowercase, collapse whitespace, keep punctuation; `(?<![a-z0-9])term(?![a-z0-9])` so `ai`≠`training` and `c++` matches).

Two numbers, both fully deterministic (no LLM):
- **static_coverage** = |dict terms covered by résumé| / |dict terms|.
- **dynamic_coverage** = of the dict terms the **JD** mentions, the fraction the résumé covers: |{t ∈ dict : t in JD and t in résumé}| / |{t ∈ dict : t in JD}| (1.0 if the JD mentions none of the dict terms).

`score(resume_text, jd_text, rubric) -> {static, dynamic, matched: [...], missing: [...]}` where `missing` = dict terms in the JD but not the résumé (the deterministic "you're missing these" list). Persisted to `score_before` / `score_after`; `delta = {static: after.static − before.static, dynamic: after.dynamic − before.dynamic}`.

JD-specific terms outside our vocab are surfaced by the **LLM review's missing_keywords** (Pass 2), not as a self-graded score.

## 4. LaTeX compile + one-page guard (`latex.py`) — deterministic

- `compile_pdf(tex: str) -> CompileResult{pdf_bytes, page_count, log}`: write `tex` to a temp dir, run `pdflatex -interaction=nonstopmode -halt-on-error` (twice for refs), read **page count from the compiled PDF via `pypdf.PdfReader`** (never the model's claim). Raises `LatexError` (with the log tail) on a compile failure.
- `enforce_one_page(tex, *, compile_fn, shrink_fn, max_retries=3) -> OnePageResult{tex, pdf_bytes, page_count, retries}`: compile; if `page_count > 1`, call `shrink_fn(tex, page_count)` (the LLM "cut to one page") and recompile; cap at `max_retries`; if it never fits, return the **last version that did fit** (tracked across attempts) or, if none fit, the last attempt (flagged `fit=False`). The page count in the result is the measured count.
- `pdflatex` is a runtime/test dependency. `compile_pdf` is the mockable boundary: orchestration and the guard-loop logic are tested with a **mocked compile_fn**; one real-compile smoke test is **skipped when `pdflatex` is absent** (same pattern as the Postgres binary in earlier phases).

## 5. LLM passes + prompt caching (`passes.py`, `llm/`)

### 5.1 Wrapper change — prompt caching
Extend `llm.complete` to `complete(task, messages, *, max_tokens, response_format=None, cache=None)`:
- `cache` is a string of **deterministic, repeated** content (the base `.tex`). When set:
  - **Anthropic adapter:** sent as a `system` block with `cache_control: {type: "ephemeral"}` so it's prompt-cached across calls.
  - **OpenAI / xAI adapter:** prepended as an ordinary system message; their automatic prefix caching handles reuse. The `cache` arg is effectively a no-op flag for them.
- The base `.tex` is identical across every tailoring call all season → a large cost saver where supported.

### 5.2 New `llm.yaml` tasks (stronger tier)
```yaml
tasks:
  tailor:
    - {provider: anthropic, model: claude-sonnet-4-latest}   # strong writer; supports prompt caching
    - {provider: openai, model: gpt-4o}
  review:
    - {provider: anthropic, model: claude-sonnet-4-latest}
    - {provider: openai, model: gpt-4o}
```
(Exact model IDs are config you tune; `route` tier from Phase 2 unchanged.)

### 5.3 The passes
- **Pass 1 — build** (`build_tailored`): `complete('tailor', messages=[system(constraints) + user(JD)], cache=base_tex)` → tailored `.tex` (raw). Prompt constraints: surgical edits only, **one page**, **no fabrication** (only reorder/rephrase/emphasize existing facts), **preserve the template's structure/macros**, output only the full `.tex`.
- **Pass 2a — critique** (`critique_resume`): `complete('review', ...)` with two personas (senior engineer → the 3 biggest weaknesses vs this JD; hiring-manager/ATS → missing keywords a parser would flag). Returns **strict JSON** `{weaknesses: [3 strings], missing_keywords: [strings]}`, schema-gated like the router (parse failure → empty critique, logged; tailoring still completes with Pass-1 output).
- **Pass 2b — patch** (`apply_critique`): `complete('review', tailored_tex + critique → apply the fixes)` → patched `.tex` (raw). Kept separate from 2a so JSON and LaTeX never share one response (brace-escaping fragility).

## 6. Storage (`storage.py`) — S3 + in-memory fake

A thin interface so the orchestrator never imports boto3 directly:
```python
class ArtifactStore(Protocol):
    def get_base_resume(self, resume_type: str) -> str: ...        # s3://<bucket>/base/{type}/main.tex
    def put_artifact(self, job_id, name: str, data: bytes) -> None: # s3://<bucket>/tailored/{job_id}/{name}
    def artifact_prefix(self, job_id) -> str: ...                   # the s3:// prefix stored on the row
```
- `S3Store(bucket, client=boto3.client("s3"))` — bucket + AWS creds from env (`S3_BUCKET`, standard `AWS_*`). `get_base_resume` raises `BaseResumeMissing` if the key is absent (the operator hasn't supplied that type yet).
- `InMemoryStore` — same interface, dict-backed, for tests (no AWS).

**Deliberate deviation flagged for your review:** `rubrics/{type}.json` stay **in the repo** (versioned config the scorer reads every run, tuned alongside the routing dictionaries) rather than S3. Base résumés (your content) and artifacts go to S3 as chosen. Move rubrics to S3 only if you'd rather.

## 7. Orchestration, CLI, human gate (`tailor.py`)

```python
def tailor_job(conn, job_id, *, store, complete, compile_fn, rubric_loader, now) -> dict: ...
```
All boundaries injected (store, LLM `complete`, `compile_fn`, rubric loader) → unit-testable without AWS / network / pdflatex.

Flow: require the row's `status == 'approved_for_tailoring'` (else raise / skip with a clear message); run Passes 0–4; build `review.json` and `diff.txt`; `put_artifact` all four files; `update jobs set score_before=%s, score_after=%s, artifact_prefix=%s, status='tailored' where id=%s`. Re-running overwrites the same S3 prefix (idempotent).

`review.json` shape:
```json
{ "score_before": {"static": ..., "dynamic": ..., "missing": [...]},
  "score_after":  {"static": ..., "dynamic": ..., "missing": [...]},
  "delta": {"static": ..., "dynamic": ...},
  "weaknesses": ["...", "...", "..."],
  "missing_keywords": ["..."],
  "page_count": 1, "retries": 0, "fit": true }
```

**CLI** (`python -m jobmaxxing.tailor`):
- `approve <job_id>` → set `status='approved_for_tailoring'`.
- `<job_id>` → `tailor_job` over the live DB (real `S3Store`, real `complete`, real `compile_pdf`); prints the review summary.
- `review <job_id>` → fetch + print `review.json` from S3.

Tailoring is **never** wired into `pollers.yml` (operator-gated cost control).

## 8. Data model

No migration. Writes only existing columns: `score_before`/`score_after` (jsonb), `artifact_prefix` (text, the S3 prefix), `status` (`approved_for_tailoring` → `tailored`). The state machine `new → routed → approved_for_tailoring → tailored → reviewed → applied|rejected` is honored.

## 9. Testing

- **Scorer** (`scorer.py`): table-driven — static vs dynamic coverage, alias matching (`c++`/`cpp`), boundary safety, missing-list correctness, empty-JD edge. Pure, no I/O.
- **LaTeX guard** (`latex.py`): the `enforce_one_page` loop with a **mocked compile_fn** (returns 2 pages then 1; asserts shrink_fn called, retry cap, fallback-to-last-fitting). One real `compile_pdf` smoke test on a tiny `.tex` **skipped if `pdflatex` absent**.
- **Passes** (`passes.py`): build with mocked `complete` (returns a fixed tex, asserts `cache=base_tex` passed); critique strict-parse (valid JSON, bad JSON → empty critique, out-of-shape rejected); patch returns raw tex.
- **Storage**: `InMemoryStore` round-trips; `S3Store` with a mocked boto3 client (no network) asserts the right keys/prefixes; `BaseResumeMissing` on absent key.
- **Caching**: the Anthropic adapter applies `cache_control` when `cache` is set (mocked SDK); openai/xai prepend a system message (mocked SDK).
- **Orchestration** (`tailor_job`): `pytest-postgresql` + `InMemoryStore` + mocked `complete` + mocked `compile_fn` — seed an approved job, run, assert the 4 artifacts exist, `score_before/after` and `status='tailored'` written, and that a non-approved job is refused. End-to-end deterministic path (scorer + diff + review.json assembly) exercised through the real code with only the LLM/compile/S3 mocked.

## 10. Deliverables

- `src/jobmaxxing/tailoring/` (rubric, scorer, latex, diffing, storage, passes, tailor) + top-level `src/jobmaxxing/tailor.py` shim.
- LLM wrapper extended with `cache`; `config/llm.yaml` gains `tailor` + `review` tasks.
- `rubrics/{type}.json` seeded for the 8 types (keyword_dict from tech-plan §7.2 + aliases); a sample base `.tex` fixture for tests.
- Tests for every unit + the orchestration integration test.
- New deps `boto3`, `pypdf`; README "Tailoring" section (setup: S3 bucket + creds, base-resume convention, `pdflatex` install; how to approve/tailor/review; the human gate).

## 11. Open items (resolve during implementation, not blocking)

- Final `keyword_dict` / `aliases` per type — seed from tech-plan §7.2, tune against real tailored jobs and the deterministic delta.
- Exact `tailor`/`review` model IDs and whether to bias toward Anthropic (prompt caching) vs OpenAI (more credit) — config, tune against cost/quality.
- The one-page "cut to fit" prompt wording and the retry cap (start at 3).
- Whether rubrics ultimately move to S3 (default: stay in-repo, §6).
- Page-count tolerance: strictly 1 page (overflow to a 2nd page even by a line triggers a shrink) vs a small slack — start strict.
