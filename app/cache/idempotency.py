"""Idempotency keys.

Ingestion and ticket mutation endpoints are retried aggressively by upstream
ITSM tooling. We store the first response for a key and replay it instead of
re-running the side effect.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.cache.redis_client import get_redis, ns

TTL_SECONDS = 24 * 3600


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def claim(key: str, body_hash: str) -> tuple[bool, dict | None]:
    """Return (is_new, stored_response)."""
    r = get_redis()
    rkey = ns("idem", key)
    ok = await r.set(rkey, json.dumps({"state": "in_flight", "hash": body_hash}), nx=True, ex=TTL_SECONDS)
    if ok:
        return True, None
    stored = json.loads(await r.get(rkey) or "{}")
    return False, stored.get("response")


async def complete(key: str, body_hash: str, response: dict) -> None:
    await get_redis().set(
        ns("idem", key),
        json.dumps({"state": "done", "hash": body_hash, "response": response}, default=str),
        ex=TTL_SECONDS,
    )
