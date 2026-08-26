"""IngestionRouter - upload → S3 → queue. Never parses inside the request."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, UploadFile, status

from app.cache import idempotency
from app.core.errors import ValidationFailed
from app.core.logging import get_logger
from app.core.security import CurrentPrincipal
from app.db.session import DbSession
from app.schemas.common import Page
from app.schemas.ingestion import (
    DocumentOut,
    IngestionAccepted,
    IngestionJobOut,
    IngestionOptions,
    TextIngestionRequest,
    UrlIngestionRequest,
)
from app.services import ingestion_service
from app.storage.s3 import storage

log = get_logger(__name__)
router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/files", response_model=IngestionAccepted, status_code=status.HTTP_202_ACCEPTED,
             summary="Upload a document for indexing")
async def upload_file(
    principal: CurrentPrincipal,
    session: DbSession,
    file: Annotated[UploadFile, File(description="PDF, DOCX, PPTX, MD, HTML, TXT or CSV")],
    options: Annotated[str | None, Form(description="JSON IngestionOptions")] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> IngestionAccepted:
    principal.require_role("ingest.write", "admin")

    try:
        parsed = IngestionOptions(**json.loads(options)) if options else IngestionOptions()
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationFailed("`options` must be a JSON object matching IngestionOptions") from exc

    data = await file.read()

    if idempotency_key:
        body_hash = idempotency.fingerprint({"name": file.filename, "size": len(data)})
        is_new, stored = await idempotency.claim(idempotency_key, body_hash)
        if not is_new and stored:
            return IngestionAccepted(**stored)

    result = await ingestion_service.ingest_bytes(
        session, principal,
        filename=file.filename or "upload.bin",
        data=data,
        content_type=file.content_type or "application/octet-stream",
        options=parsed,
    )
    if idempotency_key:
        await idempotency.complete(idempotency_key, "", result.model_dump())
    return result


@router.post("/batch", response_model=list[IngestionAccepted], status_code=202,
             summary="Upload several documents at once")
async def upload_batch(
    principal: CurrentPrincipal,
    session: DbSession,
    files: Annotated[list[UploadFile], File()],
    options: Annotated[str | None, Form()] = None,
) -> list[IngestionAccepted]:
    principal.require_role("ingest.write", "admin")
    parsed = IngestionOptions(**json.loads(options)) if options else IngestionOptions()
    results = []
    for f in files:
        results.append(await ingestion_service.ingest_bytes(
            session, principal, filename=f.filename or "upload.bin", data=await f.read(),
            content_type=f.content_type or "application/octet-stream", options=parsed,
        ))
    return results


@router.post("/urls", response_model=IngestionAccepted, status_code=202)
async def ingest_url(req: UrlIngestionRequest, principal: CurrentPrincipal, session: DbSession):
    principal.require_role("ingest.write", "admin")
    return await ingestion_service.ingest_url(
        session, principal, str(req.url), req.title, req.options
    )


@router.post("/text", response_model=IngestionAccepted, status_code=202)
async def ingest_text(req: TextIngestionRequest, principal: CurrentPrincipal, session: DbSession):
    principal.require_role("ingest.write", "admin")
    return await ingestion_service.ingest_text(
        session, principal, req.title, req.content, req.options
    )


@router.get("/jobs/{job_id}", response_model=IngestionJobOut)
async def get_job(job_id: str, principal: CurrentPrincipal, session: DbSession):
    from app.core.errors import NotFoundError

    job = await ingestion_service.get_job(session, principal.tenant_id, job_id)
    if not job:
        raise NotFoundError(f"Ingestion job {job_id} not found")
    return IngestionJobOut(
        id=str(job.id), tenant_id=job.tenant_id,
        document_id=str(job.document_id) if job.document_id else None,
        status=job.status.value, stage_detail=job.stage_detail, attempts=job.attempts,
        error=job.error, stats=job.stats or {}, created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/jobs", response_model=Page[IngestionJobOut])
async def list_jobs(
    principal: CurrentPrincipal,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    jobs, total = await ingestion_service.list_jobs(
        session, principal.tenant_id, status_filter, limit, offset
    )
    return Page(
        items=[IngestionJobOut(
            id=str(j.id), tenant_id=j.tenant_id,
            document_id=str(j.document_id) if j.document_id else None,
            status=j.status.value, stage_detail=j.stage_detail, attempts=j.attempts,
            error=j.error, stats=j.stats or {}, created_at=j.created_at, updated_at=j.updated_at)
            for j in jobs],
        total=total, limit=limit, offset=offset,
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, principal: CurrentPrincipal, session: DbSession):
    from app.core.errors import NotFoundError
    from app.workers.celery_app import celery_app

    job = await ingestion_service.get_job(session, principal.tenant_id, job_id)
    if not job:
        raise NotFoundError(f"Ingestion job {job_id} not found")
    task = celery_app.send_task(
        "ingestion.process_document",
        kwargs={"job_id": str(job.id), "document_id": str(job.document_id),
                "tenant_id": job.tenant_id},
        queue="ingestion",
    )
    job.celery_task_id = task.id
    return {"job_id": job_id, "task_id": task.id, "status": "requeued"}


@router.get("/documents", response_model=Page[DocumentOut])
async def list_documents(
    principal: CurrentPrincipal,
    session: DbSession,
    doc_class: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from sqlalchemy import func, select

    from app.db.models import Document

    stmt = select(Document).where(Document.tenant_id == principal.tenant_id,
                                  Document.is_active.is_(True))
    count_stmt = select(func.count()).select_from(Document).where(
        Document.tenant_id == principal.tenant_id, Document.is_active.is_(True))
    if doc_class:
        stmt = stmt.where(Document.doc_class == doc_class)
        count_stmt = count_stmt.where(Document.doc_class == doc_class)

    rows = (await session.execute(
        stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return Page(
        items=[DocumentOut(
            id=str(d.id), title=d.title, doc_class=d.doc_class, source_type=d.source_type,
            source_uri=d.source_uri, s3_key=d.s3_key, size_bytes=d.size_bytes,
            chunk_count=d.chunk_count, version=d.version, is_active=d.is_active,
            doc_metadata=d.doc_metadata or {}, created_at=d.created_at) for d in rows],
        total=int((await session.execute(count_stmt)).scalar_one()),
        limit=limit, offset=offset,
    )


@router.get("/documents/{document_id}/download")
async def download_document(document_id: str, principal: CurrentPrincipal, session: DbSession):
    from sqlalchemy import select

    from app.core.errors import NotFoundError
    from app.db.models import Document
    import uuid as _uuid

    doc = (await session.execute(
        select(Document).where(Document.id == _uuid.UUID(document_id),
                               Document.tenant_id == principal.tenant_id)
    )).scalars().first()
    if not doc:
        raise NotFoundError("Document not found")
    return {"url": await storage.presigned_get(doc.s3_key), "expires_in": 900}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, principal: CurrentPrincipal, session: DbSession):
    principal.require_role("admin", "ingest.write")
    return await ingestion_service.deactivate_document(session, principal.tenant_id, document_id)
