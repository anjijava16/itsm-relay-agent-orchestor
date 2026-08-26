"""Offline eval harness.

Run it against a live stack:  python -m app.evals.runner
It scores routing accuracy, groundedness and safety, and prints a table you can
paste into a change record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from app.agents.checkpointer import setup_checkpointer
from app.agents.graph import get_compiled_graph
from app.agents.state import initial_state
from app.core.logging import configure_logging, get_logger
from app.evals.dataset import GOLDEN_SET, EvalCase

log = get_logger(__name__)


async def run_case(case: EvalCase, tenant_id: str = "eval") -> dict:
    graph = get_compiled_graph()
    state = initial_state(
        tenant_id=tenant_id, user_id="eval-user", thread_id=f"eval-{case.id}",
        channel="web", message=case.message,
    )
    final = await graph.ainvoke(
        state, config={"configurable": {"thread_id": f"eval-{case.id}"}, "recursion_limit": 25}
    )
    answer = (final.get("answer") or "").lower()

    checks = {
        "intent": case.expected_intent in (final.get("intent"), "unknown")
                  or final.get("intent") == case.expected_intent,
        "priority": case.expected_priority is None or final.get("priority") == case.expected_priority,
        "path": case.expected_path is None or final.get("resolution_path") == case.expected_path,
        "must_contain": all(s.lower() in answer for s in case.must_contain),
        "safety": not any(s.lower() in answer for s in case.must_not_contain),
        "grounded": bool(final.get("citations")) or final.get("resolution_path") != "kb_resolution",
    }
    return {
        "case_id": case.id,
        "passed": all(checks.values()),
        "checks": checks,
        "actual": {
            "intent": final.get("intent"),
            "priority": final.get("priority"),
            "resolution_path": final.get("resolution_path"),
            "confidence": final.get("confidence"),
            "citations": len(final.get("citations", [])),
        },
        "cost_usd": (final.get("usage") or {}).get("cost_usd", 0.0),
    }


async def main() -> None:
    configure_logging()
    await setup_checkpointer()
    results = [await run_case(c) for c in GOLDEN_SET]

    passed = sum(r["passed"] for r in results)
    total_cost = sum(r["cost_usd"] for r in results)

    print(f"\n{'case':<12}{'pass':<7}{'intent':<18}{'path':<16}{'conf':<7}cites")
    print("-" * 68)
    for r in results:
        a = r["actual"]
        print(f"{r['case_id']:<12}{'PASS' if r['passed'] else 'FAIL':<7}"
              f"{str(a['intent']):<18}{str(a['resolution_path']):<16}"
              f"{a['confidence']:<7}{a['citations']}")
    print("-" * 68)
    print(f"{passed}/{len(results)} passed   estimated spend ${total_cost:.4f}\n")

    for r in results:
        if not r["passed"]:
            print(f"  {r['case_id']}: failed {[k for k, v in r['checks'].items() if not v]}")

    with open("eval-results.json", "w") as fh:
        json.dump(results, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
