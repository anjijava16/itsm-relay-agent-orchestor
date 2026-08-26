"""Classify the request: intent, category, priority, assignment group, CI."""

from __future__ import annotations

import time

from app.agents.state import ITSMState, merge_usage
from app.agents.tools import itsm_tools
from app.core.logging import get_logger
from app.llm import client as llm
from app.llm.prompts import TRIAGE

log = get_logger(__name__)

CHITCHAT_HINTS = ("hello", "hi", "hey", "thanks", "thank you", "good morning", "bye")


async def triage(state: ITSMState) -> dict:
    started = time.perf_counter()
    message = state.get("redacted_message") or state["message"]

    if message.strip().lower().rstrip("!.") in CHITCHAT_HINTS:
        return {
            "intent": "chitchat",
            "category": None,
            "priority": "P4",
            "triage_confidence": 0.99,
            "steps": [{"node": "triage", "summary": "chitchat shortcut",
                       "duration_ms": int((time.perf_counter() - started) * 1000), "payload": {}}],
        }

    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-6:])
    prior = await itsm_tools.search_similar_tickets(state["tenant_id"], None, None, limit=5)
    similar_text = "\n".join(f"- {t['title']} ({t['status']})" for t in prior) or "(none)"

    try:
        data = await llm.complete_json(
            [
                {"role": "system", "content": "You are an experienced ITSM triage analyst."},
                {"role": "user", "content": TRIAGE.format(
                    title=message[:200], body=f"{history_text}\n{message}"[:4000],
                    cis=state.get("metadata", {}).get("cis", "unknown"), similar=similar_text)},
            ],
            purpose="triage",
            tenant_id=state["tenant_id"],
            temperature=0.0,
            max_tokens=500,
        )
    except Exception as exc:
        log.warning("triage_failed_using_defaults", error=str(exc))
        data = {"intent": "question", "category": "Unclassified", "priority": "P3",
                "assignment_group": "Service Desk", "confidence": 0.3}

    priority = data.get("priority", "P3")
    if priority not in ("P1", "P2", "P3", "P4"):
        priority = "P3"
    if data.get("is_outage") and priority in ("P3", "P4"):
        priority = "P2"  # outages never sit in the low queues

    step = {
        "node": "triage",
        "summary": f"{data.get('intent')} / {data.get('category')} / {priority}",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": data,
    }
    return {
        "intent": data.get("intent", "question"),
        "category": data.get("category"),
        "subcategory": data.get("subcategory"),
        "priority": priority,
        "assignment_group": data.get("assignment_group", "Service Desk"),
        "affected_ci": data.get("affected_ci"),
        "is_outage": bool(data.get("is_outage")),
        "triage_confidence": float(data.get("confidence", 0.5)),
        "steps": [step],
    }
