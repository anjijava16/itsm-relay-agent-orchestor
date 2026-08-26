"""Shared state for the LangGraph workflow.

Everything a node needs is in here - nodes never reach into request objects or
globals. That is what lets us checkpoint, resume and replay a conversation.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

ResolutionPath = Literal[
    "kb_resolution", "automation", "ticket_created", "escalated", "clarify", "blocked"
]


class AgentStep(TypedDict):
    node: str
    summary: str
    duration_ms: int
    payload: dict[str, Any]


class ITSMState(TypedDict, total=False):
    # ---- input
    tenant_id: str
    user_id: str
    thread_id: str
    channel: str
    message: str
    history: list[dict[str, str]]
    metadata: dict[str, Any]

    # ---- guardrails
    allowed: bool
    guardrail_reasons: list[str]
    redacted_message: str

    # ---- triage
    intent: str
    category: str | None
    subcategory: str | None
    priority: str
    assignment_group: str | None
    affected_ci: str | None
    is_outage: bool
    triage_confidence: float

    # ---- retrieval
    retrieved: list[dict[str, Any]]
    rewritten_queries: list[str]
    citations: list[dict[str, Any]]
    similar_tickets: list[dict[str, Any]]

    # ---- reasoning
    draft_answer: str
    resolves: bool
    confidence: float
    risk_flags: list[str]
    missing: str | None

    # ---- actions
    resolution_path: ResolutionPath
    ticket_id: str | None
    ticket_number: str | None
    automation_run: str | None
    requires_human: bool
    suggested_actions: list[str]

    # ---- output
    answer: str
    usage: dict[str, Any]
    steps: Annotated[list[AgentStep], operator.add]
    errors: Annotated[list[str], operator.add]


def initial_state(**kwargs) -> ITSMState:
    base: ITSMState = {
        "allowed": True,
        "guardrail_reasons": [],
        "history": [],
        "metadata": {},
        "retrieved": [],
        "rewritten_queries": [],
        "citations": [],
        "similar_tickets": [],
        "risk_flags": [],
        "suggested_actions": [],
        "steps": [],
        "errors": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "latency_ms": 0},
        "confidence": 0.0,
        "requires_human": False,
        "is_outage": False,
        "priority": "P3",
    }
    base.update(kwargs)  # type: ignore[arg-type]
    return base


def merge_usage(state: ITSMState, usage: dict[str, Any]) -> dict[str, Any]:
    current = dict(state.get("usage") or {})
    for key in ("prompt_tokens", "completion_tokens", "latency_ms"):
        current[key] = int(current.get(key, 0)) + int(usage.get(key, 0) or 0)
    current["cost_usd"] = float(current.get("cost_usd", 0.0)) + float(usage.get("cost_usd", 0.0) or 0.0)
    if usage.get("model"):
        current["model"] = usage["model"]
    return current
