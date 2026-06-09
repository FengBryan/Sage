from __future__ import annotations

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

import asyncio
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
    s3_endpoint: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_secure: bool = False

    @classmethod
    def from_startup_config(cls, cfg) -> "WorkspaceReadCacheConfig":
        provider = (
            getattr(cfg, "workspace_read_cache_provider", "gcs") or "gcs"
        ).strip().lower()
        bucket = (
            getattr(cfg, "s3_bucket_name", None)
            if provider == "s3"
            else (
                getattr(cfg, "gcs_bucket_name", None)
                or getattr(cfg, "workspace_read_cache_gcs_bucket", None)
            )
        )
        return cls(
            enabled=bool(getattr(cfg, "workspace_read_cache_enabled", False)),
            provider=provider,
            bucket=bucket,
            prefix=getattr(
                cfg,
                "workspace_read_cache_prefix",
                None,
            )
            or getattr(
                cfg,
                "workspace_read_cache_gcs_prefix",
                "workspace-cache",
            )
            or "workspace-cache",
            credentials_json=getattr(
                cfg,
                "gcs_credentials_json",
                None,
            )
            or getattr(
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
            s3_endpoint=getattr(cfg, "s3_endpoint", None),
            s3_access_key=getattr(cfg, "s3_access_key", None),
            s3_secret_key=getattr(cfg, "s3_secret_key", None),
            s3_secure=bool(getattr(cfg, "s3_secure", False)),
        )

    @property
    def usable(self) -> bool:
        return (
            self.enabled
            and self.provider in {"gcs", "s3"}
            and bool(self.bucket)
            and (
                self.provider == "gcs"
                or bool(self.s3_endpoint and self.s3_access_key and self.s3_secret_key)
            )
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


def _metadata_matches_cheap(
    metadata: Optional[Dict[str, str]],
    resolved: ResolvedFile,
) -> bool:
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
