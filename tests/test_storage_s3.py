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
