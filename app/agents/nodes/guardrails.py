"""Input and output guardrails.

Cheap deterministic checks run first (regex for secrets, obvious injection),
then one model pass for the fuzzy cases. Failing open on the model call is
deliberate - a Langfuse-visible warning is better than a dead service desk.
"""

from __future__ import annotations

import re
import time

from app.agents.state import ITSMState
from app.core.logging import get_logger
from app.llm import client as llm
from app.llm.prompts import INPUT_GUARDRAIL

log = get_logger(__name__)

SECRET_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[CARD]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"), r"\1: [REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"), "[REDACTED]"),
]

INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore (all |any )?(previous|prior|above) instructions"),
    re.compile(r"(?i)you are now (a|an) \w+"),
    re.compile(r"(?i)(reveal|print|show) (me )?(your )?(system prompt|instructions)"),
    re.compile(r"(?i)disregard (your|the) (rules|guidelines|policy)"),
]


def redact(text: str) -> str:
    out = text
    for pattern, replacement in SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


async def input_guardrail(state: ITSMState) -> dict:
    started = time.perf_counter()
    message = state["message"]
    reasons: list[str] = []

    if any(p.search(message) for p in INJECTION_PATTERNS):
        reasons.append("prompt_injection")

    redacted = redact(message)
    if redacted != message:
        reasons.append("pii_redacted")

    allow = "prompt_injection" not in reasons

    # Model pass only when the deterministic checks did not already decide.
    if allow and len(message) > 40:
        try:
            verdict = await llm.complete_json(
                [{"role": "user", "content": INPUT_GUARDRAIL.format(message=redacted[:3000])}],
                purpose="input_guardrail",
                tenant_id=state["tenant_id"],
                temperature=0.0,
                max_tokens=400,
            )
            model_reasons = [r for r in verdict.get("reasons", []) if r != "none"]
            if not verdict.get("allow", True):
                allow = False
                reasons.extend(model_reasons)
            redacted = verdict.get("redacted_message") or redacted
        except Exception as exc:
            log.warning("input_guardrail_model_failed_failing_open", error=str(exc))

    step = {
        "node": "input_guardrail",
        "summary": "allowed" if allow else f"blocked: {', '.join(reasons)}",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": {"reasons": reasons},
    }
    return {
        "allowed": allow,
        "guardrail_reasons": reasons,
        "redacted_message": redacted,
        "answer": "" if allow else (
            "I can't help with that request. If you believe this is a mistake, "
            "please raise a ticket with the service desk and a human analyst will pick it up."
        ),
        "resolution_path": "kb_resolution" if allow else "blocked",
        "steps": [step],
    }


async def output_guardrail(state: ITSMState) -> dict:
    """Last stop before the answer leaves the process."""
    started = time.perf_counter()
    answer = state.get("answer") or state.get("draft_answer") or ""
    flags = list(state.get("risk_flags") or [])

    cleaned = redact(answer)
    if cleaned != answer:
        flags.append("secrets_redacted")

    if not state.get("citations") and state.get("resolution_path") == "kb_resolution":
        flags.append("no_grounding")

    step = {
        "node": "output_guardrail",
        "summary": f"{len(flags)} flag(s)",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "payload": {"flags": flags},
    }
    return {"answer": cleaned, "risk_flags": flags, "steps": [step]}
