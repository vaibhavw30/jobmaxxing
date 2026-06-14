# Spec — Title triage (route enrichment-exhausted, no-JD jobs on title)

**Type:** New feature — Workday-backup sub-project 1 of 3 (see `2026-06-14-workday-backup-roadmap.md`)
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** the router (Phase 2: `routing/route.py`, `tiebreaker.py`, `rules.py`, `types.py`) and the enrichment `enrich_attempts` column (Phase 5a). Extends the existing CI `route` step; no new pipeline stage.

---

## 1. Problem & rationale

A job whose deterministic rules are ambiguous (≥2 candidate types) **with no JD** is deferred by `route_one` (`route.py:34-35`, *"title-only ambiguity: defer until a JD arrives"*), and a job whose rules find no signal at all is deferred too (`route.py:29-30`). For Workday jobs that can't be enriched (the Cloudflare-gated majority — see the roadmap), the JD never arrives, so these sit **deferred forever** and never surface for review.

But a Workday URL/title embeds the role: `…/Machine-Learning-Engineer-Co-op_2611849`. The LLM can classify the résumé type from the title alone — lower confidence, but enough to (a) un-stick the job and (b) **establish relevance** (which stuck jobs are the SWE/ML/quant intern roles worth chasing in the later sub-projects). This sub-project routes the **enrichment-exhausted, no-JD** jobs on their title.

**Relevance falls out for free:** a deferred job the rules gave *candidate types* to has already matched target-role signals — so it's relevant by construction. The title-route then commits a provisional type and flags it; the rules-missed (0-candidate) jobs get an open classification with a "not a target role" escape so genuine target roles whose titles dodged the keyword signals aren't lost.

### Scope
**In:** extend `route_one`/`route_new` to title-route exhausted no-JD jobs; new `resolve_title_only` (tiebreak on title) and `classify_title` (open classify) in `tiebreaker.py`; a coarse internship pre-gate; a separate per-run title-route LLM budget; new `route_method` values `llm_title`/`not_target`; config thresholds; unit + integration tests.
**Out:** acquiring the JD elsewhere (sub-project 2) and the operator nightly queue (sub-project 3); any change to the deterministic rules, the enrichment workers, or the JD-bearing routing path.

## 2. Trigger — "enrichment exhausted, no JD"

A job is title-routable when **`enrich_attempts >= title_route_after` (default 3, = the enrichment cap) AND `description` is empty.** Until exhausted it keeps deferring (still legitimately waiting for enrichment). For Workday this fires after the operator's local worker has tried (and capped) it; for clean-ATS after CI enrichment permanent-fails it. This runs in the existing CI `route` step — no new stage.

## 3. The two title-route paths (with code)

Both are schema-gated like the existing tiebreaker and reuse `parse_tiebreaker_response`. New constant in `tiebreaker.py`:

```python
# Provisional confidence for a title-only route: capped low so it reads as "classified from
# the title, no JD" in the funnel — below any auto-advance threshold; the operator stays the gate.
_TITLE_ROUTE_CONFIDENCE = 0.4
```

### 3.1 `resolve_title_only` — tiebreak among candidates on the title (≥2-candidate case)

```python
def resolve_title_only(candidates: list[str], title, *, llm_complete, config) -> RouteDecision:
    """Tiebreak among `candidates` using the TITLE alone (no JD). method='llm_title' with a
    capped-low confidence. On LLMUnavailable / unparseable reply -> defer (retry next run)."""
    messages = build_tiebreaker_messages(
        title, "(no job description available — classify from the title)", candidates, config
    )
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)  # defer, retry later
    parsed = parse_tiebreaker_response(text, candidates)
    if parsed is None:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)  # couldn't decide -> defer
    resume_type, conf = parsed
    return RouteDecision(resume_type=resume_type, method="llm_title", confidence=min(conf, _TITLE_ROUTE_CONFIDENCE))
```

Reuses `build_tiebreaker_messages` (whose user turn is `Title: … / Job description: …`) with an explicit "no JD" placeholder so the model classifies honestly from the title.

### 3.2 `classify_title` — open classification (0-candidate / rules-missed case)

```python
def build_classify_messages(title, config) -> list[dict]:
    defs = "\n".join(f"- {t}: {config['types'][t].get('definition', '')}" for t in VALID_TYPES)
    allowed = ", ".join(VALID_TYPES)
    system = (
        "You assign an internship posting to exactly one resume type, or 'none' if it fits none.\n"
        f"Types:\n{defs}\n\n"
        f'Respond with STRICT JSON only: {{"type": <one of: {allowed}, none>, "confidence": <0.0-1.0>}}. '
        "No prose, no code fences."
    )
    user = f"Internship title (no job description available): {title}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def classify_title(title, *, llm_complete, config) -> RouteDecision:
    """Open-classify a title into one of VALID_TYPES, or 'none' (not a target role).
    -> method='llm_title' (a type) | method='not_target' (none) | defer (LLM error/unparseable)."""
    messages = build_classify_messages(title, config)
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)  # defer, retry later
    parsed = parse_tiebreaker_response(text, list(VALID_TYPES) + ["none"])
    if parsed is None:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)  # defer
    t, conf = parsed
    if t == "none":
        return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
    return RouteDecision(resume_type=t, method="llm_title", confidence=min(conf, _TITLE_ROUTE_CONFIDENCE))
```

### 3.3 Coarse internship pre-gate (saves LLM calls on obvious noise)

```python
_INTERNSHIP_MARKERS = ("intern", "co-op", "coop", "student", "apprentic", "new grad",
                       "early career", "university", "campus", "trainee", "co op")

def _looks_like_internship(title: str | None) -> bool:
    t = (title or "").lower()
    return any(m in t for m in _INTERNSHIP_MARKERS)
```

Applied only to the 0-candidate path: an exhausted, no-JD, no-rules-signal title that doesn't even look like an internship is marked `not_target` **without** an LLM call (very likely not what the operator wants; reversible if a JD later arrives).

## 4. `route_one` changes (with code)

Add two optional params (default off → existing behavior unchanged for all current callers/tests):

```python
def route_one(
    title, description, config, *, llm_complete, budget,
    exhausted: bool = False, title_budget: Budget | None = None,
) -> RouteDecision:
    outcome = route_by_rules(title, description, config)
    if outcome.decision == "routed":
        return RouteDecision(resume_type=outcome.resume_type, method="rules", confidence=outcome.confidence)

    if outcome.decision == "no_signal":
        if exhausted and title_budget is not None and title_budget.remaining > 0:
            if not _looks_like_internship(title):
                return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
            title_budget.remaining -= 1
            return classify_title(title, llm_complete=llm_complete, config=config)
        return _DEFER

    # ambiguous
    if len(outcome.candidates) == 1:
        return RouteDecision(resume_type=outcome.candidates[0], method="rules", confidence=_SINGLE_CANDIDATE_CONFIDENCE)
    if not description:
        if exhausted and title_budget is not None and title_budget.remaining > 0:
            title_budget.remaining -= 1
            return resolve_title_only(outcome.candidates, title, llm_complete=llm_complete, config=config)
        return _DEFER  # not exhausted (or out of title budget): still waiting for a JD
    if budget.remaining <= 0:
        return _DEFER
    budget.remaining -= 1
    return resolve(outcome.candidates, title, description, llm_complete=llm_complete, config=config)
```

`resolve_title_only` and `classify_title` are imported from `tiebreaker.py` (alongside the existing `resolve`); `_looks_like_internship`/`_INTERNSHIP_MARKERS` live in `route.py` (used only by `route_one`).

## 5. `route_new` changes (with code)

Fetch `enrich_attempts`, compute `exhausted`, allot a **separate** title budget, and split the write (title-routes & rules/llm go to the routed update; `not_target` gets only a marker, no `status='routed'`).

```python
cap = max_llm_calls if max_llm_calls is not None else cfg.get("thresholds", {}).get("max_llm_calls_per_run", 200)
thresholds = cfg.get("thresholds", {})
title_after = thresholds.get("title_route_after", 3)
budget = Budget(remaining=cap)
title_budget = Budget(remaining=thresholds.get("title_route_max_llm", 100))   # separate cap

if reroute:
    where = "route_method is distinct from 'manual'"
else:
    where = ("resume_type is null and route_method is distinct from 'manual' "
             "and route_method is distinct from 'not_target'")   # don't re-classify decided non-targets

rows = conn.execute(
    f"select id, title, description, enrich_attempts from jobs where {where}"
).fetchall()
counts = {"rules": 0, "llm": 0, "llm_title": 0, "not_target": 0, "deferred": 0, "manual_skipped": 0}
routed_updates: list[tuple] = []      # (resume_type, method, confidence, id) -> status='routed'
nontarget_updates: list[tuple] = []   # (id,) -> route_method='not_target' only

for job_id, title, description, enrich_attempts in rows:
    exhausted = (enrich_attempts or 0) >= title_after and not (description or "").strip()
    try:
        decision = route_one(title, description, cfg, llm_complete=do_llm, budget=budget,
                             exhausted=exhausted, title_budget=title_budget)
    except Exception as exc:  # noqa: BLE001 - one bad row never aborts the run
        logger.warning("route: job %s failed: %s", job_id, exc)
        counts["deferred"] += 1
        continue
    if decision.method is None:
        counts["deferred"] += 1
    elif decision.method == "not_target":
        nontarget_updates.append((job_id,))
        counts["not_target"] += 1
    else:
        routed_updates.append((decision.resume_type, decision.method, decision.confidence, job_id))
        counts[decision.method] += 1

with conn.transaction(), conn.cursor() as cur:
    if routed_updates:
        cur.executemany(
            "update jobs set resume_type=%s, route_method=%s, route_confidence=%s, status='routed' where id=%s",
            routed_updates,
        )
    if nontarget_updates:
        cur.executemany("update jobs set route_method='not_target' where id=%s", nontarget_updates)
```

(The `manual_skipped` count + the final log line stay as-is. The single-transaction batched write is preserved — now two `executemany`s in the one transaction.)

A title-routed job gets `status='routed'`, `route_method='llm_title'`, a real `resume_type`, and `route_confidence<=0.4`, but **no description** — so it can't auto-advance to tailoring (tailoring needs a JD); the empty description is its own gate, and `route_method='llm_title'` flags it for the sub-project-3 review queue.

## 6. Reroute contract (the seam to sub-projects 2 & 3)

When a real JD later arrives for an `llm_title` or `not_target` job (find-elsewhere or operator capture), that writer **must reset `resume_type=NULL, route_method=NULL`** on the row so the next `route_new` re-routes it from scratch with the full JD (a confident `rules`/`llm` decision). Documented here and in the roadmap; sub-projects 2/3 implement it.

## 7. Config (`config/routing.yaml` thresholds)

Add to the existing `thresholds:` map:
```yaml
thresholds: { ..., title_route_after: 3, title_route_max_llm: 100 }
```
`title_route_after` should match the enrichment cap (3). `title_route_max_llm` bounds the per-run title-route LLM spend so a large stuck backlog drains gradually without starving normal JD-routing of its own `max_llm_calls_per_run` budget.

## 8. Invariants (preserved)

| Invariant | How |
| --- | --- |
| JD-bearing routing unchanged | `route_one`'s new params default off; the `description`-present path is untouched (`method='llm'`/`rules`). |
| Not-yet-enriched jobs keep waiting | Title-routing only fires when `enrich_attempts >= title_route_after` AND no description. |
| Manual rows untouched | Candidate `where` still excludes `route_method='manual'`. |
| Normal-route budget not starved | Title-routing uses a **separate** `title_budget`; the JD `budget` is unchanged. |
| No repeated spend on decided non-targets | `not_target` rows excluded from the candidate query; pre-gate avoids LLM calls on non-internship titles. |
| Batched single-transaction write | Two `executemany`s in one `conn.transaction()`. |
| Per-row isolation | The existing try/except per row is unchanged. |
| Reversible when a JD arrives | Sub-projects 2/3 reset `resume_type`/`route_method` (§6). |

## 9. Testing — pyramid

### 9.1 Unit — `tests/test_routing_tiebreaker.py` (or a new `test_title_routing.py`), no DB/LLM (inject a fake `llm_complete`)
- `_looks_like_internship`: `"Software Engineering Intern"`/`"ML Co-op"`/`"Data Apprentice"` → True; `"Senior Director, Finance"` → False.
- `resolve_title_only`: valid in-enum JSON reply → `method='llm_title'`, `confidence == min(reply, 0.4)`; reply confidence 0.9 → capped to 0.4; LLMUnavailable / unparseable → `method is None` (defer).
- `classify_title`: a valid type → `llm_title`; `"none"` → `method='not_target'`, `resume_type is None`; LLMUnavailable/unparseable → defer.
- `route_one` (inject fakes for `resolve_title_only`/`classify_title` or a fake `llm_complete`):
  - exhausted + ≥2 candidates + no JD → `llm_title`; consumes one `title_budget`, not the JD `budget`.
  - exhausted + 0 candidates + internship title → goes through `classify_title`.
  - exhausted + 0 candidates + non-internship title → `not_target` with **no** LLM call.
  - NOT exhausted + ≥2 candidates + no JD → `_DEFER` (unchanged).
  - exhausted but `title_budget` empty → `_DEFER`.
  - has JD + ambiguous → existing `resolve` path, `method='llm'` (regression guard).

### 9.2 Integration — `tests/test_route_db.py` (pytest-postgresql)
- Seed an exhausted (`enrich_attempts=3`), no-description, ambiguous-title row → after `route_new` with a fake LLM: `route_method='llm_title'`, `route_confidence<=0.4`, `resume_type` set, `status='routed'`; counts `llm_title>=1`.
- Seed an exhausted, no-description, rules-missed **internship** title → `llm_title` or `not_target` per the fake LLM; a `not_target` row keeps `resume_type` NULL and is **not reselected** on a second `route_new` run.
- Seed a NOT-exhausted (`enrich_attempts=0`) no-description row → stays deferred (not title-routed).
- Seed a JD-bearing ambiguous row alongside → still routes via the normal `llm` path, and the title-route work draws from the separate budget (assert the JD `max_llm_calls` budget isn't consumed by title-routing).
- All existing `test_route_db.py` tests stay green (the JD path is unchanged).

## 10. Deliverables

- `tiebreaker.py`: `_TITLE_ROUTE_CONFIDENCE`, `build_classify_messages`, `resolve_title_only`, `classify_title`.
- `route.py`: `_looks_like_internship` + `_INTERNSHIP_MARKERS`; `route_one` `exhausted`/`title_budget` params + the two title-route branches; `route_new` enrich_attempts fetch, separate title budget, `not_target` exclusion + split write, expanded counts.
- `config/routing.yaml`: `title_route_after`, `title_route_max_llm`.
- Tests (unit + integration); roadmap updated with the §6 reset contract.
- No migration (reuses `enrich_attempts`; `route_method` is free text), no new pipeline stage, no CI workflow change.

## 11. Open items & risks (named, accepted)

- **Provisional misclassification:** a title-only route can pick the wrong type. Mitigated by the low confidence flag (operator reviews) and the §6 reset-on-JD-arrival (a real JD re-routes confidently). Acceptable — better than deferred-forever.
- **`not_target` false negatives:** a relevant role with an odd title could be marked `not_target`. Reversible when a JD arrives (§6); the pre-gate + open-classify "none" escape are deliberately conservative. Worth eyeballing the first run's `not_target` count.
- **Title-route LLM cost:** bounded by `title_route_max_llm`/run and the one-time nature of the exhausted backlog; uses the cheap route models (Haiku / the claude-cli subscription).
