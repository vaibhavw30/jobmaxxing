# Title Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route enrichment-exhausted, no-JD jobs on their title alone (LLM tiebreak among rule candidates, or open-classify rules-missed roles) so they stop sitting deferred forever and surface as relevant.

**Architecture:** Extend the existing router (`routing/`): two new title-only classifiers in `tiebreaker.py`, an internship pre-gate + two new branches in `route_one`, and `route_new` changes to fetch `enrich_attempts`, allot a separate title-route LLM budget, and write the new `llm_title`/`not_target` markers. No new pipeline stage; the JD-bearing path is unchanged (new params default off).

**Tech Stack:** Python 3.12, psycopg3, pytest + pytest-postgresql; reuses the LLM router (Haiku / claude-cli).

**Spec:** `docs/superpowers/specs/2026-06-14-title-triage-design.md`

---

## File structure

- Modify `src/jobmaxxing/routing/tiebreaker.py` — add `_TITLE_ROUTE_CONFIDENCE`, `resolve_title_only`, `build_classify_messages`, `classify_title`.
- Modify `src/jobmaxxing/routing/route.py` — add `_INTERNSHIP_MARKERS`/`_looks_like_internship`; `route_one` `exhausted`/`title_budget` params + branches; `route_new` enrich_attempts fetch, separate budget, split write, expanded counts; import the two new tiebreaker functions.
- Modify `config/routing.yaml` — `title_route_after`, `title_route_max_llm` in `thresholds`.
- Modify `tests/test_tiebreaker_resolve.py` — unit tests for `resolve_title_only`/`classify_title`/`build_classify_messages`.
- Modify `tests/test_route_one.py` — unit tests for `_looks_like_internship` + `route_one` exhausted branches.
- Modify `tests/test_route_db.py` — integration tests (extend `_insert` with `enrich_attempts`).

All tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

### Task 1: `resolve_title_only` — tiebreak among candidates on the title

**Files:**
- Modify: `src/jobmaxxing/routing/tiebreaker.py`
- Test: `tests/test_tiebreaker_resolve.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tiebreaker_resolve.py`:

```python
from jobmaxxing.routing.tiebreaker import resolve_title_only
from jobmaxxing.llm.client import LLMUnavailable

_TT_CONFIG = {
    "types": {
        "ai": {"definition": "AI engineering"},
        "mle": {"definition": "ML engineering"},
    }
}


def test_resolve_title_only_caps_confidence_and_tags_method():
    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.95}'
    d = resolve_title_only(["ai", "mle"], "AI / ML Engineer Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.resume_type == "ai"
    assert d.method == "llm_title"
    assert d.confidence == 0.4          # capped at _TITLE_ROUTE_CONFIDENCE even though reply said 0.95


def test_resolve_title_only_defers_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("no provider")
    d = resolve_title_only(["ai", "mle"], "AI / ML Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.method is None             # defer -> retry next run


def test_resolve_title_only_defers_on_unparseable_reply():
    def fake_llm(task, messages, **kw):
        return "not json at all"
    d = resolve_title_only(["ai", "mle"], "AI / ML Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.method is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_tiebreaker_resolve.py -k resolve_title_only -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_title_only'`.

- [ ] **Step 3: Implement `resolve_title_only`**

In `src/jobmaxxing/routing/tiebreaker.py`, add the constant near `_FALLBACK_CONFIDENCE` and the function after `resolve`:

```python
# Provisional confidence for a title-only route: capped low so it reads as "classified from
# the title, no JD" in the funnel — below any auto-advance threshold; the operator stays the gate.
_TITLE_ROUTE_CONFIDENCE = 0.4


def resolve_title_only(candidates: list[str], title, *, llm_complete, config) -> RouteDecision:
    """Tiebreak among `candidates` using the TITLE alone (no JD). method='llm_title' with a
    capped-low confidence. On LLMUnavailable / unparseable reply -> defer (retry next run)."""
    messages = build_tiebreaker_messages(
        title, "(no job description available — classify from the title)", candidates, config
    )
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    parsed = parse_tiebreaker_response(text, candidates)
    if parsed is None:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    resume_type, conf = parsed
    return RouteDecision(resume_type=resume_type, method="llm_title", confidence=min(conf, _TITLE_ROUTE_CONFIDENCE))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_tiebreaker_resolve.py -k resolve_title_only -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/routing/tiebreaker.py tests/test_tiebreaker_resolve.py
git commit -m "feat(routing): resolve_title_only — tiebreak candidates on title alone"
```

---

### Task 2: `classify_title` — open classification with a "none" escape

**Files:**
- Modify: `src/jobmaxxing/routing/tiebreaker.py`
- Test: `tests/test_tiebreaker_resolve.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tiebreaker_resolve.py`:

```python
from jobmaxxing.routing.tiebreaker import build_classify_messages, classify_title

_FULL_CONFIG = {
    "types": {t: {"definition": f"{t} work"} for t in
              ("quant-trader", "quant-dev", "mle", "swe", "fdse", "ai", "robotics", "av")}
}


def test_build_classify_messages_lists_all_types_plus_none():
    msgs = build_classify_messages("ML Intern", _FULL_CONFIG)
    system = msgs[0]["content"]
    for t in ("swe", "mle", "av", "none"):
        assert t in system
    assert "ML Intern" in msgs[1]["content"]


def test_classify_title_returns_llm_title_for_a_type():
    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.9}'
    d = classify_title("Machine Learning Co-op", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type == "mle" and d.method == "llm_title" and d.confidence == 0.4


def test_classify_title_none_is_not_target():
    def fake_llm(task, messages, **kw):
        return '{"type": "none", "confidence": 0.9}'
    d = classify_title("Warehouse Associate", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type is None and d.method == "not_target"


def test_classify_title_defers_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("no provider")
    d = classify_title("Some Intern", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.method is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_tiebreaker_resolve.py -k classify_title -v`
Expected: FAIL — `ImportError: cannot import name 'classify_title'`.

- [ ] **Step 3: Implement `build_classify_messages` + `classify_title`**

In `src/jobmaxxing/routing/tiebreaker.py`, add `from .types import VALID_TYPES` to the imports (it already imports `RouteDecision` from `.types`), then add after `resolve_title_only`:

```python
def build_classify_messages(title, config) -> list[dict]:
    """Open classification prompt: pick one of the 8 types, or 'none' (not a target role)."""
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
    """Open-classify a title -> method='llm_title' (a type) | 'not_target' ('none') | defer (LLM error)."""
    messages = build_classify_messages(title, config)
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    parsed = parse_tiebreaker_response(text, list(VALID_TYPES) + ["none"])
    if parsed is None:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)
    t, conf = parsed
    if t == "none":
        return RouteDecision(resume_type=None, method="not_target", confidence=0.0)
    return RouteDecision(resume_type=t, method="llm_title", confidence=min(conf, _TITLE_ROUTE_CONFIDENCE))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_tiebreaker_resolve.py -k "classify_title or classify_messages" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/routing/tiebreaker.py tests/test_tiebreaker_resolve.py
git commit -m "feat(routing): classify_title — open title classification with a 'none' escape"
```

---

### Task 3: `_looks_like_internship` pre-gate

**Files:**
- Modify: `src/jobmaxxing/routing/route.py`
- Test: `tests/test_route_one.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_route_one.py`:

```python
from jobmaxxing.routing.route import _looks_like_internship


def test_looks_like_internship_true_cases():
    for title in ("Software Engineering Intern", "ML Co-op", "Data Apprentice",
                  "New Grad SWE", "Campus Analyst", "Student Researcher"):
        assert _looks_like_internship(title) is True


def test_looks_like_internship_false_cases():
    for title in ("Senior Director, Finance", "Staff Software Engineer", None, ""):
        assert _looks_like_internship(title) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_route_one.py -k looks_like_internship -v`
Expected: FAIL — `ImportError: cannot import name '_looks_like_internship'`.

- [ ] **Step 3: Implement the pre-gate**

In `src/jobmaxxing/routing/route.py`, add near the top (after `_SINGLE_CANDIDATE_CONFIDENCE`):

```python
_INTERNSHIP_MARKERS = (
    "intern", "co-op", "coop", "co op", "student", "apprentic", "new grad",
    "early career", "university", "campus", "trainee",
)


def _looks_like_internship(title: str | None) -> bool:
    """Coarse gate: does the title look like an internship/early-career role? Used to skip
    open-classification (and its LLM call) on obvious non-targets."""
    t = (title or "").lower()
    return any(m in t for m in _INTERNSHIP_MARKERS)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_route_one.py -k looks_like_internship -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/routing/route.py tests/test_route_one.py
git commit -m "feat(routing): _looks_like_internship coarse pre-gate"
```

---

### Task 4: `route_one` — exhausted/title_budget branches

**Files:**
- Modify: `src/jobmaxxing/routing/route.py`
- Test: `tests/test_route_one.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_route_one.py` (CONFIG and `Budget` are already imported at the top of the file):

```python
def _ambiguous_no_jd():
    # "ai engineer ml engineer" matches BOTH ai and mle title_signals -> 2 candidates, no JD
    return ("AI Engineer / ML Engineer Intern", None)


def test_exhausted_ambiguous_no_jd_title_routes_via_llm_title():
    title, desc = _ambiguous_no_jd()
    tb = Budget(remaining=5)
    jd_b = Budget(remaining=5)

    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.9}'

    d = route_one(title, desc, CONFIG, llm_complete=fake_llm, budget=jd_b,
                  exhausted=True, title_budget=tb)
    assert d.method == "llm_title" and d.resume_type == "ai" and d.confidence == 0.4
    assert tb.remaining == 4 and jd_b.remaining == 5        # spent the TITLE budget, not the JD budget


def test_not_exhausted_ambiguous_no_jd_still_defers():
    title, desc = _ambiguous_no_jd()
    d = route_one(title, desc, CONFIG, llm_complete=_llm_never, budget=Budget(5),
                  exhausted=False, title_budget=Budget(5))
    assert d.method is None                                 # unchanged behavior when not exhausted


def test_exhausted_but_no_title_budget_defers():
    title, desc = _ambiguous_no_jd()
    d = route_one(title, desc, CONFIG, llm_complete=_llm_never, budget=Budget(5),
                  exhausted=True, title_budget=Budget(0))
    assert d.method is None


def test_exhausted_no_signal_internship_open_classifies():
    # "Robotics Perception Intern" matches no title_signal in CONFIG (swe/ai/mle only) -> no_signal
    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.8}'
    tb = Budget(remaining=2)
    d = route_one("Robotics Perception Intern", None, CONFIG, llm_complete=fake_llm,
                  budget=Budget(5), exhausted=True, title_budget=tb)
    assert d.method == "llm_title" and tb.remaining == 1


def test_exhausted_no_signal_non_internship_is_not_target_without_llm():
    d = route_one("Senior Director of Finance", None, CONFIG, llm_complete=_llm_never,
                  budget=Budget(5), exhausted=True, title_budget=Budget(2))
    assert d.method == "not_target" and d.resume_type is None


def test_has_jd_ambiguous_still_uses_normal_llm_path():
    # regression: the JD-bearing path is unchanged (method='llm', spends the JD budget)
    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.7}'
    jd_b = Budget(remaining=5)
    d = route_one("AI Engineer / ML Engineer Intern", "training pipelines", CONFIG,
                  llm_complete=fake_llm, budget=jd_b, exhausted=True, title_budget=Budget(5))
    assert d.method == "llm" and jd_b.remaining == 4
```

> Note: `"AI Engineer / ML Engineer Intern"` yields the ai+mle ambiguous set under this `CONFIG` (the existing `test_route_new_uses_llm_for_ambiguous_with_jd` in `test_route_db.py` relies on exactly that). `"Robotics Perception Intern"` and `"Senior Director of Finance"` hit no `title_signal` → `no_signal`. Do not change `CONFIG`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_route_one.py -k "exhausted or not_target or normal_llm" -v`
Expected: FAIL — `route_one() got an unexpected keyword argument 'exhausted'`.

- [ ] **Step 3: Implement the branches**

In `src/jobmaxxing/routing/route.py`, **replace** the existing `from .tiebreaker import resolve` line with:

```python
from .tiebreaker import classify_title, resolve, resolve_title_only
```

Replace `route_one` with:

```python
def route_one(
    title: str | None, description: str | None, config: dict, *, llm_complete, budget: Budget,
    exhausted: bool = False, title_budget: Budget | None = None,
) -> RouteDecision:
    """Route a single posting. Title-first deterministic; the LLM resolves ambiguous JD-bearing
    rows. When `exhausted` (enrichment gave up, no JD) and a `title_budget` remains, route on the
    title alone instead of deferring forever."""
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

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_route_one.py -v`
Expected: PASS (new tests + all pre-existing route_one tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/routing/route.py tests/test_route_one.py
git commit -m "feat(routing): route_one title-routes exhausted no-JD jobs (separate budget)"
```

---

### Task 5: `route_new` + config — wire title-routing into the batch route

**Files:**
- Modify: `src/jobmaxxing/routing/route.py`
- Modify: `config/routing.yaml`
- Test: `tests/test_route_db.py`

- [ ] **Step 1: Add the config thresholds**

In `config/routing.yaml`, extend the `thresholds:` map with `title_route_after: 3` and `title_route_max_llm: 100`. The line currently reads:

```yaml
thresholds: { min_top_score: 1.0, min_margin_ratio: 0.5, jd_hits_cap: 5, max_llm_calls_per_run: 200 }
```

Change it to:

```yaml
thresholds: { min_top_score: 1.0, min_margin_ratio: 0.5, jd_hits_cap: 5, max_llm_calls_per_run: 200, title_route_after: 3, title_route_max_llm: 100 }
```

- [ ] **Step 2: Write the failing integration tests**

In `tests/test_route_db.py`, first extend the `_insert` helper to accept `enrich_attempts` (it currently omits it). Replace the helper with:

```python
def _insert(conn, *, title, description, dedupe_key, resume_type=None, route_method=None, enrich_attempts=0):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, enrich_attempts) values (%s, 'github:simplify', 'Acme', %s, %s, %s, %s, %s, %s)",
        (dedupe_key, title, f"https://x/{dedupe_key}", description, resume_type, route_method, enrich_attempts),
    )
    conn.commit()
```

Then append these tests:

```python
def _fake_llm_ai(task, messages, **kw):
    return '{"type": "ai", "confidence": 0.9}'


def test_route_new_title_routes_exhausted_ambiguous_no_jd(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|amb", enrich_attempts=3)
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai)
    assert counts["llm_title"] == 1
    row = conn.execute(
        "select resume_type, route_method, route_confidence, status from jobs where dedupe_key='t|amb'"
    ).fetchone()
    assert row[0] == "ai" and row[1] == "llm_title" and row[2] <= 0.4 and row[3] == "routed"


def test_route_new_leaves_not_yet_exhausted_deferred(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|fresh", enrich_attempts=0)
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai)
    assert counts["deferred"] == 1 and counts["llm_title"] == 0
    assert conn.execute("select resume_type from jobs where dedupe_key='t|fresh'").fetchone()[0] is None


def test_route_new_not_target_is_marked_and_not_reselected(conn):
    # exhausted, no rules signal, non-internship title -> not_target (no LLM)
    _insert(conn, title="Senior Director of Finance", description=None,
            dedupe_key="t|nt", enrich_attempts=3)

    def _llm_never(*a, **k):
        raise AssertionError("LLM must not be called for a non-internship not_target")

    counts1 = route_new(conn, config=CONFIG, llm_complete=_llm_never)
    assert counts1["not_target"] == 1
    row = conn.execute("select resume_type, route_method from jobs where dedupe_key='t|nt'").fetchone()
    assert row == (None, "not_target")
    # second run must NOT reselect it
    counts2 = route_new(conn, config=CONFIG, llm_complete=_llm_never)
    assert counts2["not_target"] == 0 and counts2["deferred"] == 0


def test_title_routing_uses_separate_budget_not_jd_budget(conn):
    # one exhausted-no-JD ambiguous row + assert the JD max_llm_calls budget is not consumed by it
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|sep", enrich_attempts=3)
    # max_llm_calls=0 would block JD routing, but title routing has its own budget -> still routes
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai, max_llm_calls=0)
    assert counts["llm_title"] == 1
```

- [ ] **Step 3: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py -k "title_routes or not_yet or not_target or separate_budget" -v`
Expected: FAIL — `KeyError: 'llm_title'` (counts dict lacks the key) / `route_one() got an unexpected keyword 'exhausted'` is already added, so the failure is in `route_new` not yet passing the new args / counting the new methods.

- [ ] **Step 4: Implement the `route_new` changes**

In `src/jobmaxxing/routing/route.py`, replace the entire body of `route_new` (every line after its docstring, from `cfg = config if config is not None else load_routing_config()` through `return counts`) with:

```python
    cfg = config if config is not None else load_routing_config()
    do_llm = llm_complete if llm_complete is not None else llm_complete_default
    thresholds = cfg.get("thresholds", {})
    cap = max_llm_calls if max_llm_calls is not None else thresholds.get("max_llm_calls_per_run", 200)
    title_after = thresholds.get("title_route_after", 3)
    budget = Budget(remaining=cap)
    title_budget = Budget(remaining=thresholds.get("title_route_max_llm", 100))   # separate cap

    if reroute:
        where = "route_method is distinct from 'manual'"
    else:
        where = ("resume_type is null and route_method is distinct from 'manual' "
                 "and route_method is distinct from 'not_target'")

    rows = conn.execute(
        f"select id, title, description, enrich_attempts from jobs where {where}"
    ).fetchall()
    counts = {"rules": 0, "llm": 0, "llm_title": 0, "not_target": 0, "deferred": 0, "manual_skipped": 0}
    routed_updates: list[tuple] = []
    nontarget_updates: list[tuple] = []

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
    counts["manual_skipped"] = conn.execute(
        "select count(*) from jobs where route_method = 'manual'"
    ).fetchone()[0]
    logger.info("route summary: %s (budget left=%d, title left=%d)", counts, budget.remaining, title_budget.remaining)
    return counts
```

- [ ] **Step 5: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py -v`
Expected: PASS (new tests + all pre-existing route_db tests, which still pass because the JD path is unchanged and `_insert` defaults `enrich_attempts=0`).

- [ ] **Step 6: Run the full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass (the 4 skips remain: 2 pdflatex, Workday e2e, claude-cli e2e).

- [ ] **Step 7: Commit**

```bash
git add src/jobmaxxing/routing/route.py config/routing.yaml tests/test_route_db.py
git commit -m "feat(routing): route_new title-routes exhausted no-JD jobs + not_target marker"
```

---

## Done criteria

- `uv run pytest -q` green: new unit tests (`resolve_title_only`, `classify_title`, `build_classify_messages`, `_looks_like_internship`, `route_one` exhausted branches) + integration (`route_new` title-routes exhausted rows, leaves not-yet-exhausted deferred, marks/excludes `not_target`, uses the separate budget), all pre-existing routing tests still green.
- Behavior: an enrichment-exhausted (`enrich_attempts>=3`), no-JD job gets `route_method='llm_title'` + `resume_type` + `route_confidence<=0.4` + `status='routed'` (or `not_target` if non-relevant); a not-yet-exhausted no-JD job still defers; the JD-bearing path is unchanged.
- After merge, a live `python -m jobmaxxing.route` run logs `llm_title`/`not_target` counts; the Workday backlog that the local worker couldn't enrich starts getting provisional title routes instead of sitting deferred.
- Reminder (not this plan): sub-projects 2/3 must reset `resume_type`/`route_method` when a real JD later arrives (roadmap reset contract).
