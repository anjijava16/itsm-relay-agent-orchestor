"""Async helpers for Celery workers.

Celery tasks are synchronous. Rather than sharing the API's event-loop-bound
engine across forked workers, each task runs its coroutine in a fresh loop with
its own short-lived engine. Slightly more connection churn, far fewer
"attached to a different loop" incidents at 2am.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

T = TypeVar("T")


def run_async(coro_fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    return asyncio.run(coro_fn(*args, **kwargs))


@asynccontextmanager
async def worker_session():
    engine = create_async_engine(settings.postgres_dsn, pool_size=2, max_overflow=2,
                                pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()
