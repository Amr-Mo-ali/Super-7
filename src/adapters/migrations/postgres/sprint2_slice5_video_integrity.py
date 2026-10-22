"""Optional, atomic expected video bytes for verified R2 admission."""

from yoyo import step  # type: ignore[import-untyped]

__depends__ = {"sprint2_slice4_result_outbox"}

steps = [
    step(
        """SET LOCAL lock_timeout = '5s';
LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE;

ALTER TABLE analysis_jobs
    ADD COLUMN video_size_bytes BIGINT,
    ADD COLUMN video_sha256 BYTEA,
    ADD CONSTRAINT analysis_jobs_video_integrity_pair_check CHECK (
        (video_size_bytes IS NULL AND video_sha256 IS NULL)
        OR (
            video_size_bytes IS NOT NULL AND video_sha256 IS NOT NULL
            AND video_size_bytes > 0 AND octet_length(video_sha256) = 32
        )
    );""",
        """SET LOCAL lock_timeout = '5s';
LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE;

DO $super7$
BEGIN
    IF EXISTS (
        SELECT 1 FROM analysis_jobs
        WHERE video_size_bytes IS NOT NULL OR video_sha256 IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Cannot roll back Slice 5 while verified video metadata exists.';
    END IF;
END
$super7$;

ALTER TABLE analysis_jobs
    DROP CONSTRAINT analysis_jobs_video_integrity_pair_check,
    DROP COLUMN video_sha256,
    DROP COLUMN video_size_bytes;""",
    )
]
