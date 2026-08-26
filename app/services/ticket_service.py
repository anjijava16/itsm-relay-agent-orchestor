"""Ticket CRUD, SLA view and AI-assisted problem management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.security import Principal
from app.db.models import Ticket, TicketEvent, TicketPriority, TicketStatus, TicketKind
from app.llm import client as llm
from app.llm.prompts import KB_DRAFT, PROBLEM_CLUSTER
from app.schemas.ticket import TicketCreate, TicketUpdate

log = get_logger(__name__)
SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}


async def create(session: AsyncSession, principal: Principal, payload: TicketCreate) -> Ticket:
    now = datetime.now(UTC)
    ticket = Ticket(
        tenant_id=principal.tenant_id,
        kind=TicketKind(payload.kind),
        priority=TicketPriority(payload.priority),
        status=TicketStatus.new,
        title=payload.title,
        description=payload.description,
        requester_id=payload.requester_id,
        category=payload.category,
        subcategory=payload.subcategory,
        ci_name=payload.ci_name,
        external_ref=payload.external_ref,
        attributes=payload.attributes,
        sla_due_at=now + timedelta(hours=SLA_HOURS[payload.priority]),
    )
    session.add(ticket)
    await session.flush()
    if not ticket.external_ref:
        ticket.external_ref = f"INC{now.strftime('%y%m')}{str(ticket.id)[:6].upper()}"
    session.add(TicketEvent(ticket_id=ticket.id, actor=principal.subject, actor_type="human",
                            event_type="created", payload={"source": "api"}))
    return ticket


async def get(session: AsyncSession, tenant_id: str, ticket_id: str) -> Ticket:
    ticket = (
        await session.execute(
            select(Ticket).where(Ticket.id == uuid.UUID(ticket_id), Ticket.tenant_id == tenant_id)
        )
    ).scalars().first()
    if not ticket:
        raise NotFoundError(f"Ticket {ticket_id} not found")
    return ticket


async def update(
    session: AsyncSession, principal: Principal, ticket_id: str, payload: TicketUpdate
) -> Ticket:
    ticket = await get(session, principal.tenant_id, ticket_id)
    applied: dict[str, Any] = {}
    data = payload.model_dump(exclude_none=True)
    for field, value in data.items():
        if field == "status":
            ticket.status = TicketStatus(value)
            if ticket.status in (TicketStatus.resolved, TicketStatus.closed):
                ticket.resolved_at = datetime.now(UTC)
        elif field == "priority":
            ticket.priority = TicketPriority(value)
            ticket.sla_due_at = ticket.created_at + timedelta(hours=SLA_HOURS[value])
        elif field == "attributes":
            ticket.attributes = {**(ticket.attributes or {}), **value}
        else:
            setattr(ticket, field, value)
        applied[field] = value

    session.add(TicketEvent(ticket_id=ticket.id, actor=principal.subject, actor_type="human",
                            event_type="updated", payload=applied))
    return ticket


async def list_tickets(
    session: AsyncSession, tenant_id: str, *, status: str | None = None,
    priority: str | None = None, assignment_group: str | None = None,
    requester_id: str | None = None, limit: int = 25, offset: int = 0,
) -> tuple[list[Ticket], int]:
    stmt = select(Ticket).where(Ticket.tenant_id == tenant_id)
    count_stmt = select(func.count()).select_from(Ticket).where(Ticket.tenant_id == tenant_id)
    for column, value, caster in (
        (Ticket.status, status, TicketStatus),
        (Ticket.priority, priority, TicketPriority),
        (Ticket.assignment_group, assignment_group, str),
        (Ticket.requester_id, requester_id, str),
    ):
        if value:
            clause = column == caster(value)
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)

    rows = (
        await session.execute(stmt.order_by(Ticket.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return list(rows), int((await session.execute(count_stmt)).scalar_one())


async def events(session: AsyncSession, tenant_id: str, ticket_id: str) -> list[TicketEvent]:
    await get(session, tenant_id, ticket_id)
    rows = (
        await session.execute(
            select(TicketEvent).where(TicketEvent.ticket_id == uuid.UUID(ticket_id))
            .order_by(TicketEvent.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def sla_breaches(session: AsyncSession, tenant_id: str, within_minutes: int = 60):
    cutoff = datetime.now(UTC) + timedelta(minutes=within_minutes)
    rows = (
        await session.execute(
            select(Ticket).where(
                Ticket.tenant_id == tenant_id,
                Ticket.sla_due_at.isnot(None),
                Ticket.sla_due_at <= cutoff,
                Ticket.status.notin_([TicketStatus.resolved, TicketStatus.closed]),
            ).order_by(Ticket.sla_due_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def detect_problems(
    session: AsyncSession, tenant_id: str, lookback_days: int = 7, limit: int = 60
) -> list[dict[str, Any]]:
    """Cluster recent incidents into candidate problem records."""
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    rows = (
        await session.execute(
            select(Ticket).where(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= since,
                Ticket.kind == TicketKind.incident,
            ).order_by(Ticket.created_at.desc()).limit(limit)
        )
    ).scalars().all()

    if len(rows) < 3:
        return []

    incidents = "\n".join(
        f"- id={t.id} | {t.priority.value} | {t.category or 'uncategorised'} | "
        f"ci={t.ci_name or '-'} | {t.title}"
        for t in rows
    )
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": PROBLEM_CLUSTER.format(incidents=incidents[:12000])}],
            purpose="problem_clustering", tenant_id=tenant_id, temperature=0.1, max_tokens=1500,
        )
    except Exception as exc:
        log.warning("problem_clustering_failed", error=str(exc))
        return []

    clusters = []
    for c in data.get("clusters", []):
        ids = [i for i in c.get("ticket_ids", []) if i]
        if len(ids) < 2:
            continue
        clusters.append({
            "cluster_label": c.get("cluster_label", "Unlabelled"),
            "ticket_ids": ids,
            "ticket_count": len(ids),
            "common_ci": c.get("common_ci"),
            "hypothesis": c.get("hypothesis", ""),
            "recommended_action": c.get("recommended_action", ""),
        })
    return clusters


async def draft_kb_article(session: AsyncSession, tenant_id: str, ticket_id: str) -> str:
    """Turn a resolved incident into a KB article draft (continuous improvement loop)."""
    ticket = await get(session, tenant_id, ticket_id)
    ticket_events = await events(session, tenant_id, ticket_id)
    notes = "\n".join(
        str(e.payload.get("note", "")) for e in ticket_events if e.event_type == "worknote"
    )
    result = await llm.complete(
        [{"role": "user", "content": KB_DRAFT.format(
            ticket=f"{ticket.title}\n\n{ticket.description}",
            resolution=ticket.resolution or notes or "(no resolution recorded)",
            sources=notes[:2000] or "(none)")}],
        purpose="kb_draft", tenant_id=tenant_id, temperature=0.2, max_tokens=1500,
    )
    return result.text


async def metrics(session: AsyncSession, tenant_id: str, days: int = 30) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    base = select(func.count()).select_from(Ticket).where(
        Ticket.tenant_id == tenant_id, Ticket.created_at >= since
    )
    total = int((await session.execute(base)).scalar_one())
    by_agent = int((await session.execute(
        base.where(Ticket.resolved_by_agent.is_(True))
    )).scalar_one())
    resolved = int((await session.execute(
        base.where(Ticket.status.in_([TicketStatus.resolved, TicketStatus.closed]))
    )).scalar_one())

    mttr = (await session.execute(
        select(func.avg(func.extract("epoch", Ticket.resolved_at - Ticket.created_at)))
        .where(Ticket.tenant_id == tenant_id, Ticket.resolved_at.isnot(None),
               Ticket.created_at >= since)
    )).scalar()

    by_priority = dict(
        (await session.execute(
            select(Ticket.priority, func.count()).where(
                Ticket.tenant_id == tenant_id, Ticket.created_at >= since
            ).group_by(Ticket.priority)
        )).all()
    )

    return {
        "window_days": days,
        "tickets_created": total,
        "tickets_resolved": resolved,
        "auto_resolved_by_agent": by_agent,
        "deflection_rate": round(by_agent / total, 3) if total else 0.0,
        "mttr_hours": round(float(mttr) / 3600, 2) if mttr else None,
        "by_priority": {k.value if hasattr(k, "value") else str(k): v for k, v in by_priority.items()},
    }
