"""Health, readiness and Prometheus scrape endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.cache.redis_client import get_redis
from app.core.config import settings
from app.db.session import engine
from app.retrieval import opensearch_store

router = APIRouter(tags=["ops"])


@router.get("/health/live", summary="Liveness - is the process up")
async def live():
    return {"status": "ok", "service": settings.app_name, "env": settings.app_env}


@router.get("/health/ready", summary="Readiness - are dependencies reachable")
async def ready(response: Response):
    async def check_pg():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def check_redis():
        try:
            await get_redis().ping()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def check_os():
        info = await opensearch_store.health()
        return {"ok": info.get("reachable", False), **info}

    pg, redis_status, os_status = await asyncio.gather(check_pg(), check_redis(), check_os())
    checks = {"postgres": pg, "redis": redis_status, "opensearch": os_status}
    healthy = all(c["ok"] for c in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ready" if healthy else "degraded", "checks": checks}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
