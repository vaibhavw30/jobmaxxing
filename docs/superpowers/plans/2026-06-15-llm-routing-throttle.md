# Throttle LLM routing to ~every 4 days — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut recurring LLM spend ~98% by running only the deterministic (rules) routing every 3h and moving the LLM tiebreak/title-routing pass to a separate ~every-4-days workflow.

**Architecture:** Add an additive, default-off `no_llm` mode to `route_new` (zeroes both LLM budgets so `route_one` never calls the LLM — rules-only, ambiguous rows deferred) exposed via a `--no-llm` CLI flag. `pollers.yml` keeps running every 3h but routes with `--no-llm` (and no API keys); a new `llm-route.yml` runs the full LLM pass every ~4 days.

**Tech Stack:** Python 3.12, psycopg3, PyYAML (`pyyaml>=6.0`, already a dep), pytest + pytest-postgresql, GitHub Actions.

## Global Constraints
- Python **3.12**; run pytest with the Postgres server binary on PATH:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- The `no_llm` path is **additive and defaults off** — existing routing behavior and the entire
  existing routing test suite (`test_route_one.py`, `test_route_db.py`, `test_routing_rules.py`,
  `test_routing_config.py`) must stay **byte-for-byte green** (no regression).
- Never interpolate the deterministic rules/budgets; do not touch enrichment/ingestion/tailoring.
- GitHub Actions **`on:` gotcha:** PyYAML (YAML 1.1) parses a bare `on:` mapping key as the boolean
  `True`, so `wf["on"]` KeyErrors — access triggers via `wf.get("on") or wf.get(True)`.
- Push with the `vaibhavw30` gh account.

---

## Task 1: `no_llm` mode in routing (`route_new` + `route_one` zero-budget coverage)

**Files:**
- Modify: `src/jobmaxxing/routing/route.py` (`route_new`)
- Test: `tests/test_route_one.py`, `tests/test_route_db.py`

**Interfaces:**
- Produces: `route_new(conn, *, config=None, llm_complete=None, max_llm_calls=None, reroute=False, no_llm=False) -> dict`. When `no_llm=True`, both the JD budget and the title budget are 0, so `route_one` never invokes `llm_complete`; rule-routable rows still route, ambiguous rows are deferred. Counts dict unchanged: `{"rules","llm","llm_title","not_target","deferred","manual_skipped"}`.
- Consumes: existing `route_one(title, description, config, *, llm_complete, budget, exhausted=False, title_budget=None)` and `Budget(remaining=int)` from `jobmaxxing.routing.types` (both unchanged).

- [ ] **Step 1: Write failing unit tests** pinning that `route_one` never calls the LLM once budgets are exhausted. Append to `tests/test_route_one.py` (it already defines `CONFIG` and `_llm_never` which raises `AssertionError`):

```python
def test_ambiguous_jd_zero_budget_defers_without_llm():
    """Ambiguous JD row with an exhausted budget defers and never calls the LLM."""
    b = Budget(remaining=0)
    d = route_one("AI Engineer / ML Engineer Intern", "generic body", CONFIG,
                  llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None
    assert b.remaining == 0

def test_exhausted_title_zero_title_budget_defers_without_llm():
    """An enrichment-exhausted no-signal title with a zero title budget defers, no LLM."""
    b = Budget(remaining=0)
    tb = Budget(remaining=0)
    d = route_one("Barista", None, CONFIG, llm_complete=_llm_never, budget=b,
                  exhausted=True, title_budget=tb)
    assert d.resume_type is None and d.method is None
    assert tb.remaining == 0

def test_clear_title_routes_with_all_budgets_zero():
    """Rules never need the LLM: a clear title still routes even with both budgets at 0."""
    b = Budget(remaining=0)
    tb = Budget(remaining=0)
    d = route_one("Software Engineer Intern", "api work", CONFIG, llm_complete=_llm_never,
                  budget=b, exhausted=True, title_budget=tb)
    assert d.resume_type == "swe" and d.method == "rules"
```

- [ ] **Step 2: Run — expect PASS already for route_one** (the deferral logic exists; these tests just pin it). If any fails, STOP — the assumption that zero budgets defer without the LLM is wrong and the whole plan needs revisiting.

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_one.py -q`
Expected: PASS (all, including the 3 new).

- [ ] **Step 3: Write the failing integration test** for `route_new(no_llm=True)`. Append to `tests/test_route_db.py` (it defines `conn`, `CONFIG`, `_insert`):

```python
def test_route_new_no_llm_routes_rules_and_defers_ambiguous(conn):
    """no_llm=True: rule-routable rows still route; ambiguous JD rows defer; LLM never called."""
    _insert(conn, title="Software Engineer Intern", description="api work", dedupe_key="n|swe")
    _insert(conn, title="AI Engineer / ML Engineer Intern", description="generic body",
            dedupe_key="n|ambig")

    def llm_never(*a, **k):
        raise AssertionError("LLM must not be called when no_llm=True")

    counts = route_new(conn, config=CONFIG, llm_complete=llm_never, no_llm=True)
    assert counts["llm"] == 0 and counts["llm_title"] == 0
    assert counts["rules"] == 1 and counts["deferred"] == 1
    swe = conn.execute("select route_method, status from jobs where dedupe_key='n|swe'").fetchone()
    assert swe == ("rules", "routed")
    ambig = conn.execute("select resume_type, route_method from jobs where dedupe_key='n|ambig'").fetchone()
    assert ambig == (None, None)   # deferred, awaiting a later LLM pass
```

- [ ] **Step 4: Run — expect FAIL** (`route_new()` got an unexpected keyword argument `no_llm`).
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_db.py::test_route_new_no_llm_routes_rules_and_defers_ambiguous -q`
Expected: FAIL (TypeError: unexpected keyword `no_llm`).

- [ ] **Step 5: Implement `no_llm` in `route_new`.** In `src/jobmaxxing/routing/route.py`, change the signature and the budget setup. Replace:

```python
def route_new(conn: psycopg.Connection, *, config=None, llm_complete=None, max_llm_calls=None, reroute=False) -> dict:
```
with:
```python
def route_new(conn: psycopg.Connection, *, config=None, llm_complete=None, max_llm_calls=None,
              reroute=False, no_llm=False) -> dict:
```
And replace the budget-setup block:
```python
    cap = max_llm_calls if max_llm_calls is not None else thresholds.get("max_llm_calls_per_run", 200)
    title_after = thresholds.get("title_route_after", 3)
    budget = Budget(remaining=cap)
    title_budget = Budget(remaining=thresholds.get("title_route_max_llm", 100))   # separate cap
```
with:
```python
    if no_llm:
        cap = 0                                              # deterministic-only: never call the LLM
        title_cap = 0
    else:
        cap = max_llm_calls if max_llm_calls is not None else thresholds.get("max_llm_calls_per_run", 200)
        title_cap = thresholds.get("title_route_max_llm", 100)
    title_after = thresholds.get("title_route_after", 3)
    budget = Budget(remaining=cap)
    title_budget = Budget(remaining=title_cap)               # separate cap (0 when no_llm)
```
Also update the `route_new` docstring's first line to note: `With no_llm=True, both LLM budgets are 0 (rules-only; ambiguous rows deferred for a later LLM pass).`

- [ ] **Step 6: Run the new test + the whole routing suite** (no regression).
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_one.py tests/test_route_db.py tests/test_routing_rules.py tests/test_routing_config.py -q`
Expected: PASS (all, incl. the 4 new; existing unchanged).

- [ ] **Step 7: Commit**
```bash
git add src/jobmaxxing/routing/route.py tests/test_route_one.py tests/test_route_db.py
git commit -m "route: add default-off no_llm mode (rules-only, defers ambiguous)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `--no-llm` CLI flag

**Files:**
- Modify: `src/jobmaxxing/routing/route.py` (`main`)
- Test: `tests/test_route_cli.py` (create)

**Interfaces:**
- Consumes: `route_new(conn, no_llm=...)` from Task 1; existing `set_manual`.
- Produces: `python -m jobmaxxing.route --no-llm` → `route_new(conn, no_llm=True)`. Plain `route` → `no_llm=False`. `route set <id> <type>` unchanged.

- [ ] **Step 1: Write the failing CLI test.** Create `tests/test_route_cli.py`:

```python
"""CLI dispatch tests for `python -m jobmaxxing.route` (no real DB — psycopg.connect is patched)."""

import sys

import jobmaxxing.routing.route as R


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_common(monkeypatch, captured):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/db")
    monkeypatch.setattr(R.psycopg, "connect", lambda url: _FakeConn())
    monkeypatch.setattr(R, "route_new", lambda conn, **kw: captured.update(kw) or {"rules": 0})
    monkeypatch.setattr(R, "set_manual", lambda conn, jid, rt: captured.update(set=(jid, rt)))


def test_cli_no_llm_flag_sets_no_llm_true(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route", "--no-llm"])
    R.main()
    assert captured.get("no_llm") is True


def test_cli_plain_route_sets_no_llm_false(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route"])
    R.main()
    assert captured.get("no_llm") is False


def test_cli_set_subcommand_unaffected(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route", "set", "abc-123", "swe"])
    R.main()
    assert captured.get("set") == ("abc-123", "swe")
    assert "no_llm" not in captured        # route_new not called for `set`
```

- [ ] **Step 2: Run — expect FAIL** (plain route currently calls `route_new(conn)` with no `no_llm`, so `captured.get("no_llm")` is `None`, not `False`; the `--no-llm` test fails likewise).
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_cli.py -q`
Expected: FAIL (2 of 3 fail on the `no_llm` assertions).

- [ ] **Step 3: Implement the flag in `main()`.** In `src/jobmaxxing/routing/route.py`, replace the `else:` branch of `main`:
```python
        else:
            counts = route_new(conn)
            print(f"routed: {counts}")
```
with:
```python
        else:
            no_llm = "--no-llm" in sys.argv[1:]
            counts = route_new(conn, no_llm=no_llm)
            print(f"routed: {counts}")
```
(The `set` branch is unchanged.)

- [ ] **Step 4: Run — expect PASS.**
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_route_cli.py -q`
Expected: PASS (3).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/routing/route.py tests/test_route_cli.py
git commit -m "route: --no-llm CLI flag -> route_new(no_llm=True)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Workflows (pollers rules-only + new llm-route) + README + config test

**Files:**
- Modify: `.github/workflows/pollers.yml`
- Create: `.github/workflows/llm-route.yml`
- Modify: `README.md` (Routing section)
- Test: `tests/test_workflows_routing.py` (create)

**Interfaces:**
- Consumes: the `--no-llm` CLI flag from Task 2.
- Produces: two workflow files whose structure the config test asserts.

- [ ] **Step 1: Write the failing workflow-config test.** Create `tests/test_workflows_routing.py`:

```python
"""Structural asserts on the routing workflows (no network). Guards the every-4-days LLM split."""

import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WF / name).read_text())


def _on(wf):
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True, not the string "on".
    return wf.get("on") or wf.get(True)


def _steps(wf):
    (job,) = wf["jobs"].values()
    return job["steps"]


def _route_step(wf):
    for s in _steps(wf):
        if "run" in s and "jobmaxxing.route" in s["run"]:
            return s
    raise AssertionError("no route step found")


def test_pollers_routes_rules_only_and_has_no_api_keys():
    wf = _load("pollers.yml")
    step = _route_step(wf)
    assert "--no-llm" in step["run"]
    env = step.get("env", {})
    assert not any(k.endswith("_API_KEY") for k in env), f"pollers route step still has API keys: {env}"


def test_llm_route_workflow_runs_full_llm_every_4_days():
    wf = _load("llm-route.yml")
    crons = [c["cron"] for c in _on(wf)["schedule"]]
    assert "0 6 */4 * *" in crons
    step = _route_step(wf)
    assert "--no-llm" not in step["run"]
    env = step.get("env", {})
    assert {"OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"} <= set(env)
    # migrate must run before the route step
    runs = [s.get("run", "") for s in _steps(wf)]
    migrate_i = next(i for i, r in enumerate(runs) if "jobmaxxing.migrate" in r)
    route_i = next(i for i, r in enumerate(runs) if "jobmaxxing.route" in r)
    assert migrate_i < route_i
```

- [ ] **Step 2: Run — expect FAIL** (`llm-route.yml` doesn't exist; pollers route step still has `--no-llm`-less command + API keys).
Run: `uv run pytest tests/test_workflows_routing.py -q`
Expected: FAIL (both tests).

- [ ] **Step 3: Edit `pollers.yml`** — make the route step rules-only and drop the API keys. Replace the existing "Route new postings" step:
```yaml
      - name: Route new postings
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          XAI_API_KEY: ${{ secrets.XAI_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: uv run python -m jobmaxxing.route
```
with:
```yaml
      - name: Route new postings (rules only — no LLM cost)
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: uv run python -m jobmaxxing.route --no-llm
```

- [ ] **Step 4: Create `.github/workflows/llm-route.yml`** with exactly:
```yaml
name: llm-route

on:
  schedule:
    - cron: "0 6 */4 * *"   # ~every 4 days (days 1,5,9,13,17,21,25,29 at 06:00 UTC); one shorter gap at month rollover
  workflow_dispatch: {}       # manual trigger; NO pull_request (fork PRs must not touch secrets)

permissions:
  contents: read

concurrency:
  group: llm-route
  cancel-in-progress: false

jobs:
  route:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"

      - name: Sync deps (frozen)
        run: uv sync --frozen --no-dev

      - name: Apply migrations
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: uv run python -m jobmaxxing.migrate

      - name: LLM route (tiebreak + title-routing over accumulated ambiguous jobs)
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          XAI_API_KEY: ${{ secrets.XAI_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: uv run python -m jobmaxxing.route
```

- [ ] **Step 5: Run — expect PASS.**
Run: `uv run pytest tests/test_workflows_routing.py -q`
Expected: PASS (2).

- [ ] **Step 6: Update the README Routing section.** In `README.md`, under `## Routing`, replace the "Run:" bullet that reads:
```
- Run: `uv run python -m jobmaxxing.route` (also runs automatically as a second
  step in the pollers workflow, right after ingestion).
```
with:
```
- Run: `uv run python -m jobmaxxing.route` (full LLM). **Scheduled routing is split to cut LLM cost:**
  `pollers.yml` runs `route --no-llm` every 3h (deterministic rules only, no LLM spend — rule-matched
  jobs appear within hours), and `llm-route.yml` runs the full `route` (LLM tiebreak + title-routing)
  every ~4 days over the accumulated ambiguous jobs. So ambiguous postings are classified within ~4
  days; rule-matched postings stay fresh. Add `--no-llm` locally to route without any LLM calls.
```

- [ ] **Step 7: Run the full suite** (no regression across the whole repo).
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: PASS (all; skips unchanged).

- [ ] **Step 8: Commit**
```bash
git add .github/workflows/pollers.yml .github/workflows/llm-route.yml tests/test_workflows_routing.py README.md
git commit -m "ci: split routing — pollers rules-only 3h, new llm-route every ~4 days

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite green: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`.
2. Local rules-only run makes no LLM call: with LLM keys UNSET in the env, `uv run python -m jobmaxxing.route --no-llm` completes and prints counts with `'llm': 0, 'llm_title': 0` (deterministic rows routed, ambiguous deferred). Optionally confirm against the real DB (read-only-ish; routing writes resume_type for rule-matched rows — safe, that's the intended cadence).
3. Workflow structure asserted by `tests/test_workflows_routing.py`; eyeball `git diff` of the two YAML files.

## Risks & notes
- **`no_llm` is additive/default-off** — existing routing suite must stay green (the regression gate).
- **`on:` YAML gotcha** handled in the config test via `_on()`.
- **Cron `*/4`** has one sub-4-day gap per month at rollover — acceptable ("~every 4 days").
- **Backlog:** the 4-day LLM run is bounded by `max_llm_calls_per_run` (200) + `title_route_max_llm`
  (100); if ambiguous volume outpaces that, it drains over successive runs (bump the caps in
  `config/routing.yaml` if needed — out of scope here).
- **Workflow overlap** (4-day run vs a 3h run): both call `route_new`; writes are one idempotent
  batched transaction over still-unrouted rows — at worst duplicated work, never corruption.

## Execution
Isolated git worktree off `main`; subagent-driven TDD, one task per subagent (implementer reads the
routing package + both workflow files for context first); two-stage review (spec → quality) per task;
full-suite green; merge to `main`; push (gh `vaibhavw30`).
