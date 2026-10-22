"""Opt-in Slice 5 evidence using the task-owned disposable PostgreSQL harness."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, InsufficientPrivilege
from test_postgres_analysis_job_worker_integration import (
    _candidate,
    _connect,
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _force_expired,
    _grant_runtime_worker_privileges,
    _repositories,
    _run_database,
    _runner,
    _slice4_runner,
)

from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_repository import AnalysisJobPersistenceInvariantError
from adapters.yoyo_migration_backend import DatabaseMigrationError
from core.config import Settings
from domain.analysis_job import (
    AnalysisJobAccepted,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
)
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    LeaseDuration,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from domain.video_integrity import VideoIntegrity
from services.analysis_job_coordinator import (
    AnalysisJobCoordinator,
    CoordinatorTiming,
    CoordinatorUnclassifiedFailure,
)
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    FinalizableAnalysisResult,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AttemptFailed,
    AttemptResolutionAlreadyApplied,
    RecoverySummary,
)
from services.process_analysis_pool import ProcessAnalysisPool
from services.process_contracts import ChildAnalysisRequest, ParentFailure
from services.r2_video_input import R2DownloadError, R2VideoExecutor, R2VideoSettings


@pytest.mark.integration
def test_slice5_upgrade_preserves_legacy_row_and_can_roll_back_without_metadata(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        earlier = _slice4_runner(database.migration_settings)
        earlier.apply_pending()
        assert earlier.current_version() == "sprint2_slice4_result_outbox"
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        candidate = _candidate(504)
        with _connect(database.migration_settings) as connection:
            connection.execute(
                "INSERT INTO analysis_jobs (job_id, idempotency_lookup, "
                "request_fingerprint, video_id, player_id, video_reference, callback_url, "
                "state, accepted_at, analysis_version, attempt_limit, attempt_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'QUEUED', CURRENT_TIMESTAMP, %s, %s, 0)",
                (
                    candidate.job_id,
                    candidate.idempotency_lookup,
                    candidate.request_fingerprint,
                    candidate.video_id,
                    candidate.player_id,
                    candidate.video_reference,
                    candidate.callback_url,
                    candidate.analysis_version,
                    candidate.attempt_limit,
                ),
            ).close()
        runner = _runner(database.migration_settings)
        runner.apply_pending()
        assert runner.current_version() == "sprint2_slice5_video_integrity"

        async def read_upgraded() -> None:
            async with _repositories(_current_settings(database.runtime_settings)) as (
                admission,
                worker,
                pool,
            ):
                replay = await admission.accept_or_get(candidate, max_queue_size=10)
                assert isinstance(replay, AnalysisJobExisting)
                assert replay.job.video_integrity is None
                assert await _fetch_all(
                    pool,
                    "SELECT video_size_bytes, video_sha256 FROM analysis_jobs WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [(None, None)]
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=500_504)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                assert claim.job.video_integrity is None

        _run_database(read_upgraded())
        runner.rollback_last()
        assert runner.current_version() == "sprint2_slice4_result_outbox"
        with _connect(database.migration_settings) as connection:
            assert connection.execute(
                "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                (candidate.job_id,),
            ).fetchone() == ("RUNNING", 1)
            assert connection.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'analysis_jobs' "
                "AND column_name IN ('video_size_bytes', 'video_sha256')"
            ).fetchone() == (0,)
    assert factory.active_count == 0


@pytest.mark.integration
def test_slice5_verified_admission_privileges_atomicity_idempotency_and_readers(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _runner(database.migration_settings)
        assert runner.current_version() is None
        runner.apply_pending()
        assert runner.current_version() == "sprint2_slice5_video_integrity"

        with _connect(database.runtime_settings) as connection:
            assert connection.execute(
                "SELECT current_user, "
                "has_table_privilege(current_user, 'analysis_jobs', 'SELECT'), "
                "has_column_privilege(current_user, 'analysis_jobs', 'video_sha256', 'SELECT'), "
                "has_table_privilege(current_user, 'analysis_jobs', 'INSERT')"
            ).fetchone() == (database.runtime_settings._user, True, True, False)
            assert connection.execute("SELECT video_sha256 FROM analysis_jobs").fetchall() == []
        with _connect(database.migration_settings) as connection:
            connection.execute(
                sql.SQL("REVOKE SELECT ON TABLE analysis_jobs FROM {}").format(
                    sql.Identifier(database.runtime_settings._user)
                )
            ).close()
        with _connect(database.runtime_settings) as connection:
            assert connection.execute(
                "SELECT has_table_privilege(current_user, 'analysis_jobs', 'SELECT'), "
                "has_column_privilege(current_user, 'analysis_jobs', 'video_sha256', 'SELECT')"
            ).fetchone() == (False, False)
            with pytest.raises(InsufficientPrivilege):
                connection.execute("SELECT video_sha256 FROM analysis_jobs").fetchall()
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        with _connect(database.runtime_settings) as connection:
            assert connection.execute(
                "SELECT has_table_privilege(current_user, 'analysis_jobs', 'SELECT'), "
                "has_column_privilege(current_user, 'analysis_jobs', 'video_sha256', 'SELECT')"
            ).fetchone() == (True, True)
            assert connection.execute("SELECT video_sha256 FROM analysis_jobs").fetchall() == []
            with pytest.raises(InsufficientPrivilege):
                connection.execute("DELETE FROM analysis_jobs WHERE false")

        expected = VideoIntegrity(5, hashlib.sha256(b"video").hexdigest())
        candidate = replace(
            _candidate(501),
            video_integrity=expected,
            request_fingerprint=hashlib.sha256(b"verified request").digest(),
        )
        settings = _current_settings(database.runtime_settings)
        with _connect(database.migration_settings) as connection:
            connection.execute(
                "ALTER TABLE analysis_jobs ADD CONSTRAINT slice5_test_reject_metadata "
                "CHECK (video_size_bytes IS NULL)"
            ).close()

        async def rejected_insert() -> None:
            async with _repositories(settings) as (admission, _, pool):
                with pytest.raises(CheckViolation):
                    await admission.accept_or_get(candidate, max_queue_size=10)
                assert await _fetch_all(pool, "SELECT count(*) FROM analysis_jobs") == [(0,)]

        _run_database(rejected_insert())
        with _connect(database.migration_settings) as connection:
            connection.execute(
                "ALTER TABLE analysis_jobs DROP CONSTRAINT slice5_test_reject_metadata"
            ).close()

        async def accepted_and_claimed() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                accepted = await admission.accept_or_get(candidate, max_queue_size=10)
                assert isinstance(accepted, AnalysisJobAccepted)
                assert accepted.job.video_integrity == expected
                assert await _fetch_all(
                    pool,
                    "SELECT video_size_bytes, video_sha256 FROM analysis_jobs WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [(expected.size_bytes, bytes.fromhex(expected.sha256))]
                replay = await admission.accept_or_get(candidate, max_queue_size=10)
                assert isinstance(replay, AnalysisJobExisting)
                assert replay.job.video_integrity == expected
                changed = replace(
                    candidate,
                    job_id=UUID(int=500_502),
                    video_integrity=VideoIntegrity(6, expected.sha256),
                )
                conflict = await admission.accept_or_get(changed, max_queue_size=10)
                assert isinstance(conflict, AnalysisJobIdempotencyConflict)
                assert conflict.existing_job_id == candidate.job_id
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=500_501)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                assert claim.job.video_integrity == expected

        _run_database(accepted_and_claimed())
        with pytest.raises(DatabaseMigrationError):
            runner.rollback_last()
        assert runner.current_version() == "sprint2_slice5_video_integrity"
    assert factory.active_count == 0


@pytest.mark.integration
def test_slice5_corrupted_metadata_fails_closed_in_both_readers(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _runner(database.migration_settings).apply_pending()
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)
        candidate = replace(
            _candidate(502),
            video_integrity=VideoIntegrity(5, hashlib.sha256(b"video").hexdigest()),
        )

        async def insert() -> None:
            async with _repositories(settings) as (admission, _, _pool):
                assert isinstance(
                    await admission.accept_or_get(candidate, max_queue_size=10),
                    AnalysisJobAccepted,
                )

        _run_database(insert())
        with _connect(database.migration_settings) as connection:
            connection.execute(
                "ALTER TABLE analysis_jobs DROP CONSTRAINT analysis_jobs_video_integrity_pair_check"
            ).close()
            connection.execute(
                "UPDATE analysis_jobs SET video_sha256 = NULL WHERE job_id = %s",
                (candidate.job_id,),
            ).close()

        async def reject() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                with pytest.raises(AnalysisJobPersistenceInvariantError):
                    await admission.accept_or_get(candidate, max_queue_size=10)
                with pytest.raises(AnalysisJobWorkerPersistenceInvariantError):
                    await worker.claim_next(
                        worker_slot_id=WorkerSlotId(UUID(int=500_502)),
                        lease_duration=LeaseDuration(timedelta(seconds=30)),
                    )
                assert await _fetch_all(
                    pool,
                    "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [("QUEUED", 0)]

        _run_database(reject())
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("failure", ("partial", "local"))
def test_slice5_verified_worker_blocks_child_and_keeps_claim_for_recovery(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Incomplete transfer and unchecked source retain lease-based recovery."""

    class ForbiddenChild:
        calls = 0

        async def execute_enveloped(self, request: ChildAnalysisRequest) -> ParentFailure:
            self.calls += 1
            raise AssertionError("Verified bytes reached child execution.")

    class ForbiddenFinalizer:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
            self.calls += 1
            raise AssertionError("An unverified result reached finalization.")

    class R2Client:
        def get_object(self, **kwargs: str) -> dict[str, object]:
            return {
                "ContentLength": 5,
                "Body": io.BytesIO(b"wrong" if failure == "digest" else b"vid"),
            }

        def close(self) -> None:
            pass

    if failure != "local":
        monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: R2Client())

    factory = _disposable_factory(request)
    with factory() as database:
        _runner(database.migration_settings).apply_pending()
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        expected = VideoIntegrity(5, hashlib.sha256(b"video").hexdigest())
        candidate = replace(_candidate(503), video_integrity=expected)
        child = ForbiddenChild()
        finalizer = ForbiddenFinalizer()

        async def scenario() -> None:
            async with _repositories(_current_settings(database.runtime_settings)) as (
                admission,
                worker,
                pool,
            ):
                assert isinstance(
                    await admission.accept_or_get(candidate, max_queue_size=10),
                    AnalysisJobAccepted,
                )
                executor = (
                    child
                    if failure == "local"
                    else R2VideoExecutor(
                        child,
                        R2VideoSettings(
                            "https://00000000000000000000000000000000.r2.cloudflarestorage.com",
                            "test-read-id",
                            "test-read-secret",
                            tmp_path,
                            100,
                            5,
                        ),
                    )
                )
                coordinator = AnalysisJobCoordinator(
                    worker_repository=worker,
                    finalization_repository=finalizer,
                    child_executor=executor,
                    result_projector=convert_child_success,
                    worker_slot_id=WorkerSlotId(UUID(int=500_503)),
                    model_catalog={},
                    timing=CoordinatorTiming(
                        LeaseDuration(timedelta(seconds=30)), 2, 3, 1, 3, (0, 0)
                    ),
                )
                with pytest.raises(
                    CoordinatorUnclassifiedFailure if failure == "local" else R2DownloadError
                ):
                    await coordinator.run_one()
                assert coordinator.stopped
                assert child.calls == 0
                assert finalizer.calls == 0
                assert await _fetch_all(
                    pool,
                    "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [("RUNNING", 1)]
                assert await _fetch_all(
                    pool,
                    "SELECT status FROM analysis_job_attempts WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [("ACTIVE",)]
                assert await _fetch_all(pool, "SELECT count(*) FROM analysis_job_terminals") == [
                    (0,)
                ]
                assert await _fetch_all(pool, "SELECT count(*) FROM analysis_callback_outbox") == [
                    (0,)
                ]
                await asyncio.to_thread(
                    _force_expired, database.migration_settings, candidate.job_id, 1
                )
                assert await worker.recover_expired(batch_size=RecoveryBatchSize(10)) == (
                    RecoverySummary(requeued_count=1, exhausted_count=0)
                )
                assert await _fetch_all(
                    pool,
                    "SELECT state, attempt_count, video_size_bytes, video_sha256 "
                    "FROM analysis_jobs WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [("QUEUED", 1, expected.size_bytes, bytes.fromhex(expected.sha256))]
                assert await _fetch_all(
                    pool,
                    "SELECT status FROM analysis_job_attempts WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [("INTERRUPTED",)]
                assert await _fetch_all(pool, "SELECT count(*) FROM analysis_job_terminals") == [
                    (0,)
                ]
                assert await _fetch_all(pool, "SELECT count(*) FROM analysis_callback_outbox") == [
                    (0,)
                ]

        _run_database(scenario())
        assert list(tmp_path.iterdir()) == []
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("failure", ("empty", "digest", "size"))
def test_deterministic_r2_rejection_creates_failed_outbox_without_child(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    class ForbiddenChild:
        calls = 0

        async def execute_enveloped(self, request: ChildAnalysisRequest) -> ParentFailure:
            self.calls += 1
            raise AssertionError("Rejected input reached child execution.")

    class ForbiddenSuccessFinalizer:
        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
            raise AssertionError("Rejected input reached success finalization.")

    class R2Client:
        closed = False

        def get_object(self, **kwargs: str) -> dict[str, object]:
            if failure == "empty":
                return {"ContentLength": 0, "Body": io.BytesIO(b"")}
            if failure == "digest":
                return {"ContentLength": 5, "Body": io.BytesIO(b"wrong")}
            return {"ContentLength": 6, "Body": io.BytesIO(b"wrong!")}

        def close(self) -> None:
            self.closed = True

    client = R2Client()
    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: client)
    factory = _disposable_factory(request)
    with factory() as database:
        _runner(database.migration_settings).apply_pending()
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        expected = VideoIntegrity(5, hashlib.sha256(b"video").hexdigest())
        candidate = replace(
            _candidate(600 if failure == "empty" else 601 if failure == "digest" else 602),
            video_integrity=None if failure == "empty" else expected,
        )
        child = ForbiddenChild()

        async def scenario() -> None:
            async with _repositories(_current_settings(database.runtime_settings)) as (
                admission,
                worker,
                pool,
            ):
                assert isinstance(
                    await admission.accept_or_get(candidate, max_queue_size=10),
                    AnalysisJobAccepted,
                )
                coordinator = AnalysisJobCoordinator(
                    worker_repository=worker,
                    finalization_repository=ForbiddenSuccessFinalizer(),
                    child_executor=R2VideoExecutor(
                        child,
                        R2VideoSettings(
                            "https://00000000000000000000000000000000.r2.cloudflarestorage.com",
                            "test-read-id",
                            "test-read-secret",
                            tmp_path,
                            100,
                            5,
                        ),
                    ),
                    result_projector=convert_child_success,
                    worker_slot_id=WorkerSlotId(UUID(int=600_000)),
                    model_catalog={},
                    timing=CoordinatorTiming(
                        LeaseDuration(timedelta(seconds=30)), 2, 3, 1, 3, (0, 0)
                    ),
                )
                outcome = await coordinator.run_one()
                assert isinstance(outcome, AttemptFailed)
                assert outcome.authority.job_id == candidate.job_id
                assert outcome.failure_code == AttemptFailureCode("invalid_video")
                assert child.calls == 0
                assert not coordinator.stopped
                assert not coordinator.outstanding_tasks
                rows = await _fetch_all(
                    pool,
                    """SELECT j.state, j.attempt_count, a.status, a.safe_failure_code,
                              r.terminal_kind, r.safe_failure_code, r.failure_origin,
                              r.result_json, o.callback_event_id, o.delivery_state,
                              o.payload_schema_version, o.payload_json
                       FROM analysis_jobs AS j
                       JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                       JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                       JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                       WHERE j.job_id = %s""",
                    (candidate.job_id,),
                )
                assert len(rows) == 1
                row = rows[0]
                assert row[:8] == (
                    "FAILED",
                    1,
                    "TERMINAL_FAILURE",
                    "invalid_video",
                    "FAILED",
                    "invalid_video",
                    "CLASSIFIED_TERMINAL",
                    None,
                )
                assert isinstance(row[8], UUID)
                assert row[9:11] == ("PENDING", "analysis_callback_snapshot_v1")
                assert row[11] == json.loads(
                    failure_callback_snapshot_v1(
                        job_id=candidate.job_id,
                        video_id=candidate.video_id,
                        player_id=candidate.player_id,
                        safe_code="invalid_video",
                    )
                )
                replay = await worker.finalize_terminal_failure(
                    authority=outcome.authority,
                    failure_code=AttemptFailureCode("invalid_video"),
                )
                assert isinstance(replay, AttemptResolutionAlreadyApplied)
                assert await _fetch_all(
                    pool,
                    "SELECT callback_event_id FROM analysis_callback_outbox WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [(row[8],)]

        _run_database(scenario())
        assert client.closed
        assert list(tmp_path.iterdir()) == []
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("staged_failure", ("decoder", "missing_before_resolution"))
def test_untyped_staged_failure_keeps_claim_for_recovery(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    staged_failure: str,
) -> None:
    """Real child and PostgreSQL: ambiguous decode or lost staging needs recovery."""
    body_bytes = b"not a decodable video"

    class ForbiddenSuccessFinalizer:
        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
            raise AssertionError("Unclassified child failure reached success finalization.")

    class R2Client:
        def get_object(self, **kwargs: str) -> dict[str, object]:
            return {"ContentLength": len(body_bytes), "Body": io.BytesIO(body_bytes)}

        def close(self) -> None:
            pass

    class RemoveStagedBeforeChild:
        def __init__(self, child: ProcessAnalysisPool) -> None:
            self.child = child

        async def execute_enveloped(self, child_request: ChildAnalysisRequest) -> ParentFailure:
            assert child_request.staged_video_path is not None
            Path(child_request.staged_video_path).unlink()
            result = await self.child.execute_enveloped(child_request)
            assert isinstance(result, ParentFailure)
            assert result == ParentFailure("VideoNotFoundError", "Analysis could not be completed.")
            return result

    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: R2Client())
    r2_root = tmp_path / "r2"
    r2_root.mkdir()
    videos = tmp_path / "videos"
    videos.mkdir()
    settings = Settings(video_storage_root=str(videos), debug_output_dir=str(tmp_path / "debug"))
    factory = _disposable_factory(request)
    with factory() as database:
        _runner(database.migration_settings).apply_pending()
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        candidate = _candidate(603)

        async def scenario() -> None:
            child = ProcessAnalysisPool(settings)
            child.start()
            try:
                async with _repositories(_current_settings(database.runtime_settings)) as (
                    admission,
                    worker,
                    pool,
                ):
                    assert isinstance(
                        await admission.accept_or_get(candidate, max_queue_size=10),
                        AnalysisJobAccepted,
                    )
                    coordinator = AnalysisJobCoordinator(
                        worker_repository=worker,
                        finalization_repository=ForbiddenSuccessFinalizer(),
                        child_executor=R2VideoExecutor(
                            RemoveStagedBeforeChild(child)
                            if staged_failure == "missing_before_resolution"
                            else child,
                            R2VideoSettings(
                                "https://00000000000000000000000000000000.r2.cloudflarestorage.com",
                                "test-read-id",
                                "test-read-secret",
                                r2_root,
                                100,
                                5,
                            ),
                        ),
                        result_projector=convert_child_success,
                        worker_slot_id=WorkerSlotId(UUID(int=600_603)),
                        model_catalog={},
                        timing=CoordinatorTiming(
                            LeaseDuration(timedelta(seconds=30)), 2, 3, 1, 3, (0, 0)
                        ),
                    )
                    with pytest.raises(CoordinatorUnclassifiedFailure):
                        await coordinator.run_one()
                    assert coordinator.stopped
                    assert not coordinator.outstanding_tasks
                    assert not child.outstanding_submissions
                    assert await _fetch_all(
                        pool,
                        "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                        (candidate.job_id,),
                    ) == [("RUNNING", 1)]
                    assert await _fetch_all(
                        pool,
                        "SELECT status FROM analysis_job_attempts WHERE job_id = %s",
                        (candidate.job_id,),
                    ) == [("ACTIVE",)]
                    assert await _fetch_all(
                        pool, "SELECT count(*) FROM analysis_job_terminals"
                    ) == [(0,)]
                    assert await _fetch_all(
                        pool, "SELECT count(*) FROM analysis_callback_outbox"
                    ) == [(0,)]
                    await asyncio.to_thread(
                        _force_expired, database.migration_settings, candidate.job_id, 1
                    )
                    assert await worker.recover_expired(batch_size=RecoveryBatchSize(10)) == (
                        RecoverySummary(requeued_count=1, exhausted_count=0)
                    )
                    assert await _fetch_all(
                        pool,
                        "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                        (candidate.job_id,),
                    ) == [("QUEUED", 1)]
                    assert await _fetch_all(
                        pool, "SELECT count(*) FROM analysis_job_terminals"
                    ) == [(0,)]
                    assert await _fetch_all(
                        pool, "SELECT count(*) FROM analysis_callback_outbox"
                    ) == [(0,)]
            finally:
                await child.shutdown()

        _run_database(scenario())
        assert list(r2_root.iterdir()) == []
        assert not (tmp_path / "debug" / str(candidate.job_id)).exists()
    assert factory.active_count == 0
