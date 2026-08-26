from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=512)
    description: str = Field(min_length=3)
    kind: Literal["incident", "service_request", "problem", "change"] = "incident"
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"
    category: str | None = None
    subcategory: str | None = None
    requester_id: str
    ci_name: str | None = None
    external_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TicketUpdate(BaseModel):
    status: Literal[
        "new", "triaged", "in_progress", "pending_user", "resolved", "closed", "escalated"
    ] | None = None
    priority: Literal["P1", "P2", "P3", "P4"] | None = None
    assignment_group: str | None = None
    category: str | None = None
    resolution: str | None = None
    attributes: dict[str, Any] | None = None


class TicketOut(ORMModel):
    id: str
    tenant_id: str
    external_ref: str | None
    kind: str
    status: str
    priority: str
    category: str | None
    subcategory: str | None
    assignment_group: str | None
    title: str
    description: str
    requester_id: str
    ci_name: str | None
    resolution: str | None
    resolved_by_agent: bool
    confidence: float | None
    attributes: dict = {}
    created_at: datetime
    updated_at: datetime


class TicketEventOut(ORMModel):
    id: str
    actor: str
    actor_type: str
    event_type: str
    payload: dict = {}
    created_at: datetime


class TriageResult(BaseModel):
    category: str
    subcategory: str | None = None
    priority: Literal["P1", "P2", "P3", "P4"]
    assignment_group: str
    urgency_reason: str
    duplicate_of: str | None = None
    confidence: float = 0.0


class SimilarIncident(BaseModel):
    ticket_id: str
    title: str
    status: str
    resolution: str | None
    similarity: float


class ProblemCandidate(BaseModel):
    cluster_label: str
    ticket_ids: list[str]
    ticket_count: int
    common_ci: str | None
    hypothesis: str
    recommended_action: str
