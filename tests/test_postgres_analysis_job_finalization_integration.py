"""Default-off disposable PostgreSQL evidence for Slice 4 finalization.

Do not opt in during source implementation review. The existing harness owns and
cleans up its exact labeled container and credential file when separately run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
    UniqueViolation,
)
from test_analysis_job_finalization_contract import _RAW_MODEL, _SAFE_MODEL, _response
from test_postgres_analysis_job_worker_integration import (
    _admit,
    _connect,
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _force_expired,
    _grant_runtime_worker_privileges,
    _insert_attempt_writer_fixture,
    _insert_job_writer_fixture,
    _prepare_database,
    _prepare_slice3_database,
    _release_writer_and_join_actor,
    _repositories,
    _run_database,
    _runner,
    _schema_signature,
)

from adapters import psycopg_analysis_job_worker_repository as worker_adapter
from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_worker_repository import _Pool
from adapters.psycopg_database import PsycopgDatabasePool
from adapters.yoyo_migration_backend import DatabaseMigrationError
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import (
    AttemptFailureCode,
    LeaseDuration,
    LeaseId,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    FinalizableAnalysisResult,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AttemptExhausted,
    AttemptFailed,
    AttemptRequeued,
    AttemptResolutionAlreadyApplied,
    ClaimLost,
    RecoverySummary,
)
from services.process_contracts import ChildAnalysisSuccess


def _finalizer(pool: _Pool) -> PsycopgAnalysisJobFinalizationRepository:
    return PsycopgAnalysisJobFinalizationRepository(
        pool=pool,
        connection_acquire_timeout_seconds=5.0,
        operation_timeout_ms=5_000,
        lock_timeout_ms=1_000,
    )


def _snapshot(claim: WorkerClaim) -> FinalizableAnalysisResult:
    response = _response("completed").model_copy(
        update={
            "analysis_id": str(claim.authority.job_id),
            "analysis_version": claim.job.analysis_version,
        }
    )
    child = ChildAnalysisSuccess(
        str(claim.authority.job_id),
        response.model_dump_json(),
        claim.job.analysis_version,
        _RAW_MODEL,
        17,
    )
    return convert_child_success(
        child=child, response=response, claim=claim, model_catalog={_RAW_MODEL: _SAFE_MODEL}
    )


async def _job_evidence(pool: PsycopgDatabasePool, job_id: UUID) -> tuple[tuple[object, ...], ...]:
    rows = await _fetch_all(
        pool,
        """SELECT j.state, j.attempt_count, a.attempt_number, a.status,
                  a.safe_failure_code, a.finished_at, r.terminal_kind,
                  r.finalized_at, r.safe_failure_code, r.failure_origin,
                  r.result_json, r.processing_duration_ms, o.callback_event_id,
                  o.payload_json, o.created_at
           FROM analysis_jobs AS j
           LEFT JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
           LEFT JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
           LEFT JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
           WHERE j.job_id = %s ORDER BY a.attempt_number""",
        (job_id,),
    )
    return tuple(rows)


def _slice4_schema_signature(settings: DatabaseSettings) -> bytes:
    with _connect(settings) as connection:
        columns = connection.execute(
            """SELECT table_name, column_name, data_type, is_nullable, column_default
               FROM information_schema.columns WHERE table_schema = 'public'
                 AND table_name IN ('analysis_jobs', 'analysis_job_attempts',
                                    'analysis_job_terminals', 'analysis_callback_outbox')
               ORDER BY table_name, ordinal_position"""
        ).fetchall()
        constraints = connection.execute(
            """SELECT relation.relname, c.contype, pg_catalog.pg_get_constraintdef(c.oid, true)
               FROM pg_catalog.pg_constraint AS c
               JOIN pg_catalog.pg_class AS relation ON relation.oid = c.conrelid
               JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
               WHERE namespace.nspname = 'public' AND relation.relname IN
                   ('analysis_jobs', 'analysis_job_attempts',
                    'analysis_job_terminals', 'analysis_callback_outbox')
               ORDER BY relation.relname, c.contype, pg_catalog.pg_get_constraintdef(c.oid, true)"""
        ).fetchall()
        indexes = connection.execute(
            """SELECT tablename, indexdef FROM pg_catalog.pg_indexes
               WHERE schemaname = 'public' AND tablename IN
                   ('analysis_jobs', 'analysis_job_attempts',
                    'analysis_job_terminals', 'analysis_callback_outbox')
               ORDER BY tablename, indexname"""
        ).fetchall()
    return hashlib.sha256(repr((columns, constraints, indexes)).encode()).digest()


def _wait_for_slice4_migration_lock(
    settings: DatabaseSettings,
    *,
    relation: str,
    writer_pid: int,
) -> int:
    # The migration's approved per-lock timeout is 5s; reserve time for connect/release.
    deadline = time.monotonic() + 2.0
    with _connect(settings) as connection:
        while time.monotonic() < deadline:
            row = connection.execute(
                """SELECT waiting.pid, pg_blocking_pids(waiting.pid)
                   FROM pg_catalog.pg_locks AS lock_row
                   JOIN pg_catalog.pg_class AS relation ON relation.oid = lock_row.relation
                   JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                   JOIN pg_catalog.pg_stat_activity AS waiting ON waiting.pid = lock_row.pid
                   WHERE namespace.nspname = 'public' AND relation.relname = %s
                     AND lock_row.mode = 'AccessExclusiveLock'
                     AND lock_row.granted IS FALSE
                     AND waiting.query LIKE %s
                   ORDER BY waiting.pid LIMIT 1""",
                (relation, f"%LOCK TABLE {relation} IN ACCESS EXCLUSIVE MODE%"),
            ).fetchone()
            if row is not None:
                actor_pid, blockers = row
                assert type(actor_pid) is int and actor_pid != writer_pid
                assert isinstance(blockers, list)
                assert writer_pid in blockers
                assert all(type(pid) is int for pid in blockers)
                return actor_pid
            time.sleep(0.01)
    raise AssertionError(f"Slice 4 migration lock on {relation} was not observed in time.")


@pytest.mark.integration
def test_success_persists_one_terminal_event_and_replays_original_identity(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 70)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=70)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                result = _snapshot(claim)
                finalizer = _finalizer(pool)
                first = await finalizer.finalize_success(authority=claim.authority, result=result)
                assert isinstance(first, AnalysisJobCompleted)
                rows = await _fetch_all(
                    pool,
                    """SELECT j.state, a.status, r.terminal_kind, r.result_json,
                              r.processing_duration_ms, o.callback_event_id, o.payload_json,
                              o.delivery_state, r.finalized_at, o.created_at
                       FROM analysis_jobs AS j
                       JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                       JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                       JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                       WHERE j.job_id = %s""",
                    (candidate.job_id,),
                )
                assert len(rows) == 1
                (
                    state,
                    status,
                    kind,
                    projected,
                    duration,
                    event,
                    payload,
                    delivery,
                    finish,
                    created,
                ) = rows[0]
                assert (state, status, kind, duration, event, delivery) == (
                    "COMPLETED",
                    "SUCCEEDED",
                    "COMPLETED",
                    17,
                    first.callback_event_id,
                    "PENDING",
                )
                assert finish == created == first.finished_at
                assert isinstance(projected, dict) and isinstance(payload, dict)
                assert projected["analysis_id"] == payload["request_id"] == str(candidate.job_id)
                first_processing_time = projected["processing_time_ms"]
                assert payload["video_id"] == candidate.video_id
                assert payload["player_id"] == candidate.player_id
                replay = await finalizer.finalize_success(
                    authority=claim.authority,
                    result=replace(result, processing_duration_ms=99),
                )
                assert replay == AnalysisJobCompletionAlreadyApplied(
                    claim.authority, first.finished_at, first.callback_event_id
                )
                assert await _fetch_all(
                    pool,
                    """SELECT processing_duration_ms, result_json->>'processing_time_ms'
                       FROM analysis_job_terminals WHERE job_id = %s""",
                    (candidate.job_id,),
                ) == [(17, str(first_processing_time))]
                normalized = json.loads(result.result_json)
                normalized["processing_time_ms"] = 99
                normalized["video"]["fps"] = 2
                equivalent = replace(
                    result,
                    result_json=json.dumps(normalized, indent=2).encode(),
                    processing_duration_ms=999,
                )
                assert (
                    await finalizer.finalize_success(authority=claim.authority, result=equivalent)
                    == replay
                )
                assert await _fetch_all(
                    pool,
                    """SELECT processing_duration_ms, result_json->>'processing_time_ms'
                       FROM analysis_job_terminals WHERE job_id = %s""",
                    (candidate.job_id,),
                ) == [(17, str(first_processing_time))]
                substantive = json.loads(result.result_json)
                substantive["video"]["fps"] = 3
                changed_substantive = replace(result, result_json=json.dumps(substantive).encode())
                assert isinstance(
                    await finalizer.finalize_success(
                        authority=claim.authority, result=changed_substantive
                    ),
                    ClaimLost,
                )
                changed_callback = json.loads(result.callback_json)
                changed_callback["summary"]["different"] = True
                assert isinstance(
                    await finalizer.finalize_success(
                        authority=claim.authority,
                        result=replace(result, callback_json=json.dumps(changed_callback).encode()),
                    ),
                    ClaimLost,
                )
                changed_projection = json.loads(result.result_json)
                changed_projection["model_version"] = "different_model"
                changed_projection["algorithm_versions"]["player_detection"] = "different_model"
                changed = replace(
                    result,
                    result_json=json.dumps(changed_projection, separators=(",", ":")).encode(),
                    model_version="different_model",
                )
                assert isinstance(
                    await finalizer.finalize_success(authority=claim.authority, result=changed),
                    ClaimLost,
                )
                assert isinstance(
                    await finalizer.finalize_success(
                        authority=replace(
                            claim.authority, worker_slot_id=WorkerSlotId(UUID(int=700))
                        ),
                        result=result,
                    ),
                    ClaimLost,
                )
                for stale in (
                    replace(claim.authority, attempt_number=claim.authority.attempt_number + 1),
                    replace(
                        claim.authority, lease_id=type(claim.authority.lease_id)(UUID(int=701))
                    ),
                ):
                    assert isinstance(
                        await finalizer.finalize_success(authority=stale, result=result),
                        ClaimLost,
                    )
                absent_job_id = UUID(int=700_000)
                absent_result = json.loads(result.result_json)
                absent_result["analysis_id"] = str(absent_job_id)
                absent_callback = json.loads(result.callback_json)
                absent_callback["request_id"] = str(absent_job_id)
                assert isinstance(
                    await finalizer.finalize_success(
                        authority=replace(claim.authority, job_id=absent_job_id),
                        result=replace(
                            result,
                            result_json=json.dumps(absent_result).encode(),
                            callback_json=json.dumps(absent_callback).encode(),
                        ),
                    ),
                    ClaimLost,
                )
                versioned = json.loads(result.result_json)
                versioned["analysis_version"] = "Version_2.0"
                assert isinstance(
                    await finalizer.finalize_success(
                        authority=claim.authority,
                        result=replace(
                            result,
                            analysis_version="Version_2.0",
                            result_json=json.dumps(versioned).encode(),
                        ),
                    ),
                    ClaimLost,
                )

        _run_database(verify())
        with _connect(database.runtime_settings) as connection:
            for statement in (
                "UPDATE analysis_job_terminals SET terminal_kind = terminal_kind WHERE job_id = %s",
                "DELETE FROM analysis_job_terminals WHERE job_id = %s",
                "UPDATE analysis_callback_outbox SET payload_json = payload_json WHERE job_id = %s",
                "DELETE FROM analysis_callback_outbox WHERE job_id = %s",
            ):
                with pytest.raises(InsufficientPrivilege):
                    connection.execute(statement, (UUID(int=300_070),)).close()
    assert factory.active_count == 0


@pytest.mark.integration
def test_terminal_failure_creates_one_event_and_slice4_downgrade_refuses_rows(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 71)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=71)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                outcome = await worker.finalize_terminal_failure(
                    authority=claim.authority, failure_code=AttemptFailureCode("safe_failure")
                )
                assert isinstance(outcome, AttemptFailed)
                rows = await _fetch_all(
                    pool,
                    """SELECT j.state, a.status, r.terminal_kind, r.safe_failure_code,
                              r.failure_origin, o.payload_json, o.callback_event_id
                       FROM analysis_jobs AS j
                       JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                       JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                       JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                       WHERE j.job_id = %s""",
                    (candidate.job_id,),
                )
                assert len(rows) == 1
                state, status, kind, code, origin, payload, event = rows[0]
                assert (state, status, kind, code, origin) == (
                    "FAILED",
                    "TERMINAL_FAILURE",
                    "FAILED",
                    "safe_failure",
                    "CLASSIFIED_TERMINAL",
                )
                assert isinstance(payload, dict)
                assert payload["error"] == {
                    "code": "safe_failure",
                    "message": "Analysis could not be completed.",
                }
                assert type(event) is UUID

        _run_database(verify())
        signature = _schema_signature(database.migration_settings)
        with pytest.raises(DatabaseMigrationError):
            runner.rollback_last()
        assert runner.current_version() == "sprint2_slice4_result_outbox"
        assert _schema_signature(database.migration_settings) == signature
    assert factory.active_count == 0


@pytest.mark.integration
def test_committed_success_withheld_response_replays_retained_snapshot(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 72)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=72)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                retained = _snapshot(claim)
                finalizer = _finalizer(pool)

                async def lose_response_after_commit() -> None:
                    completed = await finalizer.finalize_success(
                        authority=claim.authority, result=retained
                    )
                    assert isinstance(completed, AnalysisJobCompleted)
                    raise ConnectionError("test transport withheld the committed response")

                with pytest.raises(ConnectionError, match="withheld"):
                    await lose_response_after_commit()
                committed = await _job_evidence(pool, candidate.job_id)
                assert len(committed) == 1
                first = committed[0]
                assert first[0] == "COMPLETED" and first[3] == "SUCCEEDED"
                assert type(first[12]) is UUID
                assert isinstance(first[7], datetime)
                replay = await finalizer.finalize_success(
                    authority=claim.authority, result=retained
                )
                assert replay == AnalysisJobCompletionAlreadyApplied(
                    claim.authority, first[7], first[12]
                )
                assert await _job_evidence(pool, candidate.job_id) == committed

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "limit", "expected_status", "expected_job", "terminal_code", "origin"),
    (
        ("terminal", 3, "TERMINAL_FAILURE", "FAILED", "safe_terminal", "CLASSIFIED_TERMINAL"),
        (
            "classified_limit",
            1,
            "EXHAUSTED",
            "FAILED",
            "attempts_exhausted",
            "CLASSIFIED_EXHAUSTION",
        ),
        ("expiry_limit", 1, "EXHAUSTED", "FAILED", "attempts_exhausted", "EXPIRY_EXHAUSTION"),
        ("retry_below", 2, "RETRYABLE_FAILURE", "QUEUED", None, None),
        ("expiry_below", 2, "INTERRUPTED", "QUEUED", None, None),
    ),
)
def test_failure_and_expiry_terminalization_obey_one_event_boundary(
    request: pytest.FixtureRequest,
    kind: str,
    limit: int,
    expected_status: str,
    expected_job: str,
    terminal_code: str | None,
    origin: str | None,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 73, attempt_limit=limit)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=73)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                code = AttemptFailureCode("safe_terminal" if kind == "terminal" else "safe_retry")
                if kind == "terminal":
                    terminal_outcome = await worker.finalize_terminal_failure(
                        authority=claim.authority, failure_code=code
                    )
                    assert isinstance(terminal_outcome, AttemptFailed)
                elif kind in ("classified_limit", "retry_below"):
                    retry_outcome = await worker.finalize_retryable_failure(
                        authority=claim.authority, failure_code=code
                    )
                    assert isinstance(
                        retry_outcome,
                        AttemptExhausted if kind == "classified_limit" else AttemptRequeued,
                    )
                else:
                    _force_expired(database.migration_settings, candidate.job_id, 1)
                    summary = await worker.recover_expired(batch_size=RecoveryBatchSize(10))
                    assert summary == RecoverySummary(
                        0 if kind == "expiry_limit" else 1,
                        1 if kind == "expiry_limit" else 0,
                    )
                rows = await _job_evidence(pool, candidate.job_id)
                assert len(rows) == 1
                row = rows[0]
                assert (row[0], row[1], row[2], row[3]) == (expected_job, 1, 1, expected_status)
                assert row[4] == (None if kind.startswith("expiry") else code.value)
                if terminal_code is None:
                    assert row[6:] == (None,) * 9
                    return
                assert (row[6], row[8], row[9], row[10], row[11]) == (
                    "FAILED",
                    terminal_code,
                    origin,
                    None,
                    None,
                )
                assert row[5] == row[7] == row[14]
                assert type(row[12]) is UUID and row[12].int != 0
                assert isinstance(row[13], dict)
                assert row[13] == json.loads(
                    failure_callback_snapshot_v1(
                        job_id=candidate.job_id,
                        video_id=candidate.video_id,
                        player_id=candidate.player_id,
                        safe_code=terminal_code,
                    )
                )
                if kind == "terminal":
                    replay = await worker.finalize_terminal_failure(
                        authority=claim.authority, failure_code=code
                    )
                    assert isinstance(replay, AttemptResolutionAlreadyApplied)
                elif kind == "classified_limit":
                    retry_replay = await worker.finalize_retryable_failure(
                        authority=claim.authority, failure_code=code
                    )
                    assert isinstance(retry_replay, AttemptResolutionAlreadyApplied)
                else:
                    assert await worker.recover_expired(
                        batch_size=RecoveryBatchSize(10)
                    ) == RecoverySummary(0, 0)
                assert await _job_evidence(pool, candidate.job_id) == rows

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_stale_and_expired_success_never_write_terminal_rows(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 74)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=74)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                result = _snapshot(claim)
                finalizer = _finalizer(pool)
                before = await _job_evidence(pool, candidate.job_id)
                for stale in (
                    replace(claim.authority, worker_slot_id=WorkerSlotId(UUID(int=740))),
                    replace(claim.authority, lease_id=LeaseId(UUID(int=741))),
                    replace(claim.authority, attempt_number=2),
                ):
                    assert isinstance(
                        await finalizer.finalize_success(authority=stale, result=result),
                        ClaimLost,
                    )
                    assert await _job_evidence(pool, candidate.job_id) == before
                _force_expired(database.migration_settings, candidate.job_id, 1)
                expired = await _job_evidence(pool, candidate.job_id)
                assert isinstance(
                    await finalizer.finalize_success(authority=claim.authority, result=result),
                    ClaimLost,
                )
                assert await _job_evidence(pool, candidate.job_id) == expired
                assert await _fetch_all(
                    pool,
                    "SELECT count(*) FROM analysis_job_terminals WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [(0,)]
                assert await _fetch_all(
                    pool,
                    "SELECT count(*) FROM analysis_callback_outbox WHERE job_id = %s",
                    (candidate.job_id,),
                ) == [(0,)]

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("missing", ("terminal", "outbox"))
def test_privileged_corruption_of_required_terminal_evidence_precedes_stale_classification(
    request: pytest.FixtureRequest, missing: str
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)
        retained_claim: WorkerClaim | None = None
        retained_result: FinalizableAnalysisResult | None = None

        async def create_terminal() -> None:
            nonlocal retained_claim, retained_result
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 75)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=75)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                result = _snapshot(claim)
                assert isinstance(
                    await _finalizer(pool).finalize_success(
                        authority=claim.authority, result=result
                    ),
                    AnalysisJobCompleted,
                )
                retained_claim, retained_result = claim, result

        _run_database(create_terminal())
        assert retained_claim is not None and retained_result is not None
        claim_value, result_value = retained_claim, retained_result
        # Privileged fixture corruption only: the runtime role cannot DELETE R/O.
        with _connect(database.inspection_settings) as connection:
            if missing == "terminal":
                connection.execute("SET session_replication_role = replica").close()
                connection.execute(
                    "DELETE FROM analysis_job_terminals WHERE job_id = %s",
                    (claim_value.authority.job_id,),
                ).close()
            else:
                connection.execute(
                    "DELETE FROM analysis_callback_outbox WHERE job_id = %s",
                    (claim_value.authority.job_id,),
                ).close()

        async def reject_corruption() -> None:
            async with _repositories(settings) as (_, worker, pool):
                stale = replace(claim_value.authority, worker_slot_id=WorkerSlotId(UUID(int=750)))
                before = await _job_evidence(pool, claim_value.authority.job_id)
                with pytest.raises(AnalysisJobWorkerPersistenceInvariantError):
                    await _finalizer(pool).finalize_success(authority=stale, result=result_value)
                with pytest.raises(AnalysisJobWorkerPersistenceInvariantError):
                    await worker.finalize_terminal_failure(
                        authority=stale, failure_code=AttemptFailureCode("safe_terminal")
                    )
                assert await _job_evidence(pool, claim_value.authority.job_id) == before

        _run_database(reject_corruption())
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("failed_write", ("attempt", "job", "terminal", "outbox"))
def test_success_transaction_rolls_back_after_test_owned_sql_write_failure(
    request: pytest.FixtureRequest, failed_write: str
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 76)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=76)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                before = await _job_evidence(pool, candidate.job_id)
                # Instrument only this disposable database; the production SQL is unchanged.
                with _connect(database.migration_settings) as connection:
                    if failed_write in ("attempt", "job"):
                        connection.execute(
                            """CREATE FUNCTION slice4_test_reject_write() RETURNS trigger
                               LANGUAGE plpgsql AS $$ BEGIN
                                   RAISE EXCEPTION 'task-owned write rejection';
                               END $$"""
                        ).close()
                        if failed_write == "attempt":
                            connection.execute(
                                """CREATE TRIGGER slice4_test_reject_attempt
                                   BEFORE UPDATE ON analysis_job_attempts
                                   FOR EACH ROW WHEN (NEW.status = 'SUCCEEDED')
                                   EXECUTE FUNCTION slice4_test_reject_write()"""
                            ).close()
                        else:
                            connection.execute(
                                """CREATE TRIGGER slice4_test_reject_job
                                   BEFORE UPDATE ON analysis_jobs
                                   FOR EACH ROW WHEN (NEW.state = 'COMPLETED')
                                   EXECUTE FUNCTION slice4_test_reject_write()"""
                            ).close()
                    elif failed_write == "terminal":
                        connection.execute(
                            "ALTER TABLE analysis_job_terminals ADD CONSTRAINT slice4_test_reject CHECK (false)"
                        ).close()
                    else:
                        connection.execute(
                            "ALTER TABLE analysis_callback_outbox ADD CONSTRAINT slice4_test_reject CHECK (false)"
                        ).close()
                with pytest.raises(psycopg.Error):
                    await _finalizer(pool).finalize_success(
                        authority=claim.authority, result=_snapshot(claim)
                    )
                assert await _job_evidence(pool, candidate.job_id) == before

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_event_uuid_collision_rolls_back_second_terminal_without_uuid_retry(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                first_job = await _admit(admission, 77)
                first_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=77)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(first_claim, WorkerClaim)
                first = await _finalizer(pool).finalize_success(
                    authority=first_claim.authority, result=_snapshot(first_claim)
                )
                assert isinstance(first, AnalysisJobCompleted)
                first_rows = await _job_evidence(pool, first_job.job_id)
                second_job = await _admit(admission, 78)
                second_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=78)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(second_claim, WorkerClaim)
                before = await _job_evidence(pool, second_job.job_id)
                # Test-owned DDL forces the normal INSERT DEFAULT to collide; no UUID policy changes.
                with _connect(database.migration_settings) as connection:
                    connection.execute(
                        sql.SQL(
                            """ALTER TABLE analysis_callback_outbox ALTER COLUMN callback_event_id
                               SET DEFAULT {}"""
                        ).format(sql.Literal(first.callback_event_id))
                    ).close()
                try:
                    with pytest.raises(UniqueViolation):
                        await _finalizer(pool).finalize_success(
                            authority=second_claim.authority, result=_snapshot(second_claim)
                        )
                finally:
                    with _connect(database.migration_settings) as connection:
                        connection.execute(
                            """ALTER TABLE analysis_callback_outbox ALTER COLUMN callback_event_id
                               SET DEFAULT gen_random_uuid()"""
                        ).close()
                assert await _job_evidence(pool, second_job.job_id) == before
                assert await _job_evidence(pool, first_job.job_id) == first_rows

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_slice4_database_fk_unique_and_check_constraints_reject_invalid_rows(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def establish() -> tuple[UUID, UUID]:
            async with _repositories(settings) as (admission, worker, pool):
                completed_job = await _admit(admission, 79)
                completed_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=79)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(completed_claim, WorkerClaim)
                assert isinstance(
                    await _finalizer(pool).finalize_success(
                        authority=completed_claim.authority, result=_snapshot(completed_claim)
                    ),
                    AnalysisJobCompleted,
                )
                active_job = await _admit(admission, 80)
                active_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=80)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(active_claim, WorkerClaim)
                return completed_job.job_id, active_job.job_id

        completed_id, active_id = _run_database(establish())
        with _connect(database.migration_settings) as connection:
            with pytest.raises(ForeignKeyViolation):
                connection.execute(
                    """INSERT INTO analysis_job_terminals (
                           job_id, attempt_number, terminal_kind, finalized_at,
                           terminal_schema_version, safe_failure_code, failure_origin
                       ) VALUES (%s, 1, 'FAILED', clock_timestamp(),
                           'analysis_terminal_v1', 'safe_failure', 'CLASSIFIED_TERMINAL')""",
                    (UUID(int=999_999),),
                ).close()
            with pytest.raises(CheckViolation):
                connection.execute(
                    """INSERT INTO analysis_job_terminals (
                           job_id, attempt_number, terminal_kind, finalized_at,
                           terminal_schema_version, safe_failure_code, failure_origin
                       ) VALUES (%s, 1, 'FAILED', clock_timestamp(),
                           'analysis_terminal_v1', 'INVALID_CODE', 'CLASSIFIED_TERMINAL')""",
                    (active_id,),
                ).close()
            with pytest.raises(CheckViolation):
                connection.execute(
                    "UPDATE analysis_job_attempts SET status = 'SUCCEEDED' WHERE job_id = %s",
                    (active_id,),
                ).close()
            with pytest.raises(UniqueViolation):
                connection.execute(
                    """INSERT INTO analysis_callback_outbox (
                           job_id, payload_schema_version, payload_json, delivery_state,
                           created_at, next_attempt_at)
                       SELECT job_id, payload_schema_version, payload_json, delivery_state,
                              created_at, next_attempt_at
                       FROM analysis_callback_outbox WHERE job_id = %s""",
                    (completed_id,),
                ).close()
            assert connection.execute(
                "SELECT count(*) FROM analysis_job_terminals WHERE job_id = %s", (active_id,)
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT status FROM analysis_job_attempts WHERE job_id = %s", (active_id,)
            ).fetchone() == ("ACTIVE",)
            assert connection.execute(
                "SELECT count(*) FROM analysis_callback_outbox WHERE job_id = %s", (completed_id,)
            ).fetchone() == (1,)
    assert factory.active_count == 0


@pytest.mark.integration
def test_recovery_batch_rolls_back_earlier_interruption_when_event_write_fails(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                first_job = await _admit(admission, 81, attempt_limit=2)
                first_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=81)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(first_claim, WorkerClaim)
                second_job = await _admit(admission, 82, attempt_limit=1)
                second_claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=82)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(second_claim, WorkerClaim)
                _force_expired(database.migration_settings, first_job.job_id, 1)
                _force_expired(database.migration_settings, second_job.job_id, 1)
                before_first = await _job_evidence(pool, first_job.job_id)
                before_second = await _job_evidence(pool, second_job.job_id)
                # The second candidate's outbox INSERT fails after the first candidate changed.
                with _connect(database.migration_settings) as connection:
                    connection.execute(
                        "ALTER TABLE analysis_callback_outbox ADD CONSTRAINT slice4_test_reject CHECK (false)"
                    ).close()
                with pytest.raises(CheckViolation):
                    await worker.recover_expired(batch_size=RecoveryBatchSize(2))
                assert await _job_evidence(pool, first_job.job_id) == before_first
                assert await _job_evidence(pool, second_job.job_id) == before_second

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("writer_relation", "job_id"),
    (("analysis_jobs", UUID(int=400_101)), ("analysis_job_attempts", UUID(int=400_102))),
)
def test_slice4_upgrade_waits_for_ordered_writer_lock_then_preserves_valid_rows(
    request: pytest.FixtureRequest, writer_relation: str, job_id: UUID
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_slice3_database(database)
        writer = _connect(database.migration_settings, autocommit=False)
        actor_errors: list[BaseException | None] = []

        def upgrade_actor() -> None:
            try:
                _runner(database.migration_settings).apply_pending()
            except BaseException as error:
                actor_errors.append(error)
            else:
                actor_errors.append(None)

        try:
            if writer_relation == "analysis_jobs":
                _insert_job_writer_fixture(writer, job_id)
            else:
                _insert_attempt_writer_fixture(database.migration_settings, writer, job_id)
            pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
            assert pid_row is not None and type(pid_row[0]) is int
            writer_pid = pid_row[0]
            actor = threading.Thread(target=upgrade_actor, name="slice4-upgrade", daemon=True)
            actor.start()
        except BaseException as primary:
            try:
                writer.close()
            except BaseException as cleanup:
                if not isinstance(cleanup, Exception):
                    raise cleanup from primary
                raise primary from cleanup
            raise
        writer_released = False
        try:
            actor_pid = _wait_for_slice4_migration_lock(
                database.inspection_settings,
                relation=writer_relation,
                writer_pid=writer_pid,
            )
            if writer_relation == "analysis_job_attempts":
                with _connect(database.inspection_settings) as connection:
                    assert connection.execute(
                        """SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks
                           WHERE pid = %s AND relation = 'public.analysis_jobs'::regclass
                             AND mode = 'AccessExclusiveLock' AND granted IS TRUE)""",
                        (actor_pid,),
                    ).fetchone() == (True,)
            writer.commit()
            writer_released = True
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=writer_released)
        assert actor_errors == [None]
        assert runner.current_version() == "sprint2_slice4_result_outbox"
        assert database.application_relations() == frozenset(
            {
                "public.analysis_jobs",
                "public.analysis_job_attempts",
                "public.analysis_job_terminals",
                "public.analysis_callback_outbox",
            }
        )
        with _connect(database.migration_settings) as connection:
            row = connection.execute(
                "SELECT state, attempt_count, attempt_limit FROM analysis_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        assert row == (
            ("QUEUED", 0, 3) if writer_relation == "analysis_jobs" else ("RUNNING", 1, 3)
        )
    assert factory.active_count == 0


@pytest.mark.integration
def test_slice4_upgrade_refuses_incompatible_terminal_row_without_schema_change(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_slice3_database(database)
        incompatible_id = UUID(int=400_103)
        with _connect(database.migration_settings) as connection:
            _insert_job_writer_fixture(connection, incompatible_id)
            connection.execute(
                "UPDATE analysis_jobs SET state = 'COMPLETED' WHERE job_id = %s",
                (incompatible_id,),
            ).close()
        signature = _schema_signature(database.migration_settings)
        with pytest.raises(DatabaseMigrationError):
            runner.apply_pending()
        assert runner.current_version() == "sprint2_slice3_worker_attempt"
        assert _schema_signature(database.migration_settings) == signature
        with _connect(database.migration_settings) as connection:
            assert connection.execute(
                "SELECT state, analysis_version, attempt_limit, attempt_count "
                "FROM analysis_jobs WHERE job_id = %s",
                (incompatible_id,),
            ).fetchone() == ("COMPLETED", "1.0.0", 3, 0)
        assert database.application_relations() == frozenset(
            {"public.analysis_jobs", "public.analysis_job_attempts"}
        )
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("writer_relation", ("analysis_jobs", "analysis_callback_outbox"))
def test_slice4_downgrade_waits_for_writer_then_refuses_preserving_terminal_data(
    request: pytest.FixtureRequest, writer_relation: str
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def establish() -> tuple[UUID, tuple[tuple[object, ...], ...]]:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 83)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=83)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                assert isinstance(
                    await _finalizer(pool).finalize_success(
                        authority=claim.authority, result=_snapshot(claim)
                    ),
                    AnalysisJobCompleted,
                )
                return candidate.job_id, await _job_evidence(pool, candidate.job_id)

        terminal_id, terminal_rows = _run_database(establish())
        signature = _slice4_schema_signature(database.migration_settings)
        writer = _connect(database.migration_settings, autocommit=False)
        actor_errors: list[BaseException | None] = []

        def downgrade_actor() -> None:
            try:
                _runner(database.migration_settings).rollback_last()
            except BaseException as error:
                actor_errors.append(error)
            else:
                actor_errors.append(None)

        try:
            if writer_relation == "analysis_jobs":
                _insert_job_writer_fixture(writer, UUID(int=400_104))
            else:
                writer.execute(
                    """UPDATE analysis_callback_outbox SET delivery_state = 'RETRYING'
                       WHERE job_id = %s""",
                    (terminal_id,),
                ).close()
            pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
            assert pid_row is not None and type(pid_row[0]) is int
            writer_pid = pid_row[0]
            actor = threading.Thread(target=downgrade_actor, name="slice4-downgrade", daemon=True)
            actor.start()
        except BaseException as primary:
            try:
                writer.close()
            except BaseException as cleanup:
                if not isinstance(cleanup, Exception):
                    raise cleanup from primary
                raise primary from cleanup
            raise
        writer_released = False
        try:
            actor_pid = _wait_for_slice4_migration_lock(
                database.inspection_settings,
                relation=writer_relation,
                writer_pid=writer_pid,
            )
            if writer_relation == "analysis_callback_outbox":
                with _connect(database.inspection_settings) as connection:
                    for locked in (
                        "analysis_jobs",
                        "analysis_job_attempts",
                        "analysis_job_terminals",
                    ):
                        assert connection.execute(
                            """SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks AS lock_row
                               JOIN pg_catalog.pg_class AS relation
                                 ON relation.oid = lock_row.relation
                               WHERE lock_row.pid = %s AND relation.relname = %s
                                 AND lock_row.mode = 'AccessExclusiveLock'
                                 AND lock_row.granted IS TRUE)""",
                            (actor_pid, locked),
                        ).fetchone() == (True,)
            writer.commit()
            writer_released = True
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=writer_released)
        assert len(actor_errors) == 1
        assert isinstance(actor_errors[0], DatabaseMigrationError)
        assert runner.current_version() == "sprint2_slice4_result_outbox"
        assert _slice4_schema_signature(database.migration_settings) == signature
        assert database.application_relations() == frozenset(
            {
                "public.analysis_jobs",
                "public.analysis_job_attempts",
                "public.analysis_job_terminals",
                "public.analysis_callback_outbox",
            }
        )

        async def unchanged() -> None:
            async with _repositories(settings) as (_, _, pool):
                assert await _job_evidence(pool, terminal_id) == terminal_rows

        _run_database(unchanged())
        with _connect(database.migration_settings) as connection:
            if writer_relation == "analysis_jobs":
                assert connection.execute(
                    "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                    (UUID(int=400_104),),
                ).fetchone() == ("QUEUED", 0)
            else:
                assert connection.execute(
                    "SELECT delivery_state FROM analysis_callback_outbox WHERE job_id = %s",
                    (terminal_id,),
                ).fetchone() == ("RETRYING",)
    assert factory.active_count == 0


@pytest.mark.integration
@pytest.mark.parametrize("missing", ("terminal", "outbox"))
def test_c9a_observes_terminal_corruption_after_active_locator_race(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                candidate = await _admit(admission, 84)
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=84)),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                reached_job_read = asyncio.Event()
                release_job_read = asyncio.Event()
                original_read = worker_adapter._read_job
                paused = False

                async def pause_after_real_locator(
                    connection: worker_adapter._Connection,
                    job_id: UUID,
                    *,
                    required: bool = False,
                ) -> worker_adapter._Job | None:
                    nonlocal paused
                    if not paused:
                        paused = True
                        reached_job_read.set()
                        await release_job_read.wait()
                    return await original_read(connection, job_id, required=required)

                with monkeypatch.context() as scoped:
                    scoped.setattr(worker_adapter, "_read_job", pause_after_real_locator)
                    actor = asyncio.create_task(
                        worker.claim_next(
                            worker_slot_id=claim.authority.worker_slot_id,
                            lease_duration=LeaseDuration(timedelta(seconds=30)),
                        )
                    )
                    try:
                        await asyncio.wait_for(reached_job_read.wait(), timeout=3)
                        assert isinstance(
                            await _finalizer(pool).finalize_success(
                                authority=claim.authority, result=_snapshot(claim)
                            ),
                            AnalysisJobCompleted,
                        )
                        # Privileged fixture corruption between locator and current-pair reads.
                        with _connect(database.inspection_settings) as connection:
                            if missing == "terminal":
                                connection.execute("SET session_replication_role = replica").close()
                                connection.execute(
                                    "DELETE FROM analysis_job_terminals WHERE job_id = %s",
                                    (candidate.job_id,),
                                ).close()
                            else:
                                connection.execute(
                                    "DELETE FROM analysis_callback_outbox WHERE job_id = %s",
                                    (candidate.job_id,),
                                ).close()
                        release_job_read.set()
                        with pytest.raises(AnalysisJobWorkerPersistenceInvariantError):
                            await asyncio.wait_for(actor, timeout=5)
                    finally:
                        release_job_read.set()
                        if not actor.done():
                            actor.cancel()
                        await asyncio.gather(actor, return_exceptions=True)

        _run_database(verify())
    assert factory.active_count == 0
