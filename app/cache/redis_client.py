"""Redis: cache, TTL state, rate-limit counters, idempotency, session memory."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
            health_check_interval=30,
            socket_keepalive=True,
        )
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def ns(*parts: str) -> str:
    return ":".join([settings.redis_namespace, *parts])


class Cache:
    """Thin JSON cache with namespaced keys and a default TTL."""

    def __init__(self, prefix: str, ttl: int | None = None):
        self.prefix = prefix
        self.ttl = ttl or settings.cache_ttl_seconds

    def _key(self, key: str) -> str:
        return ns(self.prefix, key)

    async def get(self, key: str) -> Any | None:
        raw = await get_redis().get(self._key(key))
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await get_redis().set(self._key(key), json.dumps(value, default=str), ex=ttl or self.ttl)

    async def delete(self, key: str) -> None:
        await get_redis().delete(self._key(key))

    async def invalidate_prefix(self) -> int:
        r = get_redis()
        removed = 0
        async for k in r.scan_iter(match=ns(self.prefix, "*"), count=500):
            await r.delete(k)
            removed += 1
        return removed
