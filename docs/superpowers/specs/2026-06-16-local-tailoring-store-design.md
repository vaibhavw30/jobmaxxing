# Spec — Local-filesystem artifact store for tailoring

**Type:** New feature — local testability for the tailoring pipeline
**Author:** Vaibhav
**Date:** 2026-06-16
**Status:** Approved for planning
**Builds on:** Phase 1 (core feed), Phase 2 (routing), Phase 3 tailoring pipeline (`tailoring/storage.py`, `tailoring/tailor.py`, `mcp/server.py`).

---

## 1. Problem & rationale

The tailoring pipeline (`python -m jobmaxxing.tailor <job_id>`) reads a base résumé from
an artifact store and writes four output artifacts (tailored `.tex`, compiled `.pdf`,
`review.json`, `diff.txt`). The only implemented store is `S3Store`, which requires
`S3_BUCKET` and valid AWS credentials. There are no base `.tex` files in the repo.

Consequence: an operator (or a new contributor) cannot run the tailoring pipeline locally
without pre-configuring S3. That blocks end-to-end local testing of everything from
`build_tailored` through `enforce_one_page`.

The fix is a `LocalFileStore` backed by a directory on disk, a `make_store()` factory
that selects the right store from env vars, and eight scaffolded base résumé templates
(one per `VALID_TYPES` entry) checked into `resume_store/` so the pipeline has
something to pull on first run.

### Scope
**In:** `LocalFileStore` class in `tailoring/storage.py`; `make_store()` factory in the
same module; `tailoring/tailor.py` and `mcp/server.py` updated to use `make_store()`;
8 template `.tex` files under `resume_store/base/{type}/main.tex`; `resume_store/README.md`;
TDD tests in `tests/test_storage_local.py`.

**Out:** operator's real résumé content (templates are clearly marked placeholders);
`pdflatex` installation (system binary; noted in README); any change to S3Store behaviour;
any change to InMemoryStore.

---

## 2. `LocalFileStore` (in `tailoring/storage.py`)

Implements the `ArtifactStore` Protocol. Constructor takes a `root: str` (the
`RESUME_STORE_DIR` value). All paths are derived under that root to match the S3 key
layout exactly:

| Operation | S3Store key | LocalFileStore path |
|---|---|---|
| `get_base_resume(type)` | `base/{type}/main.tex` | `{root}/base/{type}/main.tex` |
| `put_artifact(job_id, name, data)` | `tailored/{job_id}/{name}` | `{root}/tailored/{job_id}/{name}` |
| `artifact_prefix(job_id)` | `s3://{bucket}/tailored/{job_id}/` | `{root}/tailored/{job_id}/` |
| `get_artifact(job_id, name)` | `tailored/{job_id}/{name}` | `{root}/tailored/{job_id}/{name}` |

### Error semantics (mirroring S3Store)

- `get_base_resume` missing → `BaseResumeMissing` with the missing path + a hint.
- `get_artifact` missing → `ArtifactMissing` with the missing path.
- `put_artifact` creates intermediate directories automatically (`mkdir -p`).

```python
class LocalFileStore:
    def __init__(self, root: str):
        self._root = Path(root)

    def get_base_resume(self, resume_type: str) -> str:
        path = self._root / "base" / resume_type / "main.tex"
        if not path.is_file():
            raise BaseResumeMissing(
                f"no base resume for {resume_type!r} at {path} "
                f"— create the file or copy your real résumé there"
            )
        return path.read_text(encoding="utf-8")

    def put_artifact(self, job_id, name: str, data: bytes) -> None:
        dest = self._root / "tailored" / str(job_id) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def artifact_prefix(self, job_id) -> str:
        return str(self._root / "tailored" / str(job_id)) + "/"

    def get_artifact(self, job_id, name: str) -> bytes:
        path = self._root / "tailored" / str(job_id) / name
        if not path.is_file():
            raise ArtifactMissing(f"no artifact {name!r} for job {job_id} at {path}")
        return path.read_bytes()
```

---

## 3. `make_store()` factory (in `tailoring/storage.py`)

Single construction point for all callers. Priority:

1. `RESUME_STORE_DIR` set → `LocalFileStore(RESUME_STORE_DIR)`
2. `S3_BUCKET` set → `S3Store(S3_BUCKET)` (unchanged production path)
3. Neither → `RuntimeError` naming both env vars

```python
def make_store() -> LocalFileStore | S3Store:
    resume_store_dir = os.environ.get("RESUME_STORE_DIR")
    if resume_store_dir:
        return LocalFileStore(resume_store_dir)
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        return S3Store(bucket)
    raise RuntimeError(
        "No artifact store configured. "
        "Set RESUME_STORE_DIR (local filesystem, for testing) "
        "or S3_BUCKET (production S3)."
    )
```

**Callers updated:**

- `tailoring/tailor.py` `main()`: replace the `S3_BUCKET` check + `S3Store(bucket)` with `make_store()`.
- `mcp/server.py` `_store()`: replace the `S3_BUCKET` check + `S3Store(bucket)` with `make_store()`.

No other callers existed. `InMemoryStore` construction in tests is unchanged (tests build it directly, bypassing the environment).

---

## 4. Scaffolded base templates (`resume_store/base/`)

Eight minimal but valid LaTeX résumés, one per `VALID_TYPES` entry:

| Type | Keywords drawn from rubric |
|---|---|
| `swe` | python, java, distributed systems, api, rest, microservices, ci/cd, scalability, kubernetes, sql |
| `mle` | machine learning, training, inference, pytorch, tensorflow, xgboost, feature engineering, model evaluation, data pipeline |
| `quant-trader` | probability, expected value, market making, pnl, options, game theory, mental math, statistics, derivatives |
| `quant-dev` | low-latency, c++, market data, backtesting, time-series, order book, systems programming, multithreading, linux |
| `fdse` | customer-facing, data integration, deployment, ontology, foundry, java, stakeholders, solutions, etl |
| `ai` | large language model, llm, agentic, rag, fine-tuning, prompt engineering, retrieval augmented, embeddings, inference |
| `robotics` | ros, control systems, state estimation, perception, manipulation, reinforcement learning, simulation, kinematics, slam |
| `av` | sensor fusion, slam, motion planning, lidar, perception, localization, safety-critical, kalman filter, point cloud |

Each file:
- Opens with `% TEMPLATE — replace with your real résumé. Tailoring reads base/{type}/main.tex.`
- Uses `\documentclass{article}` so it compiles with bare TeX Live.
- Includes type-appropriate section headings and inline keyword mentions so the deterministic scorer (`scorer.py`) has signal even against the placeholder content.

The operator replaces these with their real résumés — see `resume_store/README.md`.

---

## 5. Operator workflow

```bash
# One-time setup
export RESUME_STORE_DIR=$(pwd)/resume_store
# Replace placeholders with real résumés (brew install basictex for pdflatex)

# Per job
python -m jobmaxxing.tailor approve <job_id>
python -m jobmaxxing.tailor <job_id>
# Artifacts land under resume_store/tailored/<job_id>/
```

`RESUME_STORE_DIR` can also be set in `.env` (loaded by `load_settings()`).

---

## 6. Invariants & error handling

| Invariant | How |
|---|---|
| S3 behaviour unchanged | `make_store()` falls through to `S3Store` when `RESUME_STORE_DIR` is unset; no S3Store code changed. |
| Priority is explicit | `RESUME_STORE_DIR` wins over `S3_BUCKET`; documented in factory docstring + README. |
| Missing base is clear | `BaseResumeMissing` includes file path + actionable hint. |
| Intermediate dirs auto-created | `put_artifact` uses `mkdir(parents=True, exist_ok=True)`. |
| Templates are compilable | Each `.tex` uses `\documentclass{article}` — compilable with `basictex`. |
| Templates have scorer signal | Keywords from each type's rubric appear inline so `score()` returns non-zero before any tailoring. |

---

## 7. Testing (`tests/test_storage_local.py`)

All tests are self-contained (no DB, no AWS, no network):

| Test | Asserts |
|---|---|
| `test_local_get_base_resume_reads_tex` | reads content from `{tmp}/base/swe/main.tex` |
| `test_local_get_base_resume_missing_raises` | `BaseResumeMissing` when file absent |
| `test_local_put_then_get_artifact_round_trips` | identical bytes returned |
| `test_local_put_artifact_creates_dirs` | directory created automatically |
| `test_local_artifact_prefix_returns_path` | prefix contains "tailored" and job_id |
| `test_local_get_artifact_missing_raises` | `ArtifactMissing` when file absent |
| `test_make_store_returns_local_when_resume_store_dir_set` | `isinstance(store, LocalFileStore)` |
| `test_make_store_local_takes_priority_over_s3` | `LocalFileStore` wins when both vars set |
| `test_make_store_returns_s3_when_only_s3_bucket_set` | `isinstance(store, S3Store)` + correct bucket |
| `test_make_store_raises_when_neither_set` | `RuntimeError` naming both env vars |
| `test_shipped_swe_template_exists_and_has_documentclass` | shipped `resume_store/base/swe/main.tex` is present and valid |

---

## 8. Deliverables

- `src/jobmaxxing/tailoring/storage.py`: `LocalFileStore` class + `make_store()` factory + `import os, pathlib.Path`.
- `src/jobmaxxing/tailoring/tailor.py`: `main()` uses `make_store()`; `import os` removed.
- `src/jobmaxxing/mcp/server.py`: `_store()` uses `make_store()`; `import os` removed.
- `resume_store/base/{swe,mle,quant-trader,quant-dev,fdse,ai,robotics,av}/main.tex` (8 files).
- `resume_store/README.md`.
- `tests/test_storage_local.py` (11 tests).

---

## 9. Open items & risks

- `pdflatex` is a system dependency (`brew install basictex`). The pipeline will fail at compile-to-PDF if it is not installed. The `.tex` artifact is still written. Documented in `resume_store/README.md`.
- Template keyword coverage is shallow (rubric keywords appear once in prose). For the scorer to return meaningful deltas, the operator's real résumé should be denser. The templates are scaffolds, not signal-rich résumés.
- `RESUME_STORE_DIR` and `S3_BUCKET` are read at call time by `make_store()`; the MCP server is long-lived, so changing the env var without restarting the server has no effect in that process. Acceptable — operator-controlled var, same caveat as `S3_BUCKET` today.
