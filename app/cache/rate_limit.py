"""Fixed-window rate limiter implemented as a single atomic Lua script."""

from __future__ import annotations

from app.cache.redis_client import get_redis, ns
from app.core.errors import RateLimitError

_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class RateLimiter:
    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        self._script = None

    async def hit(self, identity: str, limit: int) -> int:
        r = get_redis()
        if self._script is None:
            self._script = r.register_script(_LUA)
        key = ns("ratelimit", identity)
        count = int(await self._script(keys=[key], args=[self.window]))
        if count > limit:
            raise RateLimitError(f"Limit of {limit} requests per {self.window}s exceeded")
        return max(limit - count, 0)
