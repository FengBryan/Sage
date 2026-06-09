# Workspace GCS Read Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional GCS-backed read-through cache for client-facing agent and session workspace download/stream APIs while keeping the filesystem authoritative.

**Architecture:** Introduce a focused `common.services.workspace_read_cache` module with `FileSource`, `ObjectCacheStore`, fingerprinting, response planning, and background sync coordination. Existing routers keep their public API but ask `agent_service` for a cache-aware read plan before falling back to existing file responses. GCS is implemented behind the object-cache interface, with fake stores used for most tests.

**Tech Stack:** Python 3, FastAPI `FileResponse`/`StreamingResponse`, `google-cloud-storage`, pytest, existing `common.core.config`, existing `sagents.session_runtime` session registry.

---

## File Structure

- Create `common/services/workspace_read_cache.py`
  - Owns fingerprint calculation, host file source, cache key creation, cache hit/miss decisions, in-process sync deduplication, and fake-friendly interfaces.
- Create `common/core/client/gcs.py`
  - Owns lazy GCS client creation and `GCSObjectCacheStore`.
- Modify `common/core/config.py`
  - Adds workspace read cache settings and env parsing.
- Modify `common/services/agent_service.py`
  - Adds cache-aware read helpers for agent/session download and stream without changing list/upload/delete behavior.
- Modify `app/server/routers/agent.py`
  - Uses cache-aware read helpers for agent download.
- Modify `app/desktop/core/routers/agent.py`
  - Uses cache-aware read helpers for agent download and stream.
- Create `app/server/routers/session_workspace.py`
  - Adds server-side session download/stream routes.
- Create `app/desktop/core/routers/session_workspace.py`
  - Adds desktop-side session download/stream routes.
- Modify `app/server/routers/__init__.py`
  - Registers the server session workspace router.
- Modify `app/desktop/core/routers/__init__.py`
  - Registers the desktop session workspace router.
- Add tests:
  - `tests/common/services/test_workspace_read_cache.py`
  - `tests/common/services/test_agent_service_workspace_read_cache.py`
  - `tests/app/server/core/test_workspace_read_cache_routes.py`
  - `tests/app/desktop/core/test_workspace_read_cache_routes.py`

---

### Task 1: Add Config Flags

**Files:**
- Modify: `common/core/config.py`
- Test: `tests/common/core/test_config_security.py`

- [ ] **Step 1: Write failing config tests**

Append these tests to `tests/common/core/test_config_security.py`:

```python
def test_workspace_read_cache_defaults_disabled(monkeypatch):
    from common.core import config

    for key in (
        "SAGE_WORKSPACE_READ_CACHE_ENABLED",
        "SAGE_WORKSPACE_READ_CACHE_PROVIDER",
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
    assert cfg.workspace_read_cache_gcs_bucket is None
    assert cfg.workspace_read_cache_gcs_prefix == "workspace-cache"
    assert cfg.workspace_read_cache_gcs_credentials_json is None
    assert cfg.workspace_read_cache_sample_threshold_bytes == 3 * 1024 * 1024
    assert cfg.workspace_read_cache_sample_chunk_bytes == 512 * 1024
    assert cfg.workspace_read_cache_deployment_id == ""


def test_workspace_read_cache_env_overrides(monkeypatch):
    from common.core import config

    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_ENABLED", "true")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_PROVIDER", "gcs")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_BUCKET", "sage-cache")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_GCS_PREFIX", "custom-prefix")
    monkeypatch.setenv(
        "SAGE_WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON",
        "/secrets/gcs.json",
    )
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES", "123")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES", "45")
    monkeypatch.setenv("SAGE_WORKSPACE_READ_CACHE_DEPLOYMENT_ID", "prod-a")

    cfg = config.build_startup_config("server")

    assert cfg.workspace_read_cache_enabled is True
    assert cfg.workspace_read_cache_provider == "gcs"
    assert cfg.workspace_read_cache_gcs_bucket == "sage-cache"
    assert cfg.workspace_read_cache_gcs_prefix == "custom-prefix"
    assert cfg.workspace_read_cache_gcs_credentials_json == "/secrets/gcs.json"
    assert cfg.workspace_read_cache_sample_threshold_bytes == 123
    assert cfg.workspace_read_cache_sample_chunk_bytes == 45
    assert cfg.workspace_read_cache_deployment_id == "prod-a"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/common/core/test_config_security.py::test_workspace_read_cache_defaults_disabled tests/common/core/test_config_security.py::test_workspace_read_cache_env_overrides -q
```

Expected: FAIL with `AttributeError: 'StartupConfig' object has no attribute 'workspace_read_cache_enabled'`.

- [ ] **Step 3: Add config fields and env names**

Modify `StartupConfig` in `common/core/config.py` near the existing object storage fields:

```python
    workspace_read_cache_enabled: bool = False
    workspace_read_cache_provider: str = "gcs"
    workspace_read_cache_gcs_bucket: Optional[str] = None
    workspace_read_cache_gcs_prefix: str = "workspace-cache"
    workspace_read_cache_gcs_credentials_json: Optional[str] = None
    workspace_read_cache_sample_threshold_bytes: int = 3 * 1024 * 1024
    workspace_read_cache_sample_chunk_bytes: int = 512 * 1024
    workspace_read_cache_deployment_id: str = ""
```

Add to `ENV` near the S3 variables:

```python
    WORKSPACE_READ_CACHE_ENABLED = "SAGE_WORKSPACE_READ_CACHE_ENABLED"
    WORKSPACE_READ_CACHE_PROVIDER = "SAGE_WORKSPACE_READ_CACHE_PROVIDER"
    WORKSPACE_READ_CACHE_GCS_BUCKET = "SAGE_WORKSPACE_READ_CACHE_GCS_BUCKET"
    WORKSPACE_READ_CACHE_GCS_PREFIX = "SAGE_WORKSPACE_READ_CACHE_GCS_PREFIX"
    WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON = (
        "SAGE_WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON"
    )
    WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES = (
        "SAGE_WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES"
    )
    WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES = (
        "SAGE_WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES"
    )
    WORKSPACE_READ_CACHE_DEPLOYMENT_ID = "SAGE_WORKSPACE_READ_CACHE_DEPLOYMENT_ID"
```

- [ ] **Step 4: Parse env values in both server and desktop config builders**

In both `StartupConfig(...)` constructors inside `build_startup_config`, add:

```python
            workspace_read_cache_enabled=env_bool(
                ENV.WORKSPACE_READ_CACHE_ENABLED,
                StartupConfig.workspace_read_cache_enabled,
            ),
            workspace_read_cache_provider=env_str(
                ENV.WORKSPACE_READ_CACHE_PROVIDER,
                StartupConfig.workspace_read_cache_provider,
            )
            or StartupConfig.workspace_read_cache_provider,
            workspace_read_cache_gcs_bucket=env_str(
                ENV.WORKSPACE_READ_CACHE_GCS_BUCKET,
                StartupConfig.workspace_read_cache_gcs_bucket,
            ),
            workspace_read_cache_gcs_prefix=env_str(
                ENV.WORKSPACE_READ_CACHE_GCS_PREFIX,
                StartupConfig.workspace_read_cache_gcs_prefix,
            )
            or StartupConfig.workspace_read_cache_gcs_prefix,
            workspace_read_cache_gcs_credentials_json=env_str(
                ENV.WORKSPACE_READ_CACHE_GCS_CREDENTIALS_JSON,
                StartupConfig.workspace_read_cache_gcs_credentials_json,
            ),
            workspace_read_cache_sample_threshold_bytes=env_int(
                ENV.WORKSPACE_READ_CACHE_SAMPLE_THRESHOLD_BYTES,
                StartupConfig.workspace_read_cache_sample_threshold_bytes,
            ),
            workspace_read_cache_sample_chunk_bytes=env_int(
                ENV.WORKSPACE_READ_CACHE_SAMPLE_CHUNK_BYTES,
                StartupConfig.workspace_read_cache_sample_chunk_bytes,
            ),
            workspace_read_cache_deployment_id=env_str(
                ENV.WORKSPACE_READ_CACHE_DEPLOYMENT_ID,
                StartupConfig.workspace_read_cache_deployment_id,
            )
            or StartupConfig.workspace_read_cache_deployment_id,
```

- [ ] **Step 5: Run config tests**

Run:

```bash
pytest tests/common/core/test_config_security.py::test_workspace_read_cache_defaults_disabled tests/common/core/test_config_security.py::test_workspace_read_cache_env_overrides -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add common/core/config.py tests/common/core/test_config_security.py
git commit -m "feat: add workspace read cache config"
```

---

### Task 2: Add Fingerprinting And Host File Source

**Files:**
- Create: `common/services/workspace_read_cache.py`
- Test: `tests/common/services/test_workspace_read_cache.py`

- [ ] **Step 1: Write failing fingerprint and source tests**

Create `tests/common/services/test_workspace_read_cache.py`:

```python
import asyncio
from pathlib import Path

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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'common.services.workspace_read_cache'`.

- [ ] **Step 3: Implement core dataclasses and fingerprinting**

Create `common/services/workspace_read_cache.py` with:

```python
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import posixpath
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Protocol, Tuple

from loguru import logger

from common.core.exceptions import SageHTTPException


FULL_SHA256 = "full_sha256"
SAMPLE_SHA256 = "sample_sha256"


@dataclass(frozen=True)
class WorkspaceReadCacheConfig:
    enabled: bool
    provider: str
    bucket: Optional[str]
    prefix: str
    credentials_json: Optional[str]
    sample_threshold_bytes: int
    sample_chunk_bytes: int
    deployment_id: str

    @property
    def usable(self) -> bool:
        return (
            self.enabled
            and self.provider == "gcs"
            and bool(self.bucket)
            and self.sample_threshold_bytes > 0
            and self.sample_chunk_bytes > 0
        )


@dataclass(frozen=True)
class CacheScope:
    kind: str
    identifier: str


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    hash_kind: str
    content_hash: str


@dataclass(frozen=True)
class ResolvedFile:
    root: Path
    relative_path: str
    path: Path
    size: int
    mtime_ns: int
    filename: str
    media_type: str

    def fingerprint(
        self,
        *,
        sample_threshold_bytes: int,
        sample_chunk_bytes: int,
    ) -> FileFingerprint:
        return compute_file_fingerprint(
            self.path,
            sample_threshold_bytes=sample_threshold_bytes,
            sample_chunk_bytes=sample_chunk_bytes,
        )

    def iter_bytes(
        self,
        *,
        byte_range: Optional[Tuple[int, int]] = None,
        chunk_size: int = 64 * 1024,
    ) -> Iterable[bytes]:
        start = 0
        end = self.size - 1
        if byte_range is not None:
            start, end = byte_range
            start = max(0, start)
            end = min(self.size - 1, end)
        if self.size == 0 or start > end:
            return
        remaining = end - start + 1
        with self.path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sample_ranges(size: int, sample_chunk_bytes: int) -> list[tuple[int, int]]:
    chunk = max(1, sample_chunk_bytes)
    if size <= chunk * 3:
        return [(0, size)]
    middle_start = max(0, (size // 2) - (chunk // 2))
    ranges = [
        (0, min(chunk, size)),
        (middle_start, min(middle_start + chunk, size)),
        (max(0, size - chunk), size),
    ]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
    return merged


def _sample_sha256(path: Path, size: int, mtime_ns: int, sample_chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    ranges = _sample_ranges(size, sample_chunk_bytes)
    digest.update(f"size={size};mtime_ns={mtime_ns};".encode("utf-8"))
    with path.open("rb") as handle:
        for start, end in ranges:
            digest.update(f"range={start}-{end};".encode("utf-8"))
            handle.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


def compute_file_fingerprint(
    path: str | Path,
    *,
    sample_threshold_bytes: int,
    sample_chunk_bytes: int,
) -> FileFingerprint:
    file_path = Path(path)
    stat = file_path.stat()
    size = int(stat.st_size)
    mtime_ns = int(stat.st_mtime_ns)
    if size <= sample_threshold_bytes:
        return FileFingerprint(
            size=size,
            mtime_ns=mtime_ns,
            hash_kind=FULL_SHA256,
            content_hash=_sha256_file(file_path),
        )
    return FileFingerprint(
        size=size,
        mtime_ns=mtime_ns,
        hash_kind=SAMPLE_SHA256,
        content_hash=_sample_sha256(file_path, size, mtime_ns, sample_chunk_bytes),
    )
```

- [ ] **Step 4: Implement host source and cache key helper**

Append to `common/services/workspace_read_cache.py`:

```python
class HostWorkspaceFileSource:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def resolve(self, relative_path: str) -> ResolvedFile:
        if not relative_path:
            raise SageHTTPException(
                detail="缺少必要的路径参数",
                error_detail="file_path missing",
            )
        raw = os.fspath(relative_path).strip()
        if os.path.isabs(raw):
            candidate = Path(raw).expanduser().resolve()
        else:
            candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise SageHTTPException(
                detail="访问被拒绝：文件路径超出工作空间范围",
                error_detail="Access denied: file path outside workspace",
            )
        if not candidate.exists():
            raise SageHTTPException(
                detail=f"文件不存在: {relative_path}",
                error_detail=f"File not found: {relative_path}",
            )
        if not candidate.is_file():
            raise SageHTTPException(
                detail=f"路径不是文件: {relative_path}",
                error_detail=f"Path is not a file: {relative_path}",
            )
        stat = candidate.stat()
        rel = candidate.relative_to(self.root).as_posix()
        media_type, _ = mimetypes.guess_type(str(candidate))
        return ResolvedFile(
            root=self.root,
            relative_path=rel,
            path=candidate,
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            filename=candidate.name,
            media_type=media_type or "application/octet-stream",
        )


_SAFE_KEY_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_key_part(value: str) -> str:
    cleaned = _SAFE_KEY_PART_RE.sub("-", value.strip())
    return cleaned.strip("-") or "unknown"


def build_cache_key(
    cfg: WorkspaceReadCacheConfig,
    scope: CacheScope,
    relative_path: str,
) -> str:
    normalized_relative = posixpath.normpath(relative_path.replace("\\", "/"))
    if normalized_relative.startswith("../") or normalized_relative == "..":
        raise ValueError("relative_path must stay under the workspace")
    digest = hashlib.sha256(normalized_relative.encode("utf-8")).hexdigest()
    basename = _safe_key_part(posixpath.basename(normalized_relative) or "file")
    prefix = (cfg.prefix or "workspace-cache").strip("/")
    deployment_id = _safe_key_part(cfg.deployment_id or "default")
    scope_kind = _safe_key_part(scope.kind)
    scope_id = _safe_key_part(scope.identifier)
    return f"{prefix}/{deployment_id}/{scope_kind}/{scope_id}/{digest}/{basename}"
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add common/services/workspace_read_cache.py tests/common/services/test_workspace_read_cache.py
git commit -m "feat: add workspace read cache file source"
```

---

### Task 3: Add Cache Decision Service With Fake Store

**Files:**
- Modify: `common/services/workspace_read_cache.py`
- Test: `tests/common/services/test_workspace_read_cache.py`

- [ ] **Step 1: Add failing cache decision tests**

Append to `tests/common/services/test_workspace_read_cache.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py::test_cache_hit_uses_object_store_without_hashing tests/common/services/test_workspace_read_cache.py::test_cache_miss_returns_source_and_schedules_sync tests/common/services/test_workspace_read_cache.py::test_cache_head_failure_falls_back_to_source -q
```

Expected: FAIL with `ImportError: cannot import name 'WorkspaceReadCache'`.

- [ ] **Step 3: Implement store protocol, read plan, and cache service**

Append to `common/services/workspace_read_cache.py`:

```python
class ObjectCacheStore(Protocol):
    async def head(self, key: str) -> Optional[Dict[str, str]]: ...

    async def open_stream(
        self,
        key: str,
        byte_range: Optional[Tuple[int, int]] = None,
    ) -> Iterable[bytes]: ...

    async def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        metadata: Dict[str, str],
        content_type: str,
    ) -> None: ...

    async def update_metadata(self, key: str, metadata: Dict[str, str]) -> None: ...


class NullObjectCacheStore:
    async def head(self, key: str) -> Optional[Dict[str, str]]:
        return None

    async def open_stream(
        self,
        key: str,
        byte_range: Optional[Tuple[int, int]] = None,
    ) -> Iterable[bytes]:
        raise RuntimeError("workspace read cache is disabled")

    async def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        metadata: Dict[str, str],
        content_type: str,
    ) -> None:
        return None

    async def update_metadata(self, key: str, metadata: Dict[str, str]) -> None:
        return None


@dataclass(frozen=True)
class WorkspaceReadPlan:
    source: str
    filename: str
    media_type: str
    size: int
    mtime_ns: int
    iter_bytes: Callable[[], object]
    cache_key: Optional[str] = None


def _metadata_matches_cheap(metadata: Optional[Dict[str, str]], resolved: ResolvedFile) -> bool:
    if not metadata:
        return False
    return (
        metadata.get("sage-size") == str(resolved.size)
        and metadata.get("sage-mtime-ns") == str(resolved.mtime_ns)
    )


def _metadata_matches_strong(
    metadata: Optional[Dict[str, str]],
    fingerprint: FileFingerprint,
) -> bool:
    if not metadata:
        return False
    return (
        metadata.get("sage-size") == str(fingerprint.size)
        and metadata.get("sage-mtime-ns") == str(fingerprint.mtime_ns)
        and metadata.get("sage-hash-kind") == fingerprint.hash_kind
        and metadata.get("sage-content-hash") == fingerprint.content_hash
    )


def _metadata_for(
    scope: CacheScope,
    resolved: ResolvedFile,
    fingerprint: FileFingerprint,
) -> Dict[str, str]:
    return {
        "sage-size": str(fingerprint.size),
        "sage-mtime-ns": str(fingerprint.mtime_ns),
        "sage-hash-kind": fingerprint.hash_kind,
        "sage-content-hash": fingerprint.content_hash,
        "sage-source-id": f"{scope.kind}:{scope.identifier}",
        "sage-relative-path": resolved.relative_path,
    }


class WorkspaceReadCache:
    def __init__(self, cfg: WorkspaceReadCacheConfig, store: ObjectCacheStore):
        self.cfg = cfg
        self.store = store
        self._sync_tasks: Dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()

    async def plan_read(
        self,
        *,
        scope: CacheScope,
        source: HostWorkspaceFileSource,
        relative_path: str,
        byte_range: Optional[Tuple[int, int]] = None,
    ) -> WorkspaceReadPlan:
        resolved = source.resolve(relative_path)
        if not self.cfg.usable:
            return self._source_plan(resolved, byte_range=byte_range)

        key = build_cache_key(self.cfg, scope, resolved.relative_path)
        metadata = None
        try:
            metadata = await self.store.head(key)
        except Exception as exc:
            logger.warning(f"workspace read cache head failed key={key}: {exc}")
            self._schedule_sync(key, scope, resolved)
            return self._source_plan(resolved, byte_range=byte_range, cache_key=key)

        if _metadata_matches_cheap(metadata, resolved):
            cache_plan = await self._cache_plan(
                key,
                resolved,
                byte_range=byte_range,
            )
            if cache_plan is not None:
                return cache_plan
            self._schedule_sync(key, scope, resolved)
            return self._source_plan(resolved, byte_range=byte_range, cache_key=key)

        fingerprint = resolved.fingerprint(
            sample_threshold_bytes=self.cfg.sample_threshold_bytes,
            sample_chunk_bytes=self.cfg.sample_chunk_bytes,
        )
        if _metadata_matches_strong(metadata, fingerprint):
            cache_plan = await self._cache_plan(
                key,
                resolved,
                byte_range=byte_range,
            )
            if cache_plan is not None:
                if not _metadata_matches_cheap(metadata, resolved):
                    self._schedule_metadata_repair(key, scope, resolved, fingerprint)
                return cache_plan

        self._schedule_sync(key, scope, resolved, fingerprint=fingerprint)
        return self._source_plan(resolved, byte_range=byte_range, cache_key=key)

    def _source_plan(
        self,
        resolved: ResolvedFile,
        *,
        byte_range: Optional[Tuple[int, int]],
        cache_key: Optional[str] = None,
    ) -> WorkspaceReadPlan:
        size = _range_size(resolved.size, byte_range)

        async def chunks():
            for chunk in resolved.iter_bytes(byte_range=byte_range):
                yield chunk

        return WorkspaceReadPlan(
            source="source",
            filename=resolved.filename,
            media_type=resolved.media_type,
            size=size,
            mtime_ns=resolved.mtime_ns,
            iter_bytes=chunks,
            cache_key=cache_key,
        )

    async def _cache_plan(
        self,
        key: str,
        resolved: ResolvedFile,
        *,
        byte_range: Optional[Tuple[int, int]],
    ) -> Optional[WorkspaceReadPlan]:
        try:
            stream = await self.store.open_stream(key, byte_range=byte_range)
        except Exception as exc:
            logger.warning(f"workspace read cache get failed key={key}: {exc}")
            return None

        async def chunks():
            for chunk in stream:
                yield chunk

        return WorkspaceReadPlan(
            source="cache",
            filename=resolved.filename,
            media_type=resolved.media_type,
            size=_range_size(resolved.size, byte_range),
            mtime_ns=resolved.mtime_ns,
            iter_bytes=chunks,
            cache_key=key,
        )

    def _schedule_sync(
        self,
        key: str,
        scope: CacheScope,
        resolved: ResolvedFile,
        fingerprint: Optional[FileFingerprint] = None,
    ) -> None:
        with self._lock:
            task = self._sync_tasks.get(key)
            if task and not task.done():
                return
            loop = asyncio.get_running_loop()
            self._sync_tasks[key] = loop.create_task(
                self._sync_object(key, scope, resolved, fingerprint)
            )

    def _schedule_metadata_repair(
        self,
        key: str,
        scope: CacheScope,
        resolved: ResolvedFile,
        fingerprint: FileFingerprint,
    ) -> None:
        with self._lock:
            task_key = f"{key}:metadata"
            task = self._sync_tasks.get(task_key)
            if task and not task.done():
                return
            loop = asyncio.get_running_loop()
            self._sync_tasks[task_key] = loop.create_task(
                self._repair_metadata(key, scope, resolved, fingerprint)
            )

    async def _sync_object(
        self,
        key: str,
        scope: CacheScope,
        resolved: ResolvedFile,
        fingerprint: Optional[FileFingerprint],
    ) -> None:
        try:
            fp = fingerprint or resolved.fingerprint(
                sample_threshold_bytes=self.cfg.sample_threshold_bytes,
                sample_chunk_bytes=self.cfg.sample_chunk_bytes,
            )
            metadata = _metadata_for(scope, resolved, fp)
            await self.store.put_stream(
                key,
                resolved.iter_bytes(),
                metadata,
                resolved.media_type,
            )
        except Exception as exc:
            logger.warning(f"workspace read cache sync failed key={key}: {exc}")

    async def _repair_metadata(
        self,
        key: str,
        scope: CacheScope,
        resolved: ResolvedFile,
        fingerprint: FileFingerprint,
    ) -> None:
        try:
            await self.store.update_metadata(
                key,
                _metadata_for(scope, resolved, fingerprint),
            )
        except Exception as exc:
            logger.warning(f"workspace read cache metadata repair failed key={key}: {exc}")

    async def wait_for_pending_syncs(self) -> None:
        tasks = [task for task in self._sync_tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _range_size(total_size: int, byte_range: Optional[Tuple[int, int]]) -> int:
    if byte_range is None:
        return total_size
    start, end = byte_range
    if total_size <= 0:
        return 0
    start = max(0, start)
    end = min(total_size - 1, end)
    if start > end:
        return 0
    return end - start + 1
```

- [ ] **Step 4: Run cache-hit test**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py::test_cache_hit_uses_object_store_without_hashing -q
```

Expected: PASS, and the fake store should show one `open_stream` call for one response body read.

- [ ] **Step 5: Run cache tests**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add common/services/workspace_read_cache.py tests/common/services/test_workspace_read_cache.py
git commit -m "feat: add workspace read cache planner"
```

---

### Task 4: Add GCS Object Cache Store

**Files:**
- Create: `common/core/client/gcs.py`
- Modify: `requirements.txt`
- Test: `tests/common/services/test_workspace_read_cache.py`

- [ ] **Step 1: Write failing GCS import/config tests**

Append to `tests/common/services/test_workspace_read_cache.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py::test_gcs_store_imports_without_google_library -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'common.core.client.gcs'`.

- [ ] **Step 3: Add dependency**

Add this line to `requirements.txt`:

```text
google-cloud-storage>=2.16.0
```

- [ ] **Step 4: Implement GCS provider module**

Create `common/core/client/gcs.py`:

```python
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from loguru import logger


class GCSUnavailableError(RuntimeError):
    pass


def _load_storage_client(credentials_json: Optional[str]):
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise GCSUnavailableError("google-cloud-storage is not installed") from exc

    if credentials_json:
        return storage.Client.from_service_account_json(credentials_json)
    return storage.Client()


class GCSObjectCacheStore:
    def __init__(self, bucket_name: str, credentials_json: Optional[str] = None):
        self.bucket_name = bucket_name
        self.credentials_json = credentials_json
        self._client = None
        self._bucket = None

    def _bucket_sync(self):
        if self._bucket is None:
            self._client = _load_storage_client(self.credentials_json)
            self._bucket = self._client.bucket(self.bucket_name)
        return self._bucket

    async def head(self, key: str) -> Optional[Dict[str, str]]:
        return await asyncio.to_thread(self._head_sync, key)

    def _head_sync(self, key: str) -> Optional[Dict[str, str]]:
        blob = self._bucket_sync().blob(key)
        if not blob.exists():
            return None
        blob.reload()
        return dict(blob.metadata or {})

    async def open_stream(
        self,
        key: str,
        byte_range: Optional[Tuple[int, int]] = None,
    ) -> Iterable[bytes]:
        return await asyncio.to_thread(self._open_stream_sync, key, byte_range)

    def _open_stream_sync(
        self,
        key: str,
        byte_range: Optional[Tuple[int, int]],
    ) -> Iterable[bytes]:
        blob = self._bucket_sync().blob(key)
        if byte_range is None:
            data = blob.download_as_bytes()
        else:
            start, end = byte_range
            data = blob.download_as_bytes(start=start, end=end)
        return [data]

    async def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        metadata: Dict[str, str],
        content_type: str,
    ) -> None:
        await asyncio.to_thread(self._put_stream_sync, key, chunks, metadata, content_type)

    def _put_stream_sync(
        self,
        key: str,
        chunks: Iterable[bytes],
        metadata: Dict[str, str],
        content_type: str,
    ) -> None:
        bucket = self._bucket_sync()
        blob = bucket.blob(key)
        blob.metadata = metadata
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            tmp_path = Path(handle.name)
            for chunk in chunks:
                handle.write(chunk)
        try:
            blob.upload_from_filename(str(tmp_path), content_type=content_type)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.debug(f"failed to delete temp GCS upload file {tmp_path}: {exc}")

    async def update_metadata(self, key: str, metadata: Dict[str, str]) -> None:
        await asyncio.to_thread(self._update_metadata_sync, key, metadata)

    def _update_metadata_sync(self, key: str, metadata: Dict[str, str]) -> None:
        blob = self._bucket_sync().blob(key)
        blob.reload()
        blob.metadata = metadata
        blob.patch()
```

- [ ] **Step 5: Run import test**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py::test_gcs_store_imports_without_google_library -q
```

Expected: PASS.

- [ ] **Step 6: Run all cache tests**

Run:

```bash
pytest tests/common/services/test_workspace_read_cache.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add common/core/client/gcs.py requirements.txt tests/common/services/test_workspace_read_cache.py
git commit -m "feat: add gcs workspace cache store"
```

---

### Task 5: Add Agent Service Cache-Aware Read Helpers

**Files:**
- Modify: `common/services/agent_service.py`
- Modify: `common/services/workspace_read_cache.py`
- Test: `tests/common/services/test_agent_service_workspace_read_cache.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/common/services/test_agent_service_workspace_read_cache.py`:

```python
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
        workspace_read_cache_gcs_bucket="bucket",
        workspace_read_cache_gcs_prefix="workspace-cache",
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_read_cache.py -q
```

Expected: FAIL with `AttributeError: module 'common.services.agent_service' has no attribute 'prepare_server_agent_read_plan'`.

- [ ] **Step 3: Add config conversion and default cache factory**

Append this classmethod to `WorkspaceReadCacheConfig` in `common/services/workspace_read_cache.py`:

```python
    @classmethod
    def from_startup_config(cls, cfg) -> "WorkspaceReadCacheConfig":
        return cls(
            enabled=bool(getattr(cfg, "workspace_read_cache_enabled", False)),
            provider=getattr(cfg, "workspace_read_cache_provider", "gcs") or "gcs",
            bucket=getattr(cfg, "workspace_read_cache_gcs_bucket", None),
            prefix=getattr(
                cfg,
                "workspace_read_cache_gcs_prefix",
                "workspace-cache",
            )
            or "workspace-cache",
            credentials_json=getattr(
                cfg,
                "workspace_read_cache_gcs_credentials_json",
                None,
            ),
            sample_threshold_bytes=int(
                getattr(
                    cfg,
                    "workspace_read_cache_sample_threshold_bytes",
                    3 * 1024 * 1024,
                )
            ),
            sample_chunk_bytes=int(
                getattr(cfg, "workspace_read_cache_sample_chunk_bytes", 512 * 1024)
            ),
            deployment_id=getattr(cfg, "workspace_read_cache_deployment_id", "") or "",
        )
```

Add to `common/services/agent_service.py` imports:

```python
from common.services.workspace_read_cache import (
    CacheScope,
    HostWorkspaceFileSource,
    NullObjectCacheStore,
    ObjectCacheStore,
    WorkspaceReadCache,
    WorkspaceReadCacheConfig,
    WorkspaceReadPlan,
)
```

Then add helpers near existing workspace helpers:

```python
def _default_workspace_cache_store(cache_cfg: WorkspaceReadCacheConfig) -> ObjectCacheStore:
    from common.core.client.gcs import GCSObjectCacheStore

    if not cache_cfg.bucket:
        raise RuntimeError("workspace read cache bucket is not configured")
    return GCSObjectCacheStore(
        cache_cfg.bucket,
        credentials_json=cache_cfg.credentials_json,
    )


def _workspace_read_cache_config() -> WorkspaceReadCacheConfig:
    cfg = config.get_startup_config()
    if cfg is None:
        return WorkspaceReadCacheConfig(
            enabled=False,
            provider="gcs",
            bucket=None,
            prefix="workspace-cache",
            credentials_json=None,
            sample_threshold_bytes=3 * 1024 * 1024,
            sample_chunk_bytes=512 * 1024,
            deployment_id="",
        )
    return WorkspaceReadCacheConfig.from_startup_config(cfg)


async def _prepare_workspace_read_plan(
    *,
    workspace_path: str | Path,
    scope: CacheScope,
    file_path: str,
    byte_range: Optional[Tuple[int, int]] = None,
    cache_store_factory=_default_workspace_cache_store,
) -> WorkspaceReadPlan:
    cache_cfg = _workspace_read_cache_config()
    source = HostWorkspaceFileSource(workspace_path)
    if not cache_cfg.usable:
        disabled = WorkspaceReadCacheConfig(
            enabled=False,
            provider=cache_cfg.provider,
            bucket=cache_cfg.bucket,
            prefix=cache_cfg.prefix,
            credentials_json=cache_cfg.credentials_json,
            sample_threshold_bytes=cache_cfg.sample_threshold_bytes,
            sample_chunk_bytes=cache_cfg.sample_chunk_bytes,
            deployment_id=cache_cfg.deployment_id,
        )
        return await WorkspaceReadCache(disabled, NullObjectCacheStore()).plan_read(
            scope=scope,
            source=source,
            relative_path=file_path,
            byte_range=byte_range,
        )
    try:
        store = cache_store_factory(cache_cfg)
    except Exception as exc:
        logger.warning(f"workspace read cache store unavailable: {exc}")
        disabled = WorkspaceReadCacheConfig(
            enabled=False,
            provider=cache_cfg.provider,
            bucket=cache_cfg.bucket,
            prefix=cache_cfg.prefix,
            credentials_json=cache_cfg.credentials_json,
            sample_threshold_bytes=cache_cfg.sample_threshold_bytes,
            sample_chunk_bytes=cache_cfg.sample_chunk_bytes,
            deployment_id=cache_cfg.deployment_id,
        )
        return await WorkspaceReadCache(disabled, NullObjectCacheStore()).plan_read(
            scope=scope,
            source=source,
            relative_path=file_path,
            byte_range=byte_range,
        )
    return await WorkspaceReadCache(cache_cfg, store).plan_read(
        scope=scope,
        source=source,
        relative_path=file_path,
        byte_range=byte_range,
    )
```

- [ ] **Step 4: Add public agent read helpers**

Add to `common/services/agent_service.py`:

```python
async def prepare_server_agent_read_plan(
    agent_id: str,
    user_id: str,
    file_path: str,
    *,
    byte_range: Optional[Tuple[int, int]] = None,
    cache_store_factory=_default_workspace_cache_store,
) -> WorkspaceReadPlan:
    return await _prepare_workspace_read_plan(
        workspace_path=get_server_agent_workspace_path(agent_id, user_id),
        scope=CacheScope("agent", agent_id),
        file_path=file_path,
        byte_range=byte_range,
        cache_store_factory=cache_store_factory,
    )


async def prepare_desktop_agent_read_plan(
    agent_id: str,
    file_path: str,
    *,
    byte_range: Optional[Tuple[int, int]] = None,
    cache_store_factory=_default_workspace_cache_store,
) -> WorkspaceReadPlan:
    return await _prepare_workspace_read_plan(
        workspace_path=get_desktop_agent_workspace_path(agent_id),
        scope=CacheScope("agent", agent_id),
        file_path=file_path,
        byte_range=byte_range,
        cache_store_factory=cache_store_factory,
    )
```

- [ ] **Step 5: Run service tests**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_read_cache.py -q
```

Expected: PASS.

- [ ] **Step 6: Run prior workspace tests**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_listing.py tests/common/services/test_agent_service_workspace_upload.py tests/common/services/test_agent_service_workspace_delete.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add common/services/agent_service.py common/services/workspace_read_cache.py tests/common/services/test_agent_service_workspace_read_cache.py
git commit -m "feat: add cache aware workspace read plans"
```

---

### Task 6: Integrate Agent Download And Stream Routes

**Files:**
- Modify: `app/server/routers/agent.py`
- Modify: `app/desktop/core/routers/agent.py`
- Test: `tests/app/server/core/test_workspace_read_cache_routes.py`
- Test: `tests/app/desktop/core/test_workspace_read_cache_routes.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/app/server/core/test_workspace_read_cache_routes.py`:

```python
import asyncio
from dataclasses import dataclass

from starlette.requests import Request

from app.server.routers import agent as server_agent_router


@dataclass
class FakePlan:
    source: str = "source"
    filename: str = "a.txt"
    media_type: str = "text/plain"
    size: int = 6
    mtime_ns: int = 1

    async def iter_bytes(self):
        yield b"source"


def _request(path="/", user_id="user-a", role="user"):
    req = Request({"type": "http", "method": "GET", "path": path, "headers": []})
    req.state.user_claims = {"userid": user_id, "role": role}
    return req


def test_server_download_uses_read_plan(monkeypatch):
    calls = {}

    async def fake_prepare(agent_id, user_id, file_path):
        calls.update({"agent_id": agent_id, "user_id": user_id, "file_path": file_path})
        return FakePlan()

    monkeypatch.setattr(
        server_agent_router.agent_service,
        "prepare_server_agent_read_plan",
        fake_prepare,
    )
    request = _request("/?file_path=a.txt")

    response = asyncio.run(server_agent_router.download_file("agent-a", request))

    assert response.media_type == "text/plain"
    assert calls == {
        "agent_id": "agent-a",
        "user_id": "user-a",
        "file_path": "a.txt",
    }
```

Create `tests/app/desktop/core/test_workspace_read_cache_routes.py`:

```python
import asyncio
from dataclasses import dataclass

from starlette.requests import Request

from app.desktop.core.routers import agent as desktop_agent_router


@dataclass
class FakePlan:
    source: str = "cache"
    filename: str = "video.mp4"
    media_type: str = "video/mp4"
    size: int = 10
    mtime_ns: int = 1

    async def iter_bytes(self):
        yield b"0123456789"


def _request(path="/"):
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def test_desktop_download_uses_read_plan(monkeypatch):
    calls = {}

    async def fake_prepare(agent_id, file_path):
        calls.update({"agent_id": agent_id, "file_path": file_path})
        return FakePlan(filename="a.txt", media_type="text/plain", size=6)

    monkeypatch.setattr(
        desktop_agent_router.agent_service,
        "prepare_desktop_agent_read_plan",
        fake_prepare,
    )

    response = asyncio.run(
        desktop_agent_router.download_file("agent-a", _request("/?file_path=a.txt"))
    )

    assert response.media_type == "text/plain"
    assert calls == {"agent_id": "agent-a", "file_path": "a.txt"}


def test_desktop_stream_passes_range_to_read_plan(monkeypatch):
    calls = {}

    async def fake_prepare(agent_id, file_path, *, byte_range=None):
        calls.update(
            {
                "agent_id": agent_id,
                "file_path": file_path,
                "byte_range": byte_range,
            }
        )
        return FakePlan(size=4)

    monkeypatch.setattr(
        desktop_agent_router.agent_service,
        "prepare_desktop_agent_read_plan",
        fake_prepare,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/?file_path=video.mp4",
            "headers": [(b"range", b"bytes=2-5")],
        }
    )

    response = asyncio.run(desktop_agent_router.stream_file("agent-a", request))

    assert response.status_code == 206
    assert calls["byte_range"] == (2, 5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/app/server/core/test_workspace_read_cache_routes.py tests/app/desktop/core/test_workspace_read_cache_routes.py -q
```

Expected: FAIL because routers still call `download_*_agent_file`.

- [ ] **Step 3: Add response helper to routers**

In both `app/server/routers/agent.py` and `app/desktop/core/routers/agent.py`, import `StreamingResponse` where missing and add this helper near the router setup:

```python
def _streaming_response_from_plan(plan, *, status_code: int = 200, headers=None):
    response_headers = {
        "Content-Length": str(plan.size),
        "Content-Disposition": f'inline; filename="{plan.filename}"',
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(
        plan.iter_bytes(),
        status_code=status_code,
        headers=response_headers,
        media_type=plan.media_type,
    )
```

- [ ] **Step 4: Update server download route**

Replace the successful body in `app/server/routers/agent.py::download_file` with:

```python
        plan = await agent_service.prepare_server_agent_read_plan(
            agent_id,
            user_id,
            file_path,  # pyright: ignore[reportArgumentType]
        )
        return _streaming_response_from_plan(plan)
```

Keep the existing `try/except` logging wrapper.

- [ ] **Step 5: Update desktop download and stream routes**

Replace the successful body in `app/desktop/core/routers/agent.py::download_file` with:

```python
        plan = await agent_service.prepare_desktop_agent_read_plan(
            agent_id,
            file_path,  # pyright: ignore[reportArgumentType]
        )
        return _streaming_response_from_plan(plan)
```

Replace the successful body in `app/desktop/core/routers/agent.py::stream_file` with:

```python
        initial_plan = await agent_service.prepare_desktop_agent_read_plan(
            agent_id,
            file_path,  # pyright: ignore[reportArgumentType]
        )
        file_size = initial_plan.size
        range_header = request.headers.get("range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else file_size - 1
                end = min(end, file_size - 1)
                plan = await agent_service.prepare_desktop_agent_read_plan(
                    agent_id,
                    file_path,  # pyright: ignore[reportArgumentType]
                    byte_range=(start, end),
                )
                return _streaming_response_from_plan(
                    plan,
                    status_code=206,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                )
        return _streaming_response_from_plan(
            initial_plan,
            headers={"Accept-Ranges": "bytes"},
        )
```

- [ ] **Step 6: Run route tests**

Run:

```bash
pytest tests/app/server/core/test_workspace_read_cache_routes.py tests/app/desktop/core/test_workspace_read_cache_routes.py -q
```

Expected: PASS.

- [ ] **Step 7: Run existing workspace route tests**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_listing.py tests/common/services/test_agent_service_workspace_upload.py tests/common/services/test_agent_service_workspace_delete.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/server/routers/agent.py app/desktop/core/routers/agent.py tests/app/server/core/test_workspace_read_cache_routes.py tests/app/desktop/core/test_workspace_read_cache_routes.py
git commit -m "feat: route agent workspace reads through cache"
```

---

### Task 7: Add Session Workspace Read Helpers And Routes

**Files:**
- Modify: `common/services/agent_service.py`
- Create: `app/server/routers/session_workspace.py`
- Create: `app/desktop/core/routers/session_workspace.py`
- Modify: `app/server/routers/__init__.py`
- Modify: `app/desktop/core/routers/__init__.py`
- Test: `tests/common/services/test_agent_service_workspace_read_cache.py`
- Test: `tests/app/server/core/test_workspace_read_cache_routes.py`
- Test: `tests/app/desktop/core/test_workspace_read_cache_routes.py`

- [ ] **Step 1: Add failing service test for session source**

Append to `tests/common/services/test_agent_service_workspace_read_cache.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_read_cache.py::test_session_read_plan_reads_session_workspace -q
```

Expected: FAIL with `AttributeError: module 'common.services.agent_service' has no attribute 'prepare_session_read_plan'`.

- [ ] **Step 3: Implement session read helper**

Add import to `common/services/agent_service.py`:

```python
from sagents.session_runtime import get_global_session_manager
```

Add helper:

```python
def get_session_workspace_path(session_id: str) -> str:
    manager = get_global_session_manager()
    workspace = manager.get_session_workspace(session_id) if manager else None
    if not workspace:
        raise SageHTTPException(
            detail=f"Session 工作空间不存在: {session_id}",
            error_detail=f"Session workspace not found: {session_id}",
        )
    return workspace


async def prepare_session_read_plan(
    session_id: str,
    file_path: str,
    *,
    byte_range: Optional[Tuple[int, int]] = None,
    cache_store_factory=_default_workspace_cache_store,
) -> WorkspaceReadPlan:
    return await _prepare_workspace_read_plan(
        workspace_path=get_session_workspace_path(session_id),
        scope=CacheScope("session", session_id),
        file_path=file_path,
        byte_range=byte_range,
        cache_store_factory=cache_store_factory,
    )
```

- [ ] **Step 4: Run session service test**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_read_cache.py::test_session_read_plan_reads_session_workspace -q
```

Expected: PASS.

- [ ] **Step 5: Add failing server and desktop session route tests**

Append to `tests/app/server/core/test_workspace_read_cache_routes.py`:

```python
def test_server_session_download_route_uses_session_plan(monkeypatch):
    from app.server.routers import session_workspace

    calls = {}

    async def fake_prepare(session_id, file_path):
        calls.update({"session_id": session_id, "file_path": file_path})
        return FakePlan()

    monkeypatch.setattr(
        session_workspace.agent_service,
        "prepare_session_read_plan",
        fake_prepare,
    )

    response = asyncio.run(
        session_workspace.download_session_file(
            "session-a",
            _request("/?file_path=messages.json"),
        )
    )

    assert response.media_type == "text/plain"
    assert calls == {"session_id": "session-a", "file_path": "messages.json"}
```

Append to `tests/app/desktop/core/test_workspace_read_cache_routes.py`:

```python
def test_desktop_session_stream_route_uses_session_plan(monkeypatch):
    from app.desktop.core.routers import session_workspace

    calls = {}

    async def fake_prepare(session_id, file_path, *, byte_range=None):
        calls.update(
            {
                "session_id": session_id,
                "file_path": file_path,
                "byte_range": byte_range,
            }
        )
        return FakePlan(size=4)

    monkeypatch.setattr(
        session_workspace.agent_service,
        "prepare_session_read_plan",
        fake_prepare,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/?file_path=messages.json",
            "headers": [(b"range", b"bytes=1-3")],
        }
    )

    response = asyncio.run(
        session_workspace.stream_session_file("session-a", request)
    )

    assert response.status_code == 206
    assert calls == {
        "session_id": "session-a",
        "file_path": "messages.json",
        "byte_range": (1, 3),
    }
```

- [ ] **Step 6: Run session route tests to verify they fail**

Run:

```bash
pytest tests/app/server/core/test_workspace_read_cache_routes.py::test_server_session_download_route_uses_session_plan tests/app/desktop/core/test_workspace_read_cache_routes.py::test_desktop_session_stream_route_uses_session_plan -q
```

Expected: FAIL with import error for `session_workspace`.

- [ ] **Step 7: Create server session workspace router**

Create `app/server/routers/session_workspace.py`:

```python
import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from common.services import agent_service

session_workspace_router = APIRouter(prefix="/api/sessions", tags=["Session Workspace"])


def _streaming_response_from_plan(plan, *, status_code: int = 200, headers=None):
    response_headers = {
        "Content-Length": str(plan.size),
        "Content-Disposition": f'inline; filename="{plan.filename}"',
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(
        plan.iter_bytes(),
        status_code=status_code,
        headers=response_headers,
        media_type=plan.media_type,
    )


@session_workspace_router.get("/{session_id}/file_workspace/download")
async def download_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    return _streaming_response_from_plan(plan)


@session_workspace_router.get("/{session_id}/file_workspace/stream")
async def stream_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    initial_plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    file_size = initial_plan.size
    range_header = request.headers.get("range")
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            plan = await agent_service.prepare_session_read_plan(
                session_id,
                file_path,  # pyright: ignore[reportArgumentType]
                byte_range=(start, end),
            )
            return _streaming_response_from_plan(
                plan,
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                },
            )
    return _streaming_response_from_plan(
        initial_plan,
        headers={"Accept-Ranges": "bytes"},
    )
```

- [ ] **Step 8: Create desktop session workspace router**

Create `app/desktop/core/routers/session_workspace.py`:

```python
import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from common.services import agent_service

session_workspace_router = APIRouter(prefix="/api/sessions", tags=["Session Workspace"])


def _streaming_response_from_plan(plan, *, status_code: int = 200, headers=None):
    response_headers = {
        "Content-Length": str(plan.size),
        "Content-Disposition": f'inline; filename="{plan.filename}"',
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(
        plan.iter_bytes(),
        status_code=status_code,
        headers=response_headers,
        media_type=plan.media_type,
    )


@session_workspace_router.get("/{session_id}/file_workspace/download")
async def download_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    return _streaming_response_from_plan(plan)


@session_workspace_router.get("/{session_id}/file_workspace/stream")
async def stream_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    initial_plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    file_size = initial_plan.size
    range_header = request.headers.get("range")
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            plan = await agent_service.prepare_session_read_plan(
                session_id,
                file_path,  # pyright: ignore[reportArgumentType]
                byte_range=(start, end),
            )
            return _streaming_response_from_plan(
                plan,
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                },
            )
    return _streaming_response_from_plan(
        initial_plan,
        headers={"Accept-Ranges": "bytes"},
    )
```

- [ ] **Step 9: Register new routers**

In `app/server/routers/__init__.py`, add this import inside `register_routes` near the other router imports:

```python
from app.server.routers.session_workspace import session_workspace_router
```

Then include it immediately after `agent_router`:

```python
app.include_router(session_workspace_router)
```

In `app/desktop/core/routers/__init__.py`, add this top-level import near the other router imports:

```python
from app.desktop.core.routers.session_workspace import session_workspace_router
```

Then include it immediately after `agent_router`:

```python
app.include_router(session_workspace_router)
```

- [ ] **Step 10: Run session tests**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_read_cache.py::test_session_read_plan_reads_session_workspace tests/app/server/core/test_workspace_read_cache_routes.py::test_server_session_download_route_uses_session_plan tests/app/desktop/core/test_workspace_read_cache_routes.py::test_desktop_session_stream_route_uses_session_plan -q
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add common/services/agent_service.py app/server/routers/session_workspace.py app/desktop/core/routers/session_workspace.py app/server/routers/__init__.py app/desktop/core/routers/__init__.py tests/common/services/test_agent_service_workspace_read_cache.py tests/app/server/core/test_workspace_read_cache_routes.py tests/app/desktop/core/test_workspace_read_cache_routes.py
git commit -m "feat: add session workspace read routes"
```

---

### Task 8: Final Verification And Documentation

**Files:**
- Modify: `docs/en/api/HTTP_API_REFERENCE.md`
- Modify: `docs/zh/api/HTTP_API_REFERENCE.md`
- Test: existing suites

- [ ] **Step 1: Update API docs**

In `docs/en/api/HTTP_API_REFERENCE.md`, add session workspace read rows near the existing workspace routes:

```markdown
| GET | `/api/sessions/{session_id}/file_workspace/download` | Query: `file_path` | file stream | Download a file from a session workspace |
| GET | `/api/sessions/{session_id}/file_workspace/stream` | Query: `file_path`; Header: `Range?` | file stream | Stream a file from a session workspace with Range support |
```

In `docs/zh/api/HTTP_API_REFERENCE.md`, add:

```markdown
| GET | `/api/sessions/{session_id}/file_workspace/download` | Query: `file_path` | 文件流 | 下载 session workspace 中的文件 |
| GET | `/api/sessions/{session_id}/file_workspace/stream` | Query: `file_path`；Header: `Range?` | 文件流 | 支持 Range 的 session workspace 文件流式读取 |
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
pytest tests/common/core/test_config_security.py::test_workspace_read_cache_defaults_disabled tests/common/core/test_config_security.py::test_workspace_read_cache_env_overrides tests/common/services/test_workspace_read_cache.py tests/common/services/test_agent_service_workspace_read_cache.py tests/app/server/core/test_workspace_read_cache_routes.py tests/app/desktop/core/test_workspace_read_cache_routes.py -q
```

Expected: PASS.

- [ ] **Step 3: Run regression workspace tests**

Run:

```bash
pytest tests/common/services/test_agent_service_workspace_listing.py tests/common/services/test_agent_service_workspace_upload.py tests/common/services/test_agent_service_workspace_delete.py -q
```

Expected: PASS.

- [ ] **Step 4: Run import/static sanity**

Run:

```bash
python -m py_compile common/services/workspace_read_cache.py common/core/client/gcs.py common/services/agent_service.py app/server/routers/agent.py app/desktop/core/routers/agent.py app/server/routers/session_workspace.py app/desktop/core/routers/session_workspace.py
```

Expected: command exits 0.

- [ ] **Step 5: Commit docs and final verification fixes**

```bash
git add docs/en/api/HTTP_API_REFERENCE.md docs/zh/api/HTTP_API_REFERENCE.md
git commit -m "docs: document session workspace read APIs"
```

- [ ] **Step 6: Final status check**

Run:

```bash
git status --short
```

Expected: only unrelated pre-existing files, such as `.superpowers/`, remain untracked or modified.
