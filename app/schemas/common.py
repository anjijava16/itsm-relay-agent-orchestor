from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class Citation(BaseModel):
    marker: str = Field(description="Inline marker used in the answer, e.g. [1]")
    document_id: str
    chunk_id: str
    title: str
    heading_path: str | None = None
    page_no: int | None = None
    source_uri: str | None = None
    score: float = 0.0
    snippet: str = ""


class Usage(BaseModel):
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None
