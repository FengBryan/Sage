import pytest

from common.core import config


def test_production_like_config_rejects_default_secrets(monkeypatch):
    monkeypatch.setenv("SAGE_ENV", "production")
    monkeypatch.delenv("SAGE_JWT_KEY", raising=False)
    monkeypatch.delenv("SAGE_REFRESH_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("SAGE_SESSION_SECRET", raising=False)

    cfg = config.build_startup_config()

    with pytest.raises(ValueError, match="secure secrets"):
        config.validate_startup_config(cfg)


def test_production_like_config_forces_secure_session_cookie(monkeypatch):
    monkeypatch.setenv("SAGE_ENV", "production")
    monkeypatch.setenv("SAGE_JWT_KEY", "prod-jwt-secret")
    monkeypatch.setenv("SAGE_REFRESH_TOKEN_SECRET", "prod-refresh-secret")
    monkeypatch.setenv("SAGE_SESSION_SECRET", "prod-session-secret")
    monkeypatch.setenv("SAGE_SESSION_COOKIE_SECURE", "false")

    cfg = config.build_startup_config()

    assert cfg.session_cookie_secure is True


def test_development_config_allows_default_secrets(monkeypatch):
    monkeypatch.setenv("SAGE_ENV", "development")
    monkeypatch.delenv("SAGE_JWT_KEY", raising=False)
    monkeypatch.delenv("SAGE_REFRESH_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("SAGE_SESSION_SECRET", raising=False)

    cfg = config.build_startup_config()

    config.validate_startup_config(cfg)


def test_workspace_read_cache_defaults_disabled(monkeypatch):
    for key in (
        "SAGE_WORKSPACE_READ_CACHE_ENABLED",
        "SAGE_WORKSPACE_READ_CACHE_PROVIDER",
        "SAGE_WORKSPACE_READ_CACHE_PREFIX",
        "SAGE_GCS_BUCKET_NAME",
        "SAGE_GCS_CREDENTIALS_JSON",
        "SAGE_WORKSPACE_READ_CACHE_GCS_BUCKET",
        "SAGE_WORKSPACE_READ_CACHE_GCS_PREFIX",
        "SAGE_WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON",
        "SAGE_WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES",
        "SAGE_WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES",
        "SAGE_WORKSPACE_READ_CACHE_DEPLOYMENT_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = config.build_startup_config("server")

    assert cfg.workspace_read_cache_enabled is False
    assert cfg.workspace_read_cache_provider == "gcs"
    assert cfg.workspace_read_cache_prefix == "workspace-cache"
    assert cfg.gcs_bucket_name is None
    assert cfg.gcs_credentials_json is None
    assert cfg.workspace_read_cache_sample_threshold_bytes == 3 * 1024 * 1024
    assert cfg.workspace_read_cache_sample_chunk_bytes == 512 * 1024
    assert cfg.workspace_read_cache_deployment_id == ""


def test_workspace_read_cache_env_overrides(monkeypatch):
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_ENABLED", "true")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_PROVIDER", "gcs")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_PREFIX", "custom-prefix")
    monkeypatch.setenv("SAGE_GCS_BUCKET_NAME", "sage-cache")
    monkeypatch.setenv("SAGE_GCS_CREDENTIALS_JSON", "/secrets/gcs.json")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES", "123")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES", "45")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_DEPLOYMENT_ID", "prod-a")

    cfg = config.build_startup_config("server")

    assert cfg.workspace_read_cache_enabled is True
    assert cfg.workspace_read_cache_provider == "gcs"
    assert cfg.workspace_read_cache_prefix == "custom-prefix"
    assert cfg.gcs_bucket_name == "sage-cache"
    assert cfg.gcs_credentials_json == "/secrets/gcs.json"
    assert cfg.workspace_read_cache_sample_threshold_bytes == 123
    assert cfg.workspace_read_cache_sample_chunk_bytes == 45
    assert cfg.workspace_read_cache_deployment_id == "prod-a"


def test_workspace_read_cache_legacy_gcs_env_fallback(monkeypatch):
    monkeypatch.delenv("SAGE_WORKSPACE_READ_CACHE_PREFIX", raising=False)
    monkeypatch.delenv("SAGE_GCS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("SAGE_GCS_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_PREFIX", "legacy-prefix")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_BUCKET", "legacy-bucket")
    monkeypatch.setenv(
        "SAGE_WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON",
        "/legacy/gcs.json",
    )

    cfg = config.build_startup_config("server")

    assert cfg.workspace_read_cache_prefix == "legacy-prefix"
    assert cfg.gcs_bucket_name == "legacy-bucket"
    assert cfg.gcs_credentials_json == "/legacy/gcs.json"


def test_workspace_read_cache_new_gcs_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_PREFIX", "new-prefix")
    monkeypatch.setenv("SAGE_GCS_BUCKET_NAME", "new-bucket")
    monkeypatch.setenv("SAGE_GCS_CREDENTIALS_JSON", "/new/gcs.json")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_PREFIX", "legacy-prefix")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_BUCKET", "legacy-bucket")
    monkeypatch.setenv(
        "SAGE_WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON",
        "/legacy/gcs.json",
    )

    cfg = config.build_startup_config("server")

    assert cfg.workspace_read_cache_prefix == "new-prefix"
    assert cfg.gcs_bucket_name == "new-bucket"
    assert cfg.gcs_credentials_json == "/new/gcs.json"
