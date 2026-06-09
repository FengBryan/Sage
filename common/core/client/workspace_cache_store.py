from __future__ import annotations

from typing import Callable

from common.services.workspace_read_cache import (
    ObjectCacheStore,
    WorkspaceReadCacheConfig,
)

WorkspaceCacheStoreBuilder = Callable[[WorkspaceReadCacheConfig], ObjectCacheStore]


def _build_gcs_cache_store(cache_cfg: WorkspaceReadCacheConfig) -> ObjectCacheStore:
    from common.core.client.gcs import GCSObjectCacheStore

    if not cache_cfg.bucket:
        raise RuntimeError("workspace read cache GCS bucket is not configured")
    return GCSObjectCacheStore(
        cache_cfg.bucket,
        credentials_json=cache_cfg.credentials_json,
    )


def _build_s3_cache_store(cache_cfg: WorkspaceReadCacheConfig) -> ObjectCacheStore:
    from common.core.client.s3_cache import S3ObjectCacheStore

    missing = [
        name
        for name, value in (
            ("bucket", cache_cfg.bucket),
            ("endpoint", cache_cfg.s3_endpoint),
            ("access_key", cache_cfg.s3_access_key),
            ("secret_key", cache_cfg.s3_secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "workspace read cache S3 configuration missing: " + ", ".join(missing)
        )
    return S3ObjectCacheStore(
        cache_cfg.bucket,  # pyright: ignore[reportArgumentType]
        endpoint=cache_cfg.s3_endpoint,  # pyright: ignore[reportArgumentType]
        access_key=cache_cfg.s3_access_key,  # pyright: ignore[reportArgumentType]
        secret_key=cache_cfg.s3_secret_key,  # pyright: ignore[reportArgumentType]
        secure=cache_cfg.s3_secure,
    )


_DEFAULT_PROVIDER_BUILDERS = {
    "gcs": _build_gcs_cache_store,
    "s3": _build_s3_cache_store,
}
_PROVIDER_BUILDERS = dict(_DEFAULT_PROVIDER_BUILDERS)


def register_workspace_cache_store_provider(
    provider: str,
    builder: WorkspaceCacheStoreBuilder,
) -> None:
    normalized = (provider or "").strip().lower()
    if not normalized:
        raise ValueError("workspace cache store provider is required")
    _PROVIDER_BUILDERS[normalized] = builder


def reset_workspace_cache_store_providers() -> None:
    _PROVIDER_BUILDERS.clear()
    _PROVIDER_BUILDERS.update(_DEFAULT_PROVIDER_BUILDERS)


def create_workspace_cache_store(cache_cfg: WorkspaceReadCacheConfig) -> ObjectCacheStore:
    provider = (cache_cfg.provider or "").strip().lower()
    builder = _PROVIDER_BUILDERS.get(provider)
    if builder:
        return builder(cache_cfg)

    raise RuntimeError(f"unsupported workspace read cache provider: {provider}")
