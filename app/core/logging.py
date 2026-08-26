"""Structured JSON logging with request/trace correlation."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.core.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
tenant_ctx: ContextVar[str] = ContextVar("tenant_id", default="-")
trace_ctx: ContextVar[str] = ContextVar("trace_id", default="-")


def _inject_context(_logger, _name, event_dict):
    event_dict["request_id"] = request_id_ctx.get()
    event_dict["tenant_id"] = tenant_ctx.get()
    event_dict["trace_id"] = trace_ctx.get()
    event_dict["service"] = settings.otel_service_name
    event_dict["env"] = settings.app_env
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, settings.log_level.upper())
    )
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.app_env != "local"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper())
        ),
        cache_logger_on_first_use=True,
    )
    # third-party noise
    for noisy in ("httpx", "LiteLLM", "opensearch", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = __name__):
    return structlog.get_logger(name)
