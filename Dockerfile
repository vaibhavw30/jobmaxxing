# Multi-stage Dockerfile for jobmaxxing core pipeline.
# Stages: run, enrich, route, report, migrate, verify_url (+ web).
# Heavy/optional stages (enrich_workday=Playwright, tailor=pdflatex) are OUT OF SCOPE.
#
# Build:
#   docker build -t jobmaxxing .
#
# Run a stage (supply secrets at runtime — nothing is baked in):
#   docker run --rm \
#     -e DATABASE_URL=postgres://... \
#     jobmaxxing -m jobmaxxing.migrate
#
#   docker run --rm \
#     -e DATABASE_URL=postgres://... \
#     -e OPENAI_API_KEY=... \
#     jobmaxxing -m jobmaxxing.route
#
#   docker run --rm \
#     -e DATABASE_URL=postgres://... \
#     -p 8765:8765 \
#     jobmaxxing -m jobmaxxing.web

# ---------------------------------------------------------------------------
# Stage 1: builder — install deps into an isolated venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Build at /app (same path as the runtime stage) so the editable install's path
# record and the venv script shebangs resolve correctly after the venv is copied
# into the runtime image — a /build vs /app mismatch breaks `import jobmaxxing`.
WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy only the files needed for dependency resolution first (better layer caching)
COPY pyproject.toml uv.lock ./

# Copy src so hatchling can resolve the package (it reads src/jobmaxxing)
COPY src/ ./src/

# Sync into .venv (frozen, no dev deps, include the web extra for jobmaxxing.web)
RUN uv sync --frozen --no-dev --extra web

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image with venv + repo layout copied in
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# REPO_ROOT is resolved at runtime as Path(__file__).resolve().parents[2].
# For /app/src/jobmaxxing/*.py that is parents[2] = /app.
# So config/, rubrics/, migrations/ must live at /app/ — which the COPYs below guarantee.
WORKDIR /app

# Copy the populated venv from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Put the venv on PATH so `python` and all console scripts resolve from it
ENV PATH="/app/.venv/bin:$PATH"

# Copy the source tree and all runtime-required repo dirs
COPY src/ ./src/
COPY config/ ./config/
COPY rubrics/ ./rubrics/
COPY migrations/ ./migrations/
COPY pyproject.toml uv.lock ./

# Create a non-root user and hand over ownership
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app

USER appuser

# No secrets baked in — supply DATABASE_URL, LLM keys, S3_BUCKET etc. via -e / --env-file at run time.
# Default command shows the package is importable; override per stage:
#   docker run --rm -e DATABASE_URL=... jobmaxxing -m jobmaxxing.migrate
ENTRYPOINT ["python"]
# Default: print a one-liner confirming the package is importable.
# Override per stage at run time, e.g.:
#   docker run --rm -e DATABASE_URL=... jobmaxxing -m jobmaxxing.migrate
#   docker run --rm -e DATABASE_URL=... jobmaxxing -m jobmaxxing.run
#   docker run --rm -e DATABASE_URL=... -e OPENAI_API_KEY=... jobmaxxing -m jobmaxxing.route
#   docker run --rm -e DATABASE_URL=... -p 8765:8765 jobmaxxing -m jobmaxxing.web
CMD ["-c", "import jobmaxxing; print('jobmaxxing', jobmaxxing.__file__)"]
