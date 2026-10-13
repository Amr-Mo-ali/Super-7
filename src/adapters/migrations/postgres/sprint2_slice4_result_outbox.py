"""Slice 4 terminal result and callback-event storage; no delivery or runtime wiring."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:

    def step(apply: str, rollback: str) -> object: ...

else:
    from yoyo import step

__depends__ = {"sprint2_slice3_worker_attempt"}

steps = [
    step(
        """SET LOCAL lock_timeout = '5s';
LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE;
LOCK TABLE analysis_job_attempts IN ACCESS EXCLUSIVE MODE;

DO $super7$
DECLARE
    status_constraint TEXT;
BEGIN
    IF EXISTS (
        SELECT 1 FROM analysis_jobs
        WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED')
            OR (state = 'QUEUED' AND attempt_count >= attempt_limit)
            OR (state = 'RUNNING' AND attempt_count = 0)
    ) OR EXISTS (
        SELECT 1 FROM analysis_jobs AS j
        WHERE j.attempt_count > 0 AND NOT EXISTS (
            SELECT 1 FROM analysis_job_attempts AS a
            WHERE a.job_id = j.job_id AND a.attempt_number = j.attempt_count
        )
    ) OR EXISTS (
        SELECT 1 FROM analysis_jobs AS j
        WHERE j.attempt_count <> (
            SELECT count(*) FROM analysis_job_attempts AS a WHERE a.job_id = j.job_id
        )
    ) OR EXISTS (
        SELECT 1 FROM analysis_job_attempts AS a
        JOIN analysis_jobs AS j ON j.job_id = a.job_id
        WHERE a.attempt_number > j.attempt_count
            OR (a.attempt_number < j.attempt_count
                AND (a.status NOT IN ('INTERRUPTED', 'RETRYABLE_FAILURE')
                    OR a.attempt_number >= j.attempt_limit))
            OR (a.attempt_number = j.attempt_count AND NOT (
                (j.state = 'RUNNING' AND a.status = 'ACTIVE')
                OR (j.state = 'QUEUED'
                    AND a.status IN ('INTERRUPTED', 'RETRYABLE_FAILURE'))
            ))
    ) THEN
        RAISE EXCEPTION
            'Cannot apply sprint2_slice4_result_outbox with incompatible durable rows.';
    END IF;

    SELECT c.conname INTO STRICT status_constraint
    FROM pg_constraint AS c
    WHERE c.conrelid = 'analysis_job_attempts'::regclass
        AND c.contype = 'c'
        AND pg_get_constraintdef(c.oid) LIKE '%status%';
    EXECUTE format('ALTER TABLE analysis_job_attempts DROP CONSTRAINT %I', status_constraint);
    EXECUTE format($ddl$ALTER TABLE analysis_job_attempts ADD CONSTRAINT %I CHECK (
        (status = 'ACTIVE' AND lease_expires_at IS NOT NULL
            AND lease_expires_at > heartbeat_at AND finished_at IS NULL
            AND safe_failure_code IS NULL)
        OR (status = 'INTERRUPTED' AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL AND safe_failure_code IS NULL)
        OR (status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE')
            AND lease_expires_at IS NULL AND finished_at IS NOT NULL
            AND safe_failure_code IS NOT NULL)
        OR (status = 'EXHAUSTED' AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL)
        OR (status = 'SUCCEEDED' AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL AND safe_failure_code IS NULL)
    )$ddl$, status_constraint);
END
$super7$;

CREATE TABLE analysis_job_terminals (
    job_id UUID PRIMARY KEY REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    terminal_kind TEXT NOT NULL CHECK (terminal_kind IN ('COMPLETED', 'FAILED')),
    finalized_at TIMESTAMPTZ NOT NULL,
    terminal_schema_version TEXT NOT NULL
        CHECK (terminal_schema_version = 'analysis_terminal_v1'),
    result_schema_version TEXT NULL,
    result_json JSONB NULL,
    model_version TEXT NULL CHECK (
        model_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    processing_duration_ms BIGINT NULL CHECK (processing_duration_ms >= 0),
    safe_failure_code TEXT NULL
        CHECK (safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    failure_origin TEXT NULL CHECK (failure_origin IN
        ('CLASSIFIED_TERMINAL', 'CLASSIFIED_EXHAUSTION', 'EXPIRY_EXHAUSTION')),
    FOREIGN KEY (job_id, attempt_number)
        REFERENCES analysis_job_attempts(job_id, attempt_number) ON DELETE RESTRICT,
    CHECK (
        (terminal_kind = 'COMPLETED'
            AND result_schema_version IS NOT NULL
            AND result_schema_version = 'analysis_result_v1'
            AND result_json IS NOT NULL AND jsonb_typeof(result_json) = 'object'
            AND model_version IS NOT NULL AND processing_duration_ms IS NOT NULL
            AND safe_failure_code IS NULL AND failure_origin IS NULL)
        OR
        (terminal_kind = 'FAILED'
            AND result_schema_version IS NULL AND result_json IS NULL
            AND model_version IS NULL AND processing_duration_ms IS NULL
            AND safe_failure_code IS NOT NULL AND failure_origin IS NOT NULL)
    )
);

CREATE TABLE analysis_callback_outbox (
    callback_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        CHECK (callback_event_id <> '00000000-0000-0000-0000-000000000000'::uuid),
    job_id UUID NOT NULL UNIQUE
        REFERENCES analysis_job_terminals(job_id) ON DELETE RESTRICT,
    payload_schema_version TEXT NOT NULL
        CHECK (payload_schema_version = 'analysis_callback_snapshot_v1'),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    delivery_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (delivery_state IN ('PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')),
    created_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL
);""",
        """SET LOCAL lock_timeout = '5s';
LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE;
LOCK TABLE analysis_job_attempts IN ACCESS EXCLUSIVE MODE;
LOCK TABLE analysis_job_terminals IN ACCESS EXCLUSIVE MODE;
LOCK TABLE analysis_callback_outbox IN ACCESS EXCLUSIVE MODE;

DO $super7$
DECLARE
    status_constraint TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM analysis_job_terminals)
        OR EXISTS (SELECT 1 FROM analysis_callback_outbox)
        OR EXISTS (SELECT 1 FROM analysis_job_attempts WHERE status = 'SUCCEEDED')
        OR EXISTS (
            SELECT 1 FROM analysis_jobs
            WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED')
        ) THEN
        RAISE EXCEPTION
            'Cannot roll back sprint2_slice4_result_outbox while Slice 4 rows exist.';
    END IF;
    SELECT c.conname INTO STRICT status_constraint
    FROM pg_constraint AS c
    WHERE c.conrelid = 'analysis_job_attempts'::regclass
        AND c.contype = 'c'
        AND pg_get_constraintdef(c.oid) LIKE '%status%';
    EXECUTE format('ALTER TABLE analysis_job_attempts DROP CONSTRAINT %I', status_constraint);
    EXECUTE format($ddl$ALTER TABLE analysis_job_attempts ADD CONSTRAINT %I CHECK (
        (status = 'ACTIVE' AND lease_expires_at IS NOT NULL
            AND lease_expires_at > heartbeat_at AND finished_at IS NULL
            AND safe_failure_code IS NULL)
        OR (status = 'INTERRUPTED' AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL AND safe_failure_code IS NULL)
        OR (status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE')
            AND lease_expires_at IS NULL AND finished_at IS NOT NULL
            AND safe_failure_code IS NOT NULL)
        OR (status = 'EXHAUSTED' AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL)
    )$ddl$, status_constraint);
END
$super7$;

DROP TABLE analysis_callback_outbox;
DROP TABLE analysis_job_terminals;""",
    ),
]
