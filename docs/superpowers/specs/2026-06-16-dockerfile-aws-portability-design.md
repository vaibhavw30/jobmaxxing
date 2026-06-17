# Spec — Dockerfile + AWS portability groundwork

**Type:** Infrastructure groundwork — container image for core pipeline
**Author:** Vaibhav
**Date:** 2026-06-16
**Status:** Implemented

---

## 1. Problem & rationale

jobmaxxing already runs an idempotent, env-configured pipeline (GitHub Actions + Supabase Postgres).
Every stage is a `python -m jobmaxxing.X` CLI call; config is 100% env-var; state lives only in
Postgres + S3; boto3 uses the default credential chain (IAM-role ready).

The one missing artifact for AWS portability is a **Dockerfile**.  With a working image, porting
to ECR/Fargate/Lambda in July is a single CI step.  This spec covers that groundwork — Docker runs
anywhere; no cloud commitment yet.

### In scope
Core pipeline: `run`, `enrich`, `route`, `migrate`, `recover_jd`, `verify_url`, `web` (`web` extra included).

### Out of scope
- `enrich_workday` (requires Playwright/Chromium — headless extra).
- `tailor` (requires pdflatex/TeX Live).
- Pushing to ECR or deploying to Fargate/Lambda (July work).

---

## 2. Key portability detail: REPO_ROOT resolution

All config/rubrics/migrations are loaded relative to `REPO_ROOT`:

```python
# src/jobmaxxing/config.py
REPO_ROOT = Path(__file__).resolve().parents[2]
```

For a file at `/app/src/jobmaxxing/config.py`, `parents[2]` = `/app`.
So `config/`, `rubrics/`, `migrations/` **must sit at `/app/`** in the container — which the
`COPY` instructions in the Dockerfile guarantee.

---

## 3. Dockerfile design (multi-stage)

### Stage 1 — builder (`python:3.12-slim`)

1. Install `uv` via pip.
2. Copy `pyproject.toml`, `uv.lock`, `src/` (needed by hatchling).
3. `uv sync --frozen --no-dev --extra web` → `.venv` with all core + web deps.

`--extra web` includes Flask for `jobmaxxing.web`.  `headless` (Playwright) is excluded.

### Stage 2 — runtime (`python:3.12-slim`)

1. Copy `.venv` from builder.
2. Copy `src/`, `config/`, `rubrics/`, `migrations/`, `pyproject.toml`, `uv.lock` to `/app/`.
3. `ENV PATH="/app/.venv/bin:$PATH"` — no uv at runtime.
4. Create `appuser` (non-root), `chown /app`, `USER appuser`.
5. `ENTRYPOINT ["python"]`; default `CMD` is a health-check one-liner.

No secrets baked in.  All configuration is injected at `docker run` time via `-e` or `--env-file`.

---

## 4. Running a stage

```bash
# Build
docker build -t jobmaxxing .

# Apply migrations (needs DB only)
docker run --rm \
  -e DATABASE_URL=postgres://user:pass@host:5432/db \
  jobmaxxing -m jobmaxxing.migrate

# Run pollers (needs DB only)
docker run --rm \
  -e DATABASE_URL=postgres://... \
  jobmaxxing -m jobmaxxing.run

# Route with LLM fallback
docker run --rm \
  -e DATABASE_URL=postgres://... \
  -e OPENAI_API_KEY=sk-... \
  -e ANTHROPIC_API_KEY=... \
  jobmaxxing -m jobmaxxing.route

# Web triage table (local; needs DB)
docker run --rm \
  -e DATABASE_URL=postgres://... \
  -p 8765:8765 \
  jobmaxxing -m jobmaxxing.web
```

---

## 5. .dockerignore

Excludes: `.git`, `.venv`, `.claude`, `.worktrees`, `tests/`, `docs/`, `scripts/`,
`__pycache__`, `*.pyc`, `.env*`, `resume_store/`, generated PDFs/tex.

Keeps: `src/`, `config/`, `rubrics/`, `migrations/`, `pyproject.toml`, `uv.lock`.

---

## 6. Verification

### In-container config check (no DB needed)

```bash
docker run --rm jobmaxxing -c "
from jobmaxxing.routing.config import load_routing_config; load_routing_config()
from jobmaxxing.llm.config import load_llm_config; load_llm_config()
from jobmaxxing.migrate import MIGRATIONS_DIR
n = len(list(MIGRATIONS_DIR.glob('*.sql')))
print('config+migrations OK, migrations=', n)
assert n >= 6
"
```

This proves `REPO_ROOT` resolves correctly and all static assets are present — without needing a DB.

### Repo-layout guard (pytest)

`tests/test_docker_layout.py` runs in the normal suite and asserts:
- `config/`, `rubrics/`, `migrations/` exist at repo root.
- `migrations/` has >= 6 `.sql` files.
- `REPO_ROOT` from `jobmaxxing.config` matches the test's computed root.

These are cheap (no DB/network) and catch layout regressions before the next docker build.

---

## 7. CI workflow

`.github/workflows/docker-build.yml` runs `docker build` + the config check on every push/PR.
Mirrors `ci.yml` style: `actions/checkout@v4`, no secrets needed for the build step.

---

## 8. AWS path (July)

With the image working:

1. Push to ECR: `aws ecr get-login-password | docker login …; docker push …`
2. Fargate task def: image = ECR URI, env from Secrets Manager / Parameter Store, IAM role for S3.
3. Scheduled Fargate tasks replace the GitHub Actions pollers — same `python -m jobmaxxing.X` commands.
4. Lambda (optional, for short stages like `route`): same image via Lambda container support.

No application code changes needed — the existing env-var config pattern and boto3 default-chain
already cover IAM-role credentials.

---

## 9. Deliverables

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage image for core pipeline |
| `.dockerignore` | Lean build context, no secrets |
| `tests/test_docker_layout.py` | Repo-layout guard (pytest, no DB) |
| `.github/workflows/docker-build.yml` | CI build + in-container config check |
| `README.md` (Docker section) | Operator instructions |

---

## 10. Open items & risks

- **6 migrations today**: the `>=6` floor in `test_docker_layout.py` must be bumped when new
  migrations are added.  The CI docker check also asserts `>= 6` — same update needed there.
- **headless/tailor stages**: remain local-only.  A second, larger image could include them in
  future if needed for a "fat" local container.
- **layer caching**: `pyproject.toml` + `uv.lock` are copied before `src/` in the builder so
  the `uv sync` layer is only re-run when dependencies change.
