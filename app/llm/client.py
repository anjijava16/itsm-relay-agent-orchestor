"""Every model call in this codebase goes through here.

LiteLLM gives us one interface, provider fallbacks, retries and cost
accounting. Nothing else in the app imports openai/anthropic directly - that is
what makes the "governed path" in the architecture diagram real rather than
aspirational.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import litellm
from litellm import acompletion, aembedding

from app.cache import budget
from app.cache.redis_client import Cache
from app.core.config import settings
from app.core.errors import UpstreamError
from app.core.logging import get_logger
from app.core.observability import LLM_CALLS, LLM_COST, LLM_TOKENS, span

log = get_logger(__name__)

litellm.drop_params = True
litellm.set_verbose = False
litellm.num_retries = settings.llm_max_retries
litellm.request_timeout = settings.llm_timeout_seconds

_semantic_cache = Cache("llm:completion", ttl=3600)
_embedding_cache = Cache("llm:embedding", ttl=7 * 24 * 3600)


class LLMResult:
    __slots__ = ("text", "model", "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "raw")

    def __init__(self, text: str, model: str, prompt_tokens: int, completion_tokens: int,
                 cost_usd: float, latency_ms: int, raw: Any = None):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
        self.raw = raw

    def as_usage(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


def _langfuse_metadata(tenant_id: str, purpose: str, trace_id: str | None, session_id: str | None):
    if not settings.langfuse_enabled:
        return {}
    return {
        "metadata": {
            "generation_name": purpose,
            "trace_id": trace_id,
            "session_id": session_id,
            "tags": [settings.app_env, purpose],
            "trace_user_id": tenant_id,
        }
    }


async def complete(
    messages: list[dict[str, Any]],
    *,
    purpose: str = "general",
    tenant_id: str = "default",
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1200,
    response_format: dict | None = None,
    tools: list[dict] | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> LLMResult:
    """Non-streaming completion with fallbacks, budget guard and cost accounting."""
    await budget.check(tenant_id)
    chosen = model or settings.primary_model
    started = time.perf_counter()

    kwargs: dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": settings.llm_timeout_seconds,
        "num_retries": settings.llm_max_retries,
        **_langfuse_metadata(tenant_id, purpose, trace_id, session_id),
    }
    if settings.fallback_models:
        kwargs["fallbacks"] = settings.fallback_models
    if response_format:
        kwargs["response_format"] = response_format
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    with span("llm.complete", **{"llm.model": chosen, "llm.purpose": purpose}):
        try:
            response = await acompletion(**kwargs)
        except Exception as exc:
            LLM_CALLS.labels(chosen, "error", purpose).inc()
            log.error("llm_call_failed", model=chosen, purpose=purpose, error=str(exc))
            raise UpstreamError(f"Model call failed for purpose '{purpose}'") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    try:
        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        cost = 0.0

    LLM_CALLS.labels(chosen, "success", purpose).inc()
    LLM_TOKENS.labels(chosen, "prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(chosen, "completion").inc(completion_tokens)
    LLM_COST.labels(chosen, tenant_id).inc(cost)
    await budget.record(tenant_id, cost)

    message = response.choices[0].message
    text = message.get("content") if isinstance(message, dict) else message.content
    return LLMResult(text or "", chosen, prompt_tokens, completion_tokens, cost, latency_ms, response)


async def complete_json(
    messages: list[dict[str, Any]], *, purpose: str, tenant_id: str = "default", **kwargs
) -> dict[str, Any]:
    """Structured output helper. Retries once with a repair prompt on bad JSON."""
    result = await complete(
        messages, purpose=purpose, tenant_id=tenant_id,
        response_format={"type": "json_object"}, **kwargs
    )
    try:
        return json.loads(_strip_fences(result.text))
    except json.JSONDecodeError:
        log.warning("json_parse_failed_retrying", purpose=purpose)
        repair = [
            *messages,
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": "That was not valid JSON. Return only the JSON object."},
        ]
        retry = await complete(
            repair, purpose=f"{purpose}.repair", tenant_id=tenant_id,
            response_format={"type": "json_object"}, **kwargs
        )
        try:
            return json.loads(_strip_fences(retry.text))
        except json.JSONDecodeError as exc:
            raise UpstreamError("Model did not return parseable JSON") from exc


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


async def stream(
    messages: list[dict[str, Any]],
    *,
    purpose: str = "chat",
    tenant_id: str = "default",
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1200,
    trace_id: str | None = None,
) -> AsyncIterator[str]:
    """Token stream for SSE responses."""
    await budget.check(tenant_id)
    chosen = model or settings.primary_model
    kwargs: dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        **_langfuse_metadata(tenant_id, purpose, trace_id, None),
    }
    if settings.fallback_models:
        kwargs["fallbacks"] = settings.fallback_models
    try:
        response = await acompletion(**kwargs)
        async for chunk in response:
            delta = chunk.choices[0].delta
            piece = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
            if piece:
                yield piece
        LLM_CALLS.labels(chosen, "success", purpose).inc()
    except Exception as exc:
        LLM_CALLS.labels(chosen, "error", purpose).inc()
        raise UpstreamError("Streaming model call failed") from exc


async def embed(texts: list[str], *, tenant_id: str = "default", use_cache: bool = True) -> list[list[float]]:
    """Batch embeddings with a content-hash cache (re-ingest is cheap)."""
    import hashlib

    if not texts:
        return []

    vectors: list[list[float] | None] = [None] * len(texts)
    pending: list[tuple[int, str]] = []

    if use_cache:
        for i, text in enumerate(texts):
            key = hashlib.sha256(f"{settings.embedding_model}:{text}".encode()).hexdigest()
            cached = await _embedding_cache.get(key)
            if cached:
                vectors[i] = cached
            else:
                pending.append((i, text))
    else:
        pending = list(enumerate(texts))

    if pending:
        with span("llm.embed", **{"llm.model": settings.embedding_model, "batch": len(pending)}):
            try:
                response = await aembedding(
                    model=settings.embedding_model,
                    input=[t for _, t in pending],
                    timeout=settings.llm_timeout_seconds,
                )
            except Exception as exc:
                LLM_CALLS.labels(settings.embedding_model, "error", "embedding").inc()
                raise UpstreamError("Embedding call failed") from exc

        LLM_CALLS.labels(settings.embedding_model, "success", "embedding").inc()
        for (idx, text), item in zip(pending, response.data, strict=False):
            vec = item["embedding"] if isinstance(item, dict) else item.embedding
            vectors[idx] = vec
            if use_cache:
                key = hashlib.sha256(f"{settings.embedding_model}:{text}".encode()).hexdigest()
                await _embedding_cache.set(key, vec)

    return [v or [0.0] * settings.embedding_dim for v in vectors]


async def embed_one(text: str, *, tenant_id: str = "default") -> list[float]:
    return (await embed([text], tenant_id=tenant_id))[0]
