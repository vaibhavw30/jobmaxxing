from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class BaseResumeMissing(RuntimeError):
    """Raised when no base resume exists for a resume type."""


class ArtifactMissing(RuntimeError):
    """Raised when a requested artifact does not exist."""


class ArtifactStore(Protocol):
    def get_base_resume(self, resume_type: str) -> str: ...
    def put_artifact(self, job_id, name: str, data: bytes) -> None: ...
    def artifact_prefix(self, job_id) -> str: ...
    def get_artifact(self, job_id, name: str) -> bytes: ...


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

    def get_artifact(self, job_id, name: str) -> bytes:
        key = (str(job_id), name)
        if key not in self.artifacts:
            raise ArtifactMissing(f"no artifact {name!r} for job {job_id}")
        return self.artifacts[key]


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
            # Only a genuine missing key is BaseResumeMissing; surface permission/throttling/
            # bucket errors as themselves so a misconfig isn't misread as "no resume".
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise BaseResumeMissing(f"no base resume at s3://{self.bucket}/{key}") from exc
            raise
        return resp["Body"].read().decode("utf-8")

    def put_artifact(self, job_id, name: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=f"tailored/{str(job_id)}/{name}", Body=data)

    def artifact_prefix(self, job_id) -> str:
        return f"s3://{self.bucket}/tailored/{str(job_id)}/"

    def get_artifact(self, job_id, name: str) -> bytes:
        key = f"tailored/{str(job_id)}/{name}"
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ArtifactMissing(f"no artifact at s3://{self.bucket}/{key}") from exc
            raise
