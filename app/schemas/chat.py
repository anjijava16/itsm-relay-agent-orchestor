from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Citation, ORMModel, Usage


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, description="Resume an existing conversation")
    user_id: str | None = None
    channel: Literal["web", "slack", "teams", "email", "servicenow"] = "web"
    ticket_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    stream: bool = False


class AgentStep(BaseModel):
    node: str
    summary: str
    duration_ms: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    thread_id: str
    message_id: str
    answer: str
    intent: str
    category: str | None = None
    priority: str | None = None
    confidence: float = 0.0
    resolution_path: Literal[
        "kb_resolution", "automation", "ticket_created", "escalated", "clarify", "blocked"
    ]
    ticket_id: str | None = None
    ticket_number: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    requires_human: bool = False


class MessageOut(ORMModel):
    id: str
    role: str
    content: str
    citations: list = []
    model: str | None = None
    created_at: datetime


class SessionOut(ORMModel):
    id: str
    thread_id: str
    title: str | None
    channel: str
    is_open: bool
    created_at: datetime


class FeedbackIn(BaseModel):
    message_id: str | None = None
    ticket_id: str | None = None
    rating: int = Field(ge=-1, le=1, description="-1 down, 0 neutral, 1 up")
    reason: Literal["wrong", "incomplete", "unsafe", "slow", "helpful", "other"] | None = None
    comment: str | None = Field(default=None, max_length=2000)
