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
