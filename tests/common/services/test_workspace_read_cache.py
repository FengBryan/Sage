import asyncio

import pytest

from common.core.exceptions import SageHTTPException
from common.services.workspace_read_cache import (
    CacheScope,
    HostWorkspaceFileSource,
    WorkspaceReadCacheConfig,
    build_cache_key,
    compute_file_fingerprint,
)


def test_small_file_uses_full_sha256(tmp_path):
    file_path = tmp_path / "small.txt"
    file_path.write_bytes(b"hello")

    fp = compute_file_fingerprint(
        file_path,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
    )

    assert fp.hash_kind == "full_sha256"
    assert fp.size == 5
    assert len(fp.content_hash) == 64


def test_large_file_uses_sample_sha256(tmp_path):
    file_path = tmp_path / "large.bin"
    file_path.write_bytes(b"a" * (3 * 1024 * 1024 + 1))

    fp = compute_file_fingerprint(
        file_path,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
    )

    assert fp.hash_kind == "sample_sha256"
    assert fp.size == 3 * 1024 * 1024 + 1
    assert len(fp.content_hash) == 64


def test_host_source_rejects_outside_path(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("nope", encoding="utf-8")
    source = HostWorkspaceFileSource(workspace)

    with pytest.raises(SageHTTPException):
        source.resolve("../outside.txt")


def test_host_source_opens_range(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"abcdef")
    source = HostWorkspaceFileSource(workspace)

    resolved = source.resolve("a.txt")
    chunks = list(resolved.iter_bytes(byte_range=(2, 4), chunk_size=2))

    assert b"".join(chunks) == b"cde"
    assert resolved.filename == "a.txt"
    assert resolved.media_type == "text/plain"


def test_cache_key_is_scoped_and_stable():
    cfg = WorkspaceReadCacheConfig(
        enabled=True,
        provider="gcs",
        bucket="bucket",
        prefix="workspace-cache",
        credentials_json=None,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
        deployment_id="prod-a",
    )
    key1 = build_cache_key(
        cfg,
        CacheScope(kind="agent", identifier="agent-a"),
        "reports/final.txt",
    )
    key2 = build_cache_key(
        cfg,
        CacheScope(kind="session", identifier="session-a"),
        "reports/final.txt",
    )

    assert key1.startswith("workspace-cache/prod-a/agent/agent-a/")
    assert key2.startswith("workspace-cache/prod-a/session/session-a/")
    assert key1.endswith("/final.txt")
    assert key1 != key2


def test_s3_provider_uses_existing_s3_bucket_config():
    from common.core import config

    startup_cfg = config.StartupConfig(
        workspace_read_cache_enabled=True,
        workspace_read_cache_provider="s3",
        gcs_bucket_name="gcs-bucket",
        s3_endpoint="http://127.0.0.1:9000",
        s3_access_key="ak",
        s3_secret_key="sk",
        s3_secure=False,
        s3_bucket_name="s3-bucket",
    )

    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(startup_cfg)

    assert cache_cfg.usable is True
    assert cache_cfg.provider == "s3"
    assert cache_cfg.bucket == "s3-bucket"
    assert cache_cfg.s3_endpoint == "http://127.0.0.1:9000"
    assert cache_cfg.s3_access_key == "ak"
    assert cache_cfg.s3_secret_key == "sk"
    assert cache_cfg.s3_secure is False


def test_gcs_provider_uses_gcs_backend_config_names():
    from common.core import config

    startup_cfg = config.StartupConfig(
        workspace_read_cache_enabled=True,
        workspace_read_cache_provider="gcs",
        workspace_read_cache_prefix="cache-prefix",
        gcs_bucket_name="gcs-bucket",
        gcs_credentials_json="/secrets/gcs.json",
    )

    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(startup_cfg)

    assert cache_cfg.usable is True
    assert cache_cfg.provider == "gcs"
    assert cache_cfg.bucket == "gcs-bucket"
    assert cache_cfg.prefix == "cache-prefix"
    assert cache_cfg.credentials_json == "/secrets/gcs.json"


class FakeObjectCacheStore:
    def __init__(self):
        self.objects = {}
        self.head_calls = []
        self.get_calls = []
        self.put_calls = []
        self.fail_head = False
        self.fail_get = False
        self.fail_put = False

    async def head(self, key):
        self.head_calls.append(key)
        if self.fail_head:
            raise RuntimeError("head failed")
        item = self.objects.get(key)
        return dict(item["metadata"]) if item else None

    async def open_stream(self, key, byte_range=None):
        self.get_calls.append((key, byte_range))
        if self.fail_get:
            raise RuntimeError("get failed")
        data = self.objects[key]["data"]
        if byte_range is not None:
            start, end = byte_range
            return [data[start : end + 1]]
        return [data]

    async def put_stream(self, key, chunks, metadata, content_type):
        self.put_calls.append((key, dict(metadata), content_type))
        if self.fail_put:
            raise RuntimeError("put failed")
        data = b"".join(chunks)
        self.objects[key] = {"data": data, "metadata": dict(metadata)}

    async def update_metadata(self, key, metadata):
        if key in self.objects:
            self.objects[key]["metadata"] = dict(metadata)


async def _collect_async_chunks(chunks):
    result = []
    async for chunk in chunks:
        result.append(chunk)
    return b"".join(result)


def test_cache_hit_uses_object_store_without_hashing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "a.txt"
    file_path.write_bytes(b"source")
    source = HostWorkspaceFileSource(workspace)
    resolved = source.resolve("a.txt")
    cfg = WorkspaceReadCacheConfig(
        enabled=True,
        provider="gcs",
        bucket="bucket",
        prefix="workspace-cache",
        credentials_json=None,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
        deployment_id="test",
    )
    store = FakeObjectCacheStore()
    key = build_cache_key(cfg, CacheScope("agent", "agent-a"), "a.txt")
    store.objects[key] = {
        "data": b"cached",
        "metadata": {
            "sage-size": str(resolved.size),
            "sage-mtime-ns": str(resolved.mtime_ns),
            "sage-hash-kind": "",
            "sage-content-hash": "",
            "sage-source-id": "agent:agent-a",
            "sage-relative-path": "a.txt",
        },
    }

    from common.services.workspace_read_cache import WorkspaceReadCache

    cache = WorkspaceReadCache(cfg, store)
    plan = asyncio.run(
        cache.plan_read(
            scope=CacheScope("agent", "agent-a"),
            source=source,
            relative_path="a.txt",
        )
    )

    assert plan.source == "cache"
    assert plan.size == len(b"cached")
    assert asyncio.run(_collect_async_chunks(plan.iter_bytes())) == b"cached"


def test_cache_miss_returns_source_and_schedules_sync(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"source")
    source = HostWorkspaceFileSource(workspace)
    cfg = WorkspaceReadCacheConfig(
        enabled=True,
        provider="gcs",
        bucket="bucket",
        prefix="workspace-cache",
        credentials_json=None,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
        deployment_id="test",
    )
    store = FakeObjectCacheStore()

    from common.services.workspace_read_cache import WorkspaceReadCache

    cache = WorkspaceReadCache(cfg, store)
    plan = asyncio.run(
        cache.plan_read(
            scope=CacheScope("agent", "agent-a"),
            source=source,
            relative_path="a.txt",
        )
    )

    assert plan.source == "source"
    assert asyncio.run(_collect_async_chunks(plan.iter_bytes())) == b"source"
    asyncio.run(cache.wait_for_pending_syncs())
    assert store.put_calls
    assert next(iter(store.objects.values()))["data"] == b"source"


def test_cache_head_failure_falls_back_to_source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"source")
    source = HostWorkspaceFileSource(workspace)
    cfg = WorkspaceReadCacheConfig(
        enabled=True,
        provider="gcs",
        bucket="bucket",
        prefix="workspace-cache",
        credentials_json=None,
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
        deployment_id="test",
    )
    store = FakeObjectCacheStore()
    store.fail_head = True

    from common.services.workspace_read_cache import WorkspaceReadCache

    cache = WorkspaceReadCache(cfg, store)
    plan = asyncio.run(
        cache.plan_read(
            scope=CacheScope("agent", "agent-a"),
            source=source,
            relative_path="a.txt",
        )
    )

    assert plan.source == "source"
    assert asyncio.run(_collect_async_chunks(plan.iter_bytes())) == b"source"


def test_gcs_store_imports_without_google_library(monkeypatch):
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google.cloud.storage"):
            raise ImportError("no google cloud")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    module = importlib.import_module("common.core.client.gcs")

    assert hasattr(module, "GCSObjectCacheStore")
