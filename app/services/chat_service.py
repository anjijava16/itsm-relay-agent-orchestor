"""Orchestrates a chat turn: session → history → graph → persistence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_compiled_graph
from app.agents.state import initial_state
from app.cache.redis_client import Cache
from app.core.logging import get_logger, request_id_ctx
from app.core.observability import span
from app.core.security import Principal
from app.db.models import ChatMessage, ChatSession
from app.schemas.chat import ChatRequest, ChatResponse

log = get_logger(__name__)
_history_cache = Cache("chat:history", ttl=3600)

HISTORY_TURNS = 10


async def _get_or_create_session(
    session: AsyncSession, principal: Principal, req: ChatRequest
) -> ChatSession:
    thread_id = req.thread_id or f"th_{uuid.uuid4().hex[:16]}"
    existing = (
        await session.execute(
            select(ChatSession).where(
                ChatSession.thread_id == thread_id,
                ChatSession.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().first()
    if existing:
        return existing

    chat = ChatSession(
        tenant_id=principal.tenant_id,
        user_id=req.user_id or principal.subject,
        thread_id=thread_id,
        channel=req.channel,
        title=req.message[:120],
        ticket_id=uuid.UUID(req.ticket_id) if req.ticket_id else None,
    )
    session.add(chat)
    await session.flush()
    return chat


async def _load_history(session: AsyncSession, chat: ChatSession) -> list[dict[str, str]]:
    cached = await _history_cache.get(chat.thread_id)
    if cached:
        return cached
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(HISTORY_TURNS)
        )
    ).scalars().all()
    history = [{"role": m.role, "content": m.content} for m in reversed(rows)]
    await _history_cache.set(chat.thread_id, history)
    return history


async def handle_turn(
    session: AsyncSession, principal: Principal, req: ChatRequest
) -> ChatResponse:
    chat = await _get_or_create_session(session, principal, req)
    history = await _load_history(session, chat)

    session.add(ChatMessage(session_id=chat.id, role="user", content=req.message))

    state = initial_state(
        tenant_id=principal.tenant_id,
        user_id=req.user_id or principal.subject,
        thread_id=chat.thread_id,
        channel=req.channel,
        message=req.message,
        history=history,
        metadata={**req.metadata, "roles": principal.roles},
    )

    graph = get_compiled_graph()
    config = {
        "configurable": {"thread_id": chat.thread_id},
        "recursion_limit": 25,
        "metadata": {"tenant_id": principal.tenant_id, "request_id": request_id_ctx.get()},
    }

    with span("agent.invoke", **{"agent.thread_id": chat.thread_id}):
        final: dict[str, Any] = await graph.ainvoke(state, config=config)

    usage = final.get("usage", {})
    assistant_msg = ChatMessage(
        session_id=chat.id,
        role="assistant",
        content=final.get("answer", ""),
        citations=final.get("citations", []),
        model=usage.get("model"),
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        cost_usd=float(usage.get("cost_usd", 0.0)),
        latency_ms=int(usage.get("latency_ms", 0)),
        trace_id=request_id_ctx.get(),
    )
    session.add(assistant_msg)
    await session.flush()

    if final.get("ticket_id"):
        chat.ticket_id = uuid.UUID(final["ticket_id"])
    await _history_cache.delete(chat.thread_id)

    return ChatResponse(
        thread_id=chat.thread_id,
        message_id=str(assistant_msg.id),
        answer=final.get("answer", ""),
        intent=final.get("intent", "unknown"),
        category=final.get("category"),
        priority=final.get("priority"),
        confidence=float(final.get("confidence", 0.0)),
        resolution_path=final.get("resolution_path", "kb_resolution"),
        ticket_id=final.get("ticket_id"),
        ticket_number=final.get("ticket_number"),
        citations=final.get("citations", []),
        suggested_actions=final.get("suggested_actions", []),
        steps=final.get("steps", []),
        usage=usage,
        requires_human=bool(final.get("requires_human")),
    )
