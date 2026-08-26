"""The ITSM agent graph.

    input_guardrail
          │
      (blocked?) ──────────────────────────────► output_guardrail
          │
        triage
          │
     ┌────┴───────────────┐
 chitchat            everything else
     │                    │
 small_talk           retrieve
                          │
                     draft_answer
                          │
                    check_resolution
                          │
        ┌────────┬────────┼─────────┬──────────┐
   escalate  automation  kb_answer  clarify  create_ticket
        └────────┴────────┼─────────┴──────────┘
                          │
                   persist_outcome
                          │
                   output_guardrail

Routing is a plain function over state, not a model call - so the same input
always takes the same branch and the behaviour is testable.
"""

from __future__ import annotations

import time

from langgraph.graph import END, StateGraph

from app.agents.checkpointer import get_checkpointer
from app.agents.nodes import act, guardrails, resolve, retrieve, triage
from app.agents.state import ITSMState
from app.agents.tools import itsm_tools
from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import AGENT_NODE_SECONDS, AGENT_ROUTES

log = get_logger(__name__)
_graph = None


def _instrument(name: str, fn):
    async def wrapped(state: ITSMState):
        started = time.perf_counter()
        try:
            return await fn(state)
        except Exception as exc:
            log.exception("agent_node_failed", node=name, error=str(exc))
            return {"errors": [f"{name}: {exc}"],
                    "steps": [{"node": name, "summary": f"failed: {exc}",
                               "duration_ms": int((time.perf_counter() - started) * 1000),
                               "payload": {}}]}
        finally:
            AGENT_NODE_SECONDS.labels(name).observe(time.perf_counter() - started)

    return wrapped


# ------------------------------------------------------------------ routers
def route_after_guardrail(state: ITSMState) -> str:
    return "triage" if state.get("allowed", True) else "output_guardrail"


def route_after_triage(state: ITSMState) -> str:
    return "small_talk" if state.get("intent") == "chitchat" else "retrieve"


def route_after_check(state: ITSMState) -> str:
    """The core decision: who or what handles this request."""
    decision = _decide(state)
    AGENT_ROUTES.labels(decision).inc()
    log.info("agent_route", decision=decision, confidence=state.get("confidence"),
             intent=state.get("intent"), priority=state.get("priority"))
    return decision


def _decide(state: ITSMState) -> str:
    if state.get("is_outage") or state.get("priority") == "P1":
        return "escalate"

    if "destructive_action" in (state.get("risk_flags") or []):
        return "create_ticket"

    if state.get("requires_human"):
        return "create_ticket"

    confidence = state.get("confidence", 0.0)
    resolves = state.get("resolves", False)

    if act.match_automation(state.get("redacted_message") or state.get("message", "")):
        if state.get("intent") in ("service_request", "incident"):
            return "run_automation"

    if resolves and confidence >= settings.min_confidence_to_auto_resolve:
        return "finalize_kb_answer"

    # Middle band: we half-know the answer. Ask one question rather than
    # burning an analyst's time or bluffing.
    if 0.35 <= confidence < settings.min_confidence_to_auto_resolve and state.get("missing"):
        return "clarify"

    return "create_ticket"


# ------------------------------------------------------------------ persistence
async def persist_outcome(state: ITSMState) -> dict:
    """Write the audit trail for whatever the agent just decided."""
    await itsm_tools.audit(
        tenant_id=state["tenant_id"],
        actor=f"agent:{state.get('thread_id','-')}",
        action="agent.decision",
        resource_type="conversation",
        resource_id=state.get("thread_id"),
        payload={
            "resolution_path": state.get("resolution_path"),
            "confidence": state.get("confidence"),
            "intent": state.get("intent"),
            "priority": state.get("priority"),
            "ticket_id": state.get("ticket_id"),
            "citations": [c["chunk_id"] for c in state.get("citations", [])],
            "risk_flags": state.get("risk_flags", []),
            "errors": state.get("errors", []),
        },
    )
    return {"steps": [{"node": "persist_outcome", "summary": "audited", "duration_ms": 0, "payload": {}}]}


# ------------------------------------------------------------------ build
def build_graph():
    g = StateGraph(ITSMState)

    g.add_node("input_guardrail", _instrument("input_guardrail", guardrails.input_guardrail))
    g.add_node("triage", _instrument("triage", triage.triage))
    g.add_node("small_talk", _instrument("small_talk", act.small_talk))
    g.add_node("retrieve", _instrument("retrieve", retrieve.retrieve))
    g.add_node("draft_answer", _instrument("draft_answer", resolve.draft_answer))
    g.add_node("check_resolution", _instrument("check_resolution", resolve.check_resolution))
    g.add_node("finalize_kb_answer", _instrument("finalize_kb_answer", resolve.finalize_kb_answer))
    g.add_node("run_automation", _instrument("run_automation", act.run_automation))
    g.add_node("create_ticket", _instrument("create_ticket", act.create_ticket_node))
    g.add_node("escalate", _instrument("escalate", act.escalate))
    g.add_node("clarify", _instrument("clarify", act.clarify))
    g.add_node("persist_outcome", _instrument("persist_outcome", persist_outcome))
    g.add_node("output_guardrail", _instrument("output_guardrail", guardrails.output_guardrail))

    g.set_entry_point("input_guardrail")
    g.add_conditional_edges("input_guardrail", route_after_guardrail,
                            {"triage": "triage", "output_guardrail": "output_guardrail"})
    g.add_conditional_edges("triage", route_after_triage,
                            {"small_talk": "small_talk", "retrieve": "retrieve"})
    g.add_edge("small_talk", "persist_outcome")
    g.add_edge("retrieve", "draft_answer")
    g.add_edge("draft_answer", "check_resolution")
    g.add_conditional_edges(
        "check_resolution",
        route_after_check,
        {
            "finalize_kb_answer": "finalize_kb_answer",
            "run_automation": "run_automation",
            "create_ticket": "create_ticket",
            "escalate": "escalate",
            "clarify": "clarify",
        },
    )
    for node in ("finalize_kb_answer", "run_automation", "create_ticket", "escalate", "clarify"):
        g.add_edge(node, "persist_outcome")
    g.add_edge("persist_outcome", "output_guardrail")
    g.add_edge("output_guardrail", END)
    return g


def get_compiled_graph():
    global _graph
    if _graph is None:
        _graph = build_graph().compile(
            checkpointer=get_checkpointer(),
            # Human-in-the-loop seam: uncomment to require approval before any
            # ticket write, then resume with graph.aupdate_state + ainvoke(None).
            # interrupt_before=["create_ticket"],
        )
        log.info("agent_graph_compiled")
    return _graph


def reset_graph() -> None:
    global _graph
    _graph = None
