"""Action nodes: automation, ticket creation, escalation, clarification."""

from __future__ import annotations

import time

from app.agents.state import ITSMState, merge_usage
from app.agents.tools import itsm_tools
from app.core.logging import get_logger
from app.llm import client as llm
from app.llm.prompts import SUMMARIZE_TICKET
from app.retrieval import pipeline

log = get_logger(__name__)

AUTOMATION_HINTS = {
    "password_reset_link": ("password", "reset my password", "forgot password"),
    "unlock_account": ("locked out", "account locked", "too many attempts"),
    "resend_mfa_enrollment": ("mfa", "2fa", "authenticator", "token enrol"),
    "vpn_profile_reissue": ("vpn profile", "vpn certificate", "vpn config"),
    "mailbox_quota_bump": ("mailbox full", "quota", "cannot send mail"),
}


def match_automation(text: str) -> str | None:
    lowered = text.lower()
    for automation, hints in AUTOMATION_HINTS.items():
        if any(h in lowered for h in hints):
            return automation
    return None


async def run_automation(state: ITSMState) -> dict:
    started = time.perf_counter()
    message = state.get("redacted_message") or state["message"]
    automation = match_automation(message)

    if not automation:
        return {"resolution_path": "ticket_created", "steps": [
            {"node": "run_automation", "summary": "no safe automation matched",
             "duration_ms": int((time.perf_counter() - started) * 1000), "payload": {}}]}

    result = await itsm_tools.run_safe_automation(
        tenant_id=state["tenant_id"],
        actor=f"agent:{state.get('thread_id','-')}",
        automation=automation,
        params={"requester_id": state.get("user_id", "unknown"),
                "ci_name": state.get("affected_ci")},
    )

    answer = (
        f"{result.get('description', 'Action completed')}. "
        f"Reference: {result.get('run_id', '-')[:8]}.\n\n"
        f"{state.get('draft_answer', '')}"
    ).strip()

    step = {
        "node": "run_automation",
        "summary": f"ran {automation}",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": result,
    }
    return {
        "answer": answer,
        "automation_run": result.get("run_id"),
        "resolution_path": "automation",
        "suggested_actions": ["Confirm this resolved it", "Raise a ticket instead"],
        "steps": [step],
    }


async def create_ticket_node(state: ITSMState) -> dict:
    started = time.perf_counter()
    message = state.get("redacted_message") or state["message"]

    conversation = "\n".join(
        f"{m['role']}: {m['content']}" for m in [*state.get("history", []), {"role": "user", "content": message}]
    )
    try:
        summary = await llm.complete(
            [{"role": "user", "content": SUMMARIZE_TICKET.format(
                conversation=conversation[:6000],
                knowledge=pipeline.format_passages(state.get("retrieved", []))[:3000])}],
            purpose="ticket_summary",
            tenant_id=state["tenant_id"],
            temperature=0.1,
            max_tokens=500,
        )
        description, usage = summary.text, summary.as_usage()
    except Exception as exc:
        log.warning("ticket_summary_failed", error=str(exc))
        description, usage = message, {}

    title = (message.strip().split("\n")[0])[:120] or "Service desk request"
    ticket = await itsm_tools.create_ticket(
        tenant_id=state["tenant_id"],
        actor=f"agent:{state.get('thread_id','-')}",
        title=title,
        description=description,
        requester_id=state.get("user_id", "unknown"),
        kind="service_request" if state.get("intent") == "service_request" else "incident",
        priority=state.get("priority", "P3"),
        category=state.get("category"),
        subcategory=state.get("subcategory"),
        assignment_group=state.get("assignment_group", "Service Desk"),
        ci_name=state.get("affected_ci"),
        confidence=state.get("confidence"),
        attributes={"thread_id": state.get("thread_id"), "channel": state.get("channel"),
                    "is_outage": state.get("is_outage", False)},
    )

    if state.get("citations"):
        await itsm_tools.add_worknote(
            tenant_id=state["tenant_id"], actor="agent", ticket_id=ticket["ticket_id"],
            note="Knowledge consulted by the virtual agent before hand-off.",
            citations=state["citations"],
        )

    answer = (
        f"I've raised **{ticket['external_ref']}** ({ticket['priority']}) with "
        f"{state.get('assignment_group', 'the Service Desk')}. "
        f"Target response: {ticket['sla_due_at'][:16].replace('T', ' ')} UTC.\n\n"
    )
    if state.get("draft_answer") and "could not find" not in state["draft_answer"].lower():
        answer += "In the meantime, this may help:\n\n" + state["draft_answer"]
    else:
        answer += "An analyst will follow up. Reply here to add more detail to the ticket."

    step = {
        "node": "create_ticket",
        "summary": f"created {ticket['external_ref']}",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": ticket,
    }
    return {
        "answer": answer,
        "ticket_id": ticket["ticket_id"],
        "ticket_number": ticket["external_ref"],
        "resolution_path": "ticket_created",
        "suggested_actions": ["Track this ticket", "Add more detail"],
        "usage": merge_usage(state, usage) if usage else state.get("usage", {}),
        "steps": [step],
    }


async def escalate(state: ITSMState) -> dict:
    """P1/outage path: ticket first, then a major-incident hand-off."""
    started = time.perf_counter()
    ticket_result = await create_ticket_node(state)

    answer = (
        f"This looks like a **{state.get('priority', 'P1')}** issue affecting more than one user, "
        f"so I've escalated it immediately.\n\n{ticket_result['answer']}\n\n"
        "The major incident process has been notified. Please keep this thread open for updates."
    )
    if ticket_result.get("ticket_id"):
        await itsm_tools.update_ticket(
            tenant_id=state["tenant_id"], actor="agent",
            ticket_id=ticket_result["ticket_id"], status="escalated",
        )

    return {
        **ticket_result,
        "answer": answer,
        "resolution_path": "escalated",
        "requires_human": True,
        "steps": [*ticket_result["steps"], {
            "node": "escalate", "summary": "escalated to major incident",
            "duration_ms": int((time.perf_counter() - started) * 1000), "payload": {}}],
    }


async def clarify(state: ITSMState) -> dict:
    """Ask exactly one question - service desk users abandon long forms."""
    started = time.perf_counter()
    missing = state.get("missing") or "a bit more detail about what you're seeing"
    result = await llm.complete(
        [
            {"role": "system", "content": "You are a service desk analyst. Ask one short, specific question."},
            {"role": "user", "content":
                f"User said: {state.get('redacted_message') or state['message']}\n"
                f"We still need: {missing}\nAsk one question. No preamble."},
        ],
        purpose="clarify",
        tenant_id=state["tenant_id"],
        temperature=0.3,
        max_tokens=150,
    )
    return {
        "answer": result.text.strip(),
        "resolution_path": "clarify",
        "suggested_actions": ["Raise a ticket anyway"],
        "usage": merge_usage(state, result.as_usage()),
        "steps": [{"node": "clarify", "summary": "asked a follow-up question",
                   "duration_ms": int((time.perf_counter() - started) * 1000),
                   "payload": {"missing": missing}}],
    }


async def small_talk(state: ITSMState) -> dict:
    return {
        "answer": "Hi - I'm the IT service desk assistant. Tell me what's broken, "
                  "what you need access to, or ask about a ticket you've already raised.",
        "resolution_path": "kb_resolution",
        "confidence": 1.0,
        "suggested_actions": ["Reset my password", "Report an outage", "Check ticket status"],
        "steps": [{"node": "small_talk", "summary": "greeting", "duration_ms": 0, "payload": {}}],
    }
