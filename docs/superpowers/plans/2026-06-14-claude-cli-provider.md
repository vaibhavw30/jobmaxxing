# claude-cli LLM Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `claude-cli` LLM provider that completes via `claude -p` on the operator's Claude subscription, used as the primary candidate for the local `tailor`/`review` tasks with the API as automatic fallback.

**Architecture:** One new adapter in the existing provider-dict (`llm/providers.py`) plus a `provider_available` branch; `client.complete()`'s try-each-candidate fallback loop is unchanged. The adapter shells to `claude -p` via `subprocess`, mapping `messages`/`cache` onto `--system-prompt` + stdin, and strips `ANTHROPIC_API_KEY` from the child env so the CLI uses the subscription, not API billing.

**Tech Stack:** Python 3.12 stdlib `subprocess`/`shutil`; the installed `claude` CLI; pytest with `monkeypatch`.

**Spec:** `docs/superpowers/specs/2026-06-14-claude-cli-provider-design.md`

---

## File structure

- Modify `src/jobmaxxing/llm/providers.py` — add `import shutil`, `import subprocess`; a `claude-cli` branch in `provider_available`; the `_claude_cli` adapter + `CLAUDE_CLI_TIMEOUT`; register it in `_ADAPTERS`.
- Modify `config/llm.yaml` — make `claude-cli` the first candidate for `tailor` and `review`.
- Modify `tests/test_llm_providers.py` — unit tests for the new branch + adapter (mock `subprocess.run`/`shutil.which`).
- Create `tests/test_llm_claude_cli_e2e.py` — skip-by-default real-CLI round-trip.
- Modify `README.md` — note tailoring uses the subscription via `claude -p` when logged in.

All tests run from the repo root: `uv run pytest`.

---

### Task 1: `provider_available` branch for `claude-cli`

**Files:**
- Modify: `src/jobmaxxing/llm/providers.py`
- Test: `tests/test_llm_providers.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_providers.py`:

```python
def test_provider_available_claude_cli_checks_binary(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert providers.provider_available("claude-cli") is True
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.provider_available("claude-cli") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_providers.py::test_provider_available_claude_cli_checks_binary -v`
Expected: FAIL — `AttributeError: module 'jobmaxxing.llm.providers' has no attribute 'shutil'` (shutil not imported yet) or the assertion fails (falls through to the env check and returns False for both).

- [ ] **Step 3: Add the import and the branch**

In `src/jobmaxxing/llm/providers.py`, add `import shutil` to the imports (alongside `import os`), then change `provider_available`:

```python
def provider_available(provider: str) -> bool:
    """True if the provider can serve a request right now."""
    if provider == "claude-cli":
        return shutil.which("claude") is not None   # present locally; absent in CI -> auto-skip
    return bool(os.environ.get(PROVIDER_KEYS.get(provider, "")))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_providers.py::test_provider_available_claude_cli_checks_binary -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/llm/providers.py tests/test_llm_providers.py
git commit -m "feat(llm): provider_available recognizes the claude-cli provider"
```

---

### Task 2: `_claude_cli` adapter + registration

**Files:**
- Modify: `src/jobmaxxing/llm/providers.py`
- Test: `tests/test_llm_providers.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm_providers.py` (add `import subprocess` and `import pytest` at the top if not present):

```python
class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_claude_cli_happy_path_and_env_strips_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0, stdout="  RESULT TEXT \n")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}]
    out = providers.call_provider("claude-cli", "sonnet", messages, max_tokens=4000, cache="BASE")
    assert out == "RESULT TEXT"
    # command: model + tools-off + system flag
    assert captured["cmd"][:5] == ["claude", "-p", "--model", "sonnet", "--allowedTools"]
    assert "--system-prompt" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--system-prompt") + 1] == "SYS"
    # stdin prompt = cache then user message
    assert captured["kwargs"]["input"] == "BASE\n\nUSR"
    # THE GUARANTEE: the child env has no ANTHROPIC_API_KEY (forces subscription auth)
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["PATH"] == "/usr/bin"   # other env preserved


def test_claude_cli_omits_system_flag_when_no_system(monkeypatch):
    captured = {}
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: captured.update(cmd=cmd) or _FakeProc(0, "ok"))
    providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert "--system-prompt" not in captured["cmd"]


def test_claude_cli_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: _FakeProc(returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(RuntimeError, match="not logged in"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)


def test_claude_cli_timeout_raises(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(providers.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)


def test_claude_cli_empty_output_raises(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: _FakeProc(returncode=0, stdout="   \n"))
    with pytest.raises(RuntimeError, match="empty"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm_providers.py -k claude_cli -v`
Expected: FAIL — `ValueError: unknown provider: 'claude-cli'` (adapter not registered yet).

- [ ] **Step 3: Implement the adapter and register it**

In `src/jobmaxxing/llm/providers.py`, add `import subprocess` to the imports. Add the adapter (after `_anthropic`):

```python
CLAUDE_CLI_TIMEOUT = 300  # tailoring prompts are large; give the CLI a generous window


def _claude_cli(provider, model, messages, max_tokens, response_format, cache=None):
    """Complete via the local `claude -p` CLI on the user's Claude subscription.

    Single-shot: system messages -> --system-prompt; cache (the base résumé) + non-system
    messages -> the stdin prompt. response_format and max_tokens have no CLI knob and are
    intentionally ignored (callers parse leniently, same as the Anthropic adapter).
    ANTHROPIC_API_KEY is stripped from the child env so the CLI authenticates with the
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

Then register it in `_ADAPTERS`:

```python
_ADAPTERS = {
    "openai": _openai_compatible,
    "xai": _openai_compatible,
    "anthropic": _anthropic,
    "claude-cli": _claude_cli,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm_providers.py -k claude_cli -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/llm/providers.py tests/test_llm_providers.py
git commit -m "feat(llm): claude-cli adapter (claude -p, subscription auth, stdin prompt)"
```

---

### Task 3: Config — `claude-cli` first for `tailor`/`review`

**Files:**
- Modify: `config/llm.yaml`
- Test: `tests/test_llm_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_config.py`:

```python
def test_tailor_and_review_prefer_claude_cli():
    from jobmaxxing.llm.config import candidates_for, load_llm_config
    cfg = load_llm_config()  # the real config/llm.yaml
    for task in ("tailor", "review"):
        cands = candidates_for(task, cfg)
        assert cands[0] == {"provider": "claude-cli", "model": "sonnet"}, task
        assert any(c["provider"] == "anthropic" for c in cands[1:]), f"{task} keeps an API fallback"
    # route must NOT use claude-cli (CI has no subscription; stays API)
    assert all(c["provider"] != "claude-cli" for c in candidates_for("route", cfg))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_config.py::test_tailor_and_review_prefer_claude_cli -v`
Expected: FAIL — current first candidate for `tailor`/`review` is `anthropic`, not `claude-cli`.

- [ ] **Step 3: Edit the config**

In `config/llm.yaml`, change the `tailor` and `review` task lists so `claude-cli` is first (leave `route` untouched):

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

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_config.py::test_tailor_and_review_prefer_claude_cli -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/llm.yaml tests/test_llm_config.py
git commit -m "feat(llm): prefer claude-cli (subscription) for tailor/review, API fallback"
```

---

### Task 4: Integration — `complete()` selects/falls-through `claude-cli`

**Files:**
- Test: `tests/test_llm_client.py`

Confirms the new provider participates correctly in the existing fallback loop. No production code changes — `complete()` is provider-agnostic.

- [ ] **Step 1: Write the test**

Append to `tests/test_llm_client.py`:

```python
_TAILOR_CONFIG = {
    "tasks": {
        "tailor": [
            {"provider": "claude-cli", "model": "sonnet"},
            {"provider": "anthropic", "model": "claude-sonnet-4-5-20250929"},
        ]
    }
}


def test_tailor_uses_claude_cli_when_available(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: f"{p}:ok")
    assert complete("tailor", MESSAGES, max_tokens=4000, config=_TAILOR_CONFIG) == "claude-cli:ok"


def test_tailor_falls_back_to_api_when_cli_absent(monkeypatch):
    # claude binary missing (CI) -> claude-cli unavailable -> anthropic serves it
    monkeypatch.setattr(client, "provider_available", lambda p: p != "claude-cli")
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: f"{p}:ok")
    assert complete("tailor", MESSAGES, max_tokens=4000, config=_TAILOR_CONFIG) == "anthropic:ok"


def test_tailor_falls_back_when_cli_errors(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: True)

    def flaky(p, m, msgs, **kw):
        if p == "claude-cli":
            raise RuntimeError("claude -p failed (exit 1): not logged in")
        return f"{p}:ok"

    monkeypatch.setattr(client, "call_provider", flaky)
    assert complete("tailor", MESSAGES, max_tokens=4000, config=_TAILOR_CONFIG) == "anthropic:ok"
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_llm_client.py -k tailor -v`
Expected: PASS (3 tests) — they pass immediately because `complete()` already handles any provider name; these lock in the tailor-specific behavior.

- [ ] **Step 3: Commit**

```bash
git add tests/test_llm_client.py
git commit -m "test(llm): tailor prefers claude-cli and falls back to API on absence/error"
```

---

### Task 5: Skip-by-default e2e + README

**Files:**
- Create: `tests/test_llm_claude_cli_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Write the e2e test (skipped unless `JOBMAXXING_E2E=1` and `claude` present)**

Create `tests/test_llm_claude_cli_e2e.py`:

```python
"""Real `claude -p` round-trip. Skipped unless JOBMAXXING_E2E=1 and the claude CLI is present
(mirrors the Workday e2e + pdflatex skip pattern). Confirms subscription auth + the exact
invocation work on the operator's machine. Run: JOBMAXXING_E2E=1 uv run pytest tests/test_llm_claude_cli_e2e.py -v
"""

import os
import shutil

import pytest

from jobmaxxing.llm import providers

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1" or shutil.which("claude") is None,
    reason="set JOBMAXXING_E2E=1 and have the claude CLI logged in to run this",
)


def test_claude_cli_real_roundtrip():
    messages = [
        {"role": "system", "content": "You output only what is asked, nothing else."},
        {"role": "user", "content": "Reply with the single word PONG."},
    ]
    out = providers.call_provider("claude-cli", "sonnet", messages, max_tokens=20)
    assert out and "PONG" in out.upper()
```

- [ ] **Step 2: Verify it is collected-but-skipped in a normal run**

Run: `uv run pytest tests/test_llm_claude_cli_e2e.py -v`
Expected: `1 skipped` (the guard fires when `JOBMAXXING_E2E` is unset).

- [ ] **Step 3: Document the subscription path in the README**

Add to `README.md`, near the tailoring/LLM notes:

```markdown
### LLM cost: tailoring uses your Claude subscription

The local tailoring step (`python -m jobmaxxing.tailor`) prefers the `claude-cli` provider —
it shells to `claude -p` on your **Claude subscription** instead of spending API tokens. Make
sure the `claude` CLI is installed and **logged in to your subscription** (`claude` then `/login`).
The adapter strips `ANTHROPIC_API_KEY` from the call so it can't accidentally bill the API; if
the CLI is absent (e.g. CI) or errors, the pipeline automatically falls back to the API. Routing
stays on the cheap API model. Optional check: `JOBMAXXING_E2E=1 uv run pytest tests/test_llm_claude_cli_e2e.py -v`.
```

- [ ] **Step 4: Run the full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass; the e2e test reports skipped (alongside the 2 pdflatex + Workday e2e skips).

- [ ] **Step 5: Commit**

```bash
git add tests/test_llm_claude_cli_e2e.py README.md
git commit -m "feat(llm): skip-by-default claude-cli e2e + README subscription note"
```

---

## Done criteria

- `uv run pytest -q` green: new unit tests for `provider_available`/`_claude_cli` (env-strip, flags, stdin, failure mapping), config preference, `complete()` fallback behavior; e2e skipped by default.
- `python -m jobmaxxing.tailor <job>` run locally with the `claude` CLI logged in uses the subscription (no API spend); with the CLI absent or erroring, it transparently falls back to the API.
- Routing and CI unchanged.
- Operator-verified: `JOBMAXXING_E2E=1 uv run pytest tests/test_llm_claude_cli_e2e.py -v` passes (real subscription round-trip), and a real tailoring run produces a clean `.tex` (confirm no markdown-fence wrapping — if present, add fence-stripping to `_claude_cli` per spec §9).
```
