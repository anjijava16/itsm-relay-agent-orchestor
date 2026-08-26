"""The asynchronous half of ingestion.

    S3 object ──► parse ──► chunk ──► embed ──► index (OpenSearch)
                                          └──► persist vectors + metadata (Postgres)

Every stage writes its status back to `ingestion_jobs` so the IngestionRouter
can report real progress rather than a spinner. The task is idempotent: chunk
ids are deterministic, so a retry overwrites rather than duplicates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from celery import Task
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import INGEST_DOCS, QUEUE_DEPTH
from app.db.models import Document, DocumentChunk, IngestionJob, JobStatus
from app.llm import client as llm
from app.retrieval import opensearch_store
from app.retrieval.chunking import chunk_markdown
from app.retrieval.parsers import parse
from app.storage.s3 import S3Storage
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async, worker_session

log = get_logger(__name__)
EMBED_BATCH = 64


async def _set_status(session, job: IngestionJob, status: JobStatus, detail: str | None = None,
                      **stats) -> None:
    job.status = status
    job.stage_detail = detail
    if stats:
        job.stats = {**(job.stats or {}), **stats}
    await session.flush()
    log.info("ingestion_stage", job_id=str(job.id), status=status.value, detail=detail)


async def _process(job_id: str, document_id: str, tenant_id: str,
                   chunk_size: int, chunk_overlap: int) -> dict:
    async with worker_session() as session:
        job = (await session.execute(
            select(IngestionJob).where(IngestionJob.id == uuid.UUID(job_id))
        )).scalars().first()
        document = (await session.execute(
            select(Document).where(Document.id == uuid.UUID(document_id))
        )).scalars().first()
        if not job or not document:
            log.error("ingestion_rows_missing", job_id=job_id, document_id=document_id)
            return {"ok": False, "error": "job or document not found"}

        job.attempts += 1
        job.started_at = job.started_at or datetime.now(UTC)

        # ---- 1. fetch from S3
        await _set_status(session, job, JobStatus.parsing, "reading object from S3")
        data = S3Storage.get_bytes_sync(document.s3_bucket, document.s3_key)

        # ---- 2. parse to markdown
        markdown, parse_meta = parse(data, document.content_type, document.title)
        if not markdown.strip():
            await _set_status(session, job, JobStatus.failed, "parser produced no text")
            job.error = "Parser produced no text"
            job.finished_at = datetime.now(UTC)
            INGEST_DOCS.labels("failed").inc()
            return {"ok": False, "error": "empty parse"}

        # ---- 3. chunk
        await _set_status(session, job, JobStatus.chunking, f"parser={parse_meta.get('parser')}")
        chunks = chunk_markdown(
            markdown,
            chunk_size_tokens=chunk_size or settings.ingestion_chunk_size,
            overlap_tokens=chunk_overlap or settings.ingestion_chunk_overlap,
        )
        if not chunks:
            await _set_status(session, job, JobStatus.failed, "no chunks produced")
            return {"ok": False, "error": "no chunks"}

        # Replace any previous chunks for this document (re-ingest is idempotent).
        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )

        # ---- 4. embed
        await _set_status(session, job, JobStatus.embedding, f"{len(chunks)} chunks",
                          chunk_count=len(chunks), parser=parse_meta.get("parser"))
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[i:i + EMBED_BATCH]
            vectors.extend(await llm.embed([c.content for c in batch], tenant_id=tenant_id))
            await _set_status(session, job, JobStatus.embedding,
                              f"embedded {min(i + EMBED_BATCH, len(chunks))}/{len(chunks)}")

        # ---- 5. persist chunk rows (Postgres = system of record)
        os_docs = []
        for chunk, vector in zip(chunks, vectors, strict=False):
            chunk_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{document.id}:{chunk.ordinal}")
            session.add(DocumentChunk(
                id=chunk_id,
                document_id=document.id,
                tenant_id=tenant_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                heading_path=chunk.heading_path,
                page_no=chunk.page_no,
                token_count=chunk.token_count,
                embedding=vector,
                indexed_in_opensearch=False,
                chunk_metadata={**chunk.metadata, "parser": parse_meta.get("parser")},
            ))
            os_docs.append({
                "tenant_id": tenant_id,
                "document_id": str(document.id),
                "chunk_id": str(chunk_id),
                "ordinal": chunk.ordinal,
                "title": document.title,
                "content": chunk.content,
                "heading_path": chunk.heading_path,
                "page_no": chunk.page_no,
                "doc_class": document.doc_class,
                "category": (document.doc_metadata or {}).get("category"),
                "ci_name": (document.doc_metadata or {}).get("ci_name"),
                "acl": document.acl or [],
                "source_uri": document.source_uri,
                "is_active": True,
                "version": document.version,
                "created_at": datetime.now(UTC).isoformat(),
                "embedding": vector,
            })
        await session.flush()

        # ---- 6. index into OpenSearch
        await _set_status(session, job, JobStatus.indexing, f"indexing {len(os_docs)} chunks")
        await opensearch_store.ensure_index()
        indexed = 0
        for i in range(0, len(os_docs), 200):
            indexed += await opensearch_store.bulk_index(os_docs[i:i + 200])

        await session.execute(
            DocumentChunk.__table__.update()
            .where(DocumentChunk.document_id == document.id)
            .values(indexed_in_opensearch=True)
        )
        document.chunk_count = len(chunks)

        # ---- 7. done
        job.finished_at = datetime.now(UTC)
        await _set_status(session, job, JobStatus.completed,
                          f"{indexed} chunks live", indexed=indexed,
                          tokens=sum(c.token_count for c in chunks))
        INGEST_DOCS.labels("completed").inc()
        QUEUE_DEPTH.dec()

        return {"ok": True, "document_id": str(document.id), "chunks": len(chunks),
                "indexed": indexed}


async def _fail(job_id: str, error: str) -> None:
    async with worker_session() as session:
        job = (await session.execute(
            select(IngestionJob).where(IngestionJob.id == uuid.UUID(job_id))
        )).scalars().first()
        if job:
            job.status = JobStatus.failed if job.attempts < 3 else JobStatus.dead_letter
            job.error = error[:4000]
            job.finished_at = datetime.now(UTC)
    INGEST_DOCS.labels("failed").inc()


@celery_app.task(
    bind=True,
    name="ingestion.process_document",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def process_document(self: Task, job_id: str, document_id: str, tenant_id: str,
                     chunk_size: int = 0, chunk_overlap: int = 0) -> dict:
    log.info("ingestion_task_start", job_id=job_id, attempt=self.request.retries + 1)
    try:
        return run_async(_process, job_id, document_id, tenant_id, chunk_size, chunk_overlap)
    except Exception as exc:
        log.exception("ingestion_task_failed", job_id=job_id, error=str(exc))
        if self.request.retries >= self.max_retries:
            run_async(_fail, job_id, str(exc))
        raise
