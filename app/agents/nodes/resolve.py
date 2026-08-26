"""Draft an answer from the retrieved passages, then judge whether it resolves."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.agents.state import ITSMState, merge_usage
from app.core.config import settings
from app.core.logging import get_logger
from app.llm import client as llm
from app.llm.prompts import ANSWER, RESOLUTION_CHECK, SYSTEM_SERVICE_DESK
from app.retrieval import pipeline

log = get_logger(__name__)

NOT_FOUND = "I could not find this in our knowledge base."


def system_prompt(state: ITSMState) -> str:
    return SYSTEM_SERVICE_DESK.format(
        tenant=state["tenant_id"],
        today=datetime.now(UTC).strftime("%Y-%m-%d"),
        user_id=state.get("user_id", "unknown"),
        channel=state.get("channel", "web"),
    )


def build_messages(state: ITSMState) -> list[dict]:
    passages = pipeline.format_passages(state.get("retrieved", []))
    messages = [{"role": "system", "content": system_prompt(state)}]
    for m in state.get("history", [])[-6:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append(
        {"role": "user", "content": ANSWER.format(
            passages=passages, question=state.get("redacted_message") or state["message"])}
    )
    return messages


async def draft_answer(state: ITSMState) -> dict:
    started = time.perf_counter()
    result = await llm.complete(
        build_messages(state),
        purpose="answer",
        tenant_id=state["tenant_id"],
        session_id=state.get("thread_id"),
        temperature=0.15,
        max_tokens=1000,
    )
    step = {
        "node": "draft_answer",
        "summary": f"{len(result.text)} chars, {result.latency_ms}ms",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": {"model": result.model},
    }
    return {
        "draft_answer": result.text,
        "usage": merge_usage(state, result.as_usage()),
        "steps": [step],
    }


async def check_resolution(state: ITSMState) -> dict:
    """Self-critique gate. Decides KB resolution vs ticket vs escalation."""
    started = time.perf_counter()
    draft = state.get("draft_answer", "")
    hits = state.get("retrieved", [])

    if NOT_FOUND in draft or not hits:
        return {
            "resolves": False,
            "confidence": 0.1,
            "missing": "no grounding in the knowledge base",
            "requires_human": False,
            "risk_flags": ["no_grounding"],
            "steps": [{"node": "check_resolution", "summary": "ungrounded",
                       "duration_ms": int((time.perf_counter() - started) * 1000), "payload": {}}],
        }

    try:
        verdict = await llm.complete_json(
            [{"role": "user", "content": RESOLUTION_CHECK.format(
                issue=state.get("redacted_message") or state["message"],
                answer=draft[:4000], n_passages=len(hits))}],
            purpose="resolution_check",
            tenant_id=state["tenant_id"],
            temperature=0.0,
            max_tokens=400,
        )
    except Exception as exc:
        log.warning("resolution_check_failed", error=str(exc))
        verdict = {"resolves": False, "confidence": 0.4, "needs_human": True,
                   "risk_flags": ["check_failed"]}

    # Retrieval quality feeds the confidence, not just the model's self-report.
    top_score = max((h.get("_rerank_score", 0) for h in hits), default=0)
    retrieval_confidence = min(top_score / 10.0, 1.0) if top_score else 0.4
    confidence = round(
        0.6 * float(verdict.get("confidence", 0.5)) + 0.4 * retrieval_confidence, 3
    )

    step = {
        "node": "check_resolution",
        "summary": f"resolves={verdict.get('resolves')} confidence={confidence}",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": verdict,
    }
    return {
        "resolves": bool(verdict.get("resolves")),
        "confidence": confidence,
        "missing": verdict.get("missing"),
        "requires_human": bool(verdict.get("needs_human")),
        "risk_flags": list(verdict.get("risk_flags", [])),
        "steps": [step],
    }


async def finalize_kb_answer(state: ITSMState) -> dict:
    suggestions = ["Was this helpful?", "Raise a ticket if the issue persists"]
    if state.get("similar_tickets"):
        suggestions.append("Show similar past incidents")
    return {
        "answer": state.get("draft_answer", ""),
        "resolution_path": "kb_resolution",
        "suggested_actions": suggestions,
        "steps": [{"node": "finalize_kb_answer", "summary": "resolved from knowledge base",
                   "duration_ms": 0, "payload": {"confidence": state.get("confidence")}}],
    }
