"""S3 object storage for the raw ingested bytes.

The upload path is deliberately dumb and fast: validate, hash, put the object,
write a Postgres row, enqueue a Celery job. Parsing and embedding never happen
inside the HTTP request.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import BinaryIO

import aioboto3
import boto3

from app.core.config import settings
from app.core.errors import UpstreamError
from app.core.logging import get_logger

log = get_logger(__name__)


def _client_kwargs() -> dict:
    kwargs = {"region_name": settings.aws_region}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return kwargs


def build_key(tenant_id: str, filename: str, checksum: str) -> str:
    day = datetime.now(UTC).strftime("%Y/%m/%d")
    safe = filename.replace("/", "_").replace("..", "_")[-180:]
    return f"{settings.s3_prefix}{tenant_id}/{day}/{checksum[:12]}-{safe}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class S3Storage:
    """Async for the API path, sync for Celery workers."""

    def __init__(self, bucket: str | None = None):
        self.bucket = bucket or settings.s3_bucket
        self._session = aioboto3.Session()

    async def put_bytes(self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> str:
        try:
            async with self._session.client("s3", **_client_kwargs()) as s3:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    Metadata={k: str(v)[:1024] for k, v in (metadata or {}).items()},
                    ServerSideEncryption="AES256",
                )
        except Exception as exc:
            log.error("s3_put_failed", key=key, error=str(exc))
            raise UpstreamError("Could not store the uploaded file") from exc
        log.info("s3_put_ok", key=key, bytes=len(data))
        return key

    async def presigned_get(self, key: str, expires: int = 900) -> str:
        async with self._session.client("s3", **_client_kwargs()) as s3:
            return await s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
            )

    async def delete(self, key: str) -> None:
        async with self._session.client("s3", **_client_kwargs()) as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    # ---- sync helpers used by Celery workers -------------------------------
    @staticmethod
    def get_bytes_sync(bucket: str, key: str) -> bytes:
        s3 = boto3.client("s3", **_client_kwargs())
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise UpstreamError(f"Could not read s3://{bucket}/{key}") from exc

    @staticmethod
    def put_bytes_sync(bucket: str, key: str, data: bytes, content_type: str) -> str:
        s3 = boto3.client("s3", **_client_kwargs())
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type,
                      ServerSideEncryption="AES256")
        return key

    @staticmethod
    def ensure_bucket_sync(bucket: str) -> None:
        s3 = boto3.client("s3", **_client_kwargs())
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            s3.create_bucket(Bucket=bucket)
            log.info("s3_bucket_created", bucket=bucket)


storage = S3Storage()


def stream_checksum(fileobj: BinaryIO, chunk: int = 1 << 20) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while data := fileobj.read(chunk):
        digest.update(data)
        size += len(data)
    fileobj.seek(0)
    return digest.hexdigest(), size
