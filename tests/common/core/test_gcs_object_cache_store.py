import asyncio

from common.core.client.gcs import GCSObjectCacheStore


class FakeBlob:
    def __init__(self):
        self.exists_value = False
        self.metadata = None
        self.data = b""
        self.download_calls = []
        self.upload_content_type = None
        self.reload_calls = 0
        self.patch_calls = 0

    def exists(self):
        return self.exists_value

    def reload(self):
        self.reload_calls += 1

    def download_as_bytes(self, start=None, end=None):
        self.download_calls.append((start, end))
        if start is None:
            return self.data
        return self.data[start : end + 1]

    def upload_from_filename(self, filename, content_type):
        self.upload_content_type = content_type
        with open(filename, "rb") as handle:
            self.data = handle.read()
        self.exists_value = True

    def patch(self):
        self.patch_calls += 1


class FakeBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, key):
        self.blobs.setdefault(key, FakeBlob())
        return self.blobs[key]


class FakeClient:
    def __init__(self, bucket):
        self.bucket_obj = bucket

    def bucket(self, name):
        assert name == "bucket"
        return self.bucket_obj


def test_gcs_store_head_download_upload_and_metadata_update(monkeypatch):
    bucket = FakeBucket()

    monkeypatch.setattr(
        "common.core.client.gcs._load_storage_client",
        lambda credentials_json: FakeClient(bucket),
    )

    store = GCSObjectCacheStore("bucket", credentials_json="/tmp/creds.json")
    blob = bucket.blob("cache/key")
    blob.exists_value = True
    blob.metadata = {"sage-size": "5"}
    blob.data = b"abcdef"

    assert asyncio.run(store.head("cache/key")) == {"sage-size": "5"}
    assert blob.reload_calls == 1
    assert asyncio.run(store.open_stream("cache/key", byte_range=(2, 4))) == [b"cde"]
    assert blob.download_calls == [(2, 4)]

    asyncio.run(
        store.put_stream(
            "cache/new",
            [b"new", b"-data"],
            {"sage-size": "8"},
            "text/plain",
        )
    )
    uploaded = bucket.blob("cache/new")
    assert uploaded.data == b"new-data"
    assert uploaded.metadata == {"sage-size": "8"}
    assert uploaded.upload_content_type == "text/plain"

    asyncio.run(store.update_metadata("cache/new", {"sage-size": "9"}))
    assert uploaded.metadata == {"sage-size": "9"}
    assert uploaded.patch_calls == 1
