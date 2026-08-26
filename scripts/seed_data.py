#!/usr/bin/env python
"""Load sample KB articles and tickets so the stack does something on first run."""

from __future__ import annotations

import asyncio

import httpx

BASE = "http://localhost:8000/api/v1"
HEADERS = {"X-API-Key": "local-dev-key", "X-Tenant-Id": "default"}

ARTICLES = [
    ("VPN error 812 - certificate expired", "runbook", """# VPN error 812

## Symptoms
The client disconnects roughly every ten minutes and the log shows error 812.

## Cause
The device certificate has expired or was issued by a retired CA.

## Resolution
1. Open Company Portal and choose Devices.
2. Select this machine, then Check access.
3. Wait for the certificate to re-enrol (about two minutes).
4. Restart the VPN client.

## Workaround
Use the web portal at portal.example.com for browser-based apps until the
certificate renews.
"""),
    ("Account lockout after failed sign-ins", "kb_article", """# Account lockout

## Symptoms
Users see "your account has been locked" after repeated failed sign-ins.

## Policy
Five failed attempts lock the account for 30 minutes.

## Resolution
1. Go to selfservice.example.com/unlock.
2. Verify with the authenticator app.
3. Choose Unlock account.

Locked accounts unlock automatically after 30 minutes. Never share or reset a
password on the user's behalf over chat.
"""),
    ("Incident priority and SLA matrix", "policy", """# Priority and SLA matrix

## Priorities
- P1: business-wide outage or revenue impact, no workaround. Response 15 min, resolve 4 h.
- P2: multiple users or a critical single user blocked. Response 30 min, resolve 8 h.
- P3: single user impaired with a workaround. Response 4 h, resolve 24 h.
- P4: informational or cosmetic. Response 1 business day, resolve 72 h.

## Escalation
Any P1 pages the major incident manager immediately and opens a bridge call.
"""),
    ("Mailbox full - quota increase", "sop", """# Mailbox quota

## Symptoms
"Your mailbox is full" and outbound mail queues.

## Standard entitlement
50 GB. Increases to 100 GB are pre-approved for the Sales and Legal groups.

## Resolution
1. Empty Deleted Items and Recoverable Items.
2. Archive anything older than two years to the online archive.
3. If still above 90%, raise a standard quota request; it is auto-approved for
   the entitled groups.
"""),
    ("ERP slow or unreachable", "runbook", """# ERP performance

## Triage
1. Check the status page at status.example.com.
2. Confirm whether the issue is regional by asking for the office location.
3. If more than 20 users report it within 15 minutes, treat it as P1.

## Known causes
- Batch job overrun on the reporting node (usually 02:00-04:00 UTC).
- Certificate rotation on the load balancer.

## Escalation
Route to ERP-Platform-L2 with the office location and a browser HAR file.
"""),
]

TICKETS = [
    {"title": "VPN keeps dropping, error 812", "description": "Since Tuesday the VPN drops every ten minutes.",
     "priority": "P3", "category": "Network", "requester_id": "u1001", "ci_name": "vpn-gateway-01"},
    {"title": "Cannot sign in - account locked", "description": "Locked out after typing the wrong password.",
     "priority": "P3", "category": "Identity & Access", "requester_id": "u1002"},
    {"title": "ERP unreachable from Mumbai office", "description": "About 200 users cannot reach the ERP.",
     "priority": "P1", "category": "ERP", "requester_id": "u1003", "ci_name": "erp-prod"},
]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=60) as client:
        for title, doc_class, content in ARTICLES:
            r = await client.post("/ingestion/text", json={
                "title": title, "content": content,
                "options": {"doc_class": doc_class, "metadata": {"category": "IT"}},
            })
            print(f"KB  {title[:44]:<46} {r.status_code} {r.json().get('status', '')}")

        for ticket in TICKETS:
            r = await client.post("/tickets", json=ticket)
            print(f"TKT {ticket['title'][:44]:<46} {r.status_code}")

    print("\nSeeded. Ingestion runs asynchronously - check GET /api/v1/ingestion/jobs")


if __name__ == "__main__":
    asyncio.run(main())
