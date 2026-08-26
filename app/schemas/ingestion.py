from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

from app.schemas.common import ORMModel


class IngestionOptions(BaseModel):
    doc_class: Literal["kb_article", "runbook", "sop", "policy", "change_record", "postmortem"] = (
        "kb_article"
    )
    acl: list[str] = Field(default_factory=list, description="Groups allowed to retrieve this doc")
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    reindex: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class UrlIngestionRequest(BaseModel):
    url: HttpUrl
    title: str | None = None
    options: IngestionOptions = Field(default_factory=IngestionOptions)


class TextIngestionRequest(BaseModel):
    title: str
    content: str = Field(min_length=1)
    options: IngestionOptions = Field(default_factory=IngestionOptions)


class IngestionJobOut(ORMModel):
    id: str
    tenant_id: str
    document_id: str | None
    status: str
    stage_detail: str | None
    attempts: int
    error: str | None
    stats: dict = {}
    created_at: datetime
    updated_at: datetime


class DocumentOut(ORMModel):
    id: str
    title: str
    doc_class: str
    source_type: str
    source_uri: str | None
    s3_key: str
    size_bytes: int
    chunk_count: int
    version: int
    is_active: bool
    doc_metadata: dict = {}
    created_at: datetime


class IngestionAccepted(BaseModel):
    job_id: str
    document_id: str | None = None
    status: str
    s3_key: str | None = None
    message: str = "Upload stored in S3. Parsing, embedding and indexing run asynchronously."
