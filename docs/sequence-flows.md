# Sequence flows

Every significant path through the system, as a sequence diagram. These are the
diagrams to put in front of an architecture review board.

---

## 1. Chat — full detail, KB resolution

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant MW as Middleware
    participant SEC as security.get_principal
    participant CR as ChatRouter
    participant CS as chat_service
    participant RD as Redis
    participant PG as Postgres
    participant G as LangGraph
    participant LL as LiteLLM
    participant OS as OpenSearch

    U->>MW: POST /api/v1/chat
    MW->>MW: set request_id, tenant_id, trace_id contextvars
    MW->>RD: INCR ratelimit:{tenant}:{ip} EX 60
    RD-->>MW: 12 of 60
    MW->>SEC: resolve credentials
    SEC-->>MW: Principal(tenant=acme, roles=[user])
    MW->>CR: validated ChatRequest

    CR->>CS: handle_turn(session, principal, req)
    CS->>PG: SELECT chat_sessions WHERE thread_id
    alt new conversation
        CS->>PG: INSERT chat_sessions (thread_id = th_xxx)
    end
    CS->>RD: GET chat:history:{thread_id}
    alt cache miss
        CS->>PG: SELECT last 10 chat_messages
        CS->>RD: SET chat:history TTL 1h
    end
    CS->>PG: INSERT chat_messages (role=user)
    CS->>G: ainvoke(state, config={thread_id})

    rect rgb(45,20,22)
        Note over G,LL: input_guardrail
        G->>G: 4 injection regexes, secret redaction
        G->>LL: guardrail model pass (message > 40 chars)
        LL-->>G: {allow: true, reasons: [], redacted_message}
        G->>PG: checkpoint
    end

    rect rgb(45,27,61)
        Note over G,LL: triage
        G->>PG: recent resolved tickets for context
        G->>LL: TRIAGE prompt, response_format=json_object
        LL-->>G: {intent, category, priority, ci, is_outage, confidence}
        G->>G: force P2 if is_outage and priority in (P3,P4)
        G->>PG: checkpoint
    end

    rect rgb(20,29,51)
        Note over G,OS: retrieve
        G->>RD: GET retrieval:query:rw:{hash}
        alt cache miss
            G->>LL: QUERY_REWRITE → 3 variants
            G->>RD: SET TTL 10m
        end
        par BM25 lane
            G->>OS: multi_match x3 (filter: tenant, acl, active, ci)
        and vector lane
            G->>LL: aembedding(3 queries)
            G->>OS: knn x3 (same filters)
        end
        OS-->>G: 6 rankings, 74 candidates
        G->>G: reciprocal_rank_fusion → 30
        G->>LL: RERANK prompt, scores 0-10
        LL-->>G: keep 6 above threshold 3
        G->>G: build citations [1]..[6]
        G->>PG: checkpoint
    end

    rect rgb(18,41,26)
        Note over G,LL: draft_answer + check_resolution
        G->>LL: ANSWER prompt with 6 numbered passages
        LL-->>G: grounded answer with [n] markers
        G->>LL: RESOLUTION_CHECK
        LL-->>G: {resolves: true, confidence: 0.88, risk_flags: []}
        G->>G: 0.6*0.88 + 0.4*(8.5/10) = 0.87
        G->>PG: checkpoint
    end

    G->>G: _decide() → finalize_kb_answer
    G->>PG: INSERT audit_log (agent.decision)
    G->>G: output_guardrail — redact, flag ungrounded
    G-->>CS: final state

    CS->>PG: INSERT chat_messages (role=assistant, usage, citations, trace_id)
    CS->>RD: DEL chat:history:{thread_id}
    CS-->>CR: ChatResponse
    CR-->>MW: 200
    MW->>MW: Prometheus REQUESTS + LATENCY
    MW-->>U: 200 + x-request-id, x-response-time-ms
```

---

## 2. Chat — automation path

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant G as LangGraph
    participant T as itsm_tools
    participant ORCH as Orchestrator seam
    participant PG as Postgres

    U->>G: "I'm locked out, too many attempts"
    G->>G: triage → service_request, Identity & Access, P3
    G->>G: retrieve + draft + check → confidence 0.79

    Note over G: _decide(): match_automation() finds<br/>"locked out" → unlock_account,<br/>intent is service_request → run_automation

    G->>T: run_safe_automation("unlock_account", {requester_id})
    T->>T: verify against SAFE_AUTOMATIONS allow-list
    alt not on the list
        T-->>G: {ok: false, error: "not on the safe list"}
        Note over G: falls through to create_ticket
    else on the list
        T->>ORCH: dispatch (AWX / Flow / Step Functions)
        T->>PG: audit_log (automation.run, run_id)
        T-->>G: {ok: true, run_id, description}
    end

    G-->>U: "Unlock an account locked by failed sign-ins.<br/>Reference: a3f9c210.<br/><br/>[drafted KB guidance follows]"
```

The allow-list is the whole control. `SAFE_AUTOMATIONS` is six reversible,
idempotent operations. Anything not on it becomes a ticket — never an improvised
action.

---

## 3. Chat — destructive request, proposal not execution

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant G as LangGraph
    participant T as itsm_tools
    actor A as Analyst

    U->>G: "just delete the stuck records from the prod orders table"
    G->>G: triage → change, P2
    G->>G: retrieve + draft
    G->>G: check_resolution → risk_flags: [destructive_action]

    Note over G: _decide() rule 2 fires before<br/>any confidence check.<br/>The agent does not execute.

    G->>T: create_ticket(kind=change, P2, Database)
    T->>T: is_destructive("delete ... prod") → true
    T-->>G: INC2608D4E5F6
    G-->>U: "This needs the change process.<br/>I've raised INC2608D4E5F6 for the DBA team<br/>with the details you gave me."

    A->>T: reviews, executes under change control
```

`DESTRUCTIVE_KEYWORDS` covers delete, drop, truncate, `rm -rf`, revoke, disable
account, restart production, failover, wipe. Extend it for your estate.

---

## 4. Human-in-the-loop with checkpoint suspension

Enable by uncommenting `interrupt_before=["create_ticket"]` in `graph.py`.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant API as API
    participant G as LangGraph
    participant PG as Postgres checkpoints
    actor A as Analyst

    U->>API: "I need admin rights on the finance server"
    API->>G: ainvoke(state, thread_id=th_abc)
    G->>G: guardrail → triage → retrieve → draft → check
    G->>PG: checkpoint after check_resolution
    G->>G: _decide() → create_ticket

    Note over G,PG: interrupt_before fires.<br/>The run SUSPENDS.<br/>State is durable in Postgres.

    G-->>API: partial state, no answer yet
    API-->>U: "This needs approval — an analyst has been notified"

    Note over A: minutes or hours pass.<br/>The process can restart.<br/>The checkpoint survives.

    A->>API: POST /tickets/{id}/approve?thread_id=th_abc
    API->>G: ainvoke(None, config={thread_id: th_abc})
    G->>PG: load checkpoint
    G->>G: resume at create_ticket
    G->>G: persist_outcome → output_guardrail
    G-->>API: full state
    API-->>A: {resumed: true, resolution_path: "ticket_created"}
```

This is why checkpointing is not optional plumbing. It is what makes graduated
autonomy possible: approve everything at first, then narrow the gate as your
eval numbers earn it.

---

## 5. Ingestion — happy path with progress polling

```mermaid
sequenceDiagram
    autonumber
    actor K as Author
    participant IR as IngestionRouter
    participant RD as Redis
    participant S3
    participant PG as Postgres
    participant BR as Celery broker
    participant WK as Worker
    participant LL as LiteLLM
    participant OS as OpenSearch

    K->>IR: POST /ingestion/files + Idempotency-Key: k1
    IR->>RD: SET idem:k1 NX EX 24h
    alt key already claimed
        RD-->>IR: existing response
        IR-->>K: replayed 202, no side effect
    end
    IR->>IR: validate type + size
    IR->>IR: sha256(bytes)
    IR->>PG: SELECT documents WHERE tenant + checksum + active
    alt duplicate and not reindex
        IR-->>K: {status: "duplicate", document_id}
    end
    IR->>S3: PUT raw/acme/2026/08/22/a3f9-runbook.pdf
    IR->>PG: INSERT documents, INSERT ingestion_jobs (queued)
    IR->>BR: send_task ingestion.process_document queue=ingestion
    IR->>PG: job.celery_task_id = task.id
    IR->>RD: SET idem:k1 = response
    IR-->>K: 202 {job_id, document_id, s3_key}

    BR->>WK: deliver
    WK->>PG: attempts += 1, started_at, status=parsing
    WK->>S3: GET object
    WK->>WK: Docling → markdown
    WK->>PG: status=chunking, stage_detail="parser=docling"
    WK->>WK: heading-aware split → 118 chunks
    WK->>PG: DELETE existing chunks for this document
    WK->>PG: status=embedding, stats.chunk_count=118

    loop batches of 64
        WK->>RD: GET llm:embedding:{hash} per chunk
        WK->>LL: aembedding(cache misses only)
        WK->>RD: SET llm:embedding TTL 7d
        WK->>PG: stage_detail="embedded 64/118"
    end

    WK->>PG: INSERT 118 document_chunks (uuid5 ids, vectors)
    WK->>PG: status=indexing
    WK->>OS: ensure_index
    WK->>OS: bulk index (200 per batch)
    OS-->>WK: no errors
    WK->>PG: UPDATE indexed_in_opensearch=true, chunk_count=118
    WK->>PG: status=completed, finished_at, stats.indexed=118

    loop poll
        K->>IR: GET /ingestion/jobs/{job_id}
        IR-->>K: {status, stage_detail, stats}
    end
```

---

## 6. Ingestion — failure, retry, dead letter, recovery

```mermaid
sequenceDiagram
    autonumber
    participant BR as Broker
    participant WK as Worker
    participant PG as Postgres
    participant LL as LiteLLM
    participant B as Beat
    actor P as Platform engineer

    BR->>WK: process_document (attempt 1)
    WK->>PG: status=embedding
    WK->>LL: aembedding batch
    LL--xWK: provider 503
    WK->>WK: autoretry_for=(Exception,) backoff 10s
    Note over WK: attempts=1, task requeued

    BR->>WK: attempt 2 (after 10s)
    LL--xWK: provider 503 again
    Note over WK: backoff 20s, attempts=2

    BR->>WK: attempt 3 (after 20s)
    LL--xWK: still failing
    WK->>PG: attempts >= max_retries<br/>status=dead_letter, error recorded
    Note over WK: itsm_ingest_documents_total{status="failed"}++

    rect rgb(45,42,18)
        Note over B,PG: separately — a worker that DIED mid-task
        B->>PG: maintenance.expire_stuck_jobs (every 30m)
        PG->>PG: UPDATE jobs SET dead_letter<br/>WHERE in-flight AND updated_at < now()-2h
    end

    P->>PG: GET /ingestion/jobs?status=dead_letter
    P->>BR: POST /ingestion/jobs/{id}/retry
    BR->>WK: process_document (fresh)
    Note over WK: chunk ids are uuid5(doc:ordinal)<br/>so this overwrites cleanly.<br/>No duplicates, ever.
    WK->>PG: status=completed
```

---

## 7. Reconciliation — Postgres and OpenSearch drift

```mermaid
sequenceDiagram
    autonumber
    participant WK as Ingestion worker
    participant PG as Postgres
    participant OS as OpenSearch
    participant B as Beat
    participant MW as Maintenance worker

    Note over WK,OS: a bulk index fails halfway through 1200 chunks
    WK->>PG: INSERT 1200 chunks (indexed_in_opensearch = false)
    WK->>OS: bulk batch 1-3 (600 chunks) OK
    WK--xOS: bulk batch 4 — cluster rejected, 429
    WK->>PG: task raises, retries later

    Note over PG,OS: DRIFT: 1200 rows in Postgres,<br/>600 documents in the index

    loop every 15 minutes
        B->>MW: maintenance.reindex_stale_chunks
        MW->>PG: SELECT chunks WHERE indexed_in_opensearch=false<br/>JOIN documents WHERE is_active LIMIT 500
        PG-->>MW: 500 rows (vectors included)
        MW->>OS: ensure_index + bulk
        OS-->>MW: OK
        MW->>PG: UPDATE indexed_in_opensearch=true for those ids
        Note over MW: log reindexed_stale_chunks count=500
    end

    Note over PG,OS: after 2 more cycles, converged.<br/>This is also how you rebuild the<br/>entire index from scratch.
```

---

## 8. Problem management — scheduled incident clustering

```mermaid
sequenceDiagram
    autonumber
    participant B as Beat
    participant MW as Maintenance worker
    participant PG as Postgres
    participant LL as LiteLLM
    actor L as ITSM lead

    loop every 6 hours
        B->>MW: maintenance.detect_problems
        MW->>PG: SELECT DISTINCT tenant_id FROM documents
        loop per tenant
            MW->>PG: SELECT incidents last 7 days LIMIT 60
            alt fewer than 3 incidents
                Note over MW: skip, nothing to cluster
            else
                MW->>LL: PROBLEM_CLUSTER prompt with id/priority/category/ci/title
                LL-->>MW: {clusters: [{label, ticket_ids, common_ci, hypothesis, action}]}
                MW->>MW: discard clusters with fewer than 2 tickets
                Note over MW: log problem_candidate per cluster
            end
        end
    end

    L->>PG: GET /tickets/problems/candidates?lookback_days=7
    PG-->>L: [{cluster_label: "VPN cert expiry wave",<br/>ticket_count: 14, common_ci: "vpn-gateway-01",<br/>hypothesis: "CA rotation on 18 Aug invalidated<br/>device certs issued before June",<br/>recommended_action: "bulk re-enrol affected devices"}]
    L->>PG: POST /tickets (kind=problem) linking the cluster
```

Fourteen individual P3 incidents nobody connected become one problem record with
a hypothesis and a next action. That is the practice most ITSM teams do worst
and where AI genuinely helps.

---

## 9. Continuous improvement — the closing loop

```mermaid
sequenceDiagram
    autonumber
    actor A as Analyst
    participant TR as TicketRouter
    participant LL as LiteLLM
    participant IR as IngestionRouter
    participant OS as OpenSearch
    actor U2 as Next user with the same problem

    A->>TR: PATCH /tickets/{id} status=resolved, resolution="..."
    A->>TR: POST /tickets/{id}/kb-draft
    TR->>TR: gather ticket + worknotes
    TR->>LL: KB_DRAFT prompt
    LL-->>TR: Title / Symptoms / Environment / Root cause /<br/>Resolution steps / Workaround / Related
    TR-->>A: draft
    A->>A: edits for accuracy
    A->>IR: POST /ingestion/text (doc_class=kb_article)
    IR-->>OS: indexed within seconds

    Note over U2: three days later
    U2->>OS: same symptom, hybrid retrieval finds the new article
    Note over U2: resolution_path = kb_resolution.<br/>No ticket. The loop closed.
```

---

## 10. Boot sequence

```mermaid
sequenceDiagram
    autonumber
    participant P as Process
    participant L as logging
    participant O as OTel
    participant LF as Langfuse
    participant RD as Redis
    participant OS as OpenSearch
    participant PG as Postgres
    participant G as Graph

    P->>L: configure_logging (JSON in non-local)
    P->>O: configure_tracing (OTLP if endpoint set)
    P->>LF: configure_langfuse (registers LiteLLM callbacks)
    P->>RD: ping
    alt unreachable
        Note over P: log error, CONTINUE.<br/>Redis is degraded-mode tolerable.
    end
    P->>OS: ensure_index
    alt unreachable
        Note over P: log error, CONTINUE.<br/>ticket intake still works.
    end
    P->>PG: setup_checkpointer (AsyncPostgresSaver.setup)
    alt unreachable
        Note over P: fall back to MemorySaver,<br/>log a loud warning
    end
    P->>G: compile graph
    P->>P: instrument FastAPI, register routers
    Note over P: startup_complete
```

The service starts even when Redis and OpenSearch are down. It logs loudly,
`/health/ready` returns 503 so the load balancer holds traffic back, but the
process is up and will self-heal when the dependency returns. Only Postgres is
hard-required, and even then the checkpointer degrades to memory rather than
crash-looping.
