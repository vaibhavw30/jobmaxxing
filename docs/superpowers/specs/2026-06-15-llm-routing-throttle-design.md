# Throttle LLM routing to ~every 4 days — design

## Context
The only recurring LLM spend in the pipeline is the **`route` step** of
`.github/workflows/pollers.yml`, which runs on `cron: 0 */3 * * *` (every 3 hours, 8×/day). Routing
is deterministic-first: `route_by_rules` classifies the majority for free; the LLM (cheap
`claude-haiku-4-5`) is a **bounded tiebreaker** only for ambiguous JD-bearing postings
(`max_llm_calls_per_run: 200`) plus title-routing of enrichment-exhausted rows
(`title_route_max_llm: 100`). Ingestion, enrichment, and the nightly email use no LLM.

The repo is **public**, so GitHub Actions minutes are free — the only real cost is the LLM API.
Both "slow the whole pipeline" and "split out the LLM" cut LLM calls to ~every 4 days *equally*, so
slowing everything saves no extra money while it *does* nuke fresh job discovery (up to 4-day-stale).
**Decision: split.** Keep ingest + enrich + rules-routing frequent (fresh, rule-matched jobs — the
majority — keep landing in the triage table within hours); run only the LLM pass every ~4 days.

## Goal
Reduce LLM routing calls from ~2,400/day to ~300 every 4 days (~98% cut) with no loss of
deterministic routing cadence and no regression to existing routing behavior. The only accepted
functional cost: *ambiguous* postings (those needing the LLM) take up to ~4 days to get a
`resume_type` and thus appear in the triage table; the rule-routed majority stays fresh.

## Design

### 1. Deterministic-only mode in routing (`--no-llm`)
Add a `no_llm: bool = False` parameter to `route_new` in `src/jobmaxxing/routing/route.py`. When
`True`, both LLM budgets are set to **0**:
- `budget = Budget(remaining=0)` and `title_budget = Budget(remaining=0)`.
- Consequence via the existing `route_one` logic (unchanged): ambiguous JD rows hit
  `if budget.remaining <= 0: return _DEFER`; exhausted no-signal/ambiguous-no-JD rows hit
  `title_budget.remaining > 0` == False → `_DEFER`. **`llm_complete` is never invoked** — no cost,
  no exceptions, no log noise. Rule-routable rows still route normally; ambiguous rows are deferred
  (left `resume_type IS NULL`) for a later LLM pass.

Implementation note: keep the existing `max_llm_calls` param working; `no_llm=True` overrides both
caps to 0. Precisely: compute `cap = 0 if no_llm else (max_llm_calls if not None else config
max_llm_calls_per_run)`, and `title_cap = 0 if no_llm else config title_route_max_llm`.

Wire a `--no-llm` flag into the CLI `main()` (which currently hand-parses `sys.argv`): if `--no-llm`
appears in the args (and it's not the `set` subcommand), call `route_new(conn, no_llm=True)`. The
`set <job_id> <resume_type>` subcommand and the default full-LLM `route` are unchanged.

### 2. `pollers.yml` — rules-only, every 3h (unchanged schedule)
Change the "Route new postings" step command to `uv run python -m jobmaxxing.route --no-llm`, and
**remove** the `OPENAI_API_KEY` / `XAI_API_KEY` / `ANTHROPIC_API_KEY` env vars from that step (not
needed; belt-and-suspenders so no LLM can be called even if the flag regressed). Keep `DATABASE_URL`.
The cron stays `0 */3 * * *`. Ingest → enrich → rules-route continue every 3h at zero LLM cost.

### 3. New `.github/workflows/llm-route.yml` — LLM pass, every ~4 days
A separate workflow mirroring the pollers job shape:
- `on: schedule: - cron: "0 6 */4 * *"` (06:00 UTC on days 1, 5, 9, 13, 17, 21, 25, 29) plus
  `workflow_dispatch: {}`. `permissions: contents: read`. `concurrency: {group: llm-route,
  cancel-in-progress: false}`.
- Steps: checkout → setup-uv (python 3.12) → `uv sync --frozen --no-dev` → migrate (`DATABASE_URL`) →
  **`uv run python -m jobmaxxing.route`** (full LLM) with env `DATABASE_URL` + `OPENAI_API_KEY` +
  `XAI_API_KEY` + `ANTHROPIC_API_KEY` from secrets.
- This drains the accumulated deferred/ambiguous backlog, bounded by the existing per-run caps
  (200 JD + 100 title). If the backlog ever exceeds that, it drains over successive 4-day runs.

### 4. README cadence note
Update the Routing section: "Rules routing runs every 3h in `pollers.yml` (`route --no-llm`, no LLM
cost); the LLM tiebreak/title-routing runs every ~4 days in `llm-route.yml`. Ambiguous postings are
therefore classified within ~4 days; rule-matched postings appear within hours."

## Trade-offs & risks
- **`*/4` day-of-month** cron resets at month start → one sub-4-day gap per month (day 29 → day 1).
  Matches the user's "maybe once every 4 days."
- **Ambiguous-job latency:** up to ~4 days to classification. Acceptable (majority is rule-routed).
- **Overlap:** the 4-day `llm-route` could coincide with a 3-hourly `pollers` run. Both call
  `route_new`; the writes are a single idempotent batched transaction and `route_new` re-selects
  only still-unrouted rows, so a race causes at most duplicated work, never corruption. Separate
  `concurrency` groups are fine; no cross-workflow lock needed for a single-user pipeline.
- **Budget headroom:** if steady-state ambiguous volume exceeds 300/4-days, bump
  `max_llm_calls_per_run` / `title_route_max_llm` in `config/routing.yaml` (out of scope here).

## Testing (pyramid — comprehensive, no regression)
**Unit (`route_one`, pure — extend `tests/test_route_one.py`):**
- Ambiguous JD row with `budget.remaining == 0` → returns `_DEFER`, and the injected `llm_complete`
  spy is **never called**.
- Exhausted no-signal row with `title_budget.remaining == 0` → `_DEFER`, spy never called.
- Exhausted ambiguous-no-JD row with `title_budget.remaining == 0` → `_DEFER`, spy never called.
- Regression: a clearly rule-routable title still returns `method="rules"` with budgets at 0 (rules
  never need the LLM).

**Integration (`route_new`, DB — extend `tests/test_route_db.py`):**
- `route_new(conn, no_llm=True, llm_complete=<spy that raises if called>)`:
  - the spy is never called (no `AssertionError`/`RuntimeError` raised);
  - a rule-routable row is routed (`route_method='rules'`, `status='routed'`);
  - an ambiguous JD-bearing row is deferred (`resume_type IS NULL`, counted in `deferred`);
  - returned counts have `llm == 0` and `llm_title == 0`.
- Regression: `route_new(conn, no_llm=False, llm_complete=<fake returning a valid type>)` still LLM-
  routes an ambiguous row exactly as today (existing tests must stay green unchanged).

**CLI (extend `tests/test_route_db.py` or a small `tests/test_route_cli.py`):**
- Invoking `main()` with `sys.argv == ["route", "--no-llm"]` calls `route_new` with `no_llm=True`
  (patch `route_new`, assert kwarg); plain `["route"]` calls it with `no_llm=False`; `["route",
  "set", <id>, <type>]` still calls `set_manual` (unchanged).

**Workflow config (new `tests/test_workflows_routing.py`, YAML structure asserts, no network):**
- `pollers.yml` route step command contains `--no-llm` AND that step has **no** `*_API_KEY` env keys.
- `llm-route.yml` exists, has `cron: "0 6 */4 * *"`, its route step is `route` **without** `--no-llm`
  and **with** the three `*_API_KEY` envs, and it runs `migrate` before `route`.
- (Parse with the stdlib-friendly approach already used in the repo, or `yaml.safe_load`; if PyYAML
  isn't a dep, assert via string checks on the file contents.)

**No regression:** the full existing routing suite (`test_route_one.py`, `test_route_db.py`,
`test_routing_rules.py`, `test_routing_config.py`) must remain green — the `no_llm` path is additive
and defaulted off.

## Out of scope
Changing the deterministic rules or budgets; dynamic/per-run budget sizing; migrating cron to a
precise 96-hour cadence; any change to enrichment, ingestion, tailoring, or the nightly report.

## Execution
Isolated git worktree off `main`; subagent-driven TDD with the implementer reading the routing
package + workflows for full context first; two-stage review (spec → quality) per task; merge to
`main`; push (gh `vaibhavw30`).
