# Data model

Nine tables plus LangGraph's own checkpoint tables. Postgres is authoritative
for everything except raw bytes.

---

## Full ER diagram

```mermaid
erDiagram
    TICKETS ||--o{ TICKET_EVENTS : "audit trail"
    TICKETS ||--o| CHAT_SESSIONS : "raised from"
    TICKETS ||--o{ FEEDBACK : "rated"
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : "split into"
    DOCUMENTS ||--o{ INGESTION_JOBS : "processed by"
    CHAT_SESSIONS ||--o{ CHAT_MESSAGES : "turns"
    CHAT_MESSAGES ||--o{ FEEDBACK : "rated"

    TICKETS {
        uuid id PK
        string tenant_id
        string external_ref UK
        enum kind
        enum status
        enum priority
        string category
        string subcategory
        string assignment_group
        string title
        text description
        string requester_id
        string ci_name
        timestamp sla_due_at
        timestamp resolved_at
        text resolution
        bool resolved_by_agent
        float confidence
        jsonb attributes
        timestamp created_at
        timestamp updated_at
    }
    TICKET_EVENTS {
        uuid id PK
        uuid ticket_id FK
        string actor
        string actor_type
        string event_type
        jsonb payload
        timestamp created_at
    }
    DOCUMENTS {
        uuid id PK
        string tenant_id
        string title
        string source_type
        string source_uri
        string s3_bucket
        string s3_key
        string content_type
        string checksum_sha256
        int size_bytes
        string doc_class
        int version
        bool is_active
        int chunk_count
        jsonb acl
        jsonb doc_metadata
    }
    DOCUMENT_CHUNKS {
        uuid id PK
        uuid document_id FK
        string tenant_id
        int ordinal
        text content
        string heading_path
        int page_no
        int token_count
        vector embedding
        bool indexed_in_opensearch
        jsonb chunk_metadata
    }
    INGESTION_JOBS {
        uuid id PK
        string tenant_id
        uuid document_id FK
        enum status
        string stage_detail
        int attempts
        string celery_task_id
        text error
        timestamp started_at
        timestamp finished_at
        jsonb stats
    }
    CHAT_SESSIONS {
        uuid id PK
        string tenant_id
        string user_id
        string thread_id UK
        string title
        uuid ticket_id FK
        string channel
        bool is_open
    }
    CHAT_MESSAGES {
        uuid id PK
        uuid session_id FK
        string role
        text content
        jsonb citations
        jsonb tool_calls
        string model
        int prompt_tokens
        int completion_tokens
        float cost_usd
        int latency_ms
        string trace_id
    }
    FEEDBACK {
        uuid id PK
        string tenant_id
        uuid message_id FK
        uuid ticket_id FK
        int rating
        string reason
        text comment
        string submitted_by
    }
    AUDIT_LOG {
        uuid id PK
        string tenant_id
        string actor
        string action
        string resource_type
        string resource_id
        string request_id
        string outcome
        jsonb payload
    }
```

`AUDIT_LOG` has no foreign keys on purpose. It must survive the deletion of
whatever it describes.

---

## Ticket lifecycle

```mermaid
stateDiagram-v2
    [*] --> new: created via API
    [*] --> triaged: created by the agent<br/>already classified

    new --> triaged: analyst or agent classifies
    triaged --> in_progress: analyst picks it up
    triaged --> escalated: outage confirmed
    in_progress --> pending_user: waiting on the requester
    pending_user --> in_progress: user replies
    in_progress --> resolved: fix applied
    escalated --> in_progress: MIM hands back
    escalated --> resolved
    resolved --> closed: after the confirmation window
    resolved --> in_progress: reopened

    closed --> [*]

    note right of resolved
        resolved_at is set automatically.
        MTTR = avg(resolved_at - created_at)
    end note

    note right of triaged
        SLA clock starts at created_at.
        Changing priority via PATCH
        recalculates sla_due_at.
    end note
```

### SLA matrix

| Priority | Definition | SLA hours (`SLA_HOURS`) |
|---|---|---|
| P1 | Business-wide outage, revenue impact, no workaround | 4 |
| P2 | Multiple users or a critical single user blocked | 8 |
| P3 | Single user impaired, workaround exists | 24 |
| P4 | Informational, cosmetic, scheduled request | 72 |

`GET /tickets/sla/at-risk?within_minutes=60` returns anything due inside the
window that is not resolved or closed.

---

## Document versioning

```mermaid
graph LR
    U1["upload runbook.pdf<br/>checksum a3f9"] --> D1["documents<br/>version=1, is_active=true<br/>chunk_count=118"]
    U2["re-upload identical bytes"] --> DUP["status=duplicate<br/>no new row, no job"]
    U3["upload with reindex=true"] --> D2["documents<br/>version=2, is_active=true"]
    U4["edited runbook<br/>checksum b7c2"] --> D3["documents<br/>version=1, new checksum"]
    DEL["DELETE a document by id"] --> D4["is_active=false<br/>+ delete_by_query in OpenSearch"]

    style DUP fill:#3b2a12,stroke:#f59e0b,color:#fff
    style D4 fill:#3b1d1d,stroke:#ef4444,color:#fff
```

Deletion is soft in Postgres and hard in OpenSearch. You keep the audit trail
and the S3 object; the content stops being retrievable immediately.

---

## Chunk identity

```
chunk_id = uuid5(NAMESPACE_URL, f"{document_id}:{ordinal}")
```

This one line gives you three properties:

1. **Idempotent retries.** Re-running a failed ingestion overwrites the same
   rows and the same OpenSearch documents. No duplicates, ever.
2. **Stable citations.** A `chunk_id` in a six-month-old `audit_log` row still
   points at the same passage, so you can reconstruct what the agent actually
   read when it made a decision.
3. **Safe partial reprocessing.** You can re-embed a single document without
   touching anything else in the index.

---

## LangGraph checkpoint tables

`AsyncPostgresSaver.setup()` creates these on first boot. You do not manage them
in Alembic — the library owns their schema.

| Table | Purpose |
|---|---|
| `checkpoints` | Serialized state per `(thread_id, checkpoint_id)` |
| `checkpoint_blobs` | Large state values stored out of line |
| `checkpoint_writes` | Pending writes for interrupted runs |
| `checkpoint_migrations` | The library's own version tracking |

The `thread_id` is `chat_sessions.thread_id`. That is the join between your
domain data and the agent's runtime state — a `th_9f2c...` in a checkpoint row
and in a chat session are the same conversation.

**Retention.** Checkpoints grow with conversation volume. Add a monthly job:

```sql
DELETE FROM checkpoints
WHERE thread_id IN (
  SELECT thread_id FROM chat_sessions
  WHERE is_open = false AND updated_at < now() - interval '90 days'
);
```

---

## Query patterns and their indexes

| Query | Index used |
|---|---|
| Analyst queue: tenant + status + recency | `ix_tickets_tenant_status_created` |
| SLA at-risk sweep | `ix_tickets_priority` + `sla_due_at` scan |
| Ingestion dedupe | `ix_documents_checksum_sha256` + unique constraint |
| Document list by class | `ix_documents_tenant_active_class` |
| pgvector similarity | `ix_chunks_embedding_hnsw` |
| Degraded-mode fuzzy text | `ix_chunks_content_trgm` |
| Reconciliation sweep | `ix_document_chunks_indexed_in_opensearch` |
| Conversation transcript | `ix_chat_messages_session_id` |
| Audit by action | `ix_audit_log_action` + `ix_audit_log_created_at` |

---

## Multi-tenancy

`tenant_id` is a column on every table that holds user data, and it comes from
the authenticated `Principal` — never from a request body.

```mermaid
graph TB
    JWT["JWT claim tenant_id<br/>or X-Tenant-Id with a valid API key"] --> P["Principal.tenant_id"]
    P --> SQL["every SELECT/INSERT<br/>filters on tenant_id"]
    P --> OSF["_filters adds a mandatory<br/>term clause on tenant_id to<br/>every OpenSearch query"]
    P --> RK["Redis keys namespaced<br/>by tenant where relevant"]
    P --> BUD["budget key scoped by<br/>date and tenant"]

    style OSF fill:#3b1d1d,stroke:#ef4444,color:#fff
```

If you need harder isolation than row-level filtering — a regulated tenant, a
data-residency boundary — the clean seams are: a separate OpenSearch index per
tenant (change `settings.opensearch_index` to a per-tenant function), a separate
S3 prefix (already the case), and Postgres row-level security policies on
`tenant_id`.
