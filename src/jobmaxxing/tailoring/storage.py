from typing import Protocol

import boto3
from botocore.exceptions import ClientError


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
        return f"memory://tailored/{str(job_id)}/"  # str() for symmetry with put_artifact's key


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
        self.client.put_object(Bucket=self.bucket, Key=f"tailored/{str(job_id)}/{name}", Body=data)

    def artifact_prefix(self, job_id) -> str:
        return f"s3://{self.bucket}/tailored/{str(job_id)}/"
