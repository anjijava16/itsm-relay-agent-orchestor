"""FastAPI application factory and process lifecycle."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.agents.checkpointer import setup_checkpointer, teardown_checkpointer
from app.agents.graph import get_compiled_graph, reset_graph
from app.cache.redis_client import close_redis, get_redis
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware
from app.core.observability import configure_langfuse, configure_tracing
from app.retrieval import opensearch_store
from app.routers import admin, chat, health, ingestion, knowledge, tickets

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    configure_tracing()
    configure_langfuse()
    log.info("starting", env=settings.app_env, model=settings.primary_model)

    try:
        await get_redis().ping()
    except Exception as exc:
        log.error("redis_unreachable_at_boot", error=str(exc))

    try:
        await opensearch_store.ensure_index()
    except Exception as exc:
        log.error("opensearch_unreachable_at_boot", error=str(exc))

    await setup_checkpointer()
    get_compiled_graph()
    log.info("startup_complete")

    yield

    log.info("shutting_down")
    reset_graph()
    await teardown_checkpointer()
    await opensearch_store.close_client()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ITSM Agentic Platform",
        version="0.1.0",
        description=(
            "Generative AI across the IT service lifecycle: conversational service desk, "
            "AI triage, knowledge ingestion and hybrid retrieval, automation, problem "
            "management and governed model access."
        ),
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_prod else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_prod else None,
    )

    register_middleware(app)
    register_exception_handlers(app)

    app.include_router(health.router)
    for r in (chat.router, ingestion.router, tickets.router, knowledge.router, admin.router):
        app.include_router(r, prefix=settings.api_prefix)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="/health.*,/metrics")

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": settings.app_name, "version": "0.1.0", "docs": "/docs"}

    return app


app = create_app()
