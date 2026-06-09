from __future__ import annotations

import asyncio
import io
from typing import Dict, Iterable, Optional, Tuple


class S3UnavailableError(RuntimeError):
    pass


def _load_minio_client(endpoint: str, access_key: str, secret_key: str, secure: bool):
    try:
        from minio import Minio
    except ImportError as exc:
        raise S3UnavailableError("minio is not installed") from exc

    normalized_endpoint = endpoint
    if normalized_endpoint.startswith("http://"):
        normalized_endpoint = normalized_endpoint[7:]
    elif normalized_endpoint.startswith("https://"):
        normalized_endpoint = normalized_endpoint[8:]
    return Minio(
        normalized_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def _plain_metadata(metadata: Optional[Dict[str, str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in (metadata or {}).items():
        normalized_key = key
        lower_key = key.lower()
        if lower_key.startswith("x-amz-meta-"):
            normalized_key = key[len("x-amz-meta-") :]
        result[normalized_key] = value
    return result


class S3ObjectCacheStore:
    def __init__(
        self,
        bucket_name: str,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = False,
    ):
        self.bucket_name = bucket_name
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.secure = secure
        self._client = None

    def _client_sync(self):
        if self._client is None:
            self._client = _load_minio_client(
                self.endpoint,
                self.access_key,
                self.secret_key,
                self.secure,
            )
        return self._client

    async def head(self, key: str) -> Optional[Dict[str, str]]:
        return await asyncio.to_thread(self._head_sync, key)

    def _head_sync(self, key: str) -> Optional[Dict[str, str]]:
        try:
            stat = self._client_sync().stat_object(self.bucket_name, key)
        except Exception as exc:
            if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject"}:
                return None
            raise
        return _plain_metadata(getattr(stat, "metadata", None))

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
        offset = 0
        length = 0
        if byte_range is not None:
            start, end = byte_range
            offset = max(0, start)
            length = max(0, end - offset + 1)
        response = self._client_sync().get_object(
            self.bucket_name,
            key,
            offset=offset,
            length=length,
        )
        try:
            return [response.read()]
        finally:
            response.close()
            response.release_conn()

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
        data = b"".join(chunks)
        self._client_sync().put_object(
            self.bucket_name,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
            metadata=metadata,
        )

    async def update_metadata(self, key: str, metadata: Dict[str, str]) -> None:
        await asyncio.to_thread(self._update_metadata_sync, key, metadata)

    def _update_metadata_sync(self, key: str, metadata: Dict[str, str]) -> None:
        response = self._client_sync().get_object(self.bucket_name, key)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        self._client_sync().put_object(
            self.bucket_name,
            key,
            io.BytesIO(data),
            len(data),
            content_type="application/octet-stream",
            metadata=metadata,
        )
