# Tailoring (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an operator-approved job + its routed base résumé into a review-ready tailored one-page résumé with a deterministic before/after keyword-coverage score and LLM weakness/missing-keyword feedback, stored as artifacts in S3.

**Architecture:** A new `tailoring/` package whose deterministic units (keyword-coverage scorer, pdflatex compile + measured one-page guard, diff) bracket the LLM units (build pass, adversarial critique + patch). All external boundaries (S3, LLM `complete`, the compile function) are injected into the orchestrator `tailor_job`, so it is unit-testable without AWS, network, or pdflatex. Operator-gated and run locally via `python -m jobmaxxing.tailor`.

**Tech Stack:** Python 3.12, uv, psycopg3, the Phase-2 `llm/` wrapper (extended with prompt caching), `boto3` (S3), `pypdf` (page count), `pdflatex` (external, operator's machine), pytest + pytest-postgresql. Spec: `docs/superpowers/specs/2026-06-11-tailoring-design.md`.

**Conventions (match Phases 1–2):**
- Work in the isolated worktree off `main`; strict TDD per task; small commits.
- ENV for DB tests: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` before `uv run pytest`.
- Pure functions tested directly; LLM / S3 / compile boundaries tested via mocks/monkeypatch (Phase-1/2 pattern).
- `uv.lock` is committed; after a dependency change run `uv lock` and commit it.
- Documented CLI commands get a **top-level module** so `python -m jobmaxxing.tailor` resolves (Phase-2 entrypoint lesson).

---

### Task 1: Add deps (boto3, pypdf) + tailoring package skeleton

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/jobmaxxing/tailoring/__init__.py`, `tests/test_tailoring_skeleton.py`

- [ ] **Step 1: Write the failing test** — `tests/test_tailoring_skeleton.py`:
```python
import importlib


def test_tailoring_package_imports():
    assert importlib.import_module("jobmaxxing.tailoring")


def test_tailoring_deps_available():
    assert importlib.import_module("boto3")
    assert importlib.import_module("pypdf")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_tailoring_skeleton.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring'` (and boto3/pypdf not installed).

- [ ] **Step 3: Add deps and the package init**

In `pyproject.toml`, add to the `dependencies` list (keep all existing entries):
```toml
    "boto3>=1.34",
    "pypdf>=4.0",
```
Create `src/jobmaxxing/tailoring/__init__.py`:
```python
"""Tailoring: deterministic-anchored two-pass résumé tailoring."""
```

- [ ] **Step 4: Sync and run**

Run:
```bash
uv lock
uv sync
export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"
uv run pytest tests/test_tailoring_skeleton.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**
```bash
git add pyproject.toml uv.lock src/jobmaxxing/tailoring/__init__.py tests/test_tailoring_skeleton.py
git commit -m "chore: add boto3/pypdf deps and tailoring package skeleton"
```

---

### Task 2: Rubric loader + seed rubric files

**Files:**
- Create: `src/jobmaxxing/tailoring/rubric.py`, `rubrics/{quant-trader,quant-dev,mle,swe,fdse,ai,robotics,av}.json`, `tests/test_rubric.py`

- [ ] **Step 1: Write the failing test** — `tests/test_rubric.py`:
```python
import pytest

from jobmaxxing.routing.types import VALID_TYPES
from jobmaxxing.tailoring.rubric import RubricMissing, load_rubric


def test_load_rubric_returns_keyword_dict_and_aliases():
    r = load_rubric("swe")
    assert isinstance(r["keyword_dict"], list) and r["keyword_dict"]
    assert isinstance(r["aliases"], dict)


def test_every_type_has_a_rubric():
    for t in VALID_TYPES:
        r = load_rubric(t)
        assert r["keyword_dict"], f"{t} has empty keyword_dict"


def test_load_rubric_missing_type_raises(tmp_path):
    with pytest.raises(RubricMissing):
        load_rubric("nonexistent", base_dir=tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rubric.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.rubric'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/rubric.py`:
```python
import json
from pathlib import Path

from ..config import REPO_ROOT


class RubricMissing(RuntimeError):
    """Raised when no rubric file exists for a resume type."""


def load_rubric(resume_type: str, base_dir: Path | None = None) -> dict:
    """Load rubrics/{resume_type}.json -> {keyword_dict, aliases}."""
    base_dir = base_dir or REPO_ROOT / "rubrics"
    path = base_dir / f"{resume_type}.json"
    if not path.exists():
        raise RubricMissing(f"no rubric for resume_type {resume_type!r} at {path}")
    data = json.loads(path.read_text())
    data.setdefault("keyword_dict", [])
    data.setdefault("aliases", {})
    return data
```

Create the 8 rubric files (seed `keyword_dict` from tech-plan §7.2; aliases for punctuated/abbreviated terms). `rubrics/swe.json`:
```json
{
  "keyword_dict": ["python", "java", "distributed systems", "api", "rest", "microservices", "ci/cd", "scalability", "kubernetes", "sql"],
  "aliases": {"ci/cd": ["cicd", "continuous integration"], "api": ["apis"], "kubernetes": ["k8s"]}
}
```
`rubrics/quant-dev.json`:
```json
{
  "keyword_dict": ["low-latency", "c++", "market data", "backtesting", "time-series", "order book", "systems programming", "multithreading", "linux"],
  "aliases": {"c++": ["cpp", "c plus plus"], "low-latency": ["low latency"], "time-series": ["time series"]}
}
```
`rubrics/quant-trader.json`:
```json
{
  "keyword_dict": ["probability", "expected value", "market making", "pnl", "options", "game theory", "mental math", "statistics", "derivatives"],
  "aliases": {"expected value": ["ev"], "pnl": ["p&l", "p and l"]}
}
```
`rubrics/mle.json`:
```json
{
  "keyword_dict": ["machine learning", "training", "inference", "pytorch", "tensorflow", "xgboost", "feature engineering", "model evaluation", "data pipeline"],
  "aliases": {"machine learning": ["ml"], "feature engineering": ["features"]}
}
```
`rubrics/fdse.json`:
```json
{
  "keyword_dict": ["customer-facing", "data integration", "deployment", "ontology", "foundry", "java", "stakeholders", "solutions", "etl"],
  "aliases": {"customer-facing": ["customer facing"], "etl": ["extract transform load"]}
}
```
`rubrics/ai.json`:
```json
{
  "keyword_dict": ["large language model", "llm", "agentic", "rag", "fine-tuning", "prompt engineering", "retrieval augmented", "embeddings", "inference"],
  "aliases": {"large language model": ["llms"], "fine-tuning": ["fine tuning", "finetuning"], "retrieval augmented": ["retrieval-augmented"]}
}
```
`rubrics/robotics.json`:
```json
{
  "keyword_dict": ["ros", "control systems", "state estimation", "perception", "manipulation", "reinforcement learning", "simulation", "kinematics", "slam"],
  "aliases": {"reinforcement learning": ["rl"], "control systems": ["controls"]}
}
```
`rubrics/av.json`:
```json
{
  "keyword_dict": ["sensor fusion", "slam", "motion planning", "lidar", "perception", "localization", "safety-critical", "kalman filter", "point cloud"],
  "aliases": {"safety-critical": ["safety critical"], "lidar": ["lidars"]}
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rubric.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/rubric.py rubrics/ tests/test_rubric.py
git commit -m "feat: add rubric loader and seed keyword dictionaries for all 8 types"
```

---

### Task 3: Deterministic keyword-coverage scorer

**Files:**
- Create: `src/jobmaxxing/tailoring/scorer.py`, `tests/test_scorer.py`

**Context:** Alias- and boundary-aware matching (same boundary technique as Phase-2 routing: lowercase + whitespace-collapse, keep punctuation, `(?<![a-z0-9])term(?![a-z0-9])`). A dict term is covered if it OR any alias appears. Static = dict coverage by résumé; dynamic = of dict terms the JD mentions, fraction the résumé covers; missing = dict terms in JD but not résumé.

- [ ] **Step 1: Write the failing test** — `tests/test_scorer.py`:
```python
from jobmaxxing.tailoring.scorer import delta, score

RUBRIC = {
    "keyword_dict": ["c++", "low-latency", "backtesting", "kubernetes"],
    "aliases": {"c++": ["cpp"], "kubernetes": ["k8s"]},
}


def test_static_coverage_counts_dict_terms_in_resume():
    s = score(resume_text="I write cpp and do backtesting", jd_text="", rubric=RUBRIC)
    # cpp (alias of c++) + backtesting => 2 of 4
    assert s["static"] == 0.5
    assert set(s["matched"]) == {"c++", "backtesting"}


def test_dynamic_coverage_is_jd_conditioned():
    # JD asks for c++ and low-latency; resume has only c++
    s = score(resume_text="experienced in c++", jd_text="must know c++ and low-latency", rubric=RUBRIC)
    assert s["dynamic"] == 0.5                      # 1 of the 2 JD-mentioned terms covered
    assert s["missing"] == ["low-latency"]          # JD wants it, resume lacks it


def test_dynamic_is_one_when_jd_mentions_no_dict_terms():
    s = score(resume_text="c++", jd_text="we like teamwork and coffee", rubric=RUBRIC)
    assert s["dynamic"] == 1.0
    assert s["missing"] == []


def test_boundary_aware_alias_matching():
    # 'k8s' alias matches; bare 'cpp' must not match inside 'cppfoo'
    s = score(resume_text="deploy on k8s, cppfoo is irrelevant", jd_text="", rubric=RUBRIC)
    assert "kubernetes" in s["matched"]
    assert "c++" not in s["matched"]


def test_empty_dict_is_zero_static():
    s = score(resume_text="anything", jd_text="anything", rubric={"keyword_dict": [], "aliases": {}})
    assert s["static"] == 0.0 and s["dynamic"] == 1.0


def test_delta_subtracts_axes():
    before = {"static": 0.4, "dynamic": 0.5}
    after = {"static": 0.7, "dynamic": 0.9}
    assert delta(before, after) == {"static": 0.3, "dynamic": 0.4}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scorer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.scorer'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/scorer.py`:
```python
import re

_WS = re.compile(r"\s+")
_pattern_cache: dict[str, re.Pattern] = {}


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace; keep punctuation (tech terms like c++ need it)."""
    return _WS.sub(" ", (text or "").lower()).strip()


def _pattern(token: str) -> re.Pattern:
    pat = _pattern_cache.get(token)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])")
        _pattern_cache[token] = pat
    return pat


def _covered(text_norm: str, term: str, aliases: dict) -> bool:
    """True if the term or any of its aliases appears (boundary-aware)."""
    for token in [term, *aliases.get(term, [])]:
        if _pattern(token).search(text_norm):
            return True
    return False


def score(resume_text: str, jd_text: str, rubric: dict) -> dict:
    """Deterministic keyword coverage. Returns {static, dynamic, matched, missing}."""
    terms = rubric.get("keyword_dict", [])
    aliases = rubric.get("aliases", {})
    resume_norm = _norm(resume_text)
    jd_norm = _norm(jd_text)

    in_resume = [t for t in terms if _covered(resume_norm, t, aliases)]
    in_jd = [t for t in terms if _covered(jd_norm, t, aliases)]
    resume_set = set(in_resume)

    static = len(in_resume) / len(terms) if terms else 0.0
    jd_covered = [t for t in in_jd if t in resume_set]
    dynamic = len(jd_covered) / len(in_jd) if in_jd else 1.0
    missing = [t for t in in_jd if t not in resume_set]

    return {"static": static, "dynamic": dynamic, "matched": in_resume, "missing": missing}


def delta(before: dict, after: dict) -> dict:
    """after - before on the two coverage axes."""
    return {
        "static": after["static"] - before["static"],
        "dynamic": after["dynamic"] - before["dynamic"],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scorer.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/scorer.py tests/test_scorer.py
git commit -m "feat: add deterministic keyword-coverage scorer (static + dynamic)"
```

---

### Task 4: Unified diff utility

**Files:**
- Create: `src/jobmaxxing/tailoring/diffing.py`, `tests/test_diffing.py`

- [ ] **Step 1: Write the failing test** — `tests/test_diffing.py`:
```python
from jobmaxxing.tailoring.diffing import unified_diff


def test_unified_diff_shows_changed_lines():
    out = unified_diff("line one\nline two\n", "line one\nline TWO\n")
    assert "-line two" in out
    assert "+line TWO" in out
    assert "base.tex" in out and "tailored.tex" in out


def test_unified_diff_identical_is_empty():
    assert unified_diff("same\n", "same\n") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diffing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.diffing'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/diffing.py`:
```python
import difflib


def unified_diff(base: str, tailored: str, *, fromfile: str = "base.tex", tofile: str = "tailored.tex") -> str:
    """A unified diff base->tailored so the operator sees exactly what changed."""
    lines = difflib.unified_diff(
        base.splitlines(keepends=True),
        tailored.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    )
    return "".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diffing.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/diffing.py tests/test_diffing.py
git commit -m "feat: add base->tailored unified diff util"
```

---

### Task 5: LaTeX compile (`compile_pdf`)

**Files:**
- Create: `src/jobmaxxing/tailoring/latex.py`, `tests/test_latex_compile.py`

**Context:** Shell out to `pdflatex`; read the page count from the compiled PDF via `pypdf` (never the model's claim). The real compile test is skipped when `pdflatex` is not on PATH (same gating pattern as the Postgres binary).

- [ ] **Step 1: Write the failing test** — `tests/test_latex_compile.py`:
```python
import shutil

import pytest

from jobmaxxing.tailoring.latex import CompileResult, LatexError, compile_pdf

_HAS_PDFLATEX = shutil.which("pdflatex") is not None
_ONE_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Hello one page.
\end{document}
"""


@pytest.mark.skipif(not _HAS_PDFLATEX, reason="pdflatex not installed")
def test_compile_pdf_returns_one_page():
    result = compile_pdf(_ONE_PAGE_TEX)
    assert isinstance(result, CompileResult)
    assert result.page_count == 1
    assert result.pdf_bytes.startswith(b"%PDF")


@pytest.mark.skipif(not _HAS_PDFLATEX, reason="pdflatex not installed")
def test_compile_pdf_raises_on_invalid_tex():
    with pytest.raises(LatexError):
        compile_pdf(r"\documentclass{article}\begin{document}\undefinedcmd")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_latex_compile.py -v`
Expected: FAIL — `ModuleNotFoundError` (or, if pdflatex is absent, both tests are skipped — that still proves the import fails first; the module must exist). The import error is the failure to fix.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/latex.py`:
```python
import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


class LatexError(RuntimeError):
    """Raised when pdflatex fails to produce a PDF."""


@dataclass
class CompileResult:
    pdf_bytes: bytes
    page_count: int
    log: str


def compile_pdf(tex: str, *, runs: int = 2) -> CompileResult:
    """Compile LaTeX to PDF with pdflatex; measure page count from the PDF via pypdf.

    Runs pdflatex `runs` times (refs/labels settle on the 2nd pass). Raises LatexError
    with the log tail if no PDF is produced.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "resume.tex"
        tex_file.write_text(tex)
        log = ""
        for _ in range(runs):
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            log = proc.stdout + proc.stderr
        pdf_file = tmp_path / "resume.pdf"
        if not pdf_file.exists():
            raise LatexError(f"pdflatex produced no PDF. Log tail:\n{log[-2000:]}")
        pdf_bytes = pdf_file.read_bytes()
    page_count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    return CompileResult(pdf_bytes=pdf_bytes, page_count=page_count, log=log)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_latex_compile.py -v`
Expected: 2 passed (if `pdflatex` installed) or 2 skipped (if not) — either way, no import error and no failure.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/latex.py tests/test_latex_compile.py
git commit -m "feat: add pdflatex compile with pypdf page-count measurement"
```

---

### Task 6: One-page guard (`enforce_one_page`)

**Files:**
- Modify: `src/jobmaxxing/tailoring/latex.py`
- Create: `tests/test_one_page_guard.py`

**Context:** Pure control-flow over an injected `compile_fn` and `shrink_fn` — no real pdflatex. Compile; if >1 page, shrink and recompile; cap retries; never trust the model — the page count always comes from `compile_fn`.

- [ ] **Step 1: Write the failing test** — `tests/test_one_page_guard.py`:
```python
from jobmaxxing.tailoring.latex import CompileResult, OnePageResult, enforce_one_page


def _result(pages):
    return CompileResult(pdf_bytes=b"%PDF", page_count=pages, log="")


def test_already_one_page_no_shrink():
    calls = []

    def shrink(tex, pages):
        calls.append(1)
        return tex

    out = enforce_one_page("tex", compile_fn=lambda t: _result(1), shrink_fn=shrink)
    assert isinstance(out, OnePageResult)
    assert out.page_count == 1 and out.retries == 0 and out.fit is True
    assert calls == []                       # shrink never called


def test_shrinks_until_one_page():
    pages = iter([2, 1])                      # first compile 2 pages, after shrink 1 page

    def compile_fn(tex):
        return _result(next(pages))

    def shrink(tex, n):
        return tex + " % cut"

    out = enforce_one_page("tex", compile_fn=compile_fn, shrink_fn=shrink)
    assert out.fit is True and out.page_count == 1 and out.retries == 1


def test_gives_up_after_max_retries_and_flags_not_fit():
    out = enforce_one_page(
        "tex", compile_fn=lambda t: _result(2), shrink_fn=lambda t, n: t, max_retries=3
    )
    assert out.fit is False and out.page_count == 2 and out.retries == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_one_page_guard.py -v`
Expected: FAIL — `ImportError: cannot import name 'OnePageResult'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/tailoring/latex.py`:
```python
@dataclass
class OnePageResult:
    tex: str
    pdf_bytes: bytes
    page_count: int
    retries: int
    fit: bool


def enforce_one_page(tex: str, *, compile_fn, shrink_fn, max_retries: int = 3) -> OnePageResult:
    """Compile; if it overflows one page, ask shrink_fn to cut and recompile, up to
    max_retries. The page count is always measured by compile_fn, never self-reported.
    If it never fits, return the last attempt flagged fit=False."""
    result = compile_fn(tex)
    if result.page_count <= 1:
        return OnePageResult(tex, result.pdf_bytes, result.page_count, 0, True)
    for attempt in range(1, max_retries + 1):
        tex = shrink_fn(tex, result.page_count)
        result = compile_fn(tex)
        if result.page_count <= 1:
            return OnePageResult(tex, result.pdf_bytes, result.page_count, attempt, True)
    return OnePageResult(tex, result.pdf_bytes, result.page_count, max_retries, False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_one_page_guard.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/latex.py tests/test_one_page_guard.py
git commit -m "feat: add one-page guard loop over injected compile/shrink"
```

---

### Task 7: Artifact storage interface + in-memory fake

**Files:**
- Create: `src/jobmaxxing/tailoring/storage.py`, `tests/test_storage_memory.py`

- [ ] **Step 1: Write the failing test** — `tests/test_storage_memory.py`:
```python
import pytest

from jobmaxxing.tailoring.storage import BaseResumeMissing, InMemoryStore


def test_in_memory_get_base_resume():
    store = InMemoryStore(base_resumes={"swe": "BASE TEX"})
    assert store.get_base_resume("swe") == "BASE TEX"


def test_in_memory_missing_base_raises():
    store = InMemoryStore(base_resumes={})
    with pytest.raises(BaseResumeMissing):
        store.get_base_resume("swe")


def test_in_memory_put_artifact_and_prefix():
    store = InMemoryStore()
    store.put_artifact("job1", "tailored.tex", b"abc")
    assert store.artifacts[("job1", "tailored.tex")] == b"abc"
    assert store.artifact_prefix("job1") == "memory://tailored/job1/"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.storage'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/storage.py`:
```python
from typing import Protocol


class BaseResumeMissing(RuntimeError):
    """Raised when no base resume exists for a resume type."""


class ArtifactStore(Protocol):
    def get_base_resume(self, resume_type: str) -> str: ...
    def put_artifact(self, job_id, name: str, data: bytes) -> None: ...
    def artifact_prefix(self, job_id) -> str: ...


class InMemoryStore:
    """Dict-backed store for tests (no AWS)."""

    def __init__(self, base_resumes: dict | None = None):
        self._base = dict(base_resumes or {})
        self.artifacts: dict[tuple, bytes] = {}

    def get_base_resume(self, resume_type: str) -> str:
        if resume_type not in self._base:
            raise BaseResumeMissing(f"no base resume for {resume_type!r}")
        return self._base[resume_type]

    def put_artifact(self, job_id, name: str, data: bytes) -> None:
        self.artifacts[(str(job_id), name)] = data

    def artifact_prefix(self, job_id) -> str:
        return f"memory://tailored/{job_id}/"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_memory.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/storage.py tests/test_storage_memory.py
git commit -m "feat: add ArtifactStore interface and in-memory test store"
```

---

### Task 8: S3 store

**Files:**
- Modify: `src/jobmaxxing/tailoring/storage.py`
- Create: `tests/test_storage_s3.py`

**Context:** Tests inject a fake boto3-style client (records calls, no network).

- [ ] **Step 1: Write the failing test** — `tests/test_storage_s3.py`:
```python
import pytest

from jobmaxxing.tailoring.storage import BaseResumeMissing, S3Store


class _FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}        # key -> bytes
        self.puts = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self.puts[Key] = Body


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_get_base_resume_reads_key():
    client = _FakeS3(objects={"base/swe/main.tex": b"BASE TEX"})
    store = S3Store("mybucket", client=client)
    assert store.get_base_resume("swe") == "BASE TEX"


def test_get_base_resume_missing_raises():
    store = S3Store("mybucket", client=_FakeS3())
    with pytest.raises(BaseResumeMissing):
        store.get_base_resume("swe")


def test_put_artifact_writes_key_and_prefix():
    client = _FakeS3()
    store = S3Store("mybucket", client=client)
    store.put_artifact("job1", "tailored.tex", b"abc")
    assert client.puts["tailored/job1/tailored.tex"] == b"abc"
    assert store.artifact_prefix("job1") == "s3://mybucket/tailored/job1/"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_s3.py -v`
Expected: FAIL — `ImportError: cannot import name 'S3Store'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/tailoring/storage.py`:
```python
import boto3
from botocore.exceptions import ClientError


class S3Store:
    """S3-backed store. Base resumes at base/{type}/main.tex; artifacts at tailored/{job_id}/{name}."""

    def __init__(self, bucket: str, client=None):
        self.bucket = bucket
        self.client = client if client is not None else boto3.client("s3")

    def get_base_resume(self, resume_type: str) -> str:
        key = f"base/{resume_type}/main.tex"
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise BaseResumeMissing(f"no base resume at s3://{self.bucket}/{key}") from exc
        return resp["Body"].read().decode("utf-8")

    def put_artifact(self, job_id, name: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=f"tailored/{job_id}/{name}", Body=data)

    def artifact_prefix(self, job_id) -> str:
        return f"s3://{self.bucket}/tailored/{job_id}/"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_s3.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/storage.py tests/test_storage_s3.py
git commit -m "feat: add S3 artifact store"
```

---

### Task 9: LLM wrapper — prompt caching

**Files:**
- Modify: `src/jobmaxxing/llm/providers.py`, `src/jobmaxxing/llm/client.py`
- Create: `tests/test_llm_caching.py`

**Context:** Add a `cache` string arg threaded `complete -> call_provider -> adapter`. Anthropic sends it as a cached system block; OpenAI/xAI prepend it as a system message (their automatic caching handles reuse).

- [ ] **Step 1: Write the failing test** — `tests/test_llm_caching.py`:
```python
import anthropic
import openai

from jobmaxxing.llm import client, providers


class _FakeOpenAIClient:
    last_call = None

    def __init__(self, **kwargs):
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        _FakeOpenAIClient.last_call = kwargs

        class _R:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

        return _R()


class _FakeAnthropicClient:
    last_call = None

    def __init__(self, **kwargs):
        pass

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        _FakeAnthropicClient.last_call = kwargs

        class _R:
            content = [type("B", (), {"text": "ok"})()]

        return _R()


def test_openai_prepends_cache_as_system(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    providers.call_provider("openai", "gpt-4o", [{"role": "user", "content": "hi"}], max_tokens=50, cache="BASE")
    msgs = _FakeOpenAIClient.last_call["messages"]
    assert msgs[0] == {"role": "system", "content": "BASE"}
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_anthropic_caches_system_block(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-x")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)
    providers.call_provider(
        "anthropic", "claude-sonnet-4-latest",
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}],
        max_tokens=50, cache="BASE",
    )
    system = _FakeAnthropicClient.last_call["system"]
    assert isinstance(system, list)
    assert system[0]["text"] == "BASE"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert any(b["text"] == "rules" for b in system)


def test_complete_threads_cache(monkeypatch):
    captured = {}

    def fake_call(provider, model, messages, **kw):
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", fake_call)
    cfg = {"tasks": {"tailor": [{"provider": "anthropic", "model": "m"}]}}
    client.complete("tailor", [{"role": "user", "content": "x"}], max_tokens=10, cache="BASE", config=cfg)
    assert captured["cache"] == "BASE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_caching.py -v`
Expected: FAIL — `call_provider() got an unexpected keyword argument 'cache'`.

- [ ] **Step 3: Implement.**

In `src/jobmaxxing/llm/providers.py`, change the adapters and `call_provider` to accept `cache`:
```python
def _openai_compatible(provider, model, messages, max_tokens, response_format, cache=None):
    init: dict = {"api_key": os.environ[PROVIDER_KEYS[provider]]}
    base_url = PROVIDER_BASE_URLS.get(provider)
    if base_url:
        init["base_url"] = base_url
    client = openai.OpenAI(**init)
    if cache:
        messages = [{"role": "system", "content": cache}, *messages]
    call: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        call["response_format"] = response_format
    resp = client.chat.completions.create(**call)
    return resp.choices[0].message.content


def _anthropic(provider, model, messages, max_tokens, response_format, cache=None):
    client = anthropic.Anthropic(api_key=os.environ[PROVIDER_KEYS[provider]])
    system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] != "system"]
    if cache:
        system = [{"type": "text", "text": cache, "cache_control": {"type": "ephemeral"}}]
        if system_text:
            system.append({"type": "text", "text": system_text})
    else:
        system = system_text if system_text else anthropic.NOT_GIVEN
    resp = client.messages.create(model=model, system=system, messages=convo, max_tokens=max_tokens)
    return resp.content[0].text


_ADAPTERS = {"openai": _openai_compatible, "xai": _openai_compatible, "anthropic": _anthropic}


def call_provider(provider, model, messages, *, max_tokens, response_format=None, cache=None) -> str:
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(provider, model, messages, max_tokens, response_format, cache)
```

In `src/jobmaxxing/llm/client.py`, thread `cache` through `complete`:
```python
def complete(task, messages, *, max_tokens, response_format=None, cache=None, config=None) -> str:
    cfg = config if config is not None else load_llm_config()
    candidates = candidates_for(task, cfg)
    tried: list[str] = []
    last_error: Exception | None = None
    for cand in candidates:
        provider, model = cand["provider"], cand["model"]
        if not provider_available(provider):
            logger.debug("llm: skipping %s (no API key)", provider)
            continue
        tried.append(f"{provider}/{model}")
        try:
            return call_provider(
                provider, model, messages,
                max_tokens=max_tokens, response_format=response_format, cache=cache,
            )
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient provider fallback is the whole point
            last_error = exc
            logger.warning("llm: %s/%s failed: %s", provider, model, exc)
    raise LLMUnavailable(
        f"no llm candidate succeeded for task {task!r}; "
        f"tried={tried or 'none (all skipped/no key)'}: {last_error}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_caching.py -v && uv run pytest tests/test_llm_providers.py tests/test_llm_client.py -q`
Expected: 3 passed in the new file; the existing llm tests still pass (the `cache` param defaults to None, preserving old behavior).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/llm/providers.py src/jobmaxxing/llm/client.py tests/test_llm_caching.py
git commit -m "feat: add prompt-cache passthrough to the llm wrapper"
```

---

### Task 10: Add tailor/review tasks to llm.yaml

**Files:**
- Modify: `config/llm.yaml`
- Create: `tests/test_llm_tailor_tasks.py`

- [ ] **Step 1: Write the failing test** — `tests/test_llm_tailor_tasks.py`:
```python
from jobmaxxing.llm.config import candidates_for, load_llm_config


def test_tailor_and_review_tasks_configured():
    cfg = load_llm_config()
    assert candidates_for("tailor", cfg), "tailor task missing"
    assert candidates_for("review", cfg), "review task missing"
    assert candidates_for("route", cfg), "route task must remain"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_tailor_tasks.py -v`
Expected: FAIL — `assert [] ` (tailor task missing).

- [ ] **Step 3: Implement** — append the two tasks under `tasks:` in `config/llm.yaml` (keep the existing `route` task):
```yaml
  tailor:
    - {provider: anthropic, model: claude-sonnet-4-latest}
    - {provider: openai, model: gpt-4o}
  review:
    - {provider: anthropic, model: claude-sonnet-4-latest}
    - {provider: openai, model: gpt-4o}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_tailor_tasks.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**
```bash
git add config/llm.yaml tests/test_llm_tailor_tasks.py
git commit -m "feat: configure tailor and review llm tasks"
```

---

### Task 11: Build pass (`build_tailored`)

**Files:**
- Create: `src/jobmaxxing/tailoring/passes.py`, `tests/test_pass_build.py`

- [ ] **Step 1: Write the failing test** — `tests/test_pass_build.py`:
```python
from jobmaxxing.tailoring.passes import build_tailored


def test_build_tailored_passes_cache_and_jd():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, cache=None, **kw):
        captured["task"] = task
        captured["cache"] = cache
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass{article}...TAILORED"

    out = build_tailored("BASE TEX", "Senior SWE, needs Kubernetes.", complete=fake_complete)
    assert out == r"\documentclass{article}...TAILORED"
    assert captured["task"] == "tailor"
    assert captured["cache"] == "BASE TEX"             # base resume prompt-cached
    assert "Kubernetes" in captured["user"]            # JD passed in the user message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pass_build.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.passes'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/passes.py`:
```python
_TAILOR_SYSTEM = (
    "You tailor a LaTeX résumé to a specific job description.\n"
    "HARD CONSTRAINTS:\n"
    "- Surgical edits only: reorder, rephrase, and re-emphasize EXISTING facts. Do NOT fabricate.\n"
    "- Keep it to ONE page.\n"
    "- Preserve the template's structure, packages, and macros.\n"
    "Output ONLY the full LaTeX document, nothing else."
)


def build_tailored(base_tex: str, jd: str, *, complete) -> str:
    """Pass 1: produce the tailored .tex. The base résumé is prompt-cached."""
    messages = [
        {"role": "system", "content": _TAILOR_SYSTEM},
        {"role": "user", "content": f"Job description:\n{jd}\n\nProduce the full tailored LaTeX résumé."},
    ]
    return complete("tailor", messages, max_tokens=4000, cache=base_tex)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pass_build.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/passes.py tests/test_pass_build.py
git commit -m "feat: add tailoring build pass (cached base resume)"
```

---

### Task 12: Critique pass + strict parser

**Files:**
- Modify: `src/jobmaxxing/tailoring/passes.py`
- Create: `tests/test_pass_critique.py`

**Context:** Two-persona critique → strict JSON `{weaknesses, missing_keywords}`. Lenient on failure: any parse problem yields an empty critique (tailoring still completes), logged.

- [ ] **Step 1: Write the failing test** — `tests/test_pass_critique.py`:
```python
from jobmaxxing.tailoring.passes import critique_resume, parse_critique


def test_parse_valid_critique():
    out = parse_critique('{"weaknesses": ["a", "b", "c"], "missing_keywords": ["kafka"]}')
    assert out == {"weaknesses": ["a", "b", "c"], "missing_keywords": ["kafka"]}


def test_parse_caps_weaknesses_at_three_and_tolerates_prose():
    out = parse_critique('here:\n```json\n{"weaknesses":["a","b","c","d"],"missing_keywords":[]}\n```')
    assert out["weaknesses"] == ["a", "b", "c"]


def test_parse_garbage_yields_empty_critique():
    assert parse_critique("not json") == {"weaknesses": [], "missing_keywords": []}
    assert parse_critique('{"weaknesses": "nope"}') == {"weaknesses": [], "missing_keywords": []}


def test_critique_resume_calls_review_task():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, response_format=None, **kw):
        captured["task"] = task
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["rust"]}'

    out = critique_resume("TAILORED TEX", "JD text", complete=fake_complete)
    assert captured["task"] == "review"
    assert out["missing_keywords"] == ["rust"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pass_critique.py -v`
Expected: FAIL — `ImportError: cannot import name 'critique_resume'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/tailoring/passes.py`:
```python
import json
import re

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_EMPTY_CRITIQUE = {"weaknesses": [], "missing_keywords": []}

_REVIEW_SYSTEM = (
    "You are two reviewers of a LaTeX résumé against a job description.\n"
    "- Senior engineer: name the 3 biggest weaknesses of the résumé for THIS role.\n"
    "- Hiring manager / ATS: list the important keywords/phrases the résumé is missing that a "
    "parser screening for this role would flag.\n"
    'Respond with STRICT JSON only: {"weaknesses": [3 strings], "missing_keywords": [strings]}.'
)


def parse_critique(text) -> dict:
    """Strict parse with a lenient fallback: any problem -> empty critique."""
    if not isinstance(text, str):
        return dict(_EMPTY_CRITIQUE)
    match = _JSON_OBJ.search(text)
    if not match:
        return dict(_EMPTY_CRITIQUE)
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return dict(_EMPTY_CRITIQUE)
    if not isinstance(data, dict):
        return dict(_EMPTY_CRITIQUE)
    weaknesses = data.get("weaknesses")
    missing = data.get("missing_keywords")
    if not isinstance(weaknesses, list) or not all(isinstance(w, str) for w in weaknesses):
        return dict(_EMPTY_CRITIQUE)
    if not isinstance(missing, list) or not all(isinstance(m, str) for m in missing):
        return dict(_EMPTY_CRITIQUE)
    return {"weaknesses": weaknesses[:3], "missing_keywords": missing}


def critique_resume(tailored_tex: str, jd: str, *, complete) -> dict:
    """Pass 2a: two-persona adversarial critique -> {weaknesses, missing_keywords}."""
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": f"Job description:\n{jd}\n\nRésumé (LaTeX):\n{tailored_tex}"},
    ]
    text = complete("review", messages, max_tokens=1000, response_format={"type": "json_object"})
    return parse_critique(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pass_critique.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/passes.py tests/test_pass_critique.py
git commit -m "feat: add adversarial critique pass with strict-but-lenient parser"
```

---

### Task 13: Patch pass + one-page shrink prompt

**Files:**
- Modify: `src/jobmaxxing/tailoring/passes.py`
- Create: `tests/test_pass_patch.py`

- [ ] **Step 1: Write the failing test** — `tests/test_pass_patch.py`:
```python
from jobmaxxing.tailoring.passes import apply_critique, shrink_to_one_page


def test_apply_critique_returns_patched_tex():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, **kw):
        captured["task"] = task
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass...PATCHED"

    critique = {"weaknesses": ["thin on scale"], "missing_keywords": ["kafka"]}
    out = apply_critique("TAILORED", critique, "JD", complete=fake_complete)
    assert out == r"\documentclass...PATCHED"
    assert captured["task"] == "review"
    assert "kafka" in captured["user"]              # critique fed back into the patch prompt


def test_shrink_to_one_page_mentions_page_count():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, **kw):
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass...SHORTER"

    out = shrink_to_one_page("TOO LONG TEX", 2, complete=fake_complete)
    assert out == r"\documentclass...SHORTER"
    assert "2" in captured["user"]                  # tells the model how many pages it overflowed to
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pass_patch.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_critique'`.

- [ ] **Step 3: Implement** — append to `src/jobmaxxing/tailoring/passes.py`:
```python
_PATCH_SYSTEM = (
    "Revise the LaTeX résumé to address the reviewer feedback. Same hard constraints: "
    "surgical edits, NO fabrication, ONE page, preserve template. Output ONLY the full LaTeX document."
)
_SHRINK_SYSTEM = (
    "The compiled résumé overflowed one page. Cut it to EXACTLY one page with surgical removals "
    "(trim the least-relevant content), NO fabrication, preserve template. Output ONLY the full LaTeX document."
)


def apply_critique(tailored_tex: str, critique: dict, jd: str, *, complete) -> str:
    """Pass 2b: apply the critique's fixes -> patched .tex."""
    weaknesses = "\n".join(f"- {w}" for w in critique.get("weaknesses", []))
    missing = ", ".join(critique.get("missing_keywords", []))
    messages = [
        {"role": "system", "content": _PATCH_SYSTEM},
        {"role": "user", "content": (
            f"Job description:\n{jd}\n\nReviewer weaknesses:\n{weaknesses}\n\n"
            f"Missing keywords to incorporate where truthful: {missing}\n\n"
            f"Current résumé (LaTeX):\n{tailored_tex}"
        )},
    ]
    return complete("review", messages, max_tokens=4000)


def shrink_to_one_page(tex: str, page_count: int, *, complete) -> str:
    """The shrink_fn used by the one-page guard."""
    messages = [
        {"role": "system", "content": _SHRINK_SYSTEM},
        {"role": "user", "content": f"The résumé compiled to {page_count} pages. Cut it to one page.\n\n{tex}"},
    ]
    return complete("tailor", messages, max_tokens=4000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pass_patch.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/passes.py tests/test_pass_patch.py
git commit -m "feat: add patch pass and one-page shrink prompt"
```

---

### Task 14: Orchestration (`tailor_job`, `approve`)

**Files:**
- Create: `src/jobmaxxing/tailoring/tailor.py`, `tests/test_tailor_job.py`

**Context:** All boundaries injected. Requires `status == 'approved_for_tailoring'`. Runs Passes 0–4, writes the 4 artifacts, persists `score_before/after`, `artifact_prefix`, `status='tailored'`. DB test uses `pytest-postgresql` + `InMemoryStore` + mocked `complete` + mocked `compile_fn`.

- [ ] **Step 1: Write the failing test** — `tests/test_tailor_job.py`:
```python
import json

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.tailoring.latex import CompileResult
from jobmaxxing.tailoring.storage import InMemoryStore
from jobmaxxing.tailoring.tailor import approve, tailor_job


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


RUBRIC = {"keyword_dict": ["kubernetes", "python"], "aliases": {"kubernetes": ["k8s"]}}


def _insert(conn, *, status, resume_type="swe", description="needs kubernetes and python"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status) "
        "values ('a|swe', 'github:simplify', 'Acme', 'SWE Intern', 'https://x', %s, %s, %s)",
        (description, resume_type, status),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def _fake_complete(task, messages, **kw):
    # build/patch/shrink return tex; critique returns JSON
    if task == "review" and kw.get("response_format"):
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["kafka"]}'
    return r"\documentclass{article}\begin{document} python kubernetes \end{document}"


def _fake_compile(tex):
    return CompileResult(pdf_bytes=b"%PDF-1.5 fake", page_count=1, log="")


def test_tailor_job_writes_artifacts_and_marks_tailored(conn):
    job_id = _insert(conn, status="approved_for_tailoring")
    store = InMemoryStore(base_resumes={"swe": r"\documentclass{article} base"})

    review = tailor_job(
        conn, job_id, store=store, complete=_fake_complete,
        compile_fn=_fake_compile, rubric_loader=lambda t: RUBRIC,
    )

    # all four artifacts written
    names = {name for (jid, name) in store.artifacts}
    assert names == {"tailored.tex", "tailored.pdf", "review.json", "diff.txt"}
    # review.json round-trips with deterministic scores + critique
    saved = json.loads(store.artifacts[(str(job_id), "review.json")])
    assert "static" in saved["score_after"] and saved["weaknesses"] == ["w1", "w2", "w3"]
    assert review["missing_keywords"] == ["kafka"]
    # DB updated
    row = conn.execute("select status, artifact_prefix, score_after from jobs where id=%s", (job_id,)).fetchone()
    assert row[0] == "tailored"
    assert row[1] == store.artifact_prefix(job_id)
    assert row[2]["static"] == 1.0                  # tailored tex contains python + kubernetes


def test_tailor_job_refuses_unapproved(conn):
    job_id = _insert(conn, status="routed")
    store = InMemoryStore(base_resumes={"swe": "base"})
    with pytest.raises(ValueError):
        tailor_job(conn, job_id, store=store, complete=_fake_complete,
                   compile_fn=_fake_compile, rubric_loader=lambda t: RUBRIC)


def test_approve_sets_status(conn):
    job_id = _insert(conn, status="routed")
    approve(conn, job_id)
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "approved_for_tailoring"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_tailor_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailoring.tailor'`.

- [ ] **Step 3: Implement** — `src/jobmaxxing/tailoring/tailor.py`:
```python
import json
import logging

import psycopg
from psycopg.types.json import Json

from .diffing import unified_diff
from .latex import enforce_one_page
from .passes import apply_critique, build_tailored, critique_resume, shrink_to_one_page
from .rubric import load_rubric
from .scorer import delta, score

logger = logging.getLogger(__name__)


def tailor_job(conn, job_id, *, store, complete, compile_fn, rubric_loader=load_rubric) -> dict:
    """Run the two-pass tailoring loop for one approved job. All boundaries injected."""
    row = conn.execute(
        "select description, resume_type, status from jobs where id=%s", (job_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    jd, resume_type, status = row
    if status != "approved_for_tailoring":
        raise ValueError(f"job {job_id} is not approved_for_tailoring (status={status!r})")

    base_tex = store.get_base_resume(resume_type)
    rubric = rubric_loader(resume_type)

    before = score(base_tex, jd or "", rubric)                       # Pass 0
    tailored = build_tailored(base_tex, jd or "", complete=complete)  # Pass 1
    critique = critique_resume(tailored, jd or "", complete=complete)  # Pass 2a
    patched = apply_critique(tailored, critique, jd or "", complete=complete)  # Pass 2b

    one_page = enforce_one_page(                                      # Pass 3
        patched,
        compile_fn=compile_fn,
        shrink_fn=lambda tex, pages: shrink_to_one_page(tex, pages, complete=complete),
    )
    final_tex = one_page.tex
    after = score(final_tex, jd or "", rubric)                       # Pass 4

    review = {
        "score_before": before,
        "score_after": after,
        "delta": delta(before, after),
        "weaknesses": critique["weaknesses"],
        "missing_keywords": critique["missing_keywords"],
        "page_count": one_page.page_count,
        "retries": one_page.retries,
        "fit": one_page.fit,
    }
    diff = unified_diff(base_tex, final_tex)

    store.put_artifact(job_id, "tailored.tex", final_tex.encode("utf-8"))
    store.put_artifact(job_id, "tailored.pdf", one_page.pdf_bytes)
    store.put_artifact(job_id, "review.json", json.dumps(review, indent=2).encode("utf-8"))
    store.put_artifact(job_id, "diff.txt", diff.encode("utf-8"))

    with conn.transaction():
        conn.execute(
            "update jobs set score_before=%s, score_after=%s, artifact_prefix=%s, status='tailored' where id=%s",
            (Json(before), Json(after), store.artifact_prefix(job_id), job_id),
        )
    logger.info("tailored job %s: delta=%s fit=%s", job_id, review["delta"], one_page.fit)
    return review


def approve(conn, job_id) -> None:
    """Operator gate: mark a job approved_for_tailoring."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set status='approved_for_tailoring' where id=%s", (job_id,)
        )
    if cur.rowcount == 0:
        raise ValueError(f"no job with id {job_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_tailor_job.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**
```bash
git add src/jobmaxxing/tailoring/tailor.py tests/test_tailor_job.py
git commit -m "feat: add tailor_job orchestration and approve gate"
```

---

### Task 15: CLI + top-level entrypoint shim

**Files:**
- Modify: `src/jobmaxxing/tailoring/tailor.py`
- Create: `src/jobmaxxing/tailor.py`, `tests/test_tailor_cli.py`

**Context:** `python -m jobmaxxing.tailor` must resolve (Phase-2 lesson). The shim re-exports `main`; `main` parses `approve <id>` / `review <id>` / `<id>` and wires the real S3Store / llm.complete / compile_pdf.

- [ ] **Step 1: Write the failing test** — `tests/test_tailor_cli.py`:
```python
def test_tailor_entrypoint_module_resolves():
    import jobmaxxing.tailor as entry
    from jobmaxxing.tailoring.tailor import main as impl_main
    assert entry.main is impl_main


def test_main_is_callable():
    from jobmaxxing.tailoring.tailor import main
    assert callable(main)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tailor_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobmaxxing.tailor'` (the shim) / `cannot import name 'main'`.

- [ ] **Step 3: Implement.**

Append `main` to `src/jobmaxxing/tailoring/tailor.py` (add the imports `import os`, `import sys`, `from datetime import datetime, timezone` at the top with the others, plus `from ..config import load_settings`, `from ..llm.client import complete as llm_complete`, `from .latex import compile_pdf`, `from .storage import S3Store`):
```python
def _print_review(store, job_id) -> None:
    # review is stored as an artifact; re-fetch is store-specific, so just point the operator at it.
    print(f"review at: {store.artifact_prefix(job_id)}review.json")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        sys.exit("S3_BUCKET is not set (see README / .env.example)")
    store = S3Store(bucket)
    with psycopg.connect(settings.database_url) as conn:
        if len(sys.argv) >= 2 and sys.argv[1] == "approve":
            if len(sys.argv) != 3:
                sys.exit("usage: python -m jobmaxxing.tailor approve <job_id>")
            approve(conn, sys.argv[2])
            print(f"approved {sys.argv[2]} for tailoring")
        elif len(sys.argv) >= 2 and sys.argv[1] == "review":
            if len(sys.argv) != 3:
                sys.exit("usage: python -m jobmaxxing.tailor review <job_id>")
            _print_review(store, sys.argv[2])
        elif len(sys.argv) == 2:
            review = tailor_job(conn, sys.argv[1], store=store, complete=llm_complete, compile_fn=compile_pdf)
            print(f"tailored {sys.argv[1]}: {review['delta']}")
        else:
            sys.exit("usage: python -m jobmaxxing.tailor [approve|review] <job_id>")


if __name__ == "__main__":
    main()
```

Create the shim `src/jobmaxxing/tailor.py`:
```python
"""CLI entrypoint shim so `python -m jobmaxxing.tailor` works (parallel to jobmaxxing.run / .route).

The implementation lives in the tailoring package; this exposes its `main` at the top-level
module path the README and operator commands invoke.
"""

from .tailoring.tailor import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_tailor_cli.py -v
S3_BUCKET= DATABASE_URL= uv run python -m jobmaxxing.tailor 2>&1 | tail -1   # must NOT be ModuleNotFound
```
Expected: 2 passed; the `-m` run exits with the S3_BUCKET/usage message (proving the module resolves), not `No module named jobmaxxing.tailor`.

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/tailoring/tailor.py src/jobmaxxing/tailor.py tests/test_tailor_cli.py
git commit -m "feat: add tailor CLI and top-level entrypoint shim"
```

---

### Task 16: README + .env.example + setup docs

**Files:**
- Modify: `README.md`, `.env.example`

- [ ] **Step 1: Update `.env.example`** — append (keep existing lines):
```
# Tailoring (Phase 3): S3 bucket for base resumes + artifacts.
S3_BUCKET=
# AWS credentials are read from the standard AWS_* env vars / profile.
```

- [ ] **Step 2: Add a Tailoring section to `README.md`** — insert before "## Status & open items":
```markdown
## Tailoring

For a job the operator has approved, produce a tailored one-page résumé with a
deterministic before/after keyword-coverage score and LLM weakness/missing-keyword
feedback. **Operator-gated and run locally** — never automatic (cost control).

Setup:
- Install a LaTeX distribution providing `pdflatex` (e.g. MacTeX/TeX Live).
- Create an S3 bucket; set `S3_BUCKET` and the standard `AWS_*` credentials.
- Upload one base résumé per resume type to `s3://<bucket>/base/{type}/main.tex`
  (types: `quant-trader, quant-dev, mle, swe, fdse, ai, robotics, av`). The tailoring
  engine ships; the base résumé content is yours.
- Tune `rubrics/{type}.json` (the deterministic keyword dictionaries) over time.

Use:
- Approve: `uv run python -m jobmaxxing.tailor approve <job_id>` (sets `approved_for_tailoring`).
- Tailor: `uv run python -m jobmaxxing.tailor <job_id>` — runs the two-pass loop and writes
  `tailored.tex`, `tailored.pdf`, `review.json`, `diff.txt` to `s3://<bucket>/tailored/{job_id}/`,
  sets `score_before`/`score_after` and `status=tailored`.
- Review: `uv run python -m jobmaxxing.tailor review <job_id>` prints the artifact location.

The improvement score (keyword coverage) and the one-page check are computed in code, never
self-reported by the model. The human reviews the diff and moves the job to `applied`.
```

- [ ] **Step 3: Cross-check references** against the code before committing: the 8 type names match `VALID_TYPES`; the CLI subcommands (`approve`/`review`/`<id>`) match `main()`; `S3_BUCKET` is what `main()` reads; artifact names match `tailor_job`. Fix the README if any reference is wrong.

- [ ] **Step 4: Commit**
```bash
git add README.md .env.example
git commit -m "docs: document tailoring setup, S3 layout, and CLI"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- §2 architecture/flow → Tasks 14–15 (orchestration + CLI). §3 deterministic scorer → Task 3 (+ rubric Task 2). §4 LaTeX compile + one-page guard → Tasks 5–6. §5 LLM passes + caching → Tasks 9 (caching), 10 (tasks), 11 (build), 12 (critique), 13 (patch/shrink). §6 storage (S3 + in-memory; rubrics in-repo) → Tasks 2, 7, 8. §7 orchestration/CLI/gate → Tasks 14–15. §8 data model (no migration; existing columns) → Task 14 writes score_before/after/artifact_prefix/status. §9 testing → every task is TDD with mocked boundaries; orchestration integration test in Task 14. §10 deliverables → all tasks + README Task 16.
- No migration task — correct (Phase-1 created the columns).

**Type/signature consistency:** `load_rubric(resume_type, base_dir=None)`, `RubricMissing`; `score(resume_text, jd_text, rubric)->{static,dynamic,matched,missing}`, `delta(before,after)`; `unified_diff(base,tailored)`; `compile_pdf(tex,*,runs=2)->CompileResult(pdf_bytes,page_count,log)`, `LatexError`, `enforce_one_page(tex,*,compile_fn,shrink_fn,max_retries=3)->OnePageResult(tex,pdf_bytes,page_count,retries,fit)`; `ArtifactStore`/`InMemoryStore(base_resumes=None)`/`S3Store(bucket,client=None)`/`BaseResumeMissing`; `complete(...,cache=None)`/`call_provider(...,cache=None)`; `build_tailored(base_tex,jd,*,complete)`, `critique_resume(tailored_tex,jd,*,complete)`, `parse_critique(text)`, `apply_critique(tailored_tex,critique,jd,*,complete)`, `shrink_to_one_page(tex,page_count,*,complete)`; `tailor_job(conn,job_id,*,store,complete,compile_fn,rubric_loader=load_rubric)`, `approve(conn,job_id)`, `main()`. Used consistently across tasks.

**No placeholders:** every code/test step contains real content; rubric JSONs are seeded (tuning is a labeled open item, not a placeholder). The `tailor`/`review` model IDs are config (open item).
