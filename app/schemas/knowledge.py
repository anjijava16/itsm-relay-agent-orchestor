from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Citation


class SearchFilters(BaseModel):
    doc_class: list[str] | None = None
    ci_name: list[str] | None = None
    category: list[str] | None = None
    acl_groups: list[str] | None = None
    created_after: str | None = None
    only_active: bool = True


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    strategy: Literal["hybrid", "vector", "keyword", "pgvector"] = "hybrid"
    rerank: bool = True
    compress: bool = False
    rewrite_query: bool = True
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    heading_path: str | None = None
    page_no: int | None = None
    score: float
    bm25_score: float | None = None
    vector_score: float | None = None
    rerank_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    rewritten_queries: list[str] = Field(default_factory=list)
    strategy: str
    hits: list[SearchHit]
    took_ms: int
    total_candidates: int


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = 8
    filters: SearchFilters = Field(default_factory=SearchFilters)


class AnswerResponse(BaseModel):
    question: str
    answer: str
    citations: list[Citation]
    grounded: bool
    confidence: float
