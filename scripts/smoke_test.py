#!/usr/bin/env python
"""End-to-end smoke test. Run after `make up && make seed`."""

from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"
API = f"{BASE}/api/v1"
HEADERS = {"X-API-Key": "local-dev-key", "X-Tenant-Id": "default"}

PROMPTS = [
    "My VPN keeps dropping with error 812, what do I do?",
    "I'm locked out of my account",
    "Nobody in the Mumbai office can reach the ERP, about 200 people affected",
    "What's the SLA for a P2 incident?",
]


async def main() -> int:
    failures = 0
    async with httpx.AsyncClient(timeout=120) as client:
        health = await client.get(f"{BASE}/health/ready")
        print(f"readiness  {health.status_code}  {health.json().get('status')}")
        for name, check in health.json().get("checks", {}).items():
            print(f"   {name:<12} {'ok' if check.get('ok') else 'FAILED'}")
        if health.status_code != 200:
            failures += 1

        jobs = await client.get(f"{API}/ingestion/jobs", headers=HEADERS)
        done = [j for j in jobs.json().get("items", []) if j["status"] == "completed"]
        print(f"\ningestion  {len(done)} completed of {jobs.json().get('total', 0)}")

        search = await client.post(f"{API}/knowledge/search", headers=HEADERS,
                                   json={"query": "vpn error 812", "top_k": 3})
        print(f"search     {search.status_code}  {len(search.json().get('hits', []))} hits "
              f"in {search.json().get('took_ms', '?')}ms")

        thread = None
        for prompt in PROMPTS:
            payload = {"message": prompt, "user_id": "smoke-user"}
            if thread:
                payload["thread_id"] = thread
            r = await client.post(f"{API}/chat", headers=HEADERS, json=payload)
            if r.status_code != 200:
                print(f"\nchat FAILED {r.status_code}: {r.text[:200]}")
                failures += 1
                continue
            data = r.json()
            thread = data["thread_id"]
            print(f"\nQ: {prompt}")
            print(f"   path={data['resolution_path']} priority={data['priority']} "
                  f"confidence={data['confidence']} citations={len(data['citations'])}")
            print(f"   A: {data['answer'][:180]}...")

        metrics = await client.get(f"{API}/admin/metrics/itsm", headers=HEADERS)
        print(f"\nmetrics    {metrics.json()}")

    print(f"\n{'SMOKE TEST PASSED' if not failures else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
