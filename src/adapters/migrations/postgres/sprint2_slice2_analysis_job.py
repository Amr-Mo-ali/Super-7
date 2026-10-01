from yoyo import step  # type: ignore[import-untyped]

__depends__ = {"sprint2_slice1_foundation"}

steps = [
    step(
        """CREATE TABLE analysis_jobs (
    job_id UUID PRIMARY KEY,
    idempotency_lookup BYTEA NOT NULL UNIQUE
        CHECK (octet_length(idempotency_lookup) > 0),
    request_fingerprint BYTEA NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    video_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    video_reference TEXT NOT NULL,
    callback_url TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN (
                'QUEUED',
                'RUNNING',
                'COMPLETED',
                'FAILED',
                'CANCELLED'
            )
        ),
    accepted_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX analysis_jobs_queued_accepted_at_job_id_idx
    ON analysis_jobs (accepted_at, job_id)
    WHERE state = 'QUEUED';""",
        """DO $super7$
BEGIN
    IF EXISTS (SELECT 1 FROM analysis_jobs) THEN
        RAISE EXCEPTION
            'Cannot roll back sprint2_slice2_analysis_job while analysis_jobs contains rows.';
    END IF;
END
$super7$;

DROP TABLE analysis_jobs;""",
    ),
]
