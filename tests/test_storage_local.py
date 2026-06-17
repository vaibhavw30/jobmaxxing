"""Tests for LocalFileStore and make_store factory.

LocalFileStore: filesystem-backed ArtifactStore rooted at an arbitrary directory.
make_store: selects LocalFileStore (RESUME_STORE_DIR) > S3Store (S3_BUCKET) > RuntimeError.
"""
import os

import pytest

from jobmaxxing.tailoring.storage import (
    ArtifactMissing,
    BaseResumeMissing,
    LocalFileStore,
    S3Store,
    make_store,
)


# ---------------------------------------------------------------------------
# LocalFileStore — get_base_resume
# ---------------------------------------------------------------------------

def test_local_get_base_resume_reads_tex(tmp_path):
    """get_base_resume reads {root}/base/{type}/main.tex and returns its content."""
    tex_dir = tmp_path / "base" / "swe"
    tex_dir.mkdir(parents=True)
    (tex_dir / "main.tex").write_text(r"\documentclass{article}\begin{document}Hello\end{document}")

    store = LocalFileStore(str(tmp_path))
    content = store.get_base_resume("swe")
    assert r"\documentclass" in content


def test_local_get_base_resume_missing_raises(tmp_path):
    """Missing main.tex raises BaseResumeMissing with a clear message."""
    store = LocalFileStore(str(tmp_path))
    with pytest.raises(BaseResumeMissing, match="swe"):
        store.get_base_resume("swe")


# ---------------------------------------------------------------------------
# LocalFileStore — put_artifact / get_artifact round-trip
# ---------------------------------------------------------------------------

def test_local_put_then_get_artifact_round_trips(tmp_path):
    """put_artifact followed by get_artifact returns identical bytes."""
    store = LocalFileStore(str(tmp_path))
    payload = b"Hello, tailored!\n"
    store.put_artifact("job-42", "tailored.tex", payload)
    result = store.get_artifact("job-42", "tailored.tex")
    assert result == payload


def test_local_put_artifact_creates_dirs(tmp_path):
    """put_artifact creates intermediate directories automatically."""
    store = LocalFileStore(str(tmp_path))
    store.put_artifact("deeply/nested", "review.json", b"{}")
    # Path should now exist under tailored/
    dest = tmp_path / "tailored" / "deeply" / "nested" / "review.json"
    assert dest.exists()


def test_local_artifact_prefix_returns_path(tmp_path):
    """artifact_prefix returns the local path string for the job artifacts dir."""
    store = LocalFileStore(str(tmp_path))
    prefix = store.artifact_prefix("job-99")
    assert "tailored" in prefix
    assert "job-99" in prefix


def test_local_get_artifact_missing_raises(tmp_path):
    """Requesting a non-existent artifact raises ArtifactMissing."""
    store = LocalFileStore(str(tmp_path))
    with pytest.raises(ArtifactMissing, match="nope.txt"):
        store.get_artifact("job-1", "nope.txt")


# ---------------------------------------------------------------------------
# make_store factory
# ---------------------------------------------------------------------------

def test_make_store_returns_local_when_resume_store_dir_set(tmp_path, monkeypatch):
    """make_store returns a LocalFileStore when RESUME_STORE_DIR is set."""
    monkeypatch.setenv("RESUME_STORE_DIR", str(tmp_path))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    store = make_store()
    assert isinstance(store, LocalFileStore)


def test_make_store_local_takes_priority_over_s3(tmp_path, monkeypatch):
    """RESUME_STORE_DIR takes priority: make_store returns LocalFileStore even if S3_BUCKET is also set."""
    monkeypatch.setenv("RESUME_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET", "some-bucket")
    store = make_store()
    assert isinstance(store, LocalFileStore)


def test_make_store_returns_s3_when_only_s3_bucket_set(monkeypatch):
    """make_store returns an S3Store when only S3_BUCKET is set (no real AWS call made)."""
    monkeypatch.delenv("RESUME_STORE_DIR", raising=False)
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    store = make_store()
    assert isinstance(store, S3Store)
    assert store.bucket == "my-bucket"


def test_make_store_raises_when_neither_set(monkeypatch):
    """make_store raises a clear RuntimeError naming both env vars when neither is set."""
    monkeypatch.delenv("RESUME_STORE_DIR", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="RESUME_STORE_DIR"):
        make_store()
    # Error also names S3_BUCKET so the operator knows both options
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        make_store()


# ---------------------------------------------------------------------------
# Shipped templates sanity check
# ---------------------------------------------------------------------------

def test_shipped_swe_template_exists_and_has_documentclass():
    """resume_store/base/swe/main.tex must exist and contain \\documentclass."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    template_path = os.path.join(repo_root, "resume_store", "base", "swe", "main.tex")
    assert os.path.isfile(template_path), f"Template not found at {template_path}"
    content = open(template_path).read()
    assert r"\documentclass" in content
