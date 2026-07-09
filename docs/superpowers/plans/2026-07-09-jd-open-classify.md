# JD-grounded open-classification for `no_signal` routing rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every job posting that has a real description but zero rule-keyword signal a real path to a routing decision — today it defers forever, with no LLM call ever attempted, regardless of unused budget.

**Architecture:** Add `classify_open` — a fourth sibling to `resolve`/`resolve_title_only`/`classify_title` in `tiebreaker.py` — that open-classifies a title+JD pair (all 8 configured types + `'none'`) with the LLM's raw, uncapped confidence. `build_classify_messages` gains an optional `description` parameter (default `None`, so `classify_title`'s existing call is byte-for-byte unchanged) so both the title-only and JD-grounded open-classify prompts share one builder. `route_one` gains one new branch in its `no_signal` handling that fires only when a real JD exists and the main LLM budget has room.

**Tech Stack:** Python 3.12, existing routing/`llm` modules. No new dependency.

## Global Constraints
- Python 3.12. Run pytest with the Postgres binary on PATH for the integration test:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- **`classify_open`'s confidence is the LLM's raw value, NOT capped** — unlike `classify_title`/
  `resolve_title_only`, which cap at `_TITLE_ROUTE_CONFIDENCE` (0.4). This call has a real JD, trusted
  the same way `resolve()`'s JD-backed tiebreak already is.
- **New branch draws from the MAIN `budget`** (the same pool `resolve()` uses — `max_llm_calls_per_run`),
  NOT `title_budget` (which exists specifically for the JD-less title-triage path). No new config knob.
- **`build_classify_messages(title, config)` with no `description` arg must produce byte-identical output
  to before this change** — `classify_title`'s existing call site is untouched.
- The new branch fires ONLY when `description` is truthy (a real JD exists) — the exact complement of the
  `exhausted` branch above it in `route_one`, which fires only when there is none. The existing `ambiguous`
  branch, `_looks_like_internship`'s pre-gate, and the batched-write transaction in `route_new` are
  UNCHANGED.
- `route_new`'s `counts` dict (`src/jobmaxxing/routing/route.py:100`) MUST gain a `"llm_open": 0` key —
  `counts[decision.method] += 1` does not auto-vivify missing keys, so a `classify_open`-produced decision
  would raise `KeyError` without this.
- Worktree cwd discipline: pin every subagent's cwd to the feature worktree; verify
  `git rev-parse --show-toplevel` before any commit. Push with the `vaibhavw30` gh account.

---

## Task 1: `classify_open` + `build_classify_messages(description=...)` + `route_one` wiring

**Files:**
- Modify: `src/jobmaxxing/routing/tiebreaker.py`, `src/jobmaxxing/routing/route.py`
- Test: `tests/test_tiebreaker_resolve.py`, `tests/test_route_one.py`, `tests/test_route_db.py`

**Interfaces:**
- Produces: `build_classify_messages(title, config, description=None) -> list[dict]` (extended signature,
  backward-compatible default); `classify_open(title, description, *, llm_complete, config) -> RouteDecision`
  (method is `"llm_open"` for a matched type, `"not_target"` for `"none"`, `None` to defer). `route_one`'s
  public signature is UNCHANGED.

- [ ] **Step 1: Write failing unit tests for `build_classify_messages` + `classify_open`.** Append to
`tests/test_tiebreaker_resolve.py` (it already has `_FULL_CONFIG` — all 8 types — and imports
`build_classify_messages`/`classify_title`; add `classify_open` to that import line):
```python
def test_build_classify_messages_embeds_real_jd_when_provided():
    msgs = build_classify_messages("ML Intern", _FULL_CONFIG, description="Train models on GPUs.")
    assert "Train models on GPUs." in msgs[1]["content"]
    assert "no job description available" not in msgs[1]["content"]


def test_build_classify_messages_defaults_to_title_only_placeholder():
    # description=None (the default) -> byte-identical to classify_title's existing behavior.
    msgs = build_classify_messages("ML Intern", _FULL_CONFIG)
    assert "no job description available" in msgs[1]["content"]


def test_classify_open_returns_llm_open_with_uncapped_confidence():
    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.83}'
    d = classify_open("Data Specialist Intern", "work with data pipelines",
                      llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type == "mle" and d.method == "llm_open"
    assert d.confidence == 0.83          # NOT capped at 0.4, unlike classify_title


def test_classify_open_none_is_not_target():
    def fake_llm(task, messages, **kw):
        return '{"type": "none", "confidence": 0.9}'
    d = classify_open("Warehouse Associate", "load and unload trucks",
                      llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type is None and d.method == "not_target"


def test_classify_open_defers_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("no provider")
    d = classify_open("Some Intern", "some jd", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.method is None


def test_classify_open_defers_on_unparseable_reply():
    def fake_llm(task, messages, **kw):
        return "not json at all"
    d = classify_open("Some Intern", "some jd", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.method is None
```
Run: `uv run pytest tests/test_tiebreaker_resolve.py -q` → FAIL (`classify_open` missing; the two
`build_classify_messages` tests also fail since the function doesn't yet accept `description`).

- [ ] **Step 2: Implement in `src/jobmaxxing/routing/tiebreaker.py`.** Replace the current
`build_classify_messages` function:
```python
def build_classify_messages(title, config) -> list[dict]:
    """Open classification prompt: pick one of the configured types, or 'none' (not a target role)."""
    choices = _classify_choices(config)
    types_in_config = [c for c in choices if c != "none"]
    defs = "\n".join(f"- {t}: {config['types'][t].get('definition', '')}" for t in types_in_config)
    allowed = ", ".join(choices)   # includes 'none'; mirrors the parser's allowed set exactly
    system = (
        "You assign an internship posting to exactly one resume type, or 'none' if it fits none.\n"
        f"Types:\n{defs}\n\n"
        f'Respond with STRICT JSON only: {{"type": <one of: {allowed}>, "confidence": <0.0-1.0>}}. '
        "No prose, no code fences."
    )
    user = f"Internship title (no job description available): {title}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
```
with:
```python
def build_classify_messages(title, config, description=None) -> list[dict]:
    """Open classification prompt: pick one of the configured types, or 'none' (not a target role).
    With description=None (the default), the user message states no JD is available -- classify_title's
    title-only case, byte-identical to before this parameter existed. With a description, the real JD is
    embedded instead -- classify_open's JD-grounded case."""
    choices = _classify_choices(config)
    types_in_config = [c for c in choices if c != "none"]
    defs = "\n".join(f"- {t}: {config['types'][t].get('definition', '')}" for t in types_in_config)
    allowed = ", ".join(choices)   # includes 'none'; mirrors the parser's allowed set exactly
    system = (
        "You assign an internship posting to exactly one resume type, or 'none' if it fits none.\n"
        f"Types:\n{defs}\n\n"
        f'Respond with STRICT JSON only: {{"type": <one of: {allowed}>, "confidence": <0.0-1.0>}}. '
        "No prose, no code fences."
    )
    if description:
        user = f"Title: {title}\n\nJob description:\n{description}"
    else:
        user = f"Internship title (no job description available): {title}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
```
Then append, right after `classify_title`'s definition (do not modify `classify_title` itself — it still
calls `build_classify_messages(title, config)` with no `description` arg):
```python
def classify_open(title, description, *, llm_complete, config) -> RouteDecision:
    """Open-classify a title+JD (rules found zero signal) -> method='llm_open' (a type) |
    'not_target' ('none') | defer (LLM error/unparseable). Confidence is the LLM's raw value,
    uncapped -- unlike classify_title/resolve_title_only, this call is grounded in a real JD,
    trusted the same way resolve()'s JD-backed tiebreak already is."""
    messages = build_classify_messages(title, config, description=description)
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    parsed = parse_tiebreaker_response(text, _classify_choices(config))
    if parsed is None:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    t, conf = parsed
    if t == "none":
        return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
    return RouteDecision(resume_type=t, method="llm_open", confidence=conf)
```

- [ ] **Step 3: Run** `uv run pytest tests/test_tiebreaker_resolve.py -q` → PASS (all, including every
pre-existing test in that file — `classify_title`'s tests must be unaffected).

- [ ] **Step 4: Write failing `route_one` tests for the new branch.** Append to `tests/test_route_one.py`
(the file already has `CONFIG` with `swe`/`ai`/`mle` types and imports `route_one`/`Budget`):
```python
def test_no_signal_with_jd_calls_classify_open_and_spends_main_budget():
    # "Data Specialist Intern" / "work with data pipelines" matches no title_signal or jd_signal
    # in CONFIG (swe/ai/mle only) -> route_by_rules returns no_signal, WITH a real description.
    calls = []

    def fake_llm(task, messages, **kw):
        calls.append(1)
        return '{"type": "mle", "confidence": 0.83}'

    b = Budget(remaining=5)
    d = route_one("Data Specialist Intern", "work with data pipelines", CONFIG,
                  llm_complete=fake_llm, budget=b)
    assert d.method == "llm_open" and d.resume_type == "mle" and d.confidence == 0.83
    assert len(calls) == 1 and b.remaining == 4          # spent the MAIN budget, not title_budget


def test_no_signal_with_jd_none_answer_is_not_target():
    def fake_llm(task, messages, **kw):
        return '{"type": "none", "confidence": 0.9}'
    b = Budget(remaining=5)
    d = route_one("Warehouse Associate", "load and unload trucks", CONFIG,
                  llm_complete=fake_llm, budget=b)
    assert d.method == "not_target" and d.resume_type is None


def test_no_signal_with_jd_zero_budget_defers_without_llm():
    b = Budget(remaining=0)
    d = route_one("Data Specialist Intern", "work with data pipelines", CONFIG,
                  llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None
    assert b.remaining == 0
```
Note: `test_no_signal_defers` (already in this file, `description=None`, default `exhausted=False`) is the
existing regression guard proving the new branch does NOT fire without a JD — it must still pass unchanged.
Run: `uv run pytest tests/test_route_one.py -q` → FAIL (the 3 new tests fail; `test_no_signal_defers` still
passes since it has no JD).

- [ ] **Step 5: Wire the new branch into `route_one`.** In `src/jobmaxxing/routing/route.py`, change the
import line:
```python
from .tiebreaker import classify_title, resolve, resolve_title_only
```
to:
```python
from .tiebreaker import classify_open, classify_title, resolve, resolve_title_only
```
Then replace the `no_signal` handling block:
```python
    if outcome.decision == "no_signal":
        if exhausted and title_budget is not None and title_budget.remaining > 0:
            if not _looks_like_internship(title):
                return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
            title_budget.remaining -= 1
            return classify_title(title, llm_complete=llm_complete, config=config)
        return _DEFER
```
with:
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
Also update the module docstring one line above `route_one`'s signature (it currently ends "... route on
the title alone instead of deferring forever") — append a clause so it stays accurate:
```python
def route_one(
    title: str | None, description: str | None, config: dict, *, llm_complete, budget: Budget,
    exhausted: bool = False, title_budget: Budget | None = None,
) -> RouteDecision:
    """Route a single posting. Title-first deterministic; the LLM resolves ambiguous JD-bearing
    rows. When `exhausted` (enrichment gave up, no JD) and a `title_budget` remains, route on the
    title alone instead of deferring forever — tiebreaking among rule candidates, or open-classifying
    a no-signal title (which a non-internship title short-circuits to `not_target` with no LLM call).
    When rules find NO signal at all but a real JD exists, open-classify against the JD instead of
    deferring forever (spends the main `budget`, not `title_budget`)."""
```

- [ ] **Step 6: Run** `uv run pytest tests/test_route_one.py -q` → PASS (all, including every
pre-existing test in the file).

- [ ] **Step 7: Add the `"llm_open"` counts key.** In `src/jobmaxxing/routing/route.py`, change:
```python
    counts = {"rules": 0, "llm": 0, "llm_title": 0, "not_target": 0, "deferred": 0, "manual_skipped": 0}
```
to:
```python
    counts = {"rules": 0, "llm": 0, "llm_title": 0, "llm_open": 0, "not_target": 0, "deferred": 0,
             "manual_skipped": 0}
```
(This is required: `counts[decision.method] += 1` does not auto-create a missing key, so without this
change a `classify_open`-produced decision would raise `KeyError` inside `route_new`.)

- [ ] **Step 8: Write a failing `route_new` integration test proving the fix end-to-end.** Append to
`tests/test_route_db.py` (reuses its `conn` fixture, `_insert` helper, `CONFIG`):
```python
def test_route_new_open_classifies_a_no_signal_jd_row(conn):
    # "Data Specialist Intern" / "work with data pipelines" hits no title_signal or jd_signal in
    # CONFIG (swe/ai/mle only) -> no_signal, but it HAS a description -> must now resolve via
    # classify_open instead of deferring forever (the bug this task fixes).
    _insert(conn, title="Data Specialist Intern", description="work with data pipelines",
            dedupe_key="b|open", enrich_attempts=0)

    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.83}'

    counts = route_new(conn, config=CONFIG, llm_complete=fake_llm)
    assert counts["llm_open"] == 1 and counts["deferred"] == 0
    row = conn.execute("select resume_type, route_method, route_confidence, status from jobs "
                       "where dedupe_key='b|open'").fetchone()
    assert row == ("mle", "llm_open", 0.83, "routed")
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py -q`
→ PASS immediately. This step is confirmatory, not RED-driving: Steps 2–7 already implemented and
unit-tested `classify_open`, the `route_one` wiring, and the `counts` key, so this integration test is
proving the FULL `route_new` path (SQL candidate query → `route_one` → `classify_open` → batched write →
`counts` dict) composes correctly end-to-end — the thing no unit test alone can confirm. If it fails,
that's a real composition bug to fix before moving on (e.g. a mismatch between the unit-level mock
behavior and the real SQL candidate selection).

- [ ] **Step 9: Run the full suite** (no regression):
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → PASS (baseline 582
passed, 8 skipped + 9 new tests = 591 passed, 8 skipped; no regressions).

- [ ] **Step 10: Commit** (verify worktree cwd first — `git rev-parse --show-toplevel`):
```bash
git add src/jobmaxxing/routing/tiebreaker.py src/jobmaxxing/routing/route.py \
        tests/test_tiebreaker_resolve.py tests/test_route_one.py tests/test_route_db.py
git commit -m "route: open-classify no_signal rows against a real JD instead of deferring forever

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green,
   591 passed, 8 skipped.
2. `build_classify_messages(title, config)` (no `description`) is byte-identical to before this change —
   confirmed by every pre-existing `classify_title`/`build_classify_messages` test staying green unmodified.
3. `route_new`'s `counts` dict never `KeyError`s on a `classify_open`-produced `"llm_open"` decision (Task 1
   Step 8's integration test).
4. Optional live check (operator): after merge, the next scheduled `route` CI run (or a manual
   `python -m jobmaxxing.route`) should start resolving the historical no_signal-with-JD backlog — spot
   check a previously-stuck dedupe_key (e.g. one of the June 14 samples) now has `route_method='llm_open'`
   or `'not_target'` instead of `NULL`.

## Risks & notes
- **This does not backfill the historical stuck rows by itself** — it fixes the code path so the NEXT
  scheduled `route` run picks them up naturally (they're already valid candidates per `route_new`'s
  existing WHERE clause: `resume_type is null and route_method is distinct from 'manual' and route_method
  is distinct from 'not_target'` — no change needed there).
- **Cost:** this adds one LLM call per no_signal-with-JD row, bounded by the existing main budget
  (`max_llm_calls_per_run`, default 200/run) — the same cap `resolve()` already respects, so total LLM
  spend per run cannot increase beyond what the existing budget already allows.
- **The 16 ambiguous-and-budget-starved stuck rows are unaffected** — they already have a working
  resolution path (`resolve()`) and will drain via ordinary budget availability across runs; no change
  needed for them.

## Execution
Isolated git worktree off `main`; subagent-driven TDD (single task — small, closely mirrors the existing
`classify_title` pattern, no decomposition needed); one review pass (spec + quality); full-suite green;
merge to `main`; push (gh `vaibhavw30`).
