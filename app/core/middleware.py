"""Cross-cutting HTTP middleware: request id, timing, metrics, rate limiting."""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.cache.rate_limit import RateLimiter
from app.core.config import settings
from app.core.errors import RateLimitError
from app.core.logging import get_logger, request_id_ctx, tenant_ctx, trace_ctx
from app.core.observability import LATENCY, REQUESTS

log = get_logger(__name__)


class ContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id + tenant to logs, traces and the response headers."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        tenant = request.headers.get("x-tenant-id", "default")
        request_id_ctx.set(rid)
        tenant_ctx.set(tenant)

        from opentelemetry import trace as _trace

        ctx = _trace.get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            trace_ctx.set(format(ctx.trace_id, "032x"))

        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started

        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, route_path, str(response.status_code), tenant).inc()
        LATENCY.labels(route_path).observe(elapsed)

        response.headers["x-request-id"] = rid
        response.headers["x-response-time-ms"] = f"{elapsed * 1000:.1f}"
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 2),
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    EXEMPT = ("/health", "/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json")

    def __init__(self, app):
        super().__init__(app)
        self.limiter = RateLimiter()

    async def dispatch(self, request: Request, call_next):
        if request.url.path.endswith(self.EXEMPT):
            return await call_next(request)

        identity = request.headers.get("x-api-key") or request.client.host if request.client else "anon"
        key = f"{request.headers.get('x-tenant-id', 'default')}:{identity}"
        try:
            remaining = await self.limiter.hit(key, limit=settings.rate_limit_per_minute)
        except RateLimitError as exc:
            return JSONResponse(status_code=429, content=exc.to_payload(), headers={"retry-after": "60"})
        response = await call_next(request)
        response.headers["x-ratelimit-remaining"] = str(remaining)
        return response


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-response-time-ms", "x-ratelimit-remaining"],
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ContextMiddleware)
