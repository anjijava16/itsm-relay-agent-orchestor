"""AdminRouter - operational surface for the platform team."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.cache.redis_client import Cache, get_redis
from app.core.config import settings
from app.core.security import CurrentPrincipal
from app.db.models import AuditLog
from app.db.session import DbSession
from app.retrieval import opensearch_store
from app.services import ticket_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics/itsm")
async def itsm_metrics(
    principal: CurrentPrincipal,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
):
    """Deflection rate, MTTR, volume by priority - the numbers ITSM leaders ask for."""
    principal.require_role("admin", "analyst")
    return await ticket_service.metrics(session, principal.tenant_id, days)


@router.get("/audit")
async def audit_log(
    principal: CurrentPrincipal,
    session: DbSession,
    action: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    principal.require_role("admin")
    stmt = select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = (await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
    return [
        {"id": str(r.id), "actor": r.actor, "action": r.action, "resource_type": r.resource_type,
         "resource_id": r.resource_id, "outcome": r.outcome, "payload": r.payload,
         "request_id": r.request_id, "created_at": r.created_at}
        for r in rows
    ]


@router.get("/budget")
async def budget_status(principal: CurrentPrincipal):
    from app.cache import budget

    principal.require_role("admin")
    spent = await budget.check(principal.tenant_id)
    return {"tenant_id": principal.tenant_id, "spent_usd": round(spent, 4),
            "limit_usd": settings.daily_budget_usd,
            "remaining_usd": round(settings.daily_budget_usd - spent, 4)}


@router.post("/index/reindex")
async def recreate_index(principal: CurrentPrincipal):
    """Create the OpenSearch index if it does not exist. Safe to call repeatedly."""
    principal.require_role("admin")
    await opensearch_store.ensure_index()
    return {"index": settings.opensearch_index, "status": "ensured"}


@router.post("/cache/flush")
async def flush_cache(principal: CurrentPrincipal, prefix: Annotated[str, Query()] = "retrieval:query"):
    principal.require_role("admin")
    return {"removed": await Cache(prefix).invalidate_prefix(), "prefix": prefix}


@router.get("/config")
async def effective_config(principal: CurrentPrincipal):
    """What the process actually loaded. Secrets are never returned."""
    principal.require_role("admin")
    return {
        "env": settings.app_env,
        "primary_model": settings.primary_model,
        "fallback_models": settings.fallback_models,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "opensearch_index": settings.opensearch_index,
        "retrieval_top_k": settings.retrieval_top_k,
        "rerank_top_n": settings.rerank_top_n,
        "min_confidence_to_auto_resolve": settings.min_confidence_to_auto_resolve,
        "langfuse_enabled": settings.langfuse_enabled,
        "daily_budget_usd": settings.daily_budget_usd,
    }


@router.get("/queue")
async def queue_depth(principal: CurrentPrincipal):
    principal.require_role("admin")
    r = get_redis()
    return {"ingestion": await r.llen("ingestion"), "celery": await r.llen("celery")}
