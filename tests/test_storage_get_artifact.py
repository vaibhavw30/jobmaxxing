import pytest

from jobmaxxing.tailoring.storage import ArtifactMissing, InMemoryStore, S3Store


def test_in_memory_get_artifact_roundtrip():
    store = InMemoryStore()
    store.put_artifact("job1", "review.json", b'{"x": 1}')
    assert store.get_artifact("job1", "review.json") == b'{"x": 1}'


def test_in_memory_get_artifact_missing_raises():
    store = InMemoryStore()
    with pytest.raises(ArtifactMissing):
        store.get_artifact("job1", "review.json")


class _FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_s3_get_artifact_reads_key():
    client = _FakeS3(objects={"tailored/job1/review.json": b"DATA"})
    store = S3Store("b", client=client)
    assert store.get_artifact("job1", "review.json") == b"DATA"


def test_s3_get_artifact_missing_raises():
    store = S3Store("b", client=_FakeS3())
    with pytest.raises(ArtifactMissing):
        store.get_artifact("job1", "review.json")


def test_s3_get_artifact_other_error_propagates():
    from botocore.exceptions import ClientError

    class _Denied:
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    store = S3Store("b", client=_Denied())
    with pytest.raises(ClientError):
        store.get_artifact("job1", "review.json")
