"""KnowledgeRouter - search and grounded answers without the full agent loop."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.security import CurrentPrincipal
from app.db.session import DbSession
from app.schemas.knowledge import (
    AnswerRequest,
    AnswerResponse,
    SearchRequest,
    SearchResponse,
)
from app.services import knowledge_service

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/search", response_model=SearchResponse,
             summary="Hybrid search over the knowledge base")
async def search(req: SearchRequest, principal: CurrentPrincipal, session: DbSession):
    return await knowledge_service.search(session, principal.tenant_id, req)


@router.post("/answer", response_model=AnswerResponse,
             summary="Grounded answer with citations")
async def answer(req: AnswerRequest, principal: CurrentPrincipal, session: DbSession):
    return await knowledge_service.answer(session, principal.tenant_id, req)
