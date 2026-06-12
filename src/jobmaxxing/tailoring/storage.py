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
