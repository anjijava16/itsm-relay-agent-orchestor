"""Ingestion: the HTTP side only touches S3 and Postgres, then queues a job."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ValidationFailed
from app.core.logging import get_logger
from app.core.observability import INGEST_DOCS, QUEUE_DEPTH
from app.core.security import Principal
from app.db.models import Document, IngestionJob, JobStatus
from app.schemas.ingestion import IngestionAccepted, IngestionOptions
from app.storage.s3 import sha256_bytes, build_key, storage

log = get_logger(__name__)

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _validate(content_type: str, size_bytes: int) -> None:
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailed(f"Content type '{base}' is not accepted for ingestion")
    if size_bytes > settings.ingestion_max_file_mb * 1024 * 1024:
        raise ValidationFailed(f"File exceeds the {settings.ingestion_max_file_mb} MB limit")
    if size_bytes == 0:
        raise ValidationFailed("Empty file")


async def ingest_bytes(
    session: AsyncSession,
    principal: Principal,
    *,
    filename: str,
    data: bytes,
    content_type: str,
    options: IngestionOptions,
    source_type: str = "upload",
    source_uri: str | None = None,
) -> IngestionAccepted:
    _validate(content_type, len(data))
    checksum = sha256_bytes(data)

    existing = (
        await session.execute(
            select(Document).where(
                Document.tenant_id == principal.tenant_id,
                Document.checksum_sha256 == checksum,
                Document.is_active.is_(True),
            )
        )
    ).scalars().first()

    if existing and not options.reindex:
        log.info("ingestion_deduplicated", document_id=str(existing.id), checksum=checksum[:12])
        INGEST_DOCS.labels("duplicate").inc()
        return IngestionAccepted(
            job_id="", document_id=str(existing.id), status="duplicate", s3_key=existing.s3_key,
            message="Identical content already indexed. Pass options.reindex=true to force a new version.",
        )

    version = (existing.version + 1) if existing else 1
    key = build_key(principal.tenant_id, filename, checksum)
    await storage.put_bytes(
        key, data, content_type,
        metadata={"tenant": principal.tenant_id, "uploaded_by": principal.subject,
                  "doc_class": options.doc_class},
    )

    document = Document(
        tenant_id=principal.tenant_id,
        title=(options.metadata.get("title") or filename)[:512],
        source_type=source_type,
        source_uri=source_uri,
        s3_bucket=settings.s3_bucket,
        s3_key=key,
        content_type=content_type,
        checksum_sha256=checksum,
        size_bytes=len(data),
        doc_class=options.doc_class,
        version=version,
        acl=options.acl,
        doc_metadata={**options.metadata, "uploaded_by": principal.subject},
    )
    session.add(document)
    await session.flush()

    job = IngestionJob(
        tenant_id=principal.tenant_id,
        document_id=document.id,
        status=JobStatus.queued,
        stats={"size_bytes": len(data), "chunk_size": options.chunk_size or settings.ingestion_chunk_size},
    )
    session.add(job)
    await session.flush()

    # Queue after flush so the worker can always find the rows.
    from app.workers.celery_app import celery_app

    task = celery_app.send_task(
        "ingestion.process_document",
        kwargs={
            "job_id": str(job.id),
            "document_id": str(document.id),
            "tenant_id": principal.tenant_id,
            "chunk_size": options.chunk_size or settings.ingestion_chunk_size,
            "chunk_overlap": options.chunk_overlap or settings.ingestion_chunk_overlap,
        },
        queue="ingestion",
    )
    job.celery_task_id = task.id
    QUEUE_DEPTH.inc()
    INGEST_DOCS.labels("queued").inc()

    log.info("ingestion_queued", job_id=str(job.id), document_id=str(document.id), s3_key=key)
    return IngestionAccepted(
        job_id=str(job.id), document_id=str(document.id), status=job.status.value, s3_key=key
    )


async def ingest_url(
    session: AsyncSession, principal: Principal, url: str, title: str | None,
    options: IngestionOptions
) -> IngestionAccepted:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        response = await http.get(url)
        response.raise_for_status()
    content_type = response.headers.get("content-type", "text/html")
    filename = title or url.rstrip("/").split("/")[-1] or "web-page.html"
    return await ingest_bytes(
        session, principal, filename=filename, data=response.content,
        content_type=content_type, options=options, source_type="url", source_uri=url,
    )


async def ingest_text(
    session: AsyncSession, principal: Principal, title: str, content: str,
    options: IngestionOptions
) -> IngestionAccepted:
    return await ingest_bytes(
        session, principal, filename=f"{title[:100]}.md", data=content.encode("utf-8"),
        content_type="text/markdown", options=options, source_type="text",
    )


async def get_job(session: AsyncSession, tenant_id: str, job_id: str) -> IngestionJob | None:
    return (
        await session.execute(
            select(IngestionJob).where(
                IngestionJob.id == uuid.UUID(job_id), IngestionJob.tenant_id == tenant_id
            )
        )
    ).scalars().first()


async def list_jobs(
    session: AsyncSession, tenant_id: str, status: str | None, limit: int, offset: int
) -> tuple[list[IngestionJob], int]:
    from sqlalchemy import func

    stmt = select(IngestionJob).where(IngestionJob.tenant_id == tenant_id)
    count_stmt = select(func.count()).select_from(IngestionJob).where(
        IngestionJob.tenant_id == tenant_id
    )
    if status:
        stmt = stmt.where(IngestionJob.status == JobStatus(status))
        count_stmt = count_stmt.where(IngestionJob.status == JobStatus(status))
    rows = (
        await session.execute(stmt.order_by(IngestionJob.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    return list(rows), int(total)


async def deactivate_document(session: AsyncSession, tenant_id: str, document_id: str) -> dict[str, Any]:
    from app.retrieval import opensearch_store

    document = (
        await session.execute(
            select(Document).where(
                Document.id == uuid.UUID(document_id), Document.tenant_id == tenant_id
            )
        )
    ).scalars().first()
    if not document:
        return {"deleted": 0, "found": False}

    document.is_active = False
    removed = await opensearch_store.delete_by_document(tenant_id, document_id)
    log.info("document_deactivated", document_id=document_id, removed_chunks=removed)
    return {"deleted": removed, "found": True}
