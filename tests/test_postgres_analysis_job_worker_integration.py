"""Opt-in real PostgreSQL evidence for inactive AnalysisJob worker ownership."""

from __future__ import annotations

import asyncio
import hashlib
import selectors
import sys
import threading
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast
from uuid import UUID

import conftest as disposable_postgres
import psycopg
import pytest
from psycopg import sql
from psycopg.errors import UniqueViolation
from yoyo import read_migrations  # type: ignore[import-untyped]

from adapters.psycopg_analysis_job_repository import PsycopgAnalysisJobAdmissionRepository
from adapters.psycopg_analysis_job_worker_repository import (
    PsycopgAnalysisJobWorkerRepository,
)
from adapters.psycopg_database import PsycopgDatabasePool, PsycopgPoolFactory
from adapters.yoyo_migration_backend import DatabaseMigrationError, YoyoMigrationBackend
from core.database_config import DatabaseSettings
from domain.analysis_job import AnalysisJobAccepted, NewAnalysisJob
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    ClaimableAnalysisJob,
    LeaseDuration,
    LeaseId,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceUnavailable,
    ClaimLost,
    NoJobAvailable,
    RecoverySummary,
)
from services.postgres_foundation import MigrationRunner

_OPT_IN = "--slice1-disposable-postgres"
_SKIP_REASON = (
    "Durable AnalysisJob disposable PostgreSQL evidence was not executed because the explicit "
    "task-scoped opt-in is absent."
)
_MIGRATION_SOURCE = "package:adapters:migrations/postgres"
_FOUNDATION_REVISION = "sprint2_slice1_foundation"
_ANALYSIS_JOB_REVISION = "sprint2_slice2_analysis_job"
_WORKER_ATTEMPT_REVISION = "sprint2_slice3_worker_attempt"
_RESULT_OUTBOX_REVISION = "sprint2_slice4_result_outbox"
_CATALOG = (
    _FOUNDATION_REVISION,
    _ANALYSIS_JOB_REVISION,
    _WORKER_ATTEMPT_REVISION,
    _RESULT_OUTBOX_REVISION,
)
_SLICE3_RELATIONS = frozenset({"public.analysis_jobs", "public.analysis_job_attempts"})
_CURRENT_RELATIONS = _SLICE3_RELATIONS | frozenset(
    {"public.analysis_job_terminals", "public.analysis_callback_outbox"}
)
_ACQUIRE_TIMEOUT_SECONDS = 5.0
_OPERATION_TIMEOUT_MS = 5_000
_LOCK_TIMEOUT_MS = 2_000
_ACTOR_TIMEOUT_SECONDS = 12.0
_LEASE_DURATION = LeaseDuration(timedelta(seconds=30))
_RECOVERY_BATCH = RecoveryBatchSize(10)
_SLOT_INDEX = "analysis_job_attempts_active_worker_slot_id_idx"


class _DisposablePostgresSession(Protocol):
    migration_settings: DatabaseSettings
    runtime_settings: DatabaseSettings

    @property
    def inspection_settings(self) -> DatabaseSettings: ...

    def application_relations(self) -> frozenset[str]: ...


class _DisposablePostgresFactory(Protocol):
    @property
    def active_count(self) -> int: ...

    def __call__(self) -> AbstractContextManager[_DisposablePostgresSession]: ...


class _LeaseIds:
    def __init__(self) -> None:
        self._next = 200_000

    def __call__(self) -> UUID:
        value = UUID(int=self._next)
        self._next += 1
        return value


class _FreshInsertConflictObservation:
    def __init__(self) -> None:
        self.error: UniqueViolation | None = None

    def observe(self, query: str, error: UniqueViolation) -> None:
        if (
            query.startswith("WITH mutation_clock AS MATERIALIZED")
            and "\nINSERT INTO analysis_job_attempts (" in query
            and error.diag.constraint_name == _SLOT_INDEX
        ):
            self.error = error


class _ConflictObservingConnection:
    def __init__(self, connection: Any, observation: _FreshInsertConflictObservation) -> None:
        self._connection = connection
        self._observation = observation

    def transaction(self) -> AbstractAsyncContextManager[object]:
        return cast(AbstractAsyncContextManager[object], self._connection.transaction())

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> Any:
        try:
            return await self._connection.execute(query, params)
        except UniqueViolation as error:
            self._observation.observe(query, error)
            raise


class _ConflictObservingConnectionContext(
    AbstractAsyncContextManager[_ConflictObservingConnection]
):
    def __init__(self, context: Any, observation: _FreshInsertConflictObservation) -> None:
        self._context = context
        self._observation = observation

    async def __aenter__(self) -> _ConflictObservingConnection:
        connection = await self._context.__aenter__()
        return _ConflictObservingConnection(connection, self._observation)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self._context.__aexit__(exc_type, exc, traceback))


class _ConflictObservingPool:
    def __init__(
        self,
        pool: PsycopgDatabasePool,
        observation: _FreshInsertConflictObservation,
    ) -> None:
        self._pool = pool
        self._observation = observation

    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_ConflictObservingConnection]:
        return _ConflictObservingConnectionContext(
            self._pool.connection(timeout_seconds=timeout_seconds),
            self._observation,
        )


class _BackendPidObservation:
    def __init__(self) -> None:
        self.value: int | None = None
        self.recorded = asyncio.Event()

    def observe(self, value: object) -> None:
        assert type(value) is int
        assert value > 0
        self.value = value
        self.recorded.set()


class _PidRecordingConnectionContext(AbstractAsyncContextManager[Any]):
    def __init__(self, context: Any, observation: _BackendPidObservation) -> None:
        self._context = context
        self._observation = observation

    async def __aenter__(self) -> Any:
        connection = await self._context.__aenter__()
        self._observation.observe(connection.info.backend_pid)
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self._context.__aexit__(exc_type, exc, traceback))


class _PidRecordingPool:
    def __init__(self, pool: PsycopgDatabasePool, observation: _BackendPidObservation) -> None:
        self._pool = pool
        self._observation = observation

    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[Any]:
        return _PidRecordingConnectionContext(
            self._pool.connection(timeout_seconds=timeout_seconds),
            self._observation,
        )


class _RecoveryPause:
    """Pause only after recovery has acquired both authoritative row locks."""

    def __init__(self) -> None:
        self.backend_pid: int | None = None
        self.recovery_job_locked = False
        self.recovery_attempt_locked = False
        self.locks_acquired = asyncio.Event()
        self.release = asyncio.Event()
        self._paused = False

    def observe_backend_pid(self, value: object) -> None:
        assert type(value) is int
        assert value > 0
        self.backend_pid = value

    def observe_completed(self, query: str) -> None:
        if "FOR UPDATE OF j SKIP LOCKED" in query:
            self.recovery_job_locked = True
        if "FROM analysis_job_attempts WHERE job_id = %s" in query and query.endswith(
            "FOR UPDATE SKIP LOCKED"
        ):
            self.recovery_attempt_locked = True

    async def before_execute(self, query: str) -> None:
        if (
            not self._paused
            and query.startswith("WITH mutation_clock AS MATERIALIZED")
            and "a.lease_expires_at <= m.t_m" in query
        ):
            assert self.recovery_job_locked is True
            assert self.recovery_attempt_locked is True
            self._paused = True
            self.locks_acquired.set()
            await self.release.wait()


class _ForwardingConnection:
    def __init__(self, connection: Any, pause: _RecoveryPause) -> None:
        self._connection = connection
        self._pause = pause

    def transaction(self) -> AbstractAsyncContextManager[object]:
        return cast(AbstractAsyncContextManager[object], self._connection.transaction())

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> Any:
        await self._pause.before_execute(query)
        result = await self._connection.execute(query, params)
        self._pause.observe_completed(query)
        return result


class _ForwardingConnectionContext(AbstractAsyncContextManager[_ForwardingConnection]):
    def __init__(self, context: Any, pause: _RecoveryPause) -> None:
        self._context = context
        self._pause = pause

    async def __aenter__(self) -> _ForwardingConnection:
        connection = await self._context.__aenter__()
        self._pause.observe_backend_pid(connection.info.backend_pid)
        return _ForwardingConnection(connection, self._pause)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self._context.__aexit__(exc_type, exc, traceback))


class _RecoveryPausePool:
    def __init__(self, pool: PsycopgDatabasePool, pause: _RecoveryPause) -> None:
        self._pool = pool
        self._pause = pause

    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_ForwardingConnection]:
        return _ForwardingConnectionContext(
            self._pool.connection(timeout_seconds=timeout_seconds),
            self._pause,
        )


def _disposable_factory(request: pytest.FixtureRequest) -> _DisposablePostgresFactory:
    if not request.config.getoption(_OPT_IN, default=False):
        pytest.skip(_SKIP_REASON)
    try:
        fixture = request.getfixturevalue("slice1_disposable_postgres_factory")
    except pytest.FixtureLookupError:
        pytest.fail(
            "Durable AnalysisJob opt-in requires the existing task-owned disposable PostgreSQL fixture.",
            pytrace=False,
        )
    return cast(_DisposablePostgresFactory, fixture)


def _run_database[T](operation: Coroutine[object, object, T]) -> T:
    return asyncio.run(
        operation,
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def _connect(
    settings: DatabaseSettings,
    *,
    autocommit: bool = True,
) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=settings._host,
        port=settings._port,
        dbname=settings._database,
        user=settings._user,
        password=settings._password,
        connect_timeout=settings.connect_timeout_seconds,
        autocommit=autocommit,
    )


def _runner(settings: DatabaseSettings) -> MigrationRunner:
    return MigrationRunner(YoyoMigrationBackend(settings, (_MIGRATION_SOURCE,)))


def _catalog() -> tuple[str, ...]:
    return tuple(str(migration.id) for migration in read_migrations(_MIGRATION_SOURCE))


def _current_settings(settings: DatabaseSettings) -> DatabaseSettings:
    return replace(settings, required_schema_version=_RESULT_OUTBOX_REVISION)


def _prepare_database(database: _DisposablePostgresSession) -> MigrationRunner:
    runner = _runner(database.migration_settings)
    assert runner.current_version() is None
    assert database.application_relations() == frozenset()
    assert _catalog() == _CATALOG
    runner.apply_pending()
    assert runner.current_version() == _RESULT_OUTBOX_REVISION
    assert database.application_relations() == _CURRENT_RELATIONS
    return runner


def _prepare_slice3_database(database: _DisposablePostgresSession) -> MigrationRunner:
    runner = _prepare_database(database)
    runner.rollback_last()
    assert runner.current_version() == _WORKER_ATTEMPT_REVISION
    assert database.application_relations() == _SLICE3_RELATIONS
    return runner


def _grant_runtime_worker_privileges(
    migration_settings: DatabaseSettings,
    runtime_settings: DatabaseSettings,
) -> None:
    with _connect(migration_settings) as connection:
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE analysis_jobs TO {}").format(
                sql.Identifier(runtime_settings._user)
            )
        ).close()
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE analysis_job_attempts TO {}").format(
                sql.Identifier(runtime_settings._user)
            )
        ).close()
        for relation in ("analysis_job_terminals", "analysis_callback_outbox"):
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT ON TABLE {} TO {}").format(
                    sql.Identifier(relation), sql.Identifier(runtime_settings._user)
                )
            ).close()
    with _connect(runtime_settings) as connection:
        row = connection.execute(
            """
            SELECT
                has_table_privilege(current_user, 'analysis_jobs', 'SELECT'),
                has_table_privilege(current_user, 'analysis_jobs', 'INSERT'),
                has_table_privilege(current_user, 'analysis_jobs', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_jobs', 'DELETE'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'SELECT'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'INSERT'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'DELETE'),
                has_schema_privilege(current_user, 'public', 'CREATE'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'SELECT'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'INSERT'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'DELETE'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'SELECT'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'INSERT'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'DELETE')
            """
        ).fetchone()
    assert row == (
        True,
        True,
        True,
        False,
        True,
        True,
        True,
        False,
        False,
        True,
        True,
        False,
        False,
        True,
        True,
        False,
        False,
    )


def _candidate(index: int, *, attempt_limit: int = 3) -> NewAnalysisJob:
    marker = f"worker-integration-{index}"
    return NewAnalysisJob(
        job_id=UUID(int=300_000 + index),
        idempotency_lookup=hashlib.sha256(f"lookup:{marker}".encode()).digest(),
        request_fingerprint=hashlib.sha256(f"fingerprint:{marker}".encode()).digest(),
        video_id=f"video-{marker}",
        player_id=f"player-{marker}",
        video_reference=f"match-{marker}.mp4",
        callback_url=f"https://callback.invalid/{marker}",
        analysis_version="1.0.0",
        attempt_limit=attempt_limit,
    )


@asynccontextmanager
async def _repositories(
    settings: DatabaseSettings,
) -> AsyncIterator[
    tuple[
        PsycopgAnalysisJobAdmissionRepository,
        PsycopgAnalysisJobWorkerRepository,
        PsycopgDatabasePool,
    ]
]:
    pool = PsycopgPoolFactory()(settings)
    if not isinstance(pool, PsycopgDatabasePool):
        pytest.fail("The production pool factory returned an unexpected adapter.", pytrace=False)
    await pool.open(timeout_seconds=settings.pool.open_timeout_seconds)
    try:
        yield (
            PsycopgAnalysisJobAdmissionRepository(
                pool,
                connection_acquire_timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS,
                operation_timeout_ms=_OPERATION_TIMEOUT_MS,
                admission_lock_timeout_ms=_LOCK_TIMEOUT_MS,
            ),
            PsycopgAnalysisJobWorkerRepository(
                pool=pool,
                connection_acquire_timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS,
                operation_timeout_ms=_OPERATION_TIMEOUT_MS,
                lock_timeout_ms=_LOCK_TIMEOUT_MS,
                lease_id_factory=_LeaseIds(),
            ),
            pool,
        )
    finally:
        await pool.close(timeout_seconds=settings.pool.close_timeout_seconds)


def _worker_repository(pool: Any) -> PsycopgAnalysisJobWorkerRepository:
    return PsycopgAnalysisJobWorkerRepository(
        pool=pool,
        connection_acquire_timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS,
        operation_timeout_ms=_OPERATION_TIMEOUT_MS,
        lock_timeout_ms=_LOCK_TIMEOUT_MS,
        lease_id_factory=_LeaseIds(),
    )


async def _admit(
    repository: PsycopgAnalysisJobAdmissionRepository,
    index: int,
    *,
    attempt_limit: int = 3,
) -> NewAnalysisJob:
    candidate = _candidate(index, attempt_limit=attempt_limit)
    outcome = await repository.accept_or_get(candidate, max_queue_size=20)
    assert isinstance(outcome, AnalysisJobAccepted)
    assert outcome.job.analysis_version == candidate.analysis_version
    assert outcome.job.attempt_limit == candidate.attempt_limit
    return candidate


async def _fetch_all(
    pool: PsycopgDatabasePool,
    query: str,
    params: tuple[object, ...] | None = None,
) -> list[tuple[object, ...]]:
    async with pool.connection(timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS) as connection:
        cursor = await connection.execute(query, params)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()


async def _durable_snapshot(pool: PsycopgDatabasePool) -> tuple[tuple[object, ...], ...]:
    rows = await _fetch_all(
        pool,
        """
        SELECT
            j.job_id,
            j.state,
            j.analysis_version,
            j.attempt_limit,
            j.attempt_count,
            a.attempt_number,
            a.worker_slot_id,
            a.lease_id,
            a.status,
            a.started_at,
            a.heartbeat_at,
            a.lease_expires_at,
            a.finished_at,
            a.safe_failure_code
        FROM analysis_jobs AS j
        LEFT JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
        ORDER BY j.job_id, a.attempt_number
        """,
    )
    return tuple(rows)


async def _claims_after_barrier(
    repository: PsycopgAnalysisJobWorkerRepository,
    slots: tuple[WorkerSlotId, ...],
    *,
    return_exceptions: bool = False,
) -> list[object]:
    return await _claims_from_repositories_after_barrier(
        tuple(repository for _ in slots),
        slots,
        return_exceptions=return_exceptions,
    )


async def _claims_from_repositories_after_barrier(
    repositories: tuple[PsycopgAnalysisJobWorkerRepository, ...],
    slots: tuple[WorkerSlotId, ...],
    *,
    return_exceptions: bool = False,
) -> list[object]:
    assert len(repositories) == len(slots)
    barrier = asyncio.Barrier(len(slots))

    async def claim(
        repository: PsycopgAnalysisJobWorkerRepository,
        slot: WorkerSlotId,
    ) -> object:
        await barrier.wait()
        return await repository.claim_next(
            worker_slot_id=slot,
            lease_duration=_LEASE_DURATION,
        )

    tasks = tuple(
        asyncio.create_task(claim(repository, slot))
        for repository, slot in zip(repositories, slots, strict=True)
    )
    try:
        return list(
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=return_exceptions),
                timeout=_ACTOR_TIMEOUT_SECONDS,
            )
        )
    finally:
        await barrier.abort()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _assert_slot_conflict(
    error: BaseException,
    observation: _FreshInsertConflictObservation,
) -> None:
    assert type(error) is AnalysisJobWorkerPersistenceUnavailable
    assert observation.error is not None
    assert observation.error.diag.constraint_name == _SLOT_INDEX


def _assert_same_slot_claims_equal(first: WorkerClaim, second: WorkerClaim) -> None:
    assert first == second


def _assert_blocking_identity(
    waiting_pid: int,
    blocker_pids: tuple[int, ...],
    *,
    expected_waiting_pid: int,
    expected_blocker_pid: int,
) -> None:
    assert waiting_pid == expected_waiting_pid
    assert expected_blocker_pid in blocker_pids
    assert waiting_pid not in blocker_pids


def _force_expired(settings: DatabaseSettings, job_id: UUID, attempt_number: int) -> None:
    with _connect(settings) as connection:
        cursor = connection.execute(
            """
            UPDATE analysis_job_attempts
            SET
                started_at = clock_timestamp() - interval '3 seconds',
                heartbeat_at = clock_timestamp() - interval '2 seconds',
                lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE job_id = %s AND attempt_number = %s AND status = 'ACTIVE'
            """,
            (job_id, attempt_number),
        )
        assert cursor.rowcount == 1
        cursor.close()


def _wait_for_blocked_old_owner(
    settings: DatabaseSettings,
    *,
    expected_waiting_pid: int,
    expected_blocker_pid: int,
) -> tuple[int, tuple[int, ...]]:
    deadline = time.monotonic() + _ACTOR_TIMEOUT_SECONDS
    with _connect(settings) as connection:
        while time.monotonic() < deadline:
            row = connection.execute(
                """
                SELECT activity.pid, pg_blocking_pids(activity.pid)
                FROM pg_catalog.pg_stat_activity AS activity
                WHERE activity.datname = current_database()
                  AND activity.wait_event_type = 'Lock'
                  AND cardinality(pg_blocking_pids(activity.pid)) > 0
                  AND activity.pid = %s
                  AND activity.query LIKE %s
                  AND activity.query LIKE %s
                ORDER BY activity.pid
                LIMIT 1
                """,
                (
                    expected_waiting_pid,
                    "SELECT job_id, video_id%",
                    "%FROM analysis_jobs WHERE job_id%FOR UPDATE%",
                ),
            ).fetchone()
            if row is not None:
                pid, blockers = row
                assert type(pid) is int
                assert isinstance(blockers, list)
                assert blockers and all(type(item) is int for item in blockers)
                blocker_pids = tuple(blockers)
                _assert_blocking_identity(
                    pid,
                    blocker_pids,
                    expected_waiting_pid=expected_waiting_pid,
                    expected_blocker_pid=expected_blocker_pid,
                )
                return pid, blocker_pids
            time.sleep(0.01)
    raise AssertionError("Old-owner mutation was not observed waiting on a PostgreSQL lock.")


def _wait_for_downgrade_lock(
    settings: DatabaseSettings,
    relation_name: str,
    writer_pid: int,
) -> tuple[int, tuple[int, ...]]:
    deadline = time.monotonic() + _ACTOR_TIMEOUT_SECONDS
    with _connect(settings) as connection:
        while time.monotonic() < deadline:
            row = connection.execute(
                """
                SELECT waiting.pid, pg_blocking_pids(waiting.pid)
                FROM pg_catalog.pg_locks AS lock_row
                JOIN pg_catalog.pg_class AS relation ON relation.oid = lock_row.relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_stat_activity AS waiting ON waiting.pid = lock_row.pid
                WHERE lock_row.database = (
                        SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()
                    )
                  AND namespace.nspname = 'public'
                  AND relation.relname = %s
                  AND lock_row.mode = 'AccessExclusiveLock'
                  AND lock_row.granted IS FALSE
                  AND cardinality(pg_blocking_pids(waiting.pid)) > 0
                  AND waiting.query LIKE %s
                ORDER BY waiting.pid
                LIMIT 1
                """,
                (relation_name, "%LOCK TABLE analysis_jobs, analysis_job_attempts%"),
            ).fetchone()
            if row is not None:
                pid, blockers = row
                assert type(pid) is int
                assert isinstance(blockers, list)
                assert blockers and all(type(item) is int for item in blockers)
                assert writer_pid in blockers
                return pid, tuple(blockers)
            time.sleep(0.01)
    raise AssertionError(f"Slice 3 rollback was not observed waiting on {relation_name!r}.")


def _schema_signature(settings: DatabaseSettings) -> bytes:
    with _connect(settings) as connection:
        columns = connection.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN ('analysis_jobs', 'analysis_job_attempts')
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
        constraints = connection.execute(
            """
            SELECT relation.relname, constraint_row.contype,
                   pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
            FROM pg_catalog.pg_constraint AS constraint_row
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_row.conrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname IN ('analysis_jobs', 'analysis_job_attempts')
            ORDER BY relation.relname, constraint_row.contype,
                     pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
            """
        ).fetchall()
        indexes = connection.execute(
            """
            SELECT tablename, indexdef
            FROM pg_catalog.pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('analysis_jobs', 'analysis_job_attempts')
            ORDER BY tablename, indexname
            """
        ).fetchall()
    return hashlib.sha256(repr((columns, constraints, indexes)).encode()).digest()


def _assert_persisted_policy(
    rows: tuple[tuple[object, ...], ...],
    *,
    analysis_version: str,
    attempt_limit: int,
    attempt_count: int,
) -> None:
    assert rows
    assert {(row[2], row[3], row[4]) for row in rows} == {
        (analysis_version, attempt_limit, attempt_count)
    }


class _RepresentativeBaseFailure(BaseException):
    pass


class _FatalCleanupFailure(BaseException):
    pass


def _disposable_context_with_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    cleanup: BaseException,
) -> disposable_postgres._DisposablePostgresContext:
    resource = disposable_postgres._OwnedResource(
        task_id="cleanup-characterization",
        name="cleanup-characterization",
        password_file=Path("unused-and-never-accessed.secret"),
    )
    factory = disposable_postgres._DisposablePostgresFactory("unused-image")
    context = disposable_postgres._DisposablePostgresContext(factory)
    context._resource = resource

    def fail_cleanup(_: object) -> None:
        raise cleanup

    monkeypatch.setattr(factory, "_cleanup", fail_cleanup)
    return context


def _exit_with_active_primary(
    context: disposable_postgres._DisposablePostgresContext,
    primary: BaseException,
) -> None:
    try:
        raise primary
    except BaseException as active:
        context.__exit__(type(active), active, active.__traceback__)


class _CleanupRecordingWriter:
    def __init__(
        self,
        actions: list[str],
        *,
        commit_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._actions = actions
        self._commit_error = commit_error
        self._close_error = close_error

    def commit(self) -> None:
        self._actions.append("commit")
        if self._commit_error is not None:
            raise self._commit_error

    def close(self) -> None:
        self._actions.append("close")
        if self._close_error is not None:
            raise self._close_error


class _CleanupRecordingActor:
    def __init__(
        self,
        actions: list[str],
        *,
        join_error: BaseException | None = None,
        alive: bool = False,
    ) -> None:
        self._actions = actions
        self._join_error = join_error
        self._alive = alive

    def join(self, timeout: float | None = None) -> None:
        assert timeout == _ACTOR_TIMEOUT_SECONDS
        self._actions.append("join")
        if self._join_error is not None:
            raise self._join_error

    def is_alive(self) -> bool:
        return self._alive


def _release_writer_and_join_actor(
    writer: Any,
    actor: Any,
    *,
    writer_released: bool,
) -> None:
    primary = sys.exception()
    cleanup_errors: list[BaseException] = []
    if not writer_released:
        try:
            writer.commit()
        except BaseException as error:
            cleanup_errors.append(error)
    try:
        writer.close()
    except BaseException as error:
        cleanup_errors.append(error)
    try:
        actor.join(timeout=_ACTOR_TIMEOUT_SECONDS)
    except BaseException as error:
        cleanup_errors.append(error)
    try:
        assert actor.is_alive() is False, "Rollback actor remained alive after bounded join."
    except BaseException as error:
        cleanup_errors.append(error)
    if cleanup_errors:
        # Nested finally made the last fatal cleanup step win over earlier fatals.
        fatal_index = next(
            (
                index
                for index in range(len(cleanup_errors) - 1, -1, -1)
                if not isinstance(cleanup_errors[index], Exception)
            ),
            None,
        )
        if fatal_index is not None:
            fatal = cleanup_errors[fatal_index]
            related = (
                ([primary] if primary is not None else [])
                + cleanup_errors[:fatal_index]
                + cleanup_errors[fatal_index + 1 :]
            )
            if len(related) == 1:
                raise fatal from related[0]
            if related:
                raise fatal from BaseExceptionGroup("Downgrade cleanup failures.", related)
            raise fatal
        cleanup_error = (
            cleanup_errors[0]
            if len(cleanup_errors) == 1
            else BaseExceptionGroup("Downgrade cleanup failures.", cleanup_errors)
        )
        if primary is not None:
            raise primary from cleanup_error
        raise cleanup_error


@pytest.mark.parametrize(
    "primary",
    (
        AssertionError("primary-assertion-failure"),
        asyncio.CancelledError("primary-cancellation"),
        _RepresentativeBaseFailure("primary-base-failure"),
    ),
    ids=("assertion", "cancellation", "base-exception"),
)
def test_disposable_context_preserves_primary_failure_and_cleanup_cause(
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException,
) -> None:
    cleanup = RuntimeError("cleanup-failure")
    context = _disposable_context_with_cleanup_failure(monkeypatch, cleanup)

    with pytest.raises(type(primary)) as captured:
        _exit_with_active_primary(context, primary)

    assert captured.value is primary
    assert captured.value.__cause__ is cleanup
    assert captured.value.__context__ is cleanup


def test_disposable_context_preserves_fatal_cleanup_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = AssertionError("primary-assertion-failure")
    cleanup = _FatalCleanupFailure("fatal-cleanup-failure")
    context = _disposable_context_with_cleanup_failure(monkeypatch, cleanup)

    with pytest.raises(_FatalCleanupFailure) as captured:
        _exit_with_active_primary(context, primary)

    assert captured.value is cleanup
    assert captured.value.__cause__ is primary
    assert captured.value.__context__ is primary


def test_disposable_context_exposes_cleanup_failure_without_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = RuntimeError("cleanup-failure")
    context = _disposable_context_with_cleanup_failure(monkeypatch, cleanup)

    with pytest.raises(RuntimeError) as captured:
        context.__exit__(None, None, None)

    assert captured.value is cleanup
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_slot_conflict_assertion_accepts_only_own_recognized_insert_violation() -> None:
    diagnostic = UniqueViolation("driver-marker", info={ord("n"): _SLOT_INDEX.encode()})
    observation = _FreshInsertConflictObservation()
    query = (
        "WITH mutation_clock AS MATERIALIZED (SELECT clock_timestamp() AS t_m)\n"
        "INSERT INTO analysis_job_attempts (job_id) SELECT %s"
    )
    params = (UUID(int=1),)
    calls: list[tuple[str, tuple[object, ...] | None]] = []

    class _RaisingConnection:
        async def execute(
            self,
            supplied_query: str,
            supplied_params: tuple[object, ...] | None = None,
        ) -> None:
            calls.append((supplied_query, supplied_params))
            raise diagnostic

    async def exercise() -> None:
        connection = _ConflictObservingConnection(_RaisingConnection(), observation)
        with pytest.raises(UniqueViolation) as captured:
            await connection.execute(query, params)
        assert captured.value is diagnostic

    asyncio.run(exercise())
    error = AnalysisJobWorkerPersistenceUnavailable("public-marker")

    _assert_slot_conflict(error, observation)

    assert observation.error is diagnostic
    assert calls == [(query, params)]


@pytest.mark.parametrize("variant", ("unobserved", "unrelated-query", "wrong-constraint"))
def test_slot_conflict_assertion_rejects_unrecognized_unavailable(variant: str) -> None:
    observation = _FreshInsertConflictObservation()
    if variant != "unobserved":
        constraint = _SLOT_INDEX if variant == "unrelated-query" else "other_constraint"
        diagnostic = UniqueViolation("driver-marker", info={ord("n"): constraint.encode()})
        query = (
            "SELECT 1"
            if variant == "unrelated-query"
            else "WITH mutation_clock AS MATERIALIZED (SELECT clock_timestamp() AS t_m)\n"
            "INSERT INTO analysis_job_attempts (job_id) SELECT %s"
        )
        observation.observe(query, diagnostic)

    with pytest.raises(AssertionError):
        _assert_slot_conflict(
            AnalysisJobWorkerPersistenceUnavailable("unrelated-unavailable"),
            observation,
        )


def test_same_slot_claim_assertion_requires_complete_persisted_claim() -> None:
    candidate = _candidate(900)
    instant = datetime(2026, 10, 12, tzinfo=UTC)
    job = ClaimableAnalysisJob(
        job_id=candidate.job_id,
        video_id=candidate.video_id,
        player_id=candidate.player_id,
        video_reference=candidate.video_reference,
        callback_url=candidate.callback_url,
        analysis_version=candidate.analysis_version,
        attempt_limit=candidate.attempt_limit,
        accepted_at=instant,
    )
    authority = AttemptAuthority(
        job_id=job.job_id,
        attempt_number=1,
        worker_slot_id=WorkerSlotId(UUID(int=901)),
        lease_id=LeaseId(UUID(int=902)),
    )
    claim = WorkerClaim(
        authority=authority,
        job=job,
        started_at=instant,
        heartbeat_at=instant,
        lease_expires_at=instant + _LEASE_DURATION.value,
    )
    _assert_same_slot_claims_equal(claim, replace(claim))
    for changed in (
        replace(claim, job=replace(job, analysis_version="2.0.0")),
        replace(claim, job=replace(job, attempt_limit=4)),
        replace(claim, lease_expires_at=claim.lease_expires_at + timedelta(seconds=1)),
    ):
        assert changed.authority == claim.authority
        with pytest.raises(AssertionError):
            _assert_same_slot_claims_equal(claim, changed)


def test_blocking_identity_rejects_wrong_waiter_and_wrong_blocker() -> None:
    _assert_blocking_identity(
        101,
        (202,),
        expected_waiting_pid=101,
        expected_blocker_pid=202,
    )
    with pytest.raises(AssertionError):
        _assert_blocking_identity(
            999,
            (202,),
            expected_waiting_pid=101,
            expected_blocker_pid=202,
        )
    with pytest.raises(AssertionError):
        _assert_blocking_identity(
            101,
            (999,),
            expected_waiting_pid=101,
            expected_blocker_pid=202,
        )


@pytest.mark.parametrize("failure_boundary", ("commit", "close", "join"))
def test_downgrade_cleanup_attempts_close_and_join_after_failures(
    failure_boundary: str,
) -> None:
    actions: list[str] = []
    failure = RuntimeError(f"{failure_boundary}-failure")
    writer = _CleanupRecordingWriter(
        actions,
        commit_error=failure if failure_boundary == "commit" else None,
        close_error=failure if failure_boundary == "close" else None,
    )
    actor = _CleanupRecordingActor(
        actions,
        join_error=failure if failure_boundary == "join" else None,
    )

    with pytest.raises(RuntimeError) as captured:
        _release_writer_and_join_actor(writer, actor, writer_released=False)

    assert captured.value is failure
    assert actions == ["commit", "close", "join"]


@pytest.mark.parametrize("failure_boundary", (None, "commit", "close", "join"))
def test_downgrade_cleanup_preserves_primary_and_reports_live_actor(
    failure_boundary: str | None,
) -> None:
    actions: list[str] = []
    primary = AssertionError("primary-test-failure")
    failure = RuntimeError(f"{failure_boundary}-failure")
    writer = _CleanupRecordingWriter(
        actions,
        commit_error=failure if failure_boundary == "commit" else None,
        close_error=failure if failure_boundary == "close" else None,
    )
    actor = _CleanupRecordingActor(
        actions,
        join_error=failure if failure_boundary == "join" else None,
        alive=True,
    )

    with pytest.raises(AssertionError) as captured:
        try:
            raise primary
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=False)

    assert captured.value is primary
    assert actions == ["commit", "close", "join"]
    cleanup = captured.value.__cause__
    if failure_boundary is None:
        assert isinstance(cleanup, AssertionError)
        assert "remained alive" in str(cleanup)
    else:
        assert isinstance(cleanup, BaseExceptionGroup)
        assert cleanup.exceptions[0] is failure
        assert isinstance(cleanup.exceptions[1], AssertionError)
        assert "remained alive" in str(cleanup.exceptions[1])


def test_downgrade_cleanup_preserves_primary_when_actor_finished() -> None:
    actions: list[str] = []
    primary = AssertionError("primary-test-failure")
    writer = _CleanupRecordingWriter(actions)
    actor = _CleanupRecordingActor(actions)

    with pytest.raises(AssertionError) as captured:
        try:
            raise primary
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=False)

    assert captured.value is primary
    assert captured.value.__cause__ is None
    assert actions == ["commit", "close", "join"]


def test_downgrade_cleanup_fatal_commit_retains_primary_and_other_failures() -> None:
    actions: list[str] = []
    primary = AssertionError("primary-test-failure")
    fatal = KeyboardInterrupt("fatal-commit")
    close_failure = RuntimeError("close-failure")
    join_failure = RuntimeError("join-failure")
    writer = _CleanupRecordingWriter(
        actions,
        commit_error=fatal,
        close_error=close_failure,
    )

    class _LivenessRecordingActor(_CleanupRecordingActor):
        def is_alive(self) -> bool:
            actions.append("is_alive")
            return super().is_alive()

    actor = _LivenessRecordingActor(actions, join_error=join_failure, alive=True)

    with pytest.raises(KeyboardInterrupt) as captured:
        try:
            raise primary
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=False)

    assert captured.value is fatal
    assert actions == ["commit", "close", "join", "is_alive"]
    related = captured.value.__cause__
    assert isinstance(related, BaseExceptionGroup)
    assert related.exceptions[:3] == (primary, close_failure, join_failure)
    assert isinstance(related.exceptions[3], AssertionError)
    assert "remained alive" in str(related.exceptions[3])


def test_downgrade_cleanup_later_fatal_retains_sequential_precedence() -> None:
    actions: list[str] = []
    primary = AssertionError("primary-test-failure")
    first_fatal = KeyboardInterrupt("fatal-commit")
    later_fatal = SystemExit("fatal-join")
    writer = _CleanupRecordingWriter(actions, commit_error=first_fatal)
    actor = _CleanupRecordingActor(actions, join_error=later_fatal)

    with pytest.raises(SystemExit) as captured:
        try:
            raise primary
        finally:
            _release_writer_and_join_actor(writer, actor, writer_released=False)

    assert captured.value is later_fatal
    assert actions == ["commit", "close", "join"]
    related = captured.value.__cause__
    assert isinstance(related, BaseExceptionGroup)
    assert related.exceptions == (primary, first_fatal)


def test_persisted_policy_assertion_rejects_changed_policy_or_count() -> None:
    expected = ((UUID(int=1), "RUNNING", "1.0.0", 3, 2),)
    _assert_persisted_policy(
        expected,
        analysis_version="1.0.0",
        attempt_limit=3,
        attempt_count=2,
    )
    for changed in (
        ((UUID(int=1), "RUNNING", "2.0.0", 3, 2),),
        ((UUID(int=1), "RUNNING", "1.0.0", 4, 2),),
        ((UUID(int=1), "RUNNING", "1.0.0", 3, 1),),
    ):
        with pytest.raises(AssertionError):
            _assert_persisted_policy(
                changed,
                analysis_version="1.0.0",
                attempt_limit=3,
                attempt_count=2,
            )


@pytest.mark.integration
def test_two_slots_concurrently_claim_two_distinct_jobs(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 1)
                await _admit(admission, 2)
                slots = (WorkerSlotId(UUID(int=1_001)), WorkerSlotId(UUID(int=1_002)))
                outcomes = await _claims_after_barrier(worker, slots)
                assert all(isinstance(outcome, WorkerClaim) for outcome in outcomes)
                claims = cast(list[WorkerClaim], outcomes)
                assert {claim.authority.worker_slot_id for claim in claims} == set(slots)
                assert len({claim.authority.job_id for claim in claims}) == 2
                assert len({claim.authority.lease_id for claim in claims}) == 2
                assert {claim.authority.attempt_number for claim in claims} == {1}

                rows = await _fetch_all(
                    pool,
                    """
                    SELECT job_id, attempt_number, worker_slot_id, lease_id, status
                    FROM analysis_job_attempts
                    ORDER BY job_id
                    """,
                )
                assert {
                    (
                        claim.authority.job_id,
                        claim.authority.attempt_number,
                        claim.authority.worker_slot_id.value,
                        claim.authority.lease_id.value,
                        "ACTIVE",
                    )
                    for claim in claims
                } == set(rows)
                cardinality = await _fetch_all(
                    pool,
                    """
                    SELECT count(*), count(DISTINCT job_id), count(DISTINCT worker_slot_id)
                    FROM analysis_job_attempts WHERE status = 'ACTIVE'
                    """,
                )
                assert cardinality == [(2, 2, 2)]

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_competing_slots_for_one_job_create_one_active_authority(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 10)
                outcomes = await _claims_after_barrier(
                    worker,
                    (WorkerSlotId(UUID(int=1_010)), WorkerSlotId(UUID(int=1_011))),
                )
                claims = [outcome for outcome in outcomes if isinstance(outcome, WorkerClaim)]
                empty = [outcome for outcome in outcomes if isinstance(outcome, NoJobAvailable)]
                assert len(claims) == 1
                assert len(empty) == 1
                claim = claims[0]
                rows = await _fetch_all(
                    pool,
                    """
                    SELECT job_id, attempt_number, worker_slot_id, lease_id, status
                    FROM analysis_job_attempts
                    """,
                )
                assert rows == [
                    (
                        claim.authority.job_id,
                        1,
                        claim.authority.worker_slot_id.value,
                        claim.authority.lease_id.value,
                        "ACTIVE",
                    )
                ]

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_competing_claims_for_one_slot_preserve_single_active_attempt(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 20)
                await _admit(admission, 21)
                slot = WorkerSlotId(UUID(int=1_020))
                observations = (
                    _FreshInsertConflictObservation(),
                    _FreshInsertConflictObservation(),
                )
                workers = tuple(
                    _worker_repository(cast(Any, _ConflictObservingPool(pool, observation)))
                    for observation in observations
                )
                outcomes = await _claims_from_repositories_after_barrier(
                    workers,
                    (slot, slot),
                    return_exceptions=True,
                )
                claims = [outcome for outcome in outcomes if isinstance(outcome, WorkerClaim)]
                errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
                assert len(claims) in (1, 2)
                assert len(claims) + len(errors) == 2
                if len(claims) == 2:
                    _assert_same_slot_claims_equal(claims[0], claims[1])
                else:
                    assert len(errors) == 1
                    for outcome, observation in zip(outcomes, observations, strict=True):
                        if isinstance(outcome, BaseException):
                            _assert_slot_conflict(outcome, observation)

                rows = await _fetch_all(
                    pool,
                    """
                    SELECT job_id, attempt_number, worker_slot_id, lease_id, status
                    FROM analysis_job_attempts
                    """,
                )
                claim = claims[0]
                assert rows == [
                    (
                        claim.authority.job_id,
                        1,
                        slot.value,
                        claim.authority.lease_id.value,
                        "ACTIVE",
                    )
                ]
                jobs = await _fetch_all(
                    pool,
                    "SELECT state, attempt_count FROM analysis_jobs ORDER BY state, job_id",
                )
                assert jobs == [("QUEUED", 0), ("RUNNING", 1)]

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_expired_claim_recovers_reclaims_and_rejects_old_authority(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 30, attempt_limit=3)
                first = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=1_030)),
                    lease_duration=_LEASE_DURATION,
                )
                assert isinstance(first, WorkerClaim)
                assert first.job.analysis_version == "1.0.0"
                assert first.job.attempt_limit == 3
                await asyncio.to_thread(
                    _force_expired,
                    database.migration_settings,
                    first.authority.job_id,
                    first.authority.attempt_number,
                )
                recovered = await worker.recover_expired(batch_size=_RECOVERY_BATCH)
                assert recovered == RecoverySummary(requeued_count=1, exhausted_count=0)

                second = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=1_031)),
                    lease_duration=_LEASE_DURATION,
                )
                assert isinstance(second, WorkerClaim)
                assert second.authority.job_id == first.authority.job_id
                assert second.authority.attempt_number == 2
                assert second.authority.worker_slot_id != first.authority.worker_slot_id
                assert second.authority.lease_id != first.authority.lease_id
                assert second.job.analysis_version == "1.0.0"
                assert second.job.attempt_limit == 3

                before = await _durable_snapshot(pool)
                _assert_persisted_policy(
                    before,
                    analysis_version="1.0.0",
                    attempt_limit=3,
                    attempt_count=2,
                )
                outcomes = (
                    await worker.renew_lease(
                        authority=first.authority,
                        lease_duration=_LEASE_DURATION,
                    ),
                    await worker.finalize_retryable_failure(
                        authority=first.authority,
                        failure_code=AttemptFailureCode("transient_io"),
                    ),
                    await worker.finalize_terminal_failure(
                        authority=first.authority,
                        failure_code=AttemptFailureCode("invalid_input"),
                    ),
                )
                assert all(isinstance(outcome, ClaimLost) for outcome in outcomes)
                assert all(outcome.authority == first.authority for outcome in outcomes)
                assert await _durable_snapshot(pool) == before
                assert [(row[5], row[8]) for row in before] == [
                    (1, "INTERRUPTED"),
                    (2, "ACTIVE"),
                ]

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_recovery_holds_both_locks_then_old_owner_loses_before_reclaim(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        settings = _current_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                await _admit(admission, 40, attempt_limit=3)
                first = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=1_040)),
                    lease_duration=_LEASE_DURATION,
                )
                assert isinstance(first, WorkerClaim)
                await asyncio.to_thread(
                    _force_expired,
                    database.migration_settings,
                    first.authority.job_id,
                    first.authority.attempt_number,
                )

                pause = _RecoveryPause()
                recovery = PsycopgAnalysisJobWorkerRepository(
                    pool=cast(Any, _RecoveryPausePool(pool, pause)),
                    connection_acquire_timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS,
                    operation_timeout_ms=_OPERATION_TIMEOUT_MS,
                    lock_timeout_ms=_LOCK_TIMEOUT_MS,
                    lease_id_factory=_LeaseIds(),
                )
                recovery_task = asyncio.create_task(
                    recovery.recover_expired(batch_size=_RECOVERY_BATCH)
                )
                old_owner_pid = _BackendPidObservation()
                old_owner_repository = _worker_repository(
                    cast(Any, _PidRecordingPool(pool, old_owner_pid))
                )
                old_owner_task: asyncio.Task[object] | None = None
                try:
                    await asyncio.wait_for(
                        pause.locks_acquired.wait(), timeout=_ACTOR_TIMEOUT_SECONDS
                    )
                    assert pause.recovery_job_locked is True
                    assert pause.recovery_attempt_locked is True
                    assert pause.backend_pid is not None
                    old_owner_task = asyncio.create_task(
                        old_owner_repository.renew_lease(
                            authority=first.authority,
                            lease_duration=_LEASE_DURATION,
                        )
                    )
                    await asyncio.wait_for(
                        old_owner_pid.recorded.wait(), timeout=_ACTOR_TIMEOUT_SECONDS
                    )
                    assert old_owner_pid.value is not None
                    blocked_pid, blocker_pids = await asyncio.to_thread(
                        _wait_for_blocked_old_owner,
                        database.inspection_settings,
                        expected_waiting_pid=old_owner_pid.value,
                        expected_blocker_pid=pause.backend_pid,
                    )
                    _assert_blocking_identity(
                        blocked_pid,
                        blocker_pids,
                        expected_waiting_pid=old_owner_pid.value,
                        expected_blocker_pid=pause.backend_pid,
                    )
                    pause.release.set()
                    recovered, old_owner = await asyncio.wait_for(
                        asyncio.gather(recovery_task, old_owner_task),
                        timeout=_ACTOR_TIMEOUT_SECONDS,
                    )
                finally:
                    pause.release.set()
                    tasks = tuple(
                        task for task in (recovery_task, old_owner_task) if task is not None
                    )
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

                assert recovered == RecoverySummary(requeued_count=1, exhausted_count=0)
                assert isinstance(old_owner, ClaimLost)
                resolved = await _durable_snapshot(pool)
                assert len(resolved) == 1
                assert resolved[0][1] == "QUEUED"
                assert resolved[0][5] == 1
                assert resolved[0][8] == "INTERRUPTED"
                assert resolved[0][11] is None

                second = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(UUID(int=1_041)),
                    lease_duration=_LEASE_DURATION,
                )
                assert isinstance(second, WorkerClaim)
                assert second.authority.attempt_number == 2
                before = await _durable_snapshot(pool)
                stale = await worker.finalize_terminal_failure(
                    authority=first.authority,
                    failure_code=AttemptFailureCode("invalid_input"),
                )
                assert isinstance(stale, ClaimLost)
                assert await _durable_snapshot(pool) == before

        _run_database(verify())
    assert factory.active_count == 0


def _insert_job_writer_fixture(
    connection: psycopg.Connection[tuple[object, ...]],
    job_id: UUID,
) -> None:
    connection.execute(
        """
        INSERT INTO analysis_jobs (
            job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
            video_reference, callback_url, analysis_version, attempt_limit,
            attempt_count, state, accepted_at
        ) VALUES (
            %s, %s, %s, 'video-downgrade', 'player-downgrade',
            'match-downgrade.mp4', 'https://callback.invalid/downgrade',
            '1.0.0', 3, 0, 'QUEUED', clock_timestamp()
        )
        """,
        (
            job_id,
            hashlib.sha256(f"lookup:{job_id}".encode()).digest(),
            hashlib.sha256(f"fingerprint:{job_id}".encode()).digest(),
        ),
    ).close()


def _insert_attempt_writer_fixture(
    migration_settings: DatabaseSettings,
    writer: psycopg.Connection[tuple[object, ...]],
    job_id: UUID,
) -> None:
    with _connect(migration_settings, autocommit=False) as connection:
        _insert_job_writer_fixture(connection, job_id)
        connection.execute(
            "UPDATE analysis_jobs SET state = 'RUNNING', attempt_count = 1 WHERE job_id = %s",
            (job_id,),
        ).close()
        connection.execute(
            """
            WITH fixture_clock AS MATERIALIZED (SELECT clock_timestamp() AS t)
            INSERT INTO analysis_job_attempts (
                job_id, attempt_number, worker_slot_id, lease_id, status, started_at,
                heartbeat_at, lease_expires_at, finished_at, safe_failure_code
            )
            SELECT %s, 1, %s, %s, 'ACTIVE', t, t, t + interval '30 seconds', NULL, NULL
            FROM fixture_clock
            """,
            (job_id, UUID(int=9_001), UUID(int=9_002)),
        ).close()
    writer.execute(
        """
        UPDATE analysis_job_attempts
        SET heartbeat_at = started_at + interval '1 second'
        WHERE job_id = %s AND attempt_number = 1
        """,
        (job_id,),
    ).close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("writer_relation", "job_id"),
    (("analysis_jobs", UUID(int=400_001)), ("analysis_job_attempts", UUID(int=400_002))),
)
def test_slice3_downgrade_waits_for_writer_then_refuses_without_change(
    request: pytest.FixtureRequest,
    writer_relation: str,
    job_id: UUID,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_slice3_database(database)
        signature = _schema_signature(database.migration_settings)
        writer = _connect(database.migration_settings, autocommit=False)
        actor_errors: list[BaseException | None] = []

        def rollback_actor() -> None:
            try:
                # Yoyo executes the migration step on backend.copy(), so this
                # actor owns the copied connection observed in pg_locks.
                _runner(database.migration_settings).rollback_last()
            except BaseException as error:
                actor_errors.append(error)
            else:
                actor_errors.append(None)

        if writer_relation == "analysis_jobs":
            _insert_job_writer_fixture(writer, job_id)
        else:
            _insert_attempt_writer_fixture(database.migration_settings, writer, job_id)
        backend_pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
        assert backend_pid_row is not None
        writer_pid = backend_pid_row[0]
        assert type(writer_pid) is int

        actor = threading.Thread(
            target=rollback_actor,
            name=f"slice3-rollback-{writer_relation}",
            daemon=True,
        )
        actor.start()
        writer_released = False
        try:
            waiting_pid, blocker_pids = _wait_for_downgrade_lock(
                database.inspection_settings,
                writer_relation,
                writer_pid,
            )
            assert waiting_pid not in blocker_pids
            if writer_relation == "analysis_job_attempts":
                with _connect(database.inspection_settings) as connection:
                    jobs_lock = connection.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_catalog.pg_locks
                            WHERE pid = %s
                              AND relation = 'public.analysis_jobs'::regclass
                              AND mode = 'AccessExclusiveLock'
                              AND granted IS TRUE
                        )
                        """,
                        (waiting_pid,),
                    ).fetchone()
                assert jobs_lock == (True,)
            writer.commit()
            writer_released = True
        finally:
            _release_writer_and_join_actor(
                writer,
                actor,
                writer_released=writer_released,
            )

        assert len(actor_errors) == 1
        assert isinstance(actor_errors[0], DatabaseMigrationError)
        assert runner.current_version() == _WORKER_ATTEMPT_REVISION
        assert database.application_relations() == _SLICE3_RELATIONS
        assert _schema_signature(database.migration_settings) == signature
        with _connect(database.migration_settings) as connection:
            job = connection.execute(
                """
                SELECT analysis_version, attempt_limit, attempt_count, state
                FROM analysis_jobs WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
            attempts = connection.execute(
                """
                SELECT attempt_number, worker_slot_id, lease_id, status,
                       started_at, heartbeat_at
                FROM analysis_job_attempts WHERE job_id = %s
                """,
                (job_id,),
            ).fetchall()
        if writer_relation == "analysis_jobs":
            assert job == ("1.0.0", 3, 0, "QUEUED")
            assert attempts == []
        else:
            assert job == ("1.0.0", 3, 1, "RUNNING")
            assert len(attempts) == 1
            attempt = attempts[0]
            assert attempt[:4] == (1, UUID(int=9_001), UUID(int=9_002), "ACTIVE")
            assert isinstance(attempt[4], datetime)
            assert isinstance(attempt[5], datetime)
            assert attempt[5] == attempt[4] + timedelta(seconds=1)
    assert factory.active_count == 0
