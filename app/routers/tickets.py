"""TicketRouter - incident / request / problem / change records."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.security import CurrentPrincipal
from app.db.session import DbSession
from app.schemas.common import Page
from app.schemas.ticket import (
    ProblemCandidate,
    TicketCreate,
    TicketEventOut,
    TicketOut,
    TicketUpdate,
)
from app.services import ticket_service

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _out(t) -> TicketOut:
    return TicketOut(
        id=str(t.id), tenant_id=t.tenant_id, external_ref=t.external_ref, kind=t.kind.value,
        status=t.status.value, priority=t.priority.value, category=t.category,
        subcategory=t.subcategory, assignment_group=t.assignment_group, title=t.title,
        description=t.description, requester_id=t.requester_id, ci_name=t.ci_name,
        resolution=t.resolution, resolved_by_agent=t.resolved_by_agent, confidence=t.confidence,
        attributes=t.attributes or {}, created_at=t.created_at, updated_at=t.updated_at,
    )


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
async def create_ticket(payload: TicketCreate, principal: CurrentPrincipal, session: DbSession):
    return _out(await ticket_service.create(session, principal, payload))


@router.get("", response_model=Page[TicketOut])
async def list_tickets(
    principal: CurrentPrincipal,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    priority: Annotated[str | None, Query()] = None,
    assignment_group: Annotated[str | None, Query()] = None,
    requester_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows, total = await ticket_service.list_tickets(
        session, principal.tenant_id, status=status_filter, priority=priority,
        assignment_group=assignment_group, requester_id=requester_id, limit=limit, offset=offset,
    )
    return Page(items=[_out(t) for t in rows], total=total, limit=limit, offset=offset)


@router.get("/sla/at-risk", response_model=list[TicketOut])
async def sla_at_risk(
    principal: CurrentPrincipal,
    session: DbSession,
    within_minutes: Annotated[int, Query(ge=1, le=10080)] = 60,
):
    return [_out(t) for t in await ticket_service.sla_breaches(session, principal.tenant_id, within_minutes)]


@router.get("/problems/candidates", response_model=list[ProblemCandidate])
async def problem_candidates(
    principal: CurrentPrincipal,
    session: DbSession,
    lookback_days: Annotated[int, Query(ge=1, le=90)] = 7,
):
    """AI-assisted problem management: cluster recent incidents by likely root cause."""
    clusters = await ticket_service.detect_problems(session, principal.tenant_id, lookback_days)
    return [ProblemCandidate(**c) for c in clusters]


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: str, principal: CurrentPrincipal, session: DbSession):
    return _out(await ticket_service.get(session, principal.tenant_id, ticket_id))


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: str, payload: TicketUpdate, principal: CurrentPrincipal, session: DbSession
):
    return _out(await ticket_service.update(session, principal, ticket_id, payload))


@router.get("/{ticket_id}/events", response_model=list[TicketEventOut])
async def ticket_events(ticket_id: str, principal: CurrentPrincipal, session: DbSession):
    rows = await ticket_service.events(session, principal.tenant_id, ticket_id)
    return [
        TicketEventOut(id=str(e.id), actor=e.actor, actor_type=e.actor_type,
                       event_type=e.event_type, payload=e.payload or {}, created_at=e.created_at)
        for e in rows
    ]


@router.post("/{ticket_id}/kb-draft")
async def kb_draft(ticket_id: str, principal: CurrentPrincipal, session: DbSession):
    """Continuous service improvement: turn a resolved ticket into a KB article."""
    principal.require_role("kb.author", "admin")
    return {"ticket_id": ticket_id,
            "draft": await ticket_service.draft_kb_article(session, principal.tenant_id, ticket_id)}


@router.post("/{ticket_id}/approve")
async def approve_action(ticket_id: str, principal: CurrentPrincipal, session: DbSession,
                         thread_id: Annotated[str, Query()] = ""):
    """Human-in-the-loop resume point.

    When the graph is compiled with `interrupt_before=["create_ticket"]` (or any
    other gated node), this endpoint resumes the checkpointed run after a human
    signs off.
    """
    principal.require_role("analyst", "admin")
    from app.agents.graph import get_compiled_graph

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": thread_id or ticket_id}}
    result = await graph.ainvoke(None, config=config)
    return {"resumed": True, "resolution_path": result.get("resolution_path")}
