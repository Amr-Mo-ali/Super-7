"""SQLAlchemy models for Super-7-owned durable job records only."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from persistence.base import Base
from persistence.enums import AnalysisStatus, AttemptOutcome, CallbackStatus, DispatchStatus


def _uuid() -> UUID:
    return uuid4()


class AnalysisJobRecord(Base):
    __tablename__ = "super7_analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "analysis_status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="analysis_status",
        ),
        CheckConstraint(
            "callback_status IN ('NOT_READY', 'PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')",
            name="callback_status",
        ),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        Index("ix_super7_analysis_jobs_claim", "analysis_status", "queued_at"),
        Index("ix_super7_analysis_jobs_lease_recovery", "analysis_status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    caller_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    video_id: Mapped[str] = mapped_column(String(255), nullable=False)
    player_id: Mapped[str] = mapped_column(String(255), nullable=False)
    video_reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    callback_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_analysis_version: Mapped[str | None] = mapped_column(String(128))
    resolved_analysis_version: Mapped[str] = mapped_column(String(128), nullable=False)
    analysis_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AnalysisStatus.QUEUED
    )
    callback_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CallbackStatus.NOT_READY
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_error_code: Mapped[str | None] = mapped_column(String(128))
    terminal_error_classification: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class IdempotencyBindingRecord(Base):
    __tablename__ = "super7_idempotency_bindings"
    __table_args__ = (
        UniqueConstraint("caller_scope", "key_digest", name="scoped_key_digest"),
        UniqueConstraint("job_id", name="job_binding"),
        Index("ix_super7_idempotency_bindings_job_id", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    caller_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionAttemptRecord(Base):
    __tablename__ = "super7_execution_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="job_attempt_number"),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        CheckConstraint("fencing_value >= 0", name="fencing_value_nonnegative"),
        CheckConstraint(
            "outcome IN ('RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'TERMINAL_FAILURE', 'INTERRUPTED')",
            name="outcome",
        ),
        Index("ix_super7_execution_attempts_job_id", "job_id"),
        Index("ix_super7_execution_attempts_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_identity: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default=AttemptOutcome.RUNNING)
    error_classification: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnalysisResultRecord(Base):
    __tablename__ = "super7_analysis_results"
    __table_args__ = (
        UniqueConstraint("job_id", name="result_job"),
        CheckConstraint(
            "terminal_status IN ('COMPLETED', 'FAILED', 'CANCELLED')", name="terminal_status"
        ),
        Index("ix_super7_analysis_results_job_id", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(128), nullable=False)
    scoring_version: Mapped[str | None] = mapped_column(String(128))
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DispatchIntentRecord(Base):
    __tablename__ = "super7_dispatch_intents"
    __table_args__ = (
        UniqueConstraint("job_id", name="dispatch_job"),
        CheckConstraint("status IN ('PENDING', 'CLAIMED', 'COMPLETED')", name="status"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_super7_dispatch_intents_due", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=DispatchStatus.PENDING)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CallbackOutboxRecord(Base):
    __tablename__ = "super7_callback_outbox"
    __table_args__ = (
        UniqueConstraint("job_id", name="callback_job"),
        CheckConstraint(
            "delivery_status IN ('PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')",
            name="delivery_status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_super7_callback_outbox_due", "delivery_status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("super7_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    callback_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CallbackStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
