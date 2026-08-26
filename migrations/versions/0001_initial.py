"""Initial ITSM schema: tickets, documents, chunks (pgvector), jobs, chat, audit.

Revision ID: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

from app.core.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TICKET_STATUS = sa.Enum("new", "triaged", "in_progress", "pending_user", "resolved", "closed",
                        "escalated", name="ticketstatus")
TICKET_PRIORITY = sa.Enum("p1", "p2", "p3", "p4", name="ticketpriority")
TICKET_KIND = sa.Enum("incident", "service_request", "problem", "change", name="ticketkind")
JOB_STATUS = sa.Enum("queued", "uploading", "parsing", "chunking", "embedding", "indexing",
                     "completed", "failed", "dead_letter", name="jobstatus")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    bind = op.get_bind()
    for enum in (TICKET_STATUS, TICKET_PRIORITY, TICKET_KIND, JOB_STATUS):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("external_ref", sa.String(128), index=True),
        sa.Column("kind", TICKET_KIND, nullable=False),
        sa.Column("status", TICKET_STATUS, nullable=False),
        sa.Column("priority", TICKET_PRIORITY, nullable=False),
        sa.Column("category", sa.String(128), index=True),
        sa.Column("subcategory", sa.String(128)),
        sa.Column("assignment_group", sa.String(128), index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("requester_id", sa.String(128), nullable=False, index=True),
        sa.Column("ci_name", sa.String(256), index=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text),
        sa.Column("resolved_by_agent", sa.Boolean, server_default=sa.false()),
        sa.Column("confidence", sa.Float),
        sa.Column("attributes", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "external_ref", name="uq_ticket_tenant_external"),
    )
    op.create_index("ix_tickets_tenant_status_created", "tickets",
                    ["tenant_id", "status", "created_at"])

    op.create_table(
        "ticket_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), index=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("actor_type", sa.String(32), server_default="agent"),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("payload", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source_type", sa.String(64), server_default="upload"),
        sa.Column("source_uri", sa.String(1024)),
        sa.Column("s3_bucket", sa.String(255), nullable=False),
        sa.Column("s3_key", sa.String(1024), nullable=False),
        sa.Column("content_type", sa.String(128)),
        sa.Column("checksum_sha256", sa.String(64), nullable=False, index=True),
        sa.Column("size_bytes", sa.Integer, server_default="0"),
        sa.Column("doc_class", sa.String(64), server_default="kb_article"),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), index=True),
        sa.Column("chunk_count", sa.Integer, server_default="0"),
        sa.Column("acl", postgresql.JSONB, server_default="[]"),
        sa.Column("doc_metadata", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "checksum_sha256", "version",
                            name="uq_doc_tenant_checksum"),
    )
    op.create_index("ix_documents_tenant_active_class", "documents",
                    ["tenant_id", "is_active", "doc_class"])

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("heading_path", sa.String(1024)),
        sa.Column("page_no", sa.Integer),
        sa.Column("token_count", sa.Integer, server_default="0"),
        sa.Column("embedding", Vector(settings.embedding_dim)),
        sa.Column("indexed_in_opensearch", sa.Boolean, server_default=sa.false(), index=True),
        sa.Column("chunk_metadata", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunk_doc_ordinal"),
    )
    # HNSW beats IVFFlat here: no training step, and recall stays stable as the
    # KB grows article by article rather than in bulk loads.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_chunks_content_trgm ON document_chunks "
        "USING gin (content gin_trgm_ops)"
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("status", JOB_STATUS, nullable=False, index=True),
        sa.Column("stage_detail", sa.String(512)),
        sa.Column("attempts", sa.Integer, server_default="0"),
        sa.Column("celery_task_id", sa.String(128), index=True),
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("stats", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(128), nullable=False, index=True),
        sa.Column("thread_id", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("title", sa.String(512)),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tickets.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(32), server_default="web"),
        sa.Column("is_open", sa.Boolean, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("citations", postgresql.JSONB, server_default="[]"),
        sa.Column("tool_calls", postgresql.JSONB, server_default="[]"),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_tokens", sa.Integer, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Float, server_default="0"),
        sa.Column("latency_ms", sa.Integer, server_default="0"),
        sa.Column("trace_id", sa.String(64), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chat_messages.id", ondelete="CASCADE")),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tickets.id", ondelete="CASCADE")),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(128)),
        sa.Column("comment", sa.Text),
        sa.Column("submitted_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False, index=True),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), index=True),
        sa.Column("request_id", sa.String(64), index=True),
        sa.Column("outcome", sa.String(32), server_default="success"),
        sa.Column("payload", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  index=True),
    )


def downgrade() -> None:
    for table in ("audit_log", "feedback", "chat_messages", "chat_sessions", "ingestion_jobs",
                  "document_chunks", "documents", "ticket_events", "tickets"):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in (JOB_STATUS, TICKET_KIND, TICKET_PRIORITY, TICKET_STATUS):
        enum.drop(bind, checkfirst=True)
