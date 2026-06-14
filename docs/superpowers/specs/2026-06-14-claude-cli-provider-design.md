# Spec — `claude-cli` LLM provider (use the Claude subscription locally)

**Type:** New feature (cost optimization) — a new provider in the LLM wrapper
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** the `llm/` wrapper (Phase 2). Adds one provider adapter + config; `complete()`'s control flow is unchanged.

---

## 1. Problem & rationale

The pipeline calls the LLM through `llm.client.complete(task, …)`, which tries the configured `(provider, model)` candidates for a task and bills **API tokens**. The expensive task is **tailoring** (`tailor`/`review`): Sonnet, multi-pass build→critique→patch over long prompts (full résumé + JD), run **locally** by the operator (`python -m jobmaxxing.tailor`). Routing is cheap Haiku and runs in CI.

The operator has a flat-fee **Claude subscription** and the `claude` CLI installed + logged in (verified: v2.1.177). `claude -p` (print mode) serves completions on the subscription, not API billing. Since tailoring already runs on the operator's logged-in machine, those pricey calls can go through the subscription for **zero marginal token cost**, with the API as automatic fallback.

### Goal
Add a `claude-cli` provider that completes via `claude -p`, make it the **first** candidate for `tailor` and `review` (API kept as fallback), and ensure it authenticates with the **subscription** (not a stray API key). `route` and CI are untouched. No change to `complete()`'s fallback loop.

### Scope
**In:** a `_claude_cli` adapter in `llm/providers.py`, a `provider_available` branch for it, its registration, `config/llm.yaml` edits for `tailor`/`review`, and a unit/integration/e2e test set.
**Out:** routing via subscription (cheap Haiku anyway; a Sonnet-class subscription model would be overkill + slower); prompt-caching (irrelevant on a flat-fee subscription); MCP (not a billing path); the Claude Agent SDK (heavier than shelling to the installed CLI).

## 2. Architecture

The wrapper already dispatches by a provider-name → adapter dict (`_ADAPTERS`) and gates each by `provider_available(provider)`. We add one adapter and one availability branch; `client.complete()` is **not modified** — its existing "try each candidate, skip unavailable, fall through on error" loop gives graceful degradation for free.

```
complete("tailor", …)
   └─ candidates_for("tailor")  →  [claude-cli/sonnet, anthropic/…, openai/…]   (config order)
        ├─ provider_available("claude-cli")?  shutil.which("claude")
        │     yes → _claude_cli(...)  →  `claude -p` on the SUBSCRIPTION  →  text
        │     (binary absent in CI, or call errors/rate-limited)  → fall through ↓
        ├─ provider_available("anthropic")?  ANTHROPIC_API_KEY set → _anthropic(...) (API)
        └─ … → openai
```

### Verified invocation (the exact command the adapter runs)

Confirmed working on the operator's machine (returns clean text, exit 0, subscription auth):

```
printf '<prompt>' | env -u ANTHROPIC_API_KEY \
    claude -p --model sonnet --system-prompt '<system text>' --allowedTools ''
```

- `env -u ANTHROPIC_API_KEY` (in code: a child env without that key) — **the critical detail.** With `ANTHROPIC_API_KEY` present, the CLI may bill the **API** instead of the subscription; stripping it forces subscription auth. This is the whole point of the feature.
- `--allowedTools ''` — no tools, so `claude -p` behaves as a pure text completion, never an agent touching files/bash.
- `--system-prompt <text>` — our system message replaces the default Claude Code agent prompt.
- prompt on **stdin** — safe for long résumé/JD content (no arg-length/escaping issues).

## 3. Components (with code)

### 3.1 `provider_available` — a branch for the CLI (`llm/providers.py`)

```python
import shutil  # add to imports

def provider_available(provider: str) -> bool:
    """True if the provider can serve a request right now."""
    if provider == "claude-cli":
        return shutil.which("claude") is not None   # present locally; absent in CI -> auto-skip
    return bool(os.environ.get(PROVIDER_KEYS.get(provider, "")))
```

The binary being present is the gate. If `claude` exists but isn't logged in / is rate-limited, the call still *fails at runtime* and `complete()` falls through to the next candidate — so this coarse check is sufficient.

### 3.2 `_claude_cli` adapter (`llm/providers.py`)

```python
import subprocess  # add to imports

CLAUDE_CLI_TIMEOUT = 300  # tailoring prompts are large; give the CLI a generous window


def _claude_cli(provider, model, messages, max_tokens, response_format, cache=None):
    """Complete via the local `claude -p` CLI on the user's Claude subscription.

    Single-shot: system messages -> --system-prompt; cache (the base résumé) + non-system
    messages -> the stdin prompt. `response_format` and `max_tokens` have no CLI knob and are
    intentionally ignored — callers parse leniently (parse_critique), same as the Anthropic
    adapter. ANTHROPIC_API_KEY is stripped from the child env so the CLI authenticates with the
    subscription, not API billing.
    """
    system_text = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt_parts = ([cache] if cache else []) + [m["content"] for m in messages if m["role"] != "system"]
    prompt = "\n\n".join(prompt_parts)

    cmd = ["claude", "-p", "--model", model, "--allowedTools", ""]
    if system_text:
        cmd += ["--system-prompt", system_text]
    env = {k: v for k, v in os.environ.items() if k != PROVIDER_KEYS["anthropic"]}

    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            env=env, timeout=CLAUDE_CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude -p timed out after {CLAUDE_CLI_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("claude -p returned empty output")
    return out
```

A non-zero exit, timeout, or empty output raises `RuntimeError` — caught by `complete()`'s `except Exception` and demoted to a fallback (next candidate). This matches how the other adapters surface failures.

### 3.3 Registration (`llm/providers.py`)

```python
_ADAPTERS = {
    "openai": _openai_compatible,
    "xai": _openai_compatible,
    "anthropic": _anthropic,
    "claude-cli": _claude_cli,
}
```

### 3.4 Config (`config/llm.yaml`)

```yaml
  tailor:
    - {provider: claude-cli, model: sonnet}                  # local subscription (no API tokens)
    - {provider: anthropic, model: claude-sonnet-4-5-20250929}
    - {provider: openai, model: gpt-4o}
  review:
    - {provider: claude-cli, model: sonnet}
    - {provider: anthropic, model: claude-sonnet-4-5-20250929}
    - {provider: openai, model: gpt-4o}
```

`claude-cli` uses the CLI **alias** `sonnet` (the CLI resolves it to the current Sonnet — aliases are safe via the CLI, unlike the `-latest` API aliases that 404'd, because the CLI maps to whatever the subscription currently serves). `route` is unchanged (no `claude-cli` entry — stays API Haiku in CI).

## 4. Message flattening (the one semantic mapping)

`claude -p` is single-shot (no multi-turn messages API like the SDKs). The adapter maps the wrapper's `messages` + `cache` onto it:

| Wrapper input | → `claude -p` |
| --- | --- |
| `system` message(s) | `--system-prompt` (joined with `\n\n`) |
| `cache` (e.g. `base_tex`, the base résumé) | prepended to the stdin prompt as context |
| `user` (and any `assistant`) message(s) | the stdin prompt (joined with `\n\n`) |

This preserves the existing prompts verbatim — `build_tailored`/`critique_resume`/`apply_critique`/`shrink_to_one_page` keep their `_TAILOR_SYSTEM`/`_REVIEW_SYSTEM`/etc. The base résumé that the API path prompt-caches simply becomes inline context (no caching, but the subscription is flat-fee, so cost is unaffected).

## 5. Fallback & error handling (no new control flow)

`complete()` already: skips a provider when `provider_available` is false, calls the adapter, and on `except Exception` records the error and tries the next candidate. So:
- **CI** (no `claude` binary) → `claude-cli` skipped → API as today.
- **Local, logged in** → `claude-cli` serves it on the subscription.
- **Local, not logged in / rate-limited / CLI error / timeout** → adapter raises → automatic fall-through to the API.

No change to `complete()`. The only risk it introduces is latency (a failed CLI attempt before falling back), bounded by `CLAUDE_CLI_TIMEOUT`.

## 6. Testing — full pyramid

### 6.1 Unit (`tests/test_llm_claude_cli.py`) — mock `subprocess.run` + `shutil.which`, no real CLI
- `provider_available("claude-cli")` → True when `shutil.which` returns a path, False when `None`.
- `_claude_cli` happy path: mock `subprocess.run` to return `CompletedProcess(returncode=0, stdout="  RESULT \n")` → returns `"RESULT"`. Assert the call:
  - `cmd` contains `["claude", "-p", "--model", "sonnet", "--allowedTools", ""]` and `--system-prompt <system text>`.
  - `input` (stdin) equals `cache + "\n\n" + user` flattening (cache first).
  - **`env` passed to `subprocess.run` does NOT contain `ANTHROPIC_API_KEY`** (the core guarantee) while retaining other vars (e.g. `PATH`).
- `_claude_cli` with no system message → no `--system-prompt` flag.
- Failure mapping: `returncode=1` → `RuntimeError` (message includes stderr); `subprocess.TimeoutExpired` → `RuntimeError`; empty stdout → `RuntimeError`.
- `response_format`/`max_tokens` are accepted and ignored (call with both set still returns stdout).

### 6.2 Integration (`tests/test_llm_fallback.py` or extend existing) — exercise `complete()` end-to-end with fakes
- Config `[claude-cli/sonnet, anthropic/x]`; monkeypatch `provider_available` so `claude-cli` is **unavailable** → assert the `anthropic` adapter is the one invoked (claude-cli skipped). 
- `claude-cli` **available** but its adapter raises (monkeypatch `call_provider`/the adapter to raise) → assert fall-through to `anthropic`.
- `claude-cli` available and returns text → assert `complete()` returns it and never calls the API adapter.
- (If an existing `complete()` fallback test exists, mirror its style; do not duplicate the wrapper's own logic.)

### 6.3 End-to-end (`tests/test_llm_claude_cli_e2e.py`) — real `claude -p`, skip-by-default
- Marked `skipif` unless `JOBMAXXING_E2E=1` **and** `shutil.which("claude")` (mirrors the Workday e2e + pdflatex skip pattern — never runs in CI/normal `pytest`).
- One real round-trip: `_claude_cli("claude-cli", "sonnet", [{"role":"system","content":"Output only what is asked."},{"role":"user","content":"Reply with the single word PONG."}], max_tokens=20, response_format=None)` → asserts non-empty output containing `PONG`. Confirms subscription auth + the exact invocation work on the operator's machine.

## 7. Invariants & constraints

| Invariant | How it holds |
| --- | --- |
| CI behavior unchanged | `claude` binary absent in CI → `claude-cli` skipped → existing API path; no workflow/route edits. |
| Subscription used, not API tokens | `ANTHROPIC_API_KEY` removed from the child env in `_claude_cli`; unit test asserts it. |
| Existing prompts unchanged | Flattening maps system/user/cache onto CLI flags + stdin; `passes.py` is untouched. |
| Graceful degradation | All CLI failures raise → caught by `complete()`'s fallback loop. |
| No new dependency | Shells to the already-installed `claude`; uses stdlib `subprocess`/`shutil` only. |
| Routing cost untouched | `route` config unchanged (API Haiku). |

## 8. Deliverables

- `llm/providers.py`: `import shutil`, `import subprocess`; `provider_available` `claude-cli` branch; `_claude_cli` adapter + `CLAUDE_CLI_TIMEOUT`; `_ADAPTERS` registration.
- `config/llm.yaml`: `claude-cli` first for `tailor` and `review`.
- `tests/test_llm_claude_cli.py` (unit), integration additions, `tests/test_llm_claude_cli_e2e.py` (skip-by-default).
- README note: tailoring uses your Claude subscription via `claude -p` when the CLI is logged in; ensure `claude` is authenticated to your subscription (and that a local `ANTHROPIC_API_KEY` won't be used for tailoring).
- No migration, no CI change.

## 9. Open items & risks (named, accepted)

- **CLI may wrap output** (e.g. ```` ```latex ```` fences or a chat preamble) where the raw API returns bare text. The verified smoke test returned clean text, but the LaTeX tasks are longer — the e2e test + first real `python -m jobmaxxing.tailor` run confirm it. If fencing appears, add a thin "strip surrounding code fence" step in `_claude_cli` (resolve during implementation against a real tailoring call, not speculatively).
- **Subscription usage limits / acceptable-use:** bulk automation on a Claude *Code* subscription counts against plan limits and is somewhat gray on intended use. Fine at the operator's volume (tailoring is human-gated to a handful of jobs); if limits hit, the API fallback covers it.
- **Latency on fallback:** a failed CLI attempt costs up to `CLAUDE_CLI_TIMEOUT` before the API takes over. Acceptable for a local, human-paced step; the timeout bounds it.
- **`claude` present but logged into an API key, not a subscription:** then `claude -p` would still bill API (via the CLI's own key) — but we strip `ANTHROPIC_API_KEY` from the env, and the CLI's stored credential is the operator's chosen login. Documented in the README so the operator ensures a subscription login.
