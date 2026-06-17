"""
test_docker_layout.py — guards the repo-root invariant that the Dockerfile relies on.

The Dockerfile COPYs config/, rubrics/, migrations/, and src/ to /app/.
REPO_ROOT (= Path(__file__).resolve().parents[2] for any src/jobmaxxing/*.py) resolves
to /app in the container — which means those dirs must sit at /app/.

This test asserts that the *source* directories exist at repo root (the COPY source),
so if someone accidentally removes or renames them the docker build will fail loudly
rather than silently producing a broken image.

These checks are cheap (no DB, no network) and run in the normal `uv run pytest` suite.
"""

from pathlib import Path

# REPO_ROOT as Python code computes it: parents[2] of src/jobmaxxing/config.py
_REPO_ROOT = Path(__file__).resolve().parents[1]  # tests/ -> repo root


def test_config_dir_exists():
    """config/ must exist at repo root (COPY source for docker)."""
    assert (_REPO_ROOT / "config").is_dir(), (
        f"config/ not found at {_REPO_ROOT}; "
        "the Dockerfile COPY for config/ would fail without it"
    )


def test_rubrics_dir_exists():
    """rubrics/ must exist at repo root (COPY source for docker)."""
    assert (_REPO_ROOT / "rubrics").is_dir(), (
        f"rubrics/ not found at {_REPO_ROOT}; "
        "the Dockerfile COPY for rubrics/ would fail without it"
    )


def test_migrations_dir_exists():
    """migrations/ must exist at repo root (COPY source for docker)."""
    assert (_REPO_ROOT / "migrations").is_dir(), (
        f"migrations/ not found at {_REPO_ROOT}; "
        "the Dockerfile COPY for migrations/ would fail without it"
    )


def test_migrations_count():
    """migrations/ must contain at least 10 *.sql files (0001..0010 exist today).

    This is a cheap canary: if someone deletes migration files (or the COPY set
    in the Dockerfile drifts), the image's MIGRATIONS_DIR would be incomplete.
    Update this floor when new migrations are added.
    """
    sql_files = list((_REPO_ROOT / "migrations").glob("*.sql"))
    assert len(sql_files) >= 10, (
        f"Expected >=10 migration files, found {len(sql_files)}: {sorted(f.name for f in sql_files)}"
    )


def test_src_jobmaxxing_exists():
    """src/jobmaxxing/ must exist (the main package the image runs)."""
    assert (_REPO_ROOT / "src" / "jobmaxxing").is_dir(), (
        f"src/jobmaxxing/ not found at {_REPO_ROOT}"
    )


def test_repo_root_resolution():
    """REPO_ROOT derived from src/jobmaxxing/config.py must equal the actual repo root.

    This is the exact same computation the running container uses.  If the package
    layout changes (e.g. extra nesting), this will catch it before the next docker build.
    """
    from jobmaxxing.config import REPO_ROOT as module_repo_root

    assert module_repo_root == _REPO_ROOT, (
        f"REPO_ROOT mismatch: module says {module_repo_root}, test root is {_REPO_ROOT}. "
        "The Dockerfile path assumptions would be wrong."
    )
