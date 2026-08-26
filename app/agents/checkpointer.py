"""LangGraph checkpointing in Postgres.

Checkpoints are what make the agent restartable and auditable: a conversation
that crashed mid-tool-call resumes from the last committed node instead of
starting over, and human-in-the-loop approvals can suspend a run for hours.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_checkpointer = None
_cm = None


async def setup_checkpointer():
    """Called once at startup. Falls back to in-memory if Postgres is unavailable."""
    global _checkpointer, _cm
    if _checkpointer is not None:
        return _checkpointer
    dsn = settings.postgres_sync_dsn
    try:
        _cm = AsyncPostgresSaver.from_conn_string(dsn)
        _checkpointer = await _cm.__aenter__()
        await _checkpointer.setup()
        log.info("checkpointer_ready", backend="postgres")
    except Exception as exc:
        log.warning("checkpointer_postgres_unavailable_using_memory", error=str(exc))
        _checkpointer = MemorySaver()
    return _checkpointer


async def teardown_checkpointer() -> None:
    global _checkpointer, _cm
    if _cm is not None:
        try:
            await _cm.__aexit__(None, None, None)
        except Exception:  # pragma: no cover
            pass
    _checkpointer, _cm = None, None


def get_checkpointer():
    return _checkpointer
