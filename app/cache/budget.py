"""Per-tenant daily spend guard, tracked in Redis with a day-scoped key."""

from __future__ import annotations

from datetime import UTC, datetime

from app.cache.redis_client import get_redis, ns
from app.core.config import settings
from app.core.errors import BudgetExceededError


def _key(tenant_id: str) -> str:
    return ns("budget", datetime.now(UTC).strftime("%Y%m%d"), tenant_id)


async def check(tenant_id: str) -> float:
    spent = float(await get_redis().get(_key(tenant_id)) or 0.0)
    if spent >= settings.daily_budget_usd:
        raise BudgetExceededError(f"Tenant {tenant_id} used ${spent:.2f} today")
    return spent


async def record(tenant_id: str, cost_usd: float) -> float:
    r = get_redis()
    key = _key(tenant_id)
    new_total = await r.incrbyfloat(key, cost_usd)
    await r.expire(key, 3 * 24 * 3600)
    return float(new_total)
