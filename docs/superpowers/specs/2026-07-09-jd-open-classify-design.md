# JD-grounded open-classification for `no_signal` rows — design

## Context
Live-data investigation (2026-07-09) into why 8,776 jobs sit unrouted found a genuine, permanent logic
gap in `route_one` (`src/jobmaxxing/routing/route.py`), not a timing or budget issue. Of 397 jobs that
already have a real job description but are still unrouted, **381 (96%) hit the deterministic rules'
`no_signal` outcome** (neither the title nor the JD matches any of the 8 types' keyword dictionaries) —
and for that case, the code only offers an LLM fallback when `exhausted` (enrichment gave up, no JD
exists at all). A row that HAS a JD but simply doesn't hit any rule keyword falls straight to
`return _DEFER` with **no LLM call ever attempted**, regardless of how much LLM budget is unused. This
has been silently accumulating since routing went live — the oldest confirmed stuck sample dates to
2026-06-14, deferred on every 3-hourly rules-only run and every ~4-day LLM-tiebreak run since.

The sibling gap for JD-less rows was already solved: when `exhausted` is true, `route_one` calls
`classify_title` (title-only open classification against all 8 types + `'none'`). This fix builds the
equivalent open classification for the has-JD case — grounded in the JD text, which is strictly better
signal than a title-only guess.

## Goal
Give every `no_signal`-with-a-JD row a real path to resolution: an LLM open-classification call scoped
across all 8 types + `'none'`, using the actual JD text, gated by the existing main routing budget so it
never runs unbounded.

## Design

### `classify_open` — a fourth sibling in `tiebreaker.py`
Alongside `resolve` (rules-narrowed JD tiebreak), `resolve_title_only` (rules-narrowed title tiebreak),
and `classify_title` (open title-only classification), add:

```python
def classify_open(title, description, *, llm_complete, config) -> RouteDecision:
    """Open-classify a title+JD (zero rule signal) -> method='llm_open' (a type) |
    'not_target' ('none') | defer (LLM error/unparseable). Confidence is the LLM's raw
    value, uncapped -- unlike classify_title/resolve_title_only, this call is grounded in
    a real JD, so it is trusted the same way resolve()'s JD-backed tiebreak is."""
```

It reuses `build_classify_messages`-style prompt construction (all 8 configured types + `'none'`, STRICT
JSON `{"type": ..., "confidence": ...}`) but with the actual JD in the user message instead of the
"(no job description available)" placeholder `build_classify_messages` uses today. `build_classify_messages`
gains an optional `description` parameter (default `None` → today's exact placeholder text, so
`classify_title`'s existing call site and behavior are byte-for-byte unchanged) so both functions share
one prompt-building path — no duplicated prompt-construction code between the JD-grounded and title-only
open-classify calls.

Parsing reuses the existing `parse_tiebreaker_response` + `_classify_choices` (no new parsing logic).
`'none'` → `RouteDecision(resume_type=None, method="not_target", confidence=0.0)` (mirrors
`classify_title`). A valid type → `RouteDecision(resume_type=t, method="llm_open", confidence=conf)` —
**confidence is NOT capped** (unlike `classify_title`/`resolve_title_only`'s `_TITLE_ROUTE_CONFIDENCE`
cap of 0.4), because a real JD backs the answer, the same trust level `resolve()` already extends to its
JD-grounded tiebreak. Any parse failure or `LLMUnavailable` → defer (retry on a later run, consistent
with every other LLM path in this module).

### Integration — one new branch in `route_one`
```python
if outcome.decision == "no_signal":
    if exhausted and title_budget is not None and title_budget.remaining > 0:
        if not _looks_like_internship(title):
            return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
        title_budget.remaining -= 1
        return classify_title(title, llm_complete=llm_complete, config=config)
    if description and budget.remaining > 0:
        budget.remaining -= 1
        return classify_open(title, description, llm_complete=llm_complete, config=config)
    return _DEFER
```
The new branch fires only when `description` is truthy (a real JD exists — the exact complement of the
`exhausted` branch above it, which only fires when there is none) and the **main** `budget` (the same pool
`resolve()` draws from — `max_llm_calls_per_run`, default 200/run) has room. No new budget/config knob.
Everything else — the `ambiguous` branch, `_looks_like_internship`'s coarse pre-gate (used only by the
exhausted/title path, unchanged), the batched-write transaction, `route_new`'s counts dict — is untouched.
`counts["llm_open"]` needs one new key so the summary log/return dict accounts for the new method (mirrors
how `counts` already has a key per existing method).

### Why the main budget, not a new one
This is JD-grounded classification — the same information tier as `resolve()`'s ambiguous-JD tiebreak,
which already draws from the main budget. Giving it a separate pool (like `title_budget`, which exists
specifically to keep the JD-less title-triage path from starving normal JD routing) would be
inconsistent: there is no analogous risk here of a JD-less flood starving anything, since this path
requires a JD to fire at all.

## Error handling / robustness
No new error paths. `classify_open` follows the exact `resolve`/`classify_title` shape: `LLMUnavailable`
and any parse failure both defer (retry next run) rather than raise; a single bad row's exception in
`route_new`'s per-row `try/except` is already isolated and never aborts the batch (unchanged).

## Testing (pyramid)
- **Unit — `build_classify_messages` with a description:** the user message embeds the real JD text
  instead of the placeholder; the no-description call site (`classify_title`'s existing behavior)
  produces byte-identical output to before this change.
- **Unit — `classify_open`:** a valid in-enum type response → `RouteDecision(method="llm_open",
  confidence=<raw, uncapped>)`; `"none"` → `not_target`; malformed/unparseable JSON → defer;
  `LLMUnavailable` → defer. Mirrors `classify_title`'s existing test shape exactly.
- **Unit — `route_one` integration:** a `no_signal` decision with a real `description` and budget
  remaining calls `classify_open` (not `_DEFER`); budget is decremented from the **main** `budget` object,
  not `title_budget`; when `budget.remaining == 0`, it still defers (no regression to the budget gate).
- **No regression:** existing `route_one`/`route_new`/`tiebreaker` test suites stay green — this only adds
  a new branch + a new function; the `exhausted` branch, `ambiguous` branch, and every existing
  `resolve`/`resolve_title_only`/`classify_title` test case are untouched in behavior.

## Out of scope
Retuning the rule keyword dictionaries themselves (a `no_signal` outcome is now *resolved* via LLM, not
prevented — that's a separate, ongoing tuning task already on the roadmap); the 16 ambiguous-and-
budget-starved stuck rows (already have a working path via `resolve()`, no code change needed); a
backfill/reroute of the historical stuck backlog (the next scheduled `route` run naturally picks these
rows up — no reason for the JD-having-but-unrouted set); changing `max_llm_calls_per_run`'s default value.

## Execution
Isolated git worktree off `main`; subagent-driven TDD (single task — small, cohesive, mirrors an existing
pattern closely enough not to need decomposition); one review pass (spec + quality); merge to `main`;
push (gh `vaibhavw30`).
