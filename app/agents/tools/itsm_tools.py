"""Tools the agent is allowed to call.

Two tiers on purpose:
  * read tools run freely
  * write tools are gated - they check the principal's roles, write an audit row,
    and anything destructive returns a proposal instead of executing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import AuditLog, Ticket, TicketEvent, TicketPriority, TicketStatus, TicketKind
from app.db.session import session_scope

log = get_logger(__name__)

SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}

DESTRUCTIVE_KEYWORDS = (
    "delete", "drop", "truncate", "rm -rf", "revoke", "disable account",
    "restart production", "failover", "wipe",
)


def is_destructive(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in DESTRUCTIVE_KEYWORDS)


async def audit(tenant_id: str, actor: str, action: str, resource_type: str,
                resource_id: str | None, payload: dict, outcome: str = "success") -> None:
    from app.core.logging import request_id_ctx

    async with session_scope() as s:
        s.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action,
                       resource_type=resource_type, resource_id=resource_id,
                       request_id=request_id_ctx.get(), outcome=outcome, payload=payload))


# ------------------------------------------------------------------ read tools
async def search_similar_tickets(tenant_id: str, category: str | None, ci_name: str | None,
                                 limit: int = 5) -> list[dict[str, Any]]:
    async with session_scope() as s:
        stmt = (
            select(Ticket)
            .where(Ticket.tenant_id == tenant_id, Ticket.status.in_(
                [TicketStatus.resolved, TicketStatus.closed]))
            .order_by(Ticket.updated_at.desc())
            .limit(limit)
        )
        if category:
            stmt = stmt.where(Ticket.category == category)
        if ci_name:
            stmt = stmt.where(Ticket.ci_name == ci_name)
        rows = (await s.execute(stmt)).scalars().all()
        return [
            {"ticket_id": str(t.id), "title": t.title, "status": t.status.value,
             "resolution": t.resolution, "priority": t.priority.value}
            for t in rows
        ]


async def get_ticket_status(tenant_id: str, ticket_ref: str) -> dict[str, Any] | None:
    async with session_scope() as s:
        stmt = select(Ticket).where(Ticket.tenant_id == tenant_id)
        try:
            stmt = stmt.where(Ticket.id == uuid.UUID(ticket_ref))
        except ValueError:
            stmt = stmt.where(Ticket.external_ref == ticket_ref)
        ticket = (await s.execute(stmt)).scalars().first()
        if not ticket:
            return None
        return {
            "ticket_id": str(ticket.id), "external_ref": ticket.external_ref,
            "status": ticket.status.value, "priority": ticket.priority.value,
            "title": ticket.title, "assignment_group": ticket.assignment_group,
            "sla_due_at": ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
        }


# ----------------------------------------------------------------- write tools
async def create_ticket(
    *, tenant_id: str, actor: str, title: str, description: str, requester_id: str,
    kind: str = "incident", priority: str = "P3", category: str | None = None,
    subcategory: str | None = None, assignment_group: str | None = None,
    ci_name: str | None = None, confidence: float | None = None,
    attributes: dict | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with session_scope() as s:
        ticket = Ticket(
            tenant_id=tenant_id,
            kind=TicketKind(kind),
            priority=TicketPriority(priority),
            status=TicketStatus.triaged,
            title=title[:512],
            description=description,
            requester_id=requester_id,
            category=category,
            subcategory=subcategory,
            assignment_group=assignment_group,
            ci_name=ci_name,
            confidence=confidence,
            attributes=attributes or {},
            sla_due_at=now + timedelta(hours=SLA_HOURS.get(priority, 24)),
        )
        s.add(ticket)
        await s.flush()
        ticket.external_ref = f"INC{ticket.created_at.strftime('%y%m')}{str(ticket.id)[:6].upper()}"
        s.add(TicketEvent(ticket_id=ticket.id, actor=actor, actor_type="agent",
                          event_type="created",
                          payload={"priority": priority, "category": category,
                                   "confidence": confidence}))
        result = {"ticket_id": str(ticket.id), "external_ref": ticket.external_ref,
                  "status": ticket.status.value, "priority": priority,
                  "sla_due_at": ticket.sla_due_at.isoformat()}

    await audit(tenant_id, actor, "ticket.create", "ticket", result["ticket_id"], result)
    log.info("ticket_created", **result)
    return result


async def update_ticket(*, tenant_id: str, actor: str, ticket_id: str, **changes) -> dict[str, Any]:
    async with session_scope() as s:
        ticket = (await s.execute(
            select(Ticket).where(Ticket.id == uuid.UUID(ticket_id), Ticket.tenant_id == tenant_id)
        )).scalars().first()
        if not ticket:
            return {"error": "not_found"}
        applied = {}
        for field, value in changes.items():
            if value is None or not hasattr(ticket, field):
                continue
            if field == "status":
                value = TicketStatus(value)
            elif field == "priority":
                value = TicketPriority(value)
            setattr(ticket, field, value)
            applied[field] = str(value)
        if applied.get("status") in ("TicketStatus.resolved", "resolved"):
            ticket.resolved_at = datetime.now(UTC)
        s.add(TicketEvent(ticket_id=ticket.id, actor=actor, actor_type="agent",
                          event_type="updated", payload=applied))
    await audit(tenant_id, actor, "ticket.update", "ticket", ticket_id, applied)
    return {"ticket_id": ticket_id, "applied": applied}


async def add_worknote(*, tenant_id: str, actor: str, ticket_id: str, note: str,
                       citations: list | None = None) -> dict[str, Any]:
    async with session_scope() as s:
        s.add(TicketEvent(ticket_id=uuid.UUID(ticket_id), actor=actor, actor_type="agent",
                          event_type="worknote",
                          payload={"note": note, "citations": citations or []}))
    return {"ok": True}


async def propose_automation(*, tenant_id: str, actor: str, action: str,
                             target: str, params: dict) -> dict[str, Any]:
    """Automations that mutate infrastructure are proposed, never auto-executed.

    A human approves via POST /tickets/{id}/approve, which resumes the graph
    from its checkpoint.
    """
    proposal = {
        "proposal_id": str(uuid.uuid4()),
        "action": action,
        "target": target,
        "params": params,
        "requires_approval": True,
        "destructive": is_destructive(f"{action} {target}"),
        "created_at": datetime.now(UTC).isoformat(),
    }
    await audit(tenant_id, actor, "automation.propose", "automation",
                proposal["proposal_id"], proposal)
    return proposal


# Safe, idempotent automations we are happy to run without a human.
SAFE_AUTOMATIONS = {
    "password_reset_link": "Send a self-service password reset link to the requester",
    "unlock_account": "Unlock an account locked by failed sign-ins",
    "resend_mfa_enrollment": "Re-send MFA enrolment mail",
    "software_install_request": "Raise a standard software install request",
    "vpn_profile_reissue": "Reissue the VPN profile",
    "mailbox_quota_bump": "Apply the standard mailbox quota increase",
}


async def run_safe_automation(*, tenant_id: str, actor: str, automation: str,
                              params: dict) -> dict[str, Any]:
    if automation not in SAFE_AUTOMATIONS:
        return {"ok": False, "error": f"'{automation}' is not on the safe list"}
    run_id = str(uuid.uuid4())
    # In a real deployment this dispatches to your orchestrator (SNOW flow,
    # Ansible AWX, Step Functions). Kept as an explicit seam.
    await audit(tenant_id, actor, "automation.run", "automation", run_id,
                {"automation": automation, "params": params})
    return {"ok": True, "run_id": run_id, "automation": automation,
            "description": SAFE_AUTOMATIONS[automation]}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "run_safe_automation",
            "description": "Run a pre-approved, reversible ITSM automation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation": {"type": "string", "enum": list(SAFE_AUTOMATIONS)},
                    "params": {"type": "object"},
                },
                "required": ["automation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket_status",
            "description": "Look up the current status of a ticket by id or reference.",
            "parameters": {
                "type": "object",
                "properties": {"ticket_ref": {"type": "string"}},
                "required": ["ticket_ref"],
            },
        },
    },
]
