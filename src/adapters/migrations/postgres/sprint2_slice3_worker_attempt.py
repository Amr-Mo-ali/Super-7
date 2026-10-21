from yoyo import step  # type: ignore[import-untyped]

__depends__ = {"sprint2_slice2_analysis_job"}

steps = [
    step(
        """DO $super7$
BEGIN
    IF EXISTS (SELECT 1 FROM analysis_jobs) THEN
        RAISE EXCEPTION
            'Cannot apply sprint2_slice3_worker_attempt while analysis_jobs contains rows.';
    END IF;
END
$super7$;

ALTER TABLE analysis_jobs
    ADD COLUMN analysis_version TEXT NOT NULL
        CHECK (analysis_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    ADD COLUMN attempt_limit INTEGER NOT NULL CHECK (attempt_limit > 0),
    ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    ADD CHECK (attempt_count <= attempt_limit);

CREATE TABLE analysis_job_attempts (
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    worker_slot_id UUID NOT NULL,
    lease_id UUID NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    safe_failure_code TEXT NULL,
    PRIMARY KEY (job_id, attempt_number),
    FOREIGN KEY (job_id) REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT,
    CHECK (heartbeat_at >= started_at),
    CHECK (finished_at >= heartbeat_at),
    CHECK (safe_failure_code IS NULL OR safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    CHECK (
        (status = 'ACTIVE'
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at > heartbeat_at
            AND finished_at IS NULL
            AND safe_failure_code IS NULL)
        OR (status = 'INTERRUPTED'
            AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL
            AND safe_failure_code IS NULL)
        OR (status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE')
            AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL
            AND safe_failure_code IS NOT NULL)
        OR (status = 'EXHAUSTED'
            AND lease_expires_at IS NULL
            AND finished_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX analysis_job_attempts_active_job_id_idx
    ON analysis_job_attempts (job_id) WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX analysis_job_attempts_active_worker_slot_id_idx
    ON analysis_job_attempts (worker_slot_id) WHERE status = 'ACTIVE';
CREATE INDEX analysis_job_attempts_active_expiry_job_attempt_idx
    ON analysis_job_attempts (lease_expires_at, job_id, attempt_number)
    WHERE status = 'ACTIVE';""",
        """DO $super7$
BEGIN
    LOCK TABLE analysis_jobs, analysis_job_attempts IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM analysis_jobs)
        OR EXISTS (SELECT 1 FROM analysis_job_attempts) THEN
        RAISE EXCEPTION
            'Cannot roll back sprint2_slice3_worker_attempt while durable rows exist.';
    END IF;
END
$super7$;

DROP TABLE analysis_job_attempts;

ALTER TABLE analysis_jobs
    DROP COLUMN analysis_version,
    DROP COLUMN attempt_limit,
    DROP COLUMN attempt_count;""",
    ),
]
