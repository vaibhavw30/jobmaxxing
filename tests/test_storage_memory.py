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
