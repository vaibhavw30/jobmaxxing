# Spec — Phase 2: Routing

**Sprint:** Phase 2 of the Internship Recruiting Pipeline (`docs/PRD.md` §6.3/§10.2, `docs/TECHNICAL_IMPLEMENTATION_PLAN.md` §5/§8)
**Author:** Vaibhav
**Date:** 2026-06-11
**Status:** Approved for planning
**Builds on:** Phase 1 core feed (merged) — the `jobs` table already has `resume_type`, `route_method`, `route_confidence`, `status`.

---

## 1. Goal & rationale

Classify each new posting into exactly one of the 8 fixed resume **types** so the right base resume is selected downstream. This is a **router, not a ranker** — no semantic relevance scoring.

The 8 types (fixed set, per PRD non-goals):
`quant-trader, quant-dev, mle, swe, fdse, ai, robotics, av`.

**Guiding principle (operator directive): maximize deterministic routing; minimize LLM use.** Deterministic rules do the heavy lifting; the LLM is a *bounded last resort* invoked only when deterministic layers cannot separate candidates, and its output is always forced through a hard schema gate. The target is for the large majority of postings to route with zero LLM cost.

## 2. Success criteria

- Every new, non-manual posting with enough signal gets a `resume_type`, `route_method`, and `route_confidence`, and advances `status: new → routed`.
- **Deterministic dominance:** the large majority of routed postings carry `route_method='rules'`; `route_method='llm'` is the exception, bounded to genuinely ambiguous JD-bearing cases. Each run logs the rules-vs-llm split so the dictionaries can be tuned to drive LLM usage down.
- The LLM never produces an out-of-enum label: its output is validated against the 8 types and the candidate set; any invalid output falls back to the deterministic top pick.
- Operator manual labels (`route_method='manual'`) are never overwritten by automated routing.
- Re-running routing is idempotent (only unrouted, non-manual rows are processed).
- Routing runs as a second step after ingestion in the existing pollers workflow.
- The LLM access path is provider-agnostic (OpenAI / xAI / Anthropic) and fails over automatically; swapping models is a config change.

## 3. The 8 types (definitions used by both the rules config and the LLM prompt)

| Type | What it is | Disambiguation |
| --- | --- | --- |
| `quant-trader` | Trading desk / market-making intern: markets, EV, PnL, game theory | Trading/markets focus, not systems-building |
| `quant-dev` | Low-latency systems for trading: C++, market data, backtesting | Engineering for trading infra, not the trading itself |
| `mle` | ML engineering: model training/inference, features, eval at scale | Builds/operates ML models, not LLM-app or general SWE |
| `swe` | General software engineering: languages/frameworks, APIs, distributed, CI/CD | Default for generic software roles with no specialized track |
| `fdse` | Forward-deployed / customer-facing engineering (Palantir-style): deployment, data integration, ontology/Foundry | Customer-facing + integration, not pure backend |
| `ai` | Applied generative AI: LLMs, agents, RAG, fine-tuning, inference infra | LLM/agent focus, distinct from classical `mle` |
| `robotics` | Robotics: control, perception, ROS, state estimation, sim, RL | Physical robots / control |
| `av` | Autonomous vehicles: perception, sensor fusion, planning, SLAM, safety | Self-driving specifically |

These definitions are stored once and referenced by the LLM tiebreaker prompt so the rules and the LLM share one taxonomy.

## 4. Architecture

```
            jobs rows: resume_type IS NULL AND route_method <> 'manual'
                                  │
                                  ▼
   ┌─────────────────────────  router  ─────────────────────────┐
   │ (1) score types from TITLE signals (authoritative)          │
   │ (2) deterministic tie-break: JD-keyword margin among the    │
   │     title-matched candidates + exclusion signals            │
   │ (3) only if still tied AND a JD exists → LLM tiebreaker      │
   │     (constrained to the tied candidate set, schema-gated)   │
   │ (4) ambiguous AND title-only (no JD) → DEFER (leave 'new')  │
   └──────────────────────────────┬──────────────────────────────┘
                                   ▼
        update resume_type, route_method, route_confidence, status='routed'
                                   │
                LLM path only ────▶│  llm.complete('route', …)  (provider-agnostic)
                                   ▼
                          config/routing.yaml  +  config/llm.yaml
```

New modules under `src/jobmaxxing/`:
- `llm/` — the provider-agnostic wrapper (`client.py`, provider adapters, config loader).
- `routing/` — `rules.py` (pure scorer + decision), `tiebreaker.py` (LLM gate + schema validation), `route.py` (DB selection, update, entrypoint, manual-override CLI).

## 5. LLM wrapper (`src/jobmaxxing/llm/`)

The only code that touches a provider SDK. Interface:

```
complete(task: str, messages: list[dict], *, max_tokens: int, response_format=None) -> str
```

- **Config** `config/llm.yaml` maps each `task` to an ordered list of `(provider, model)` candidates. The `route` task default:
  ```yaml
  tasks:
    route:
      - {provider: openai,    model: gpt-4o-mini}
      - {provider: xai,       model: grok-3-mini}     # large credit pool, cheap
      - {provider: anthropic, model: claude-3-5-haiku-latest}
  ```
  (Exact model IDs are config and can be tuned without code changes.)
- **Provider adapters:** an **OpenAI-compatible** adapter serves both `openai` (default base URL) and `xai` (`base_url=https://api.x.ai/v1`) — same client class, different base URL + key; an **Anthropic** adapter for `anthropic`. Adds `openai` + `anthropic` deps.
- **Fallback:** try candidates in order; on error / timeout / rate-limit / **missing API key**, log and fall through to the next. A candidate whose key env var is absent is skipped (not fatal). If all candidates fail, raise `LLMUnavailable` — the caller (tiebreaker) catches it and falls back to the deterministic pick.
- **Keys** from env: `OPENAI_API_KEY`, `XAI_API_KEY`, `ANTHROPIC_API_KEY`. These become GitHub Actions secrets for the workflow.
- **No prompt-caching plumbing** this phase (Phase 3 tailoring introduces it).

## 6. Routing rules (deterministic, free) — `routing/rules.py`

### 6.1 Signal config `config/routing.yaml`
```yaml
weights:    { title: 3.0, jd: 1.0 }
thresholds: { min_top_score: 1.0, min_margin_ratio: 0.5, max_llm_calls_per_run: 200 }
types:
  quant-dev:
    title_signals: ["quantitative developer", "quant developer", "quant dev", "quantitative software"]
    jd_signals:    ["low-latency", "c++", "market data", "backtesting", "time-series", "order book"]
    exclude_signals: []          # presence subtracts from this type's score
  # ... one entry per type
```
Matching is substring over normalized text (lowercase, punctuation/whitespace-collapsed — reuse `normalize.normalize_text`). `title_hits` / `jd_hits` count **unique** signals matched; `jd_hits` is capped (default 5) so a keyword-stuffed JD can't dominate. `exclude_signals` matched in the text subtract a title-weight penalty from that type.

### 6.2 Decision flow (each step deterministic until the final gate)
1. Compute `title_hits` per type from the **title**.
2. **Exactly one** type has `title_hits > 0` → route to it. `method='rules'`, `confidence` scaled by hit count (capped at 1.0, floor 0.7 for a clean single-title match). *Most postings resolve here — title is authoritative.*
3. **Multiple** types have title hits (title ambiguous) → score those candidates by `jd_hits` (− exclusions). Clear winner by `margin_ratio ≥ min_margin_ratio` → `method='rules'`, `confidence=margin_ratio`. Else go to §7.
4. **No** type has title hits → score **all** types by JD signals. If a JD exists and a winner clears `min_top_score` and `min_margin_ratio` → `method='rules'`, `confidence=margin_ratio`. Else go to §7.

`margin_ratio = (top − second) / max(top, 1)`, clamped to [0, 1].

## 7. LLM tiebreaker & title-only deferral — `routing/tiebreaker.py`

Reached only when §6 cannot resolve. The LLM is **always** a fallback, never the primary path, and is always schema-gated.

- **Ambiguous + JD present:** one `llm.complete('route', …)` call. The prompt supplies the §3 type definitions, the title, the JD, and the **tied candidate set**, and demands strict JSON `{type, confidence}`.
  - **Hard schema gate (deterministic):** parse the JSON; `type` must be one of the candidate set (preferred) or the 8 valid types; `confidence` must be a float in [0,1]. **Any** parse failure, out-of-enum value, or `LLMUnavailable` → deterministic fallback to the highest-JD-score candidate with a low confidence (e.g. 0.4) and `method='rules'` (the LLM did not decide). On a valid response → `method='llm'`, store the returned confidence.
  - **Per-run cap:** at most `max_llm_calls_per_run` LLM calls; once hit, remaining ambiguous rows **defer** (logged), so a bad dictionary can never cause runaway spend.
- **Ambiguous + title-only (no JD):** **defer** — leave `resume_type=null`, `status='new'`, no LLM spend. Retried on a later run after an ATS poller enriches the row with a JD.

## 8. Entrypoint, orchestration, override, idempotency — `routing/route.py`

- **`route_new(conn, *, now, reroute=False, max_llm_calls=...)`**: selects rows where `resume_type IS NULL AND route_method IS DISTINCT FROM 'manual'` (or, with `reroute=True`, rows that already have a type but `route_method <> 'manual'`, for after dictionary tuning). For each, run §6→§7; on a decision, update `resume_type/route_method/route_confidence` and set `status='routed'`. Deferred rows are left untouched. Returns counts `{rules, llm, deferred, manual_skipped}`.
- **CLI:** `python -m jobmaxxing.route` runs `route_new` over the live DB; `python -m jobmaxxing.route set <job_id> <type>` performs a manual override (`resume_type=<type>`, `route_method='manual'`, `route_confidence=1.0`, `status='routed'`; validates `<type>` against the 8). SQL works too; the MCP tool arrives in Phase 4.
- **Orchestration:** add a `route` step to `.github/workflows/pollers.yml` **after** the ingestion step, sharing the same `DATABASE_URL` and now also the LLM key secrets. One workflow: ingest, then route the new rows.
- **Idempotency:** only unrouted, non-manual rows are touched, so re-runs and overlapping cycles are safe. Manual rows are never overwritten.

## 9. Cost guardrails & observability

- LLM is bounded to ambiguous-with-JD rows, further capped per run, and never invoked for title-only or confidently-routed rows.
- Each run logs structured counts: `rules`, `llm`, `deferred`, `manual_skipped`, plus the rules:llm ratio. This is the signal for tuning `config/routing.yaml` to push LLM usage toward zero.
- `route_method` + `route_confidence` are persisted per row, so routing quality is auditable in SQL.

## 10. Data model

No migration needed — Phase 1 already created `resume_type text`, `route_method text`, `route_confidence real`, `status text`. Routing only writes these columns (and `status`). `route_method` values: `'rules' | 'llm' | 'manual'`.

## 11. Testing

- **Pure scorer + decision logic** (`rules.py`): table-driven tests across all 8 types — single-title match, title ambiguity resolved by JD margin, exclusion signals, no-title-signal JD routing, margin/threshold boundaries. No DB, no network.
- **Tiebreaker** (`tiebreaker.py`) with a **mocked LLM client** (same pattern as the mocked `fetch_json`): ambiguous+JD calls the LLM; title-only defers; manual rows skipped; **schema gate** — valid response routes as `llm`, invalid/out-of-enum/`LLMUnavailable` falls back deterministically; per-run cap enforced.
- **LLM wrapper** (`llm/`): provider selection from config, fallback on simulated error/missing-key, OpenAI/xAI sharing the compatible adapter — all with mocked SDK clients (no real calls).
- **DB integration** (`route_new` against `pytest-postgresql`): seed rows (clear-title, ambiguous-with-JD via mocked LLM, title-only, manual) → assert `resume_type/route_method/route_confidence/status` and that manual rows are untouched; assert returned counts.
- **Untested boundary:** the real provider SDK network calls — one optional smoke test gated behind env keys, skipped in CI.

## 12. Deliverables

- `src/jobmaxxing/llm/` — provider-agnostic wrapper + OpenAI-compatible & Anthropic adapters + config loader; `config/llm.yaml`.
- `src/jobmaxxing/routing/` — `rules.py`, `tiebreaker.py`, `route.py`; `config/routing.yaml` (8 type entries, seeded from §3 and tech-plan §7.2).
- `.github/workflows/pollers.yml` — added `route` step; LLM key secrets documented in the README.
- Tests for all of the above; README section on routing (running it, tuning the dictionaries, adding a manual override, the rules-vs-llm metric).
- New deps: `openai`, `anthropic`.

## 13. Open items (resolve during implementation, not blocking)

- Final contents of each type's `title_signals` / `jd_signals` / `exclude_signals` — seed from §3 + tech-plan §7.2, then tune against real routed postings (watch the rules:llm ratio and manual corrections).
- Exact cheap model IDs per provider (e.g. the current xAI mini model) — config, verify against live availability.
- Default threshold values (`min_margin_ratio`, `jd_hits` cap, `max_llm_calls_per_run`) — start from the §6.1 defaults, tune against real data.
- Whether `--reroute` should also re-run on `route_method='llm'` rows after dictionary improvements (default: yes, since only `manual` is sacrosanct).
