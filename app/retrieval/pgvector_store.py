"""pgvector fallback / transactional similarity.

OpenSearch owns fleet-scale hybrid retrieval. pgvector earns its place for
three things:
  * strongly-consistent reads right after ingestion (no refresh lag)
  * "similar incidents" over ticket text that never leaves Postgres
  * a working retrieval path if the OpenSearch cluster is degraded
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk
from app.core.logging import get_logger

log = get_logger(__name__)


async def similarity_search(
    session: AsyncSession,
    tenant_id: str,
    vector: list[float],
    top_k: int = 20,
    doc_classes: list[str] | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.content,
            DocumentChunk.heading_path,
            DocumentChunk.page_no,
            DocumentChunk.chunk_metadata,
            Document.title,
            Document.doc_class,
            Document.source_uri,
            DocumentChunk.embedding.cosine_distance(vector).label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            Document.is_active.is_(True),
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(text("distance"))
        .limit(top_k)
    )
    if doc_classes:
        stmt = stmt.where(Document.doc_class.in_(doc_classes))

    rows = (await session.execute(stmt)).all()
    return [
        {
            "chunk_id": str(r.id),
            "document_id": str(r.document_id),
            "title": r.title,
            "content": r.content,
            "heading_path": r.heading_path,
            "page_no": r.page_no,
            "doc_class": r.doc_class,
            "source_uri": r.source_uri,
            "_score": 1.0 - float(r.distance),
            "_kind": "pgvector",
            "metadata": r.chunk_metadata or {},
        }
        for r in rows
    ]


async def similar_tickets(
    session: AsyncSession, tenant_id: str, vector: list[float], top_k: int = 5
) -> list[dict[str, Any]]:
    """Nearest resolved incidents, using the chunk index over ticket summaries."""
    sql = text(
        """
        SELECT t.id, t.title, t.status, t.resolution,
               1 - (c.embedding <=> CAST(:vec AS vector)) AS similarity
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN tickets t ON t.id = (d.doc_metadata->>'ticket_id')::uuid
        WHERE c.tenant_id = :tenant
          AND d.doc_class = 'postmortem'
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector)
        LIMIT :k
        """
    )
    rows = (await session.execute(sql, {"vec": str(vector), "tenant": tenant_id, "k": top_k})).all()
    return [
        {"ticket_id": str(r.id), "title": r.title, "status": r.status,
         "resolution": r.resolution, "similarity": float(r.similarity)}
        for r in rows
    ]
