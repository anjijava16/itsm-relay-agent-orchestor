#!/usr/bin/env python
"""One-shot setup: make sure the S3 bucket and OpenSearch index exist."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.retrieval import opensearch_store
from app.storage.s3 import S3Storage

log = get_logger(__name__)


async def main() -> None:
    configure_logging()
    try:
        S3Storage.ensure_bucket_sync(settings.s3_bucket)
        log.info("bucket_ready", bucket=settings.s3_bucket)
    except Exception as exc:
        log.error("bucket_setup_failed", error=str(exc))

    try:
        await opensearch_store.ensure_index()
        log.info("index_ready", index=settings.opensearch_index)
    except Exception as exc:
        log.error("index_setup_failed", error=str(exc))
    finally:
        await opensearch_store.close_client()


if __name__ == "__main__":
    asyncio.run(main())
