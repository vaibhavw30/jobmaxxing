# Five-axis tailoring scorer + code-fence fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the P0 bug that makes tailored `.tex` uncompilable (leaked markdown fence), then implement the PRD §7 five-axis weighted scorer (deterministic keyword axis + four temperature-0 LLM-graded axes + rubric-weighted composite).

**Architecture:** A shared `strip_code_fence` (promoted to `llm/text.py`) guarantees every `.tex`-producing pass emits fence-free LaTeX. The scorer splits into three seams in `tailoring/scorer.py`: pure `score_keywords` (unchanged deterministic coverage — the grounded anchor), LLM-injected `score_qualitative` (four 0–10 axes, one temp-0 call, lenient parse), and pure `score` (composes the five axes + rubric-weighted composite). A `temperature` param is threaded through the LLM wrapper so the new `score` tier can request temp-0 on the API providers.

**Tech Stack:** Python 3.12, existing `llm/` wrapper (anthropic/openai/claude-cli), pytest. No new dependency.

## Global Constraints
- Python **3.12**. Run pytest with the Postgres binary on PATH (some suites need it):
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- **Backward-compatible / additive only:** `score()` returns a SUPERSET of today's dict
  (`static`/`dynamic`/`matched`/`missing` preserved) plus `axes` + `composite`. No DB migration; existing
  `review.json`/`get_review` readers keep working.
- **`keyword_coverage` axis = `10 × dynamic`** (0–10). **`composite = round(Σ weights[axis] × axes[axis], 2)`**,
  weights summing to 1.0 → composite in [0, 10]. Each axis is 0–10.
- **Determinism:** the new `score` task tier lists the **anthropic API model FIRST** (honors temperature) with
  `claude-cli` as best-effort fallback. The scoring call passes **`temperature=0`** with an identical prompt
  for before and after. The `temperature` param defaults to `None` → **behavior unchanged for every existing
  caller**; `claude-cli` accepts but ignores it (no CLI knob).
- **Lenient scoring:** `parse_qualitative` clamps each axis to [0, 10]; on ANY structural failure (no JSON,
  bad/missing axis) returns all four = **5.0** so a flaky call never crashes tailoring.
- **Fence fix:** one shared `strip_code_fence` in `llm/text.py` (SDK-free), applied in `build_tailored`,
  `apply_critique`, `shrink_to_one_page`. `providers.py` reuses it (behavior-preserving for `claude-cli`).
- **Reuse, don't reinvent:** follow the existing `parse_critique` lenient-parse idiom, the `test_pass_build.py`
  fake-`complete` pattern, and the `test_llm_providers.py` fake-SDK-client pattern.
- **Worktree cwd discipline:** every subagent pins its cwd to the feature worktree and verifies
  `git rev-parse --show-toplevel` before committing. Push with the `vaibhavw30` gh account.

---

## Task 1: Fence fix (P0) — shared `strip_code_fence`, applied to the `.tex` passes

**Why first:** until the tailored `.tex` is fence-free, tailoring can't compile a real PDF, so the scorer
can't be validated end-to-end.

**Files:**
- Create: `src/jobmaxxing/llm/text.py`
- Modify: `src/jobmaxxing/llm/providers.py` (reuse the shared helper), `src/jobmaxxing/tailoring/passes.py`
- Test: `tests/test_llm_text.py` (create), `tests/test_pass_build.py`, `tests/test_pass_patch.py`

**Interfaces:**
- Produces: `strip_code_fence(text: str) -> str` — drops a single markdown fence wrapping the WHOLE payload
  (```` ```lang\n…\n``` ````); returns the text unchanged if it isn't wholly fenced.

- [ ] **Step 1: Write the failing pure-helper tests.** Create `tests/test_llm_text.py`:
```python
from jobmaxxing.llm.text import strip_code_fence


def test_strips_latex_fence():
    assert strip_code_fence("```latex\n\\documentclass{article}\n```") == r"\documentclass{article}"

def test_strips_plain_fence():
    assert strip_code_fence("```\nhello\n```") == "hello"

def test_no_fence_returned_unchanged():
    assert strip_code_fence(r"\documentclass{article}" + "\nBODY") == "\\documentclass{article}\nBODY"

def test_not_wholly_wrapped_is_unchanged():
    # a fence in the interior (not at the very start) must NOT be stripped
    tex = "\\begin{lstlisting}\n```\ncode\n```\n\\end{lstlisting}"
    assert strip_code_fence(tex) == tex
```
Run: `uv run pytest tests/test_llm_text.py -q` → FAIL (module missing).

- [ ] **Step 2: Create `src/jobmaxxing/llm/text.py`** (SDK-free; the regex is moved verbatim from
`providers.py`):
```python
import re

# A single surrounding markdown fence (``` or ```lang) wrapping the WHOLE payload. LaTeX/JSON outputs
# never start with ``` so this only fires on a genuine wrapper (a broken .tex/JSON otherwise).
_CODE_FENCE = re.compile(r"\A```[^\n]*\n(.*)\n```\s*\Z", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Drop a single surrounding markdown code fence if a model wrapped the output; else unchanged."""
    m = _CODE_FENCE.match(text)
    return m.group(1).strip() if m else text
```
Run: `uv run pytest tests/test_llm_text.py -q` → PASS (4).

- [ ] **Step 3: Point `providers.py` at the shared helper (behavior-preserving).** In
`src/jobmaxxing/llm/providers.py`: add `from .text import strip_code_fence` to the imports; DELETE the local
`_CODE_FENCE` constant and the local `_strip_code_fence` function (currently ~lines 76–83); in `_claude_cli`
change `return _strip_code_fence(out)` to `return strip_code_fence(out)`.
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_llm_providers.py tests/test_llm_claude_cli_e2e.py -q` → PASS (unchanged behavior; e2e skips without a subscription).

- [ ] **Step 4: Write failing pass-level fence tests.** Append to `tests/test_pass_build.py`:
```python
def test_build_tailored_strips_code_fence():
    def fake_complete(task, messages, *, max_tokens, cache=None, **kw):
        return "```latex\n\\documentclass{article}\nTAILORED\n```"
    out = build_tailored("BASE", "JD", complete=fake_complete)
    assert out == "\\documentclass{article}\nTAILORED"
    assert "```" not in out
```
Append to `tests/test_pass_patch.py` (imports `apply_critique`, `shrink_to_one_page` from
`jobmaxxing.tailoring.passes` — add whichever isn't already imported):
```python
def test_apply_critique_strips_code_fence():
    def fake_complete(task, messages, *, max_tokens, cache=None, **kw):
        return "```latex\n\\documentclass{article}\nPATCHED\n```"
    out = apply_critique("TEX", {"weaknesses": [], "missing_keywords": []}, "JD", complete=fake_complete)
    assert out == "\\documentclass{article}\nPATCHED" and "```" not in out

def test_shrink_to_one_page_strips_code_fence():
    def fake_complete(task, messages, *, max_tokens, cache=None, **kw):
        return "```\n\\documentclass{article}\nSHORTER\n```"
    out = shrink_to_one_page("TEX", 2, complete=fake_complete)
    assert out == "\\documentclass{article}\nSHORTER" and "```" not in out
```
Run: `uv run pytest tests/test_pass_build.py tests/test_pass_patch.py -q` → FAIL (fences not stripped yet).

- [ ] **Step 5: Apply the strip in the three `.tex` passes.** In `src/jobmaxxing/tailoring/passes.py`: add
`from ..llm.text import strip_code_fence` at the top. Wrap the three returns:
  - `build_tailored`: `return strip_code_fence(complete("tailor", messages, max_tokens=4000, cache=base_tex))`
  - `apply_critique`: `return strip_code_fence(complete("review", messages, max_tokens=4000))`
  - `shrink_to_one_page`: `return strip_code_fence(complete("tailor", messages, max_tokens=4000))`
Leave `critique_resume` untouched (it returns JSON, gated by `parse_critique`).
Run: `uv run pytest tests/test_pass_build.py tests/test_pass_patch.py tests/test_llm_text.py -q` → PASS.

- [ ] **Step 6: Commit** (verify worktree cwd first: `git rev-parse --show-toplevel`).
```bash
git add src/jobmaxxing/llm/text.py src/jobmaxxing/llm/providers.py src/jobmaxxing/tailoring/passes.py \
        tests/test_llm_text.py tests/test_pass_build.py tests/test_pass_patch.py
git commit -m "tailoring: strip markdown code fence from tailored .tex (shared llm/text.strip_code_fence)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `temperature` plumbing + `score` LLM tier

**Files:**
- Modify: `src/jobmaxxing/llm/client.py`, `src/jobmaxxing/llm/providers.py`, `config/llm.yaml`
- Test: `tests/test_llm_client.py`, `tests/test_llm_providers.py`, `tests/test_llm_config.py`

**Interfaces:**
- Produces: `complete(task, messages, *, max_tokens, response_format=None, cache=None, config=None, temperature=None)`
  forwards `temperature` to `call_provider` → adapters. API adapters include it in the SDK call when not
  `None`; `claude-cli` ignores it. Config gains a `score` task tier (anthropic API first).

- [ ] **Step 1: Write failing client + config tests.** Append to `tests/test_llm_client.py`:
```python
def test_forwards_temperature(monkeypatch):
    captured = {}
    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: captured.update(kw) or "ok")
    complete("route", MESSAGES, max_tokens=50, config=CONFIG, temperature=0)
    assert captured["temperature"] == 0

def test_temperature_defaults_to_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: captured.update(kw) or "ok")
    complete("route", MESSAGES, max_tokens=50, config=CONFIG)
    assert captured.get("temperature") is None
```
Append to `tests/test_llm_config.py` (uses `load_llm_config` + `candidates_for` from
`jobmaxxing.llm.config` — mirror the existing imports in that file):
```python
def test_score_tier_prefers_anthropic_api():
    from jobmaxxing.llm.config import candidates_for, load_llm_config
    cands = candidates_for("score", load_llm_config())
    assert cands[0]["provider"] == "anthropic"          # API first -> temperature honored
    assert any(c["provider"] == "claude-cli" for c in cands)  # subscription fallback present
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_llm_client.py tests/test_llm_config.py -q` → FAIL.

- [ ] **Step 2: Thread `temperature` through `client.py`.** In `src/jobmaxxing/llm/client.py`, change the
signature and the forwarded call:
```python
def complete(task, messages, *, max_tokens, response_format=None, cache=None, config=None, temperature=None) -> str:
```
and in the `try` body:
```python
            return call_provider(
                provider, model, messages,
                max_tokens=max_tokens, response_format=response_format, cache=cache, temperature=temperature,
            )
```

- [ ] **Step 3: Thread `temperature` through `providers.py`.** Update `call_provider` and the adapters:
```python
def call_provider(provider, model, messages, *, max_tokens, response_format=None, cache=None, temperature=None) -> str:
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(provider, model, messages, max_tokens, response_format, cache, temperature)
```
`_openai_compatible(provider, model, messages, max_tokens, response_format, cache=None, temperature=None)` —
after the `response_format` block, before the create call:
```python
    if temperature is not None:
        call["temperature"] = temperature
```
`_anthropic(provider, model, messages, max_tokens, response_format, cache=None, temperature=None)` — assemble
kwargs so temperature is only sent when set:
```python
    kwargs = {"model": model, "system": system, "messages": convo, "max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.messages.create(**kwargs)
```
`_claude_cli(provider, model, messages, max_tokens, response_format, cache=None, temperature=None)` — add the
`temperature=None` param and a one-line note that `claude -p` has no temperature knob so it is ignored.

- [ ] **Step 4: Write failing adapter temperature tests, then confirm.** Append to
`tests/test_llm_providers.py` (the file already defines `_FakeOpenAIClient` / `_FakeAnthropicClient`, each
capturing the SDK call into `.last_call`, installed via `monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)`
/ `monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)` — same as `test_openai_adapter`):
```python
def test_openai_includes_temperature_when_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    providers._openai_compatible("openai", "gpt-4o", [{"role": "user", "content": "hi"}], 50, None, None, 0)
    assert _FakeOpenAIClient.last_call["temperature"] == 0

def test_openai_omits_temperature_when_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    providers._openai_compatible("openai", "gpt-4o", [{"role": "user", "content": "hi"}], 50, None, None, None)
    assert "temperature" not in _FakeOpenAIClient.last_call

def test_anthropic_includes_temperature_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)
    providers._anthropic("anthropic", "claude", [{"role": "user", "content": "hi"}], 50, None, None, 0)
    assert _FakeAnthropicClient.last_call["temperature"] == 0
```

- [ ] **Step 5: Add the `score` tier to `config/llm.yaml`** under `tasks:`:
```yaml
  score:
    - {provider: anthropic, model: claude-sonnet-4-5-20250929}  # API first: honors temperature=0 (reproducible delta)
    - {provider: claude-cli, model: sonnet}                     # best-effort fallback (no temperature knob)
    - {provider: openai, model: gpt-4o}
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_llm_client.py tests/test_llm_providers.py tests/test_llm_config.py -q` → PASS.

- [ ] **Step 6: Commit** (verify worktree cwd first).
```bash
git add src/jobmaxxing/llm/client.py src/jobmaxxing/llm/providers.py config/llm.yaml \
        tests/test_llm_client.py tests/test_llm_providers.py tests/test_llm_config.py
git commit -m "llm: optional temperature param (default None) + score task tier (API-first for temp-0)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Per-type rubric weights + `load_rubric` default

**Files:**
- Modify: all 8 `rubrics/*.json`, `src/jobmaxxing/tailoring/rubric.py`
- Test: `tests/test_rubric_weights.py` (create)

**Interfaces:**
- Produces: every rubric carries a `weights` object with the five axis keys summing to 1.0; `load_rubric`
  defaults `weights` to `{0.2 × 5}` when a file omits it.

- [ ] **Step 1: Write failing tests.** Create `tests/test_rubric_weights.py`:
```python
import json
from pathlib import Path

import pytest

from jobmaxxing.config import REPO_ROOT
from jobmaxxing.tailoring.rubric import load_rubric

AXES = {"keyword_coverage", "technical_depth", "impact", "ats", "relevance_order"}
RUBRICS = sorted((REPO_ROOT / "rubrics").glob("*.json"))


@pytest.mark.parametrize("path", RUBRICS, ids=lambda p: p.stem)
def test_weights_present_and_sum_to_one(path):
    w = json.loads(path.read_text())["weights"]
    assert set(w) == AXES
    assert abs(sum(w.values()) - 1.0) < 1e-9

def test_load_rubric_defaults_weights(tmp_path):
    (tmp_path / "swe.json").write_text('{"keyword_dict": ["python"]}')
    w = load_rubric("swe", base_dir=tmp_path)["weights"]
    assert set(w) == AXES and abs(sum(w.values()) - 1.0) < 1e-9
```
Run: `uv run pytest tests/test_rubric_weights.py -q` → FAIL (no `weights` key).

- [ ] **Step 2: Add a `weights` key to each `rubrics/{type}.json`.** Insert this exact object (values from
the spec's §7.2 table; each sums to 1.0) alongside the existing `keyword_dict`/`aliases`:
  - `quant-trader.json`: `"weights": {"keyword_coverage": 0.3, "technical_depth": 0.2, "impact": 0.3, "ats": 0.1, "relevance_order": 0.1}`
  - `quant-dev.json`:    `"weights": {"keyword_coverage": 0.3, "technical_depth": 0.3, "impact": 0.2, "ats": 0.1, "relevance_order": 0.1}`
  - `mle.json`:          `"weights": {"keyword_coverage": 0.2, "technical_depth": 0.3, "impact": 0.3, "ats": 0.1, "relevance_order": 0.1}`
  - `swe.json`:          `"weights": {"keyword_coverage": 0.3, "technical_depth": 0.2, "impact": 0.1, "ats": 0.3, "relevance_order": 0.1}`
  - `fdse.json`:         `"weights": {"keyword_coverage": 0.2, "technical_depth": 0.1, "impact": 0.3, "ats": 0.1, "relevance_order": 0.3}`
  - `ai.json`:           `"weights": {"keyword_coverage": 0.2, "technical_depth": 0.3, "impact": 0.1, "ats": 0.1, "relevance_order": 0.3}`
  - `robotics.json`:     `"weights": {"keyword_coverage": 0.3, "technical_depth": 0.3, "impact": 0.2, "ats": 0.1, "relevance_order": 0.1}`
  - `av.json`:           `"weights": {"keyword_coverage": 0.3, "technical_depth": 0.3, "impact": 0.1, "ats": 0.2, "relevance_order": 0.1}`
Keep each file valid JSON (mind the comma between keys). Verify:
`uv run python -c "import json,glob; [print(f, abs(sum(json.load(open(f))['weights'].values())-1)<1e-9) for f in glob.glob('rubrics/*.json')]"` → all `True`.

- [ ] **Step 3: Default `weights` in `load_rubric`.** In `src/jobmaxxing/tailoring/rubric.py`, after the
existing `setdefault` lines add:
```python
    data.setdefault("weights", {"keyword_coverage": 0.2, "technical_depth": 0.2,
                                "impact": 0.2, "ats": 0.2, "relevance_order": 0.2})
```
and update the docstring return note to `-> {keyword_dict, aliases, weights}`.
Run: `uv run pytest tests/test_rubric_weights.py -q` → PASS (8 rubrics + default).

- [ ] **Step 4: Commit** (verify worktree cwd first).
```bash
git add rubrics/*.json src/jobmaxxing/tailoring/rubric.py tests/test_rubric_weights.py
git commit -m "rubrics: add per-type 5-axis weight vectors (sum 1.0) + load_rubric default

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `score_qualitative` + `parse_qualitative` (the four LLM axes)

**Files:**
- Modify: `src/jobmaxxing/tailoring/scorer.py`
- Test: `tests/test_scorer_qualitative.py` (create)

**Interfaces:**
- Consumes: an injected `complete` (task tier `score`, temperature-0).
- Produces: `parse_qualitative(text) -> {technical_depth, impact, ats, relevance_order}` (each 0–10 float;
  all-5.0 neutral on any failure); `score_qualitative(resume, jd, rubric, *, complete) -> same dict`.

- [ ] **Step 1: Write failing tests.** Create `tests/test_scorer_qualitative.py`:
```python
from jobmaxxing.tailoring.scorer import parse_qualitative, score_qualitative

AXES = {"technical_depth", "impact", "ats", "relevance_order"}


def test_parse_good_json():
    out = parse_qualitative('{"technical_depth": 7, "impact": 5, "ats": 9, "relevance_order": 6}')
    assert out == {"technical_depth": 7.0, "impact": 5.0, "ats": 9.0, "relevance_order": 6.0}

def test_parse_clamps_out_of_range():
    out = parse_qualitative('{"technical_depth": 15, "impact": -3, "ats": 9, "relevance_order": 6}')
    assert out["technical_depth"] == 10.0 and out["impact"] == 0.0

def test_parse_malformed_is_neutral():
    for bad in ["not json", '{"technical_depth": 7}', '{"technical_depth":"x","impact":1,"ats":1,"relevance_order":1}', "", None]:
        assert parse_qualitative(bad) == {a: 5.0 for a in AXES}

def test_score_qualitative_calls_score_tier_at_temp0():
    captured = {}
    def fake_complete(task, messages, *, max_tokens, temperature=None, **kw):
        captured["task"] = task
        captured["temperature"] = temperature
        return '{"technical_depth": 8, "impact": 8, "ats": 8, "relevance_order": 8}'
    out = score_qualitative("RESUME", "JD", {"keyword_dict": ["python", "kubernetes"]}, complete=fake_complete)
    assert out["ats"] == 8.0
    assert captured["task"] == "score" and captured["temperature"] == 0
```
Run: `uv run pytest tests/test_scorer_qualitative.py -q` → FAIL (functions missing).

- [ ] **Step 2: Implement in `src/jobmaxxing/tailoring/scorer.py`.** Add `import json` at the top (keep the
existing `import re`). Append:
```python
_QUAL_AXES = ("technical_depth", "impact", "ats", "relevance_order")

_SCORE_SYSTEM = (
    "You score a LaTeX résumé against a job description on four axes, each an integer 0-10 (10 = ideal):\n"
    "- technical_depth: does the résumé show the LEVEL the role wants, not just the noun?\n"
    "- impact: bullets carrying real numbers / measurable outcomes.\n"
    "- ats: clean structure, standard section headers, no parser-breaking formatting.\n"
    "- relevance_order: most JD-relevant experience surfaced first.\n"
    "Anchor to the role's key terms provided. Respond with STRICT JSON only: "
    '{"technical_depth": n, "impact": n, "ats": n, "relevance_order": n}.'
)


def parse_qualitative(text) -> dict:
    """Lenient parse of the four 0-10 axes. Clamps to [0, 10]; ANY structural failure (no JSON, bad or
    missing axis) -> all four = 5.0 (neutral), so a flaky scoring call never crashes tailoring."""
    neutral = {a: 5.0 for a in _QUAL_AXES}
    if not isinstance(text, str):
        return dict(neutral)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return dict(neutral)
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return dict(neutral)
    if not isinstance(data, dict):
        return dict(neutral)
    out = {}
    for axis in _QUAL_AXES:
        value = data.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return dict(neutral)  # all-or-nothing: a single bad/missing axis -> neutral
        out[axis] = float(min(10, max(0, value)))
    return out


def score_qualitative(resume_text: str, jd_text: str, rubric: dict, *, complete) -> dict:
    """Pass the résumé + JD + the rubric's key terms to the LLM for the four qualitative axes (temp-0)."""
    terms = ", ".join(rubric.get("keyword_dict", []))
    messages = [
        {"role": "system", "content": _SCORE_SYSTEM},
        {"role": "user", "content": f"Role key terms: {terms}\n\nJob description:\n{jd_text}\n\nRésumé (LaTeX):\n{resume_text}"},
    ]
    text = complete("score", messages, max_tokens=300, response_format={"type": "json_object"}, temperature=0)
    return parse_qualitative(text)
```
Run: `uv run pytest tests/test_scorer_qualitative.py -q` → PASS (4).

- [ ] **Step 3: Commit** (verify worktree cwd first).
```bash
git add src/jobmaxxing/tailoring/scorer.py tests/test_scorer_qualitative.py
git commit -m "scorer: score_qualitative + lenient parse_qualitative (4 LLM axes, temp-0, neutral fallback)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Compose the 5-axis `score`, rename the deterministic core, extend `delta`, wire `tailor.py`

**Files:**
- Modify: `src/jobmaxxing/tailoring/scorer.py`, `src/jobmaxxing/tailoring/tailor.py`
- Test: `tests/test_scorer.py`, `tests/test_scorer_composite.py` (create), `tests/test_tailor_job.py`

**Interfaces:**
- Consumes: `score_keywords` (renamed), `score_qualitative` (Task 4), rubric `weights` (Task 3).
- Produces: `score_keywords(resume, jd, rubric) -> {static, dynamic, matched, missing}` (the old `score`);
  `score(resume, jd, rubric, *, complete) -> {static, dynamic, matched, missing, axes:{5}, composite}`;
  `delta(before, after) -> {static, dynamic, composite, axes:{5}}`.

- [ ] **Step 1: Rename `score` -> `score_keywords` and update its tests.** In
`src/jobmaxxing/tailoring/scorer.py` rename `def score(` to `def score_keywords(` (body unchanged). In
`tests/test_scorer.py` replace every `score(` call with `score_keywords(` and the import
`from jobmaxxing.tailoring.scorer import score` → `... import score_keywords` (leave `delta` import).
Run: `uv run pytest tests/test_scorer.py -q` → PASS (renamed, still deterministic).

- [ ] **Step 2: Write failing composition + delta tests.** Create `tests/test_scorer_composite.py`:
```python
from jobmaxxing.tailoring.scorer import score, delta

RUBRIC = {
    "keyword_dict": ["python", "kubernetes"], "aliases": {},
    "weights": {"keyword_coverage": 0.2, "technical_depth": 0.2, "impact": 0.2, "ats": 0.2, "relevance_order": 0.2},
}


def _fake_complete(task, messages, *, max_tokens, temperature=None, **kw):
    return '{"technical_depth": 6, "impact": 4, "ats": 8, "relevance_order": 7}'


def test_score_is_additive_superset_with_axes_and_composite():
    out = score("python kubernetes", "python kubernetes role", RUBRIC, complete=_fake_complete)
    assert {"static", "dynamic", "matched", "missing"} <= set(out)     # backward-compatible keys kept
    assert out["axes"]["keyword_coverage"] == 10.0                     # 10 x dynamic (both terms in JD+resume)
    assert out["axes"] == {"keyword_coverage": 10.0, "technical_depth": 6.0, "impact": 4.0, "ats": 8.0, "relevance_order": 7.0}
    # equal 0.2 weights: 0.2*(10+6+4+8+7) = 7.0
    assert out["composite"] == 7.0

def test_delta_reports_composite_and_axes():
    before = score("python", "python kubernetes role", RUBRIC, complete=lambda *a, **k: '{"technical_depth":2,"impact":2,"ats":2,"relevance_order":2}')
    after = score("python kubernetes", "python kubernetes role", RUBRIC, complete=_fake_complete)
    d = delta(before, after)
    assert "composite" in d and "axes" in d and "static" in d and "dynamic" in d
    assert d["axes"]["ats"] == 6.0                                     # 8 - 2
```
Run: `uv run pytest tests/test_scorer_composite.py -q` → FAIL (`score` now takes no `complete`; composite missing).

- [ ] **Step 3: Add the composition `score` + extend `delta`.** Append to
`src/jobmaxxing/tailoring/scorer.py`:
```python
_AXES = ("keyword_coverage", "technical_depth", "impact", "ats", "relevance_order")


def score(resume_text: str, jd_text: str, rubric: dict, *, complete) -> dict:
    """Full five-axis score: deterministic keyword coverage + four LLM-graded axes + weighted composite.
    A superset of score_keywords() (static/dynamic/matched/missing preserved), plus `axes` and `composite`."""
    kw = score_keywords(resume_text, jd_text, rubric)
    qual = score_qualitative(resume_text, jd_text, rubric, complete=complete)
    axes = {"keyword_coverage": round(10 * kw["dynamic"], 4), **qual}
    weights = rubric.get("weights") or {a: 0.2 for a in _AXES}
    composite = round(sum(weights.get(a, 0.0) * axes[a] for a in _AXES), 2)
    return {**kw, "axes": axes, "composite": composite}
```
Replace `delta` with the extended version:
```python
def delta(before: dict, after: dict) -> dict:
    """after - before: keyword coverage (static/dynamic), the composite, and each 0-10 axis."""
    out = {
        "static": round(after["static"] - before["static"], 10),
        "dynamic": round(after["dynamic"] - before["dynamic"], 10),
        "composite": round(after.get("composite", 0.0) - before.get("composite", 0.0), 2),
    }
    ba, aa = before.get("axes", {}), after.get("axes", {})
    out["axes"] = {a: round(aa.get(a, 0.0) - ba.get(a, 0.0), 2) for a in _AXES}
    return out
```
Run: `uv run pytest tests/test_scorer_composite.py tests/test_scorer.py tests/test_scorer_qualitative.py -q` → PASS.

- [ ] **Step 4: Wire `tailor.py` to pass `complete` into scoring.** In `src/jobmaxxing/tailoring/tailor.py`
the import `from .scorer import delta, score` is unchanged (both still exist). Update the two scoring calls:
```python
    before = score(base_tex, jd or "", rubric, complete=complete)     # Pass 0
```
```python
    after = score(final_tex, jd or "", rubric, complete=complete)     # Pass 4
```
(The `review` dict and `delta(before, after)` line are unchanged — they now carry `axes`/`composite`
automatically.)

- [ ] **Step 5: Update the `tailor_job` test's fake `complete` to answer the `score` task.** `score()` is
now called for Pass 0 and Pass 4, so the fake must handle `task == "score"`. In `tests/test_tailor_job.py`,
replace `_fake_complete` with (add the `score` branch FIRST, since the score call also sets
`response_format`):
```python
def _fake_complete(task, messages, **kw):
    if task == "score":
        return '{"technical_depth": 6, "impact": 5, "ats": 8, "relevance_order": 7}'
    if task == "review" and kw.get("response_format"):
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["kafka"]}'
    return r"\documentclass{article}\begin{document} python kubernetes \end{document}"
```
Then, in `test_tailor_job_writes_artifacts_and_marks_tailored`, after the existing
`assert "static" in saved["score_after"] …` line, add the composite/axes assertions (the existing
`row[2]["static"] == 1.0` assertion still holds — the dict is a superset):
```python
    assert "composite" in saved["score_after"] and "keyword_coverage" in saved["score_after"]["axes"]
    assert "composite" in review["delta"]
```
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_tailor_job.py -q` → PASS.

- [ ] **Step 6: Full suite (no regression).**
Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → all PASS.

- [ ] **Step 7: Commit** (verify worktree cwd first).
```bash
git add src/jobmaxxing/tailoring/scorer.py src/jobmaxxing/tailoring/tailor.py \
        tests/test_scorer.py tests/test_scorer_composite.py tests/test_tailor_job.py
git commit -m "scorer: 5-axis weighted composite score() + extended delta; wire into tailor_job

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green.
2. **Real-compile e2e** (local, `pdflatex` on PATH via `/Library/TeX/texbin`): tailor one real approved job
   (or a throwaway synthetic job with a real JD, deleted after) against `RESUME_STORE_DIR=$(pwd)/resume_store`
   and the scaffold base résumés → `tailored.pdf` is a valid ≤1-page PDF (the fence fix), and `review.json`
   carries `score_before.composite`, `score_after.composite`, `delta.composite`, and all five `axes`.
   (The controller runs this after merge, mirroring the earlier tailoring validation.)

## Risks & notes
- **Provider variance** — temp-0 on the API path makes the delta reproducible; the `claude-cli` fallback
  can't set temperature, but the identical before/after prompt keeps it fair in expectation.
- **Neutral fallback masks a flaky call** — an all-5.0 qualitative result reads as "≈0 delta on those axes,"
  never a crash; acceptable and logged upstream by the LLM wrapper's warnings.
- **Weights are seeded, not tuned** — from PRD §7.2; the operator refines against real tailored jobs.
- **`score` now spends API budget** (2 calls/job, anthropic-first) instead of the subscription, by design,
  for determinism; negligible over the season.

## Execution
Isolated git worktree off the local `main` HEAD (which carries this plan + spec); subagent-driven TDD, one
task per subagent (implementer reads `scorer.py`, `passes.py`, `providers.py`, `client.py`, and the relevant
tests for context first); two-stage review (spec → quality) per task; full-suite green; Opus whole-branch
review; the real-compile e2e above; merge to `main`; push (gh `vaibhavw30`).
