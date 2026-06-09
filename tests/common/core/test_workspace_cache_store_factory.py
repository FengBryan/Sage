from common.core import config
from common.core.client.workspace_cache_store import (
    create_workspace_cache_store,
    register_workspace_cache_store_provider,
    reset_workspace_cache_store_providers,
)
from common.services.workspace_read_cache import WorkspaceReadCacheConfig


def test_cache_store_factory_routes_gcs(monkeypatch):
    calls = {}

    class FakeGCSObjectCacheStore:
        def __init__(self, bucket_name, credentials_json=None):
            calls["bucket_name"] = bucket_name
            calls["credentials_json"] = credentials_json

    monkeypatch.setattr(
        "common.core.client.gcs.GCSObjectCacheStore",
        FakeGCSObjectCacheStore,
    )
    cache_cfg = WorkspaceReadCacheConfig(
        enabled=True,
        provider="gcs",
        bucket="gcs-bucket",
        prefix="workspace-cache",
        credentials_json="/tmp/gcs.json",
        sample_threshold_bytes=3 * 1024 * 1024,
        sample_chunk_bytes=512 * 1024,
        deployment_id="test",
    )

    store = create_workspace_cache_store(cache_cfg)

    assert isinstance(store, FakeGCSObjectCacheStore)
    assert calls == {"bucket_name": "gcs-bucket", "credentials_json": "/tmp/gcs.json"}


def test_cache_store_factory_routes_s3(monkeypatch):
    calls = {}

    class FakeS3ObjectCacheStore:
        def __init__(self, bucket_name, endpoint, access_key, secret_key, secure):
            calls.update(
                {
                    "bucket_name": bucket_name,
                    "endpoint": endpoint,
                    "access_key": access_key,
                    "secret_key": secret_key,
                    "secure": secure,
                }
            )

    monkeypatch.setattr(
        "common.core.client.s3_cache.S3ObjectCacheStore",
        FakeS3ObjectCacheStore,
        raising=False,
    )
    startup_cfg = config.StartupConfig(
        workspace_read_cache_enabled=True,
        workspace_read_cache_provider="s3",
        s3_endpoint="https://s3.example.com",
        s3_access_key="ak",
        s3_secret_key="sk",
        s3_secure=True,
        s3_bucket_name="s3-bucket",
    )
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(startup_cfg)

    store = create_workspace_cache_store(cache_cfg)

    assert isinstance(store, FakeS3ObjectCacheStore)
    assert calls == {
        "bucket_name": "s3-bucket",
        "endpoint": "https://s3.example.com",
        "access_key": "ak",
        "secret_key": "sk",
        "secure": True,
    }


def test_cache_store_factory_can_override_provider_slot():
    calls = {}

    class FakeStore:
        pass

    def fake_builder(cache_cfg):
        calls["provider"] = cache_cfg.provider
        return FakeStore()

    try:
        register_workspace_cache_store_provider("gcs", fake_builder)
        cache_cfg = WorkspaceReadCacheConfig(
            enabled=True,
            provider="gcs",
            bucket="bucket",
            prefix="workspace-cache",
            credentials_json=None,
            sample_threshold_bytes=3 * 1024 * 1024,
            sample_chunk_bytes=512 * 1024,
            deployment_id="test",
        )

        store = create_workspace_cache_store(cache_cfg)

        assert isinstance(store, FakeStore)
        assert calls == {"provider": "gcs"}
    finally:
        reset_workspace_cache_store_providers()
