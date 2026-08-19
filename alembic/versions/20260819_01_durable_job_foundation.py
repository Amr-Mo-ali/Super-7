"""Create Super-7 durable job foundation tables.

Revision ID: 20260819_01
Revises:
Create Date: 2026-08-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260819_01"
down_revision = None
branch_labels = None
depends_on = None


def _uuid_column(
    name: str, *constraints: sa.schema.SchemaItem, **kwargs: object
) -> sa.Column[object]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *constraints, **kwargs)


def upgrade() -> None:
    op.create_table(
        "super7_analysis_jobs",
        _uuid_column("id", primary_key=True, nullable=False),
        sa.Column("caller_scope", sa.String(length=128), nullable=False),
        sa.Column("video_id", sa.String(length=255), nullable=False),
        sa.Column("player_id", sa.String(length=255), nullable=False),
        sa.Column("video_reference", sa.String(length=1024), nullable=False),
        sa.Column("callback_url", sa.String(length=2048), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("requested_analysis_version", sa.String(length=128)),
        sa.Column("resolved_analysis_version", sa.String(length=128), nullable=False),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("callback_status", sa.String(length=32), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_error_code", sa.String(length=128)),
        sa.Column("terminal_error_classification", sa.String(length=128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "analysis_status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_super7_analysis_jobs_analysis_status",
        ),
        sa.CheckConstraint(
            "callback_status IN ('NOT_READY', 'PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')",
            name="ck_super7_analysis_jobs_callback_status",
        ),
        sa.CheckConstraint(
            "max_attempts > 0", name="ck_super7_analysis_jobs_max_attempts_positive"
        ),
    )
    op.create_index(
        "ix_super7_analysis_jobs_claim", "super7_analysis_jobs", ["analysis_status", "queued_at"]
    )
    op.create_index(
        "ix_super7_analysis_jobs_lease_recovery",
        "super7_analysis_jobs",
        ["analysis_status", "lease_expires_at"],
    )

    op.create_table(
        "super7_idempotency_bindings",
        _uuid_column("id", primary_key=True, nullable=False),
        sa.Column("caller_scope", sa.String(length=128), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        _uuid_column(
            "job_id", sa.ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "caller_scope", "key_digest", name="uq_super7_idempotency_bindings_scoped_key_digest"
        ),
        sa.UniqueConstraint("job_id", name="uq_super7_idempotency_bindings_job_binding"),
    )
    op.create_index(
        "ix_super7_idempotency_bindings_job_id", "super7_idempotency_bindings", ["job_id"]
    )

    op.create_table(
        "super7_execution_attempts",
        _uuid_column("id", primary_key=True, nullable=False),
        _uuid_column(
            "job_id", sa.ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_identity", sa.String(length=255)),
        sa.Column("lease_token", sa.String(length=128), nullable=False),
        sa.Column("fencing_value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_classification", sa.String(length=128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "job_id", "attempt_number", name="uq_super7_execution_attempts_job_attempt_number"
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_super7_execution_attempts_attempt_number_positive"
        ),
        sa.CheckConstraint(
            "fencing_value >= 0", name="ck_super7_execution_attempts_fencing_value_nonnegative"
        ),
        sa.CheckConstraint(
            "outcome IN ('RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'TERMINAL_FAILURE', 'INTERRUPTED')",
            name="ck_super7_execution_attempts_outcome",
        ),
    )
    op.create_index("ix_super7_execution_attempts_job_id", "super7_execution_attempts", ["job_id"])
    op.create_index(
        "ix_super7_execution_attempts_lease_expires_at",
        "super7_execution_attempts",
        ["lease_expires_at"],
    )

    op.create_table(
        "super7_analysis_results",
        _uuid_column("id", primary_key=True, nullable=False),
        _uuid_column(
            "job_id", sa.ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("result_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("analysis_version", sa.String(length=128), nullable=False),
        sa.Column("scoring_version", sa.String(length=128)),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", name="uq_super7_analysis_results_result_job"),
        sa.CheckConstraint(
            "terminal_status IN ('COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_super7_analysis_results_terminal_status",
        ),
    )
    op.create_index("ix_super7_analysis_results_job_id", "super7_analysis_results", ["job_id"])

    op.create_table(
        "super7_dispatch_intents",
        _uuid_column("id", primary_key=True, nullable=False),
        _uuid_column(
            "job_id", sa.ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", name="uq_super7_dispatch_intents_dispatch_job"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'COMPLETED')",
            name="ck_super7_dispatch_intents_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_super7_dispatch_intents_attempt_count_nonnegative"
        ),
    )
    op.create_index(
        "ix_super7_dispatch_intents_due", "super7_dispatch_intents", ["status", "available_at"]
    )

    op.create_table(
        "super7_callback_outbox",
        _uuid_column("id", primary_key=True, nullable=False),
        _uuid_column(
            "job_id", sa.ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("callback_url", sa.String(length=2048), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivery_status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("exhausted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", name="uq_super7_callback_outbox_callback_job"),
        sa.CheckConstraint(
            "delivery_status IN ('PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')",
            name="ck_super7_callback_outbox_delivery_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_super7_callback_outbox_attempt_count_nonnegative"
        ),
    )
    op.create_index(
        "ix_super7_callback_outbox_due",
        "super7_callback_outbox",
        ["delivery_status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_super7_callback_outbox_due", table_name="super7_callback_outbox")
    op.drop_table("super7_callback_outbox")
    op.drop_index("ix_super7_dispatch_intents_due", table_name="super7_dispatch_intents")
    op.drop_table("super7_dispatch_intents")
    op.drop_index("ix_super7_analysis_results_job_id", table_name="super7_analysis_results")
    op.drop_table("super7_analysis_results")
    op.drop_index(
        "ix_super7_execution_attempts_lease_expires_at", table_name="super7_execution_attempts"
    )
    op.drop_index("ix_super7_execution_attempts_job_id", table_name="super7_execution_attempts")
    op.drop_table("super7_execution_attempts")
    op.drop_index("ix_super7_idempotency_bindings_job_id", table_name="super7_idempotency_bindings")
    op.drop_table("super7_idempotency_bindings")
    op.drop_index("ix_super7_analysis_jobs_lease_recovery", table_name="super7_analysis_jobs")
    op.drop_index("ix_super7_analysis_jobs_claim", table_name="super7_analysis_jobs")
    op.drop_table("super7_analysis_jobs")
