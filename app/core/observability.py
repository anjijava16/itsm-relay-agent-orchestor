"""OpenTelemetry tracing, Prometheus metrics and Langfuse wiring.

Observability is deliberately *out of band* (see the diagram): nothing in the
request path blocks on a trace export, and every exporter degrades to a no-op
when its endpoint is not configured.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
_tracer: trace.Tracer | None = None

# ---------------------------------------------------------------- metrics
REQUESTS = Counter(
    "itsm_http_requests_total", "HTTP requests", ["method", "route", "status", "tenant"]
)
LATENCY = Histogram(
    "itsm_http_request_seconds",
    "HTTP latency",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
LLM_CALLS = Counter("itsm_llm_calls_total", "LLM calls", ["model", "outcome", "purpose"])
LLM_TOKENS = Counter("itsm_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_COST = Counter("itsm_llm_cost_usd_total", "Estimated LLM spend", ["model", "tenant"])
AGENT_NODE_SECONDS = Histogram("itsm_agent_node_seconds", "Agent node duration", ["node"])
AGENT_ROUTES = Counter("itsm_agent_routes_total", "Agent routing decisions", ["decision"])
RETRIEVAL_HITS = Histogram(
    "itsm_retrieval_hits", "Docs returned after fusion", ["strategy"], buckets=(0, 1, 3, 5, 10, 30)
)
INGEST_DOCS = Counter("itsm_ingest_documents_total", "Documents ingested", ["status"])
QUEUE_DEPTH = Gauge("itsm_ingest_queue_depth", "Pending ingestion jobs")


def configure_tracing() -> None:
    global _tracer
    resource = Resource.create(
        {"service.name": settings.otel_service_name, "deployment.environment": settings.app_env}
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter_otlp_endpoint:
        with suppress(Exception):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
                )
            )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.otel_service_name)
    log.info("tracing_configured", endpoint=settings.otel_exporter_otlp_endpoint)


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer(settings.otel_service_name)
    return _tracer


@contextmanager
def span(name: str, **attrs: Any):
    with get_tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)
        yield sp


def configure_langfuse() -> None:
    """Register Langfuse as a LiteLLM callback so every model call is traced."""
    if not settings.langfuse_enabled:
        log.info("langfuse_disabled")
        return
    try:
        import litellm

        litellm.success_callback = list({*(litellm.success_callback or []), "langfuse"})
        litellm.failure_callback = list({*(litellm.failure_callback or []), "langfuse"})
        log.info("langfuse_configured", host=settings.langfuse_host)
    except Exception as exc:  # pragma: no cover - optional dependency path
        log.warning("langfuse_configuration_failed", error=str(exc))
