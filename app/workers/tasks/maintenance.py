"""Scheduled housekeeping: reconcile indexes, sweep stuck jobs, mine problems."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk, IngestionJob, JobStatus
from app.retrieval import opensearch_store
from app.services import ticket_service
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async, worker_session

log = get_logger(__name__)


async def _reindex_stale() -> dict:
    """Chunks that exist in Postgres but never made it into OpenSearch."""
    async with worker_session() as session:
        rows = (await session.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.indexed_in_opensearch.is_(False), Document.is_active.is_(True))
            .limit(500)
        )).all()
        if not rows:
            return {"reindexed": 0}

        docs = [{
            "tenant_id": c.tenant_id, "document_id": str(c.document_id), "chunk_id": str(c.id),
            "ordinal": c.ordinal, "title": d.title, "content": c.content,
            "heading_path": c.heading_path, "page_no": c.page_no, "doc_class": d.doc_class,
            "acl": d.acl or [], "source_uri": d.source_uri, "is_active": True,
            "version": d.version, "created_at": c.created_at.isoformat(), "embedding": c.embedding,
        } for c, d in rows]

        await opensearch_store.ensure_index()
        await opensearch_store.bulk_index(docs)
        await session.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id.in_([c.id for c, _ in rows]))
            .values(indexed_in_opensearch=True)
        )
        log.info("reindexed_stale_chunks", count=len(docs))
        return {"reindexed": len(docs)}


async def _expire_stuck() -> dict:
    cutoff = datetime.now(UTC) - timedelta(hours=2)
    async with worker_session() as session:
        result = await session.execute(
            update(IngestionJob)
            .where(
                IngestionJob.status.in_([JobStatus.parsing, JobStatus.chunking,
                                         JobStatus.embedding, JobStatus.indexing]),
                IngestionJob.updated_at < cutoff,
            )
            .values(status=JobStatus.dead_letter, error="Stuck for over 2 hours; moved to DLQ")
        )
        return {"expired": result.rowcount or 0}


async def _detect_problems() -> dict:
    async with worker_session() as session:
        tenants = (await session.execute(select(Document.tenant_id).distinct())).scalars().all()
        found = 0
        for tenant in tenants:
            clusters = await ticket_service.detect_problems(session, tenant, lookback_days=7)
            found += len(clusters)
            for c in clusters:
                log.info("problem_candidate", tenant=tenant, label=c["cluster_label"],
                         tickets=c["ticket_count"], ci=c.get("common_ci"))
        return {"clusters": found}


@celery_app.task(name="maintenance.reindex_stale_chunks")
def reindex_stale_chunks() -> dict:
    return run_async(_reindex_stale)


@celery_app.task(name="maintenance.expire_stuck_jobs")
def expire_stuck_jobs() -> dict:
    return run_async(_expire_stuck)


@celery_app.task(name="maintenance.detect_problems")
def detect_problems() -> dict:
    return run_async(_detect_problems)
