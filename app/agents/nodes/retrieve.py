"""Pull grounding passages and lookalike incidents."""

from __future__ import annotations

import time

from app.agents.state import ITSMState
from app.agents.tools import itsm_tools
from app.core.logging import get_logger
from app.retrieval import pipeline

log = get_logger(__name__)


async def retrieve(state: ITSMState) -> dict:
    started = time.perf_counter()
    question = state.get("redacted_message") or state["message"]
    history = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-4:])

    filters: dict = {"only_active": True}
    if state.get("affected_ci"):
        filters["ci_name"] = [state["affected_ci"]]
    acl = state.get("metadata", {}).get("acl_groups")
    if acl:
        filters["acl_groups"] = acl

    result = await pipeline.retrieve(
        question=question,
        tenant_id=state["tenant_id"],
        filters=filters,
        do_rewrite=True,
        do_rerank=True,
        do_compress=len(question) > 400,
        history=history,
    )

    # If CI filtering starved the result set, retry unfiltered rather than
    # answering from nothing.
    if not result.hits and "ci_name" in filters:
        log.info("retrieval_retry_without_ci_filter")
        result = await pipeline.retrieve(
            question=question, tenant_id=state["tenant_id"],
            filters={"only_active": True}, do_rewrite=False, history=history,
        )

    similar = await itsm_tools.search_similar_tickets(
        state["tenant_id"], state.get("category"), state.get("affected_ci"), limit=3
    )

    step = {
        "node": "retrieve",
        "summary": f"{len(result.hits)} passages from {result.total_candidates} candidates",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": {"queries": result.rewritten_queries, "strategy": result.strategy},
    }
    return {
        "retrieved": result.hits,
        "rewritten_queries": result.rewritten_queries,
        "citations": pipeline.to_citations(result.hits),
        "similar_tickets": similar,
        "steps": [step],
    }
