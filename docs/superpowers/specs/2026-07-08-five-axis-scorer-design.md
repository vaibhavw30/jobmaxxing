# Five-axis tailoring scorer + code-fence fix — design

## Context
Phase 3 tailoring is engine-complete, but a first-ever end-to-end validation (2026-07-08, against a real
JD + a scaffold base résumé + a real `pdflatex`) surfaced **two gaps**:

1. **The scorer is keyword-coverage-only.** `tailoring/scorer.py::score()` returns
   `{static, dynamic, matched, missing}` — just the deterministic keyword axis. The PRD §7 five-axis
   weighted composite (keyword_coverage + technical_depth + impact + ats + relevance_order) and its LLM
   qualitative scoring call were never built. The 8 `rubrics/{type}.json` carry **no `weights`** (only
   `keyword_dict` + `aliases`), so there is nothing to weight a composite with.
2. **P0 bug — tailored `.tex` is uncompilable.** The three `.tex`-producing passes in `passes.py`
   (`build_tailored`, `apply_critique`, `shrink_to_one_page`) `return complete(...)` on the **raw** model
   text. Models wrap output in a ```` ```latex … ``` ```` fence, which lands in the file → `pdflatex` fails
   with "Missing \begin{document}". The `claude-cli` provider strips a fence, but the API providers do not,
   so the output is provider-dependent and currently broken. Never caught because tailoring had never
   compiled a real PDF.

## Goal
Implement the PRD §7 **five-axis weighted scorer** (deterministic keyword axis + four LLM-graded axes +
rubric-weighted composite, temperature-0, identical before/after), and **fix the fence bug** so tailoring
produces a valid one-page PDF. The deterministic keyword axis stays the trustworthy anchor; the composite
is grounded because one of its five axes is deterministic.

## Design

### Seams — three functions in `tailoring/scorer.py`
- **`score_keywords(resume, jd, rubric) -> dict`** — **pure/deterministic**, byte-for-byte today's
  `score()` renamed. Returns `{static, dynamic, matched, missing}`. The grounded anchor; no LLM.
- **`score_qualitative(resume, jd, rubric, *, complete) -> dict`** — **new, LLM-injected.** One
  temperature-0 call returns `{technical_depth, impact, ats, relevance_order}`, each **0–10**, anchored to
  the rubric (the call is handed the type's `keyword_dict` + explicit axis definitions + the JD + the
  résumé). Lenient parse (below). No pandas/network beyond the injected `complete`.
- **`score(resume, jd, rubric, *, complete) -> dict`** — **pure composition** over the two. Assembles the
  five 0–10 axes and the weighted composite. This is what `tailor.py` calls for Pass 0 (before) and Pass 4
  (after).

### Score dict — additive, backward-compatible
`score()` returns a **superset** of today's dict (existing `review.json`/DB readers keep working):
```json
{
  "static": 0.9, "dynamic": 1.0,              // unchanged deterministic coverage (grounded anchor)
  "matched": ["python", "..."], "missing": [],
  "axes": {                                    // each 0–10
    "keyword_coverage": 10.0,                  // = 10 × dynamic
    "technical_depth": 6.0, "impact": 5.0, "ats": 8.0, "relevance_order": 7.0
  },
  "composite": 7.80                            // rubric-weighted sum of axes, 0–10, round(…, 2)
}
// composite worked with swe weights (kc .3, td .2, im .1, at .3, ro .1):
//   .3×10 + .2×6 + .1×5 + .3×8 + .1×7 = 3.0 + 1.2 + 0.5 + 2.4 + 0.7 = 7.80
```
- **`keyword_coverage` axis = `10 × dynamic`** (JD-conditioned coverage — "of the dict terms *this JD*
  wants, the fraction the résumé has"; the more meaningful "survives *this* ATS" signal than JD-independent
  `static`).
- **`composite = round(Σ weights[axis] × axes[axis], 2)`**, weights summing to 1.0 → composite in [0, 10].

`delta(before, after)` is extended to `{static, dynamic, composite, axes: {per-axis deltas}}` (keeps
`static`/`dynamic` for continuity; adds `composite` + each axis's after−before).

### The LLM qualitative scoring call
- **New task tier `score`** in `config/llm.yaml`, listing the **anthropic API model first**
  (temperature-0 capable → deterministic delta) with `claude-cli` as a best-effort fallback. Scoring is
  2 calls/job (before + after) → negligible API cost even over the season.
- **temperature = 0**, identical prompt for before and after (fair delta). See "Determinism" below.
- **System prompt** defines the four axes crisply so scoring is anchored, not vibes:
  - `technical_depth` — does the résumé show the *level* the role wants, not just the noun?
  - `impact` — bullets carrying real numbers/outcomes.
  - `ats` — clean structure, standard headers, no parser-breaking formatting.
  - `relevance_order` — most JD-relevant experience surfaced first.
  It is handed the rubric's `keyword_dict` (the same anchor the deterministic axis uses), the JD, and the
  résumé. Demands STRICT JSON `{technical_depth, impact, ats, relevance_order}`, each an integer/float 0–10.
- **`parse_qualitative(text)`** — lenient, mirroring `passes.parse_critique`: extract the JSON object;
  coerce each of the four axes to a float and **clamp to [0, 10]**; on ANY structural failure (no JSON,
  bad types, missing keys) return all four = **5.0** (neutral). A flaky scoring call therefore never
  crashes tailoring — the delta just reads ≈0 on the qualitative axes that turn.

### Per-type weights — added to every `rubrics/{type}.json`
A `weights` block (five axes, summing to 1.0), seeded from the PRD §7.2 emphasis table, following the
PRD's own example magnitudes (two heaviest 0.3, then 0.2/0.1/0.1). Keys:
`keyword_coverage` (kc), `technical_depth` (td), `impact` (im), `ats` (at), `relevance_order` (ro).

| type | kc | td | im | at | ro | (heaviest per PRD §7.2) |
| --- | --- | --- | --- | --- | --- | --- |
| quant-trader | 0.3 | 0.2 | 0.3 | 0.1 | 0.1 | keyword + impact |
| quant-dev    | 0.3 | 0.3 | 0.2 | 0.1 | 0.1 | technical depth + keyword |
| mle          | 0.2 | 0.3 | 0.3 | 0.1 | 0.1 | technical depth + impact |
| swe          | 0.3 | 0.2 | 0.1 | 0.3 | 0.1 | keyword + ATS |
| fdse         | 0.2 | 0.1 | 0.3 | 0.1 | 0.3 | impact + relevance ordering |
| ai           | 0.2 | 0.3 | 0.1 | 0.1 | 0.3 | technical depth + relevance |
| robotics     | 0.3 | 0.3 | 0.2 | 0.1 | 0.1 | technical depth + keyword |
| av           | 0.3 | 0.3 | 0.1 | 0.2 | 0.1 | technical depth + keyword |

(Each row sums to 1.0; numbers are tunable in review.) `load_rubric` defaults `weights` to an equal
`{0.2 × 5}` vector when absent, so a rubric without weights still scores.

### Determinism & the LLM layer
- Add an **optional `temperature` parameter** threaded through `llm/client.py::complete()` into the API
  adapters (`_openai_compatible`, `_anthropic`); default `None` → omitted → **behavior unchanged** for
  every existing caller. `claude-cli` cannot set temperature via `claude -p`, so it **ignores** the param
  (documented); that is why the `score` tier prefers the API provider.
- Even if the `claude-cli` fallback is used, the **identical before/after prompt** keeps the delta fair in
  expectation; temperature-0 on the API path makes it exactly reproducible.

### Fence fix — `tailoring/passes.py`
- **`_strip_code_fence(text) -> str`**: if the stripped text starts with a ```` ``` ```` fence
  (optionally ```` ```latex ````/```` ```tex ````) and ends with a closing ```` ``` ````, remove the two
  fence lines and return the inner body; otherwise return the text unchanged. Only ever strips a **single
  wrapping** fence — never touches fences in the interior.
- Apply it to the three `.tex`-producing passes: `build_tailored`, `apply_critique`, `shrink_to_one_page`.
  NOT to `critique_resume` (that returns JSON, already gated by `parse_critique`).

### `tailor.py` wiring
Pass 0 → `before = score(base_tex, jd, rubric, complete=complete)`; Pass 4 →
`after = score(final_tex, jd, rubric, complete=complete)`. `review` gains `composite`/`axes` via the new
dict; `delta` uses the extended `delta()`. `score_before`/`score_after` jsonb columns store the superset.
The `tailor_job` signature already injects `complete`, so no new boundary.

### Backward compatibility
Purely additive: DB `score_before`/`score_after` gain keys (existing readers unaffected); `review.json`
is a superset; the MCP `get_review` contract is unchanged (richer content). No migration.

## Error handling / robustness
- Lenient `parse_qualitative` → tailoring never crashes on a bad scoring response (neutral 5.0 fallback).
- All axes clamped to [0, 10]; composite is a bounded weighted sum.
- Missing rubric `weights` → equal-weight default.
- `_strip_code_fence` is defensive: only a wrapping fence is removed, unfenced text passes through.

## Testing (pyramid)
- **`score_keywords`** — unchanged deterministic tests (renamed from the `score` tests).
- **`score_qualitative`** (fake `complete`) — good JSON parses; out-of-range clamps to [0,10]; malformed /
  missing-keys / non-JSON → all-5.0 neutral.
- **`score` composition** — pure: fake qualitative + a known `dynamic` + known weights → exact composite,
  exact `axes`, and the additive dict shape (static/dynamic/matched/missing preserved).
- **weights** — every `rubrics/*.json` `weights` sums to 1.0 (parametrized); `load_rubric` default when
  absent.
- **`delta`** — composite + per-axis deltas; static/dynamic preserved.
- **`_strip_code_fence`** — ```` ```latex ````-wrapped, plain ```` ``` ````-wrapped, no fence, and interior
  ```` ``` ```` left intact; applied outputs of the three passes are fence-free (behind a fake `complete`).
- **temperature plumbing** — `complete(..., temperature=0)` forwards to the adapter (spy); default omits it.
- **e2e real-compile validation** (local, operator machine w/ `pdflatex`) — one real approved job →
  `tailored.pdf` is a valid ≤1-page PDF and `review.json` carries `composite` before/after + all five axes.

## Out of scope
Retuning `keyword_dict` contents; multi-sample / adversarial scoring (single temp-0 call chosen);
changing the tailoring prompts beyond the fence strip; surfacing `composite` in the web triage UI (the DB
carries it; UI is a later concern); OpenAI/xAI being the scoring provider (anthropic-first by config).

## Execution
Isolated git worktree; subagent-driven TDD — **fence fix first** (so the real compile is unblocked and
confirmed), then the scorer seams, weights, and wiring; two-stage review (spec → quality) per task; Opus
whole-branch review; a final real-`pdflatex` e2e validation; merge to `main`; push (gh `vaibhavw30`).
