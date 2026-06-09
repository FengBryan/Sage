import asyncio
from pathlib import Path

from common.core import config
from common.services import agent_service


class FakeObjectCacheStore:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    async def head(self, key):
        item = self.objects.get(key)
        return dict(item["metadata"]) if item else None

    async def open_stream(self, key, byte_range=None):
        data = self.objects[key]["data"]
        if byte_range is not None:
            start, end = byte_range
            return [data[start : end + 1]]
        return [data]

    async def put_stream(self, key, chunks, metadata, content_type):
        self.put_calls.append((key, metadata, content_type))
        self.objects[key] = {"data": b"".join(chunks), "metadata": dict(metadata)}

    async def update_metadata(self, key, metadata):
        self.objects[key]["metadata"] = dict(metadata)


def _cfg(tmp_path: Path, *, enabled: bool) -> config.StartupConfig:
    return config.StartupConfig(
        app_mode="server",
        session_dir=str(tmp_path / "sessions"),
        agents_dir=str(tmp_path / "agents"),
        skill_dir=str(tmp_path / "skills"),
        user_dir=str(tmp_path / "users"),
        logs_dir=str(tmp_path / "logs"),
        workspace_read_cache_enabled=enabled,
        workspace_read_cache_provider="gcs",
        workspace_read_cache_prefix="workspace-cache",
        gcs_bucket_name="bucket",
        workspace_read_cache_sample_threshold_bytes=3 * 1024 * 1024,
        workspace_read_cache_sample_chunk_bytes=512 * 1024,
        workspace_read_cache_deployment_id="test",
    )


async def _collect(plan):
    return b"".join([chunk async for chunk in plan.iter_bytes()])


def test_agent_read_plan_disabled_returns_source(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, enabled=False)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    workspace = Path(cfg.agents_dir) / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"source")

    plan = asyncio.run(
        agent_service.prepare_server_agent_read_plan(
            "agent-a",
            "user-a",
            "a.txt",
            cache_store_factory=lambda _cfg: FakeObjectCacheStore(),
        )
    )

    assert plan.source == "source"
    assert asyncio.run(_collect(plan)) == b"source"


def test_agent_read_plan_cache_hit_returns_cached_bytes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, enabled=True)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    workspace = Path(cfg.agents_dir) / "user-a" / "agent-a"
    workspace.mkdir(parents=True)
    file_path = workspace / "a.txt"
    file_path.write_bytes(b"source")

    from common.services.workspace_read_cache import (
        CacheScope,
        HostWorkspaceFileSource,
        WorkspaceReadCacheConfig,
        build_cache_key,
    )

    source = HostWorkspaceFileSource(workspace)
    resolved = source.resolve("a.txt")
    cache_cfg = WorkspaceReadCacheConfig.from_startup_config(cfg)
    key = build_cache_key(cache_cfg, CacheScope("agent", "agent-a"), "a.txt")
    store = FakeObjectCacheStore()
    store.objects[key] = {
        "data": b"cached",
        "metadata": {
            "sage-size": str(resolved.size),
            "sage-mtime-ns": str(resolved.mtime_ns),
        },
    }

    plan = asyncio.run(
        agent_service.prepare_server_agent_read_plan(
            "agent-a",
            "user-a",
            "a.txt",
            cache_store_factory=lambda _cfg: store,
        )
    )

    assert plan.source == "cache"
    assert asyncio.run(_collect(plan)) == b"cached"


def test_session_read_plan_reads_session_workspace(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, enabled=False)
    monkeypatch.setattr(config, "_GLOBAL_STARTUP_CONFIG", cfg, raising=False)
    session_workspace = tmp_path / "sessions" / "session-a"
    session_workspace.mkdir(parents=True)
    (session_workspace / "messages.json").write_bytes(b"session")

    class FakeManager:
        def get_session_workspace(self, session_id):
            assert session_id == "session-a"
            return str(session_workspace)

    monkeypatch.setattr(
        agent_service,
        "get_global_session_manager",
        lambda: FakeManager(),
        raising=False,
    )

    plan = asyncio.run(
        agent_service.prepare_session_read_plan(
            "session-a",
            "messages.json",
            cache_store_factory=lambda _cfg: FakeObjectCacheStore(),
        )
    )

    assert plan.source == "source"
    assert asyncio.run(_collect(plan)) == b"session"
