import asyncio
from io import BytesIO

from common.core.client.s3_cache import S3ObjectCacheStore


class FakeStat:
    def __init__(self, metadata=None):
        self.metadata = metadata or {}


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.closed = False
        self.released = False

    def read(self):
        return self.data

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class FakeMinioClient:
    def __init__(self):
        self.objects = {}
        self.stat_calls = []
        self.get_calls = []
        self.put_calls = []

    def stat_object(self, bucket, key):
        self.stat_calls.append((bucket, key))
        item = self.objects.get((bucket, key))
        if item is None:
            raise RuntimeError("missing")
        return FakeStat(item["metadata"])

    def get_object(self, bucket, key, offset=0, length=0):
        self.get_calls.append((bucket, key, offset, length))
        data = self.objects[(bucket, key)]["data"]
        if length:
            data = data[offset : offset + length]
        elif offset:
            data = data[offset:]
        return FakeResponse(data)

    def put_object(self, bucket, key, data, length, content_type, metadata=None):
        self.put_calls.append((bucket, key, length, content_type, metadata))
        self.objects[(bucket, key)] = {
            "data": data.read(length),
            "metadata": dict(metadata or {}),
        }


def test_s3_store_head_range_download_upload_and_metadata_update(monkeypatch):
    client = FakeMinioClient()
    client.objects[("bucket", "cache/key")] = {
        "data": b"abcdef",
        "metadata": {"sage-size": "6"},
    }

    monkeypatch.setattr(
        "common.core.client.s3_cache._load_minio_client",
        lambda endpoint, access_key, secret_key, secure: client,
    )

    store = S3ObjectCacheStore(
        "bucket",
        endpoint="https://s3.example.com",
        access_key="ak",
        secret_key="sk",
        secure=True,
    )

    assert asyncio.run(store.head("cache/key")) == {"sage-size": "6"}
    assert asyncio.run(store.open_stream("cache/key", byte_range=(2, 4))) == [b"cde"]
    assert client.get_calls == [("bucket", "cache/key", 2, 3)]

    asyncio.run(
        store.put_stream(
            "cache/new",
            [b"new", b"-data"],
            {"sage-size": "8"},
            "text/plain",
        )
    )
    assert client.objects[("bucket", "cache/new")] == {
        "data": b"new-data",
        "metadata": {"sage-size": "8"},
    }

    asyncio.run(store.update_metadata("cache/new", {"sage-size": "9"}))
    assert client.objects[("bucket", "cache/new")] == {
        "data": b"new-data",
        "metadata": {"sage-size": "9"},
    }
