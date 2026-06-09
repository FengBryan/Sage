from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.server.routers.agent import agent_router
from app.server.routers.session_workspace import session_workspace_router
from common.core import config
from common.core.client.workspace_cache_store import (
    register_workspace_cache_store_provider,
    reset_workspace_cache_store_providers,
)
from common.services.workspace_read_cache import (
    CacheScope,
    HostWorkspaceFileSource,
    WorkspaceReadCacheConfig,
    build_cache_key,
)


class FakeGCSBlob:
    def __init__(self, key: str):
        self.key = key
        self.data = b""
        self.metadata = {}
        self.exists_value = False
        self.download_calls = []
        self.upload_calls = []
        self.patch_calls = 0
        self.reload_calls = 0

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
        self.upload_calls.append((filename, content_type))
        with open(filename, "rb") as handle:
            self.data = handle.read()
        self.exists_value = True

    def patch(self):
        self.patch_calls += 1


class FakeGCSBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, key: str):
        self.blobs.setdefault(key, FakeGCSBlob(key))
        return self.blobs[key]


class FakeGCSClient:
    def __init__(self, bucket: FakeGCSBucket):
        self.bucket_obj = bucket

    def bucket(self, bucket_name: str):
        assert bucket_name == "bucket"
        return self.bucket_obj


class FakeS3Stat:
    def __init__(self, metadata):
        self.metadata = metadata


class FakeS3Response:
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
        self.get_calls = []

    def stat_object(self, bucket, key):
        item = self.objects[(bucket, key)]
        return FakeS3Stat(item["metadata"])

    def get_object(self, bucket, key, offset=0, length=0):
        self.get_calls.append((bucket, key, offset, length))
        data = self.objects[(bucket, key)]["data"]
        if length:
            data = data[offset : offset + length]
        return FakeS3Response(data)

    def put_object(self, bucket, key, data, length, content_type, metadata=None):
        self.objects[(bucket, key)] = {
            "data": data.read(length),
            "metadata": dict(metadata or {}),
        }


class FakeSlotObjectCacheStore:
    def __init__(self):
        self.objects = {}
        self.head_calls = []
        self.get_calls = []
        self.put_calls = []

    async def head(self, key):
        self.head_calls.append(key)
        item = self.objects.get(key)
        return dict(item["metadata"]) if item else None

    async def open_stream(self, key, byte_range=None):
        self.get_calls.append((key, byte_range))
        data = self.objects[key]["data"]
        if byte_range is not None:
            start, end = byte_range
            return [data[start : end + 1]]
        return [data]

    async def put_stream(self, key, chunks, metadata, content_type):
        self.put_calls.append((key, dict(metadata), content_type))
        self.objects[key] = {
            "data": b"".join(chunks),
            "metadata": dict(metadata),
        }

    async def update_metadata(self, key, metadata):
        self.objects[key]["metadata"] = dict(metadata)


def _cfg(tmp_path, *, provider="gcs"):
    return config.StartupConfig(
        app_mode="server",
        session_dir=str(tmp_path / "sessions"),
        agents_dir=str(tmp_path / "agents"),
        skill_dir=str(tmp_path / "skills"),
        user_dir=str(tmp_path / "users"),
        logs_dir=str(tmp_path / "logs"),
        workspace_read_cache_enabled=True,
        workspace_read_cache_provider=provider,
        workspace_read_cache_prefix="workspace-cache",
        gcs_bucket_name="bucket",
        workspace_read_cache_sample_threshold_bytes=3 * 1024 * 1024,
        workspace_read_cache_sample_chunk_bytes=512 * 1024,
        workspace_read_cache_deployment_id="test",
        s3_endpoint="https://s3.example.com",
        s3_access_key="ak",
        s3_secret_key="sk",
        s3_secure=True,
        s3_bucket_name="bucket",
    )


def _app():
    app = FastAPI()

    @app.middleware("http")
    async def add_user_claims(request, call_next):
        request.state.user_claims = {"userid": "user-a", "role": "user"}
        return await call_next(request)

    app.include_router(agent_router)
    app.include_router(session_workspace_router)
    return app


def _prime_blob(bucket, cache_cfg, scope, source_root, relative_path, data):
    source = HostWorkspaceFileSource(source_root)
    resolved = source.resolve(relative_path)
    key = build_cache_key(cache_cfg, scope, relative_path)
    blob = bucket.blob(key)
    blob.data = data
    blob.exists_value = True
    blob.metadata = {
        "sage-size": str(resolved.size),
        "sage-mtime-ns": str(resolved.mtime_ns),
        "sage-source-id": f"{scope.kind}:{scope.identifier}",
        "sage-relative-path": relative_path,
    }
    return blob


def test_agent_download_reads_cache_from_mocked_gcs_end_to_end(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    workspace = tmp_path / "agents" / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    (workspace / "report.txt").write_bytes(b"disk-version")
    bucket = FakeGCSBucket()
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    blob = _prime_blob(
        bucket,
        cache_cfg,
        CacheScope("agent", "agent-a"),
        workspace,
        "report.txt",
        b"cached-version",
    )

    monkeypatch.setattr(
        "common.core.client.gcs._load_storage_client",
        lambda credentials_json: FakeGCSClient(bucket),
    )

    with TestClient(_app()) as client:
        response = client.get(
            "/api/agent/agent-a/file_workspace/download",
            params={"file_path": "report.txt"},
        )

    assert response.status_code == 200
    assert response.content == b"cached-version"
    assert response.headers["content-length"] == str(len(b"disk-version"))
    assert blob.download_calls == [(None, None)]


def test_session_stream_range_reads_cache_from_mocked_gcs_end_to_end(
    tmp_path,
    monkeypatch,
):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    session_workspace = tmp_path / "sessions" / "session-a"
    session_workspace.mkdir(parents=True)
    (session_workspace / "messages.json").write_bytes(b"0123456789")
    bucket = FakeGCSBucket()
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    blob = _prime_blob(
        bucket,
        cache_cfg,
        CacheScope("session", "session-a"),
        session_workspace,
        "messages.json",
        b"abcdefghij",
    )

    class FakeManager:
        def get_session_workspace(self, session_id):
            assert session_id == "session-a"
            return str(session_workspace)

    monkeypatch.setattr(
        "common.services.agent_service.get_global_session_manager",
        lambda: FakeManager(),
    )
    monkeypatch.setattr(
        "common.core.client.gcs._load_storage_client",
        lambda credentials_json: FakeGCSClient(bucket),
    )

    with TestClient(_app()) as client:
        response = client.get(
            "/api/sessions/session-a/file_workspace/stream",
            params={"file_path": "messages.json"},
            headers={"Range": "bytes=2-5"},
        )

    assert response.status_code == 206
    assert response.content == b"cdef"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert blob.download_calls == [(None, None), (2, 5)]


def test_agent_download_cache_miss_returns_disk_and_syncs_mocked_gcs(
    tmp_path,
    monkeypatch,
):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    workspace = tmp_path / "agents" / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    (workspace / "fresh.txt").write_bytes(b"fresh-from-disk")
    bucket = FakeGCSBucket()
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    key = build_cache_key(cache_cfg, CacheScope("agent", "agent-a"), "fresh.txt")

    monkeypatch.setattr(
        "common.core.client.gcs._load_storage_client",
        lambda credentials_json: FakeGCSClient(bucket),
    )

    with TestClient(_app()) as client:
        response = client.get(
            "/api/agent/agent-a/file_workspace/download",
            params={"file_path": "fresh.txt"},
        )

    synced_blob = bucket.blob(key)
    assert response.status_code == 200
    assert response.content == b"fresh-from-disk"
    assert synced_blob.exists_value is True
    assert synced_blob.data == b"fresh-from-disk"
    assert synced_blob.metadata["sage-source-id"] == "agent:agent-a"
    assert synced_blob.metadata["sage-relative-path"] == "fresh.txt"


def test_agent_download_routes_through_s3_cache_backend_end_to_end(
    tmp_path,
    monkeypatch,
):
    cfg = _cfg(tmp_path, provider="s3")
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    workspace = tmp_path / "agents" / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    (workspace / "report.txt").write_bytes(b"disk-version")
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    key = build_cache_key(cache_cfg, CacheScope("agent", "agent-a"), "report.txt")
    source = HostWorkspaceFileSource(workspace)
    resolved = source.resolve("report.txt")
    s3_client = FakeMinioClient()
    s3_client.objects[("bucket", key)] = {
        "data": b"s3-cached-version",
        "metadata": {
            "sage-size": str(resolved.size),
            "sage-mtime-ns": str(resolved.mtime_ns),
            "sage-source-id": "agent:agent-a",
            "sage-relative-path": "report.txt",
        },
    }

    monkeypatch.setattr(
        "common.core.client.s3_cache._load_minio_client",
        lambda endpoint, access_key, secret_key, secure: s3_client,
    )

    with TestClient(_app()) as client:
        response = client.get(
            "/api/agent/agent-a/file_workspace/download",
            params={"file_path": "report.txt"},
        )

    assert response.status_code == 200
    assert response.content == b"s3-cached-version"
    assert s3_client.get_calls == [("bucket", key, 0, 0)]


def test_agent_download_uses_mocked_gcs_backend_slot_end_to_end(tmp_path):
    cfg = _cfg(tmp_path, provider="gcs")
    config._GLOBAL_STARTUP_CONFIG = cfg
    workspace = tmp_path / "agents" / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    (workspace / "slot.txt").write_bytes(b"disk-slot")
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    key = build_cache_key(cache_cfg, CacheScope("agent", "agent-a"), "slot.txt")
    resolved = HostWorkspaceFileSource(workspace).resolve("slot.txt")
    store = FakeSlotObjectCacheStore()
    store.objects[key] = {
        "data": b"gcs-slot-cache",
        "metadata": {
            "sage-size": str(resolved.size),
            "sage-mtime-ns": str(resolved.mtime_ns),
        },
    }

    try:
        register_workspace_cache_store_provider("gcs", lambda cache_cfg: store)
        with TestClient(_app()) as client:
            response = client.get(
                "/api/agent/agent-a/file_workspace/download",
                params={"file_path": "slot.txt"},
            )

        assert response.status_code == 200
        assert response.content == b"gcs-slot-cache"
        assert store.head_calls == [key]
        assert store.get_calls == [(key, None)]
    finally:
        reset_workspace_cache_store_providers()


def test_session_stream_uses_mocked_s3_backend_slot_end_to_end(tmp_path):
    cfg = _cfg(tmp_path, provider="s3")
    config._GLOBAL_STARTUP_CONFIG = cfg
    session_workspace = tmp_path / "sessions" / "session-a"
    session_workspace.mkdir(parents=True)
    (session_workspace / "slot.json").write_bytes(b"0123456789")
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    key = build_cache_key(cache_cfg, CacheScope("session", "session-a"), "slot.json")
    resolved = HostWorkspaceFileSource(session_workspace).resolve("slot.json")
    store = FakeSlotObjectCacheStore()
    store.objects[key] = {
        "data": b"abcdefghij",
        "metadata": {
            "sage-size": str(resolved.size),
            "sage-mtime-ns": str(resolved.mtime_ns),
        },
    }

    class FakeManager:
        def get_session_workspace(self, session_id):
            assert session_id == "session-a"
            return str(session_workspace)

    try:
        register_workspace_cache_store_provider("s3", lambda cache_cfg: store)
        from common.services import agent_service

        original_manager = agent_service.get_global_session_manager
        agent_service.get_global_session_manager = lambda: FakeManager()
        with TestClient(_app()) as client:
            response = client.get(
                "/api/sessions/session-a/file_workspace/stream",
                params={"file_path": "slot.json"},
                headers={"Range": "bytes=3-6"},
            )

        assert response.status_code == 206
        assert response.content == b"defg"
        assert response.headers["content-range"] == "bytes 3-6/10"
        assert store.head_calls == [key, key]
        assert store.get_calls == [(key, None), (key, (3, 6))]
    finally:
        agent_service.get_global_session_manager = original_manager
        reset_workspace_cache_store_providers()
