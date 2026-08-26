"""ChatRouter - the conversational surface of the service desk agent."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.agents.graph import get_compiled_graph
from app.agents.state import initial_state
from app.core.logging import get_logger
from app.core.security import CurrentPrincipal
from app.db.models import ChatMessage, ChatSession, Feedback
from app.db.session import DbSession
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    FeedbackIn,
    MessageOut,
    SessionOut,
)
from app.schemas.common import Page
from app.services import chat_service

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, summary="Send a message to the ITSM agent")
async def chat(req: ChatRequest, principal: CurrentPrincipal, session: DbSession) -> ChatResponse:
    principal.require_role("user", "agent.invoke")
    return await chat_service.handle_turn(session, principal, req)


@router.post("/stream", summary="Server-sent event stream of an agent turn")
async def chat_stream(req: ChatRequest, principal: CurrentPrincipal, session: DbSession):
    """Streams node-level progress then the final answer.

    We stream *graph events* rather than raw tokens: for a service desk the
    useful signal is "searching the knowledge base", "raising a ticket", not a
    token trickle. The final event carries the full ChatResponse payload.
    """
    principal.require_role("user", "agent.invoke")
    chat_session = await chat_service._get_or_create_session(session, principal, req)
    history = await chat_service._load_history(session, chat_session)
    session.add(ChatMessage(session_id=chat_session.id, role="user", content=req.message))
    await session.commit()

    state = initial_state(
        tenant_id=principal.tenant_id,
        user_id=req.user_id or principal.subject,
        thread_id=chat_session.thread_id,
        channel=req.channel,
        message=req.message,
        history=history,
        metadata=req.metadata,
    )
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": chat_session.thread_id}, "recursion_limit": 25}

    NODE_LABELS = {
        "input_guardrail": "Checking your message",
        "triage": "Classifying the request",
        "retrieve": "Searching the knowledge base",
        "draft_answer": "Drafting an answer",
        "check_resolution": "Checking the answer holds up",
        "run_automation": "Running the automation",
        "create_ticket": "Raising a ticket",
        "escalate": "Escalating",
        "clarify": "Preparing a follow-up question",
    }

    async def event_stream():
        final: dict = {}
        try:
            async for event in graph.astream(state, config=config, stream_mode="updates"):
                for node, update in event.items():
                    final.update(update or {})
                    yield {
                        "event": "progress",
                        "data": json.dumps({"node": node, "label": NODE_LABELS.get(node, node)}),
                    }
            answer = final.get("answer", "")
            assistant = ChatMessage(
                session_id=chat_session.id, role="assistant", content=answer,
                citations=final.get("citations", []),
                model=(final.get("usage") or {}).get("model"),
            )
            session.add(assistant)
            await session.commit()
            yield {
                "event": "final",
                "data": json.dumps({
                    "thread_id": chat_session.thread_id,
                    "message_id": str(assistant.id),
                    "answer": answer,
                    "citations": final.get("citations", []),
                    "resolution_path": final.get("resolution_path"),
                    "ticket_number": final.get("ticket_number"),
                    "confidence": final.get("confidence", 0.0),
                }, default=str),
            }
        except Exception as exc:
            log.exception("chat_stream_failed", error=str(exc))
            yield {"event": "error", "data": json.dumps({"message": "The agent run failed"})}

    return EventSourceResponse(event_stream())


@router.get("/sessions", response_model=Page[SessionOut])
async def list_sessions(
    principal: CurrentPrincipal,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[SessionOut]:
    from sqlalchemy import func

    stmt = (
        select(ChatSession)
        .where(ChatSession.tenant_id == principal.tenant_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit).offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    total = int((await session.execute(
        select(func.count()).select_from(ChatSession)
        .where(ChatSession.tenant_id == principal.tenant_id)
    )).scalar_one())
    return Page(
        items=[SessionOut(id=str(r.id), thread_id=r.thread_id, title=r.title,
                          channel=r.channel, is_open=r.is_open, created_at=r.created_at)
               for r in rows],
        total=total, limit=limit, offset=offset,
    )


@router.get("/sessions/{thread_id}/messages", response_model=list[MessageOut])
async def get_messages(thread_id: str, principal: CurrentPrincipal, session: DbSession):
    chat_session = (await session.execute(
        select(ChatSession).where(ChatSession.thread_id == thread_id,
                                  ChatSession.tenant_id == principal.tenant_id)
    )).scalars().first()
    if not chat_session:
        return []
    rows = (await session.execute(
        select(ChatMessage).where(ChatMessage.session_id == chat_session.id)
        .order_by(ChatMessage.created_at.asc())
    )).scalars().all()
    return [
        MessageOut(id=str(m.id), role=m.role, content=m.content, citations=m.citations,
                   model=m.model, created_at=m.created_at)
        for m in rows
    ]


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(payload: FeedbackIn, principal: CurrentPrincipal, session: DbSession):
    """Thumbs up/down feeds the offline eval set - see app/evals."""
    session.add(Feedback(
        tenant_id=principal.tenant_id,
        message_id=uuid.UUID(payload.message_id) if payload.message_id else None,
        ticket_id=uuid.UUID(payload.ticket_id) if payload.ticket_id else None,
        rating=payload.rating, reason=payload.reason, comment=payload.comment,
        submitted_by=principal.subject,
    ))
    return {"ok": True}
