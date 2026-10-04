"""Opt-in real PostgreSQL evidence for inactive AnalysisJob admission."""

from __future__ import annotations

import asyncio
import hashlib
import selectors
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from yoyo import read_migrations  # type: ignore[import-untyped]

from adapters.psycopg_analysis_job_repository import (
    AnalysisJobAdmissionLockTimeout,
    AnalysisJobPersistenceUnavailable,
    PsycopgAnalysisJobAdmissionRepository,
)
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
    PsycopgPoolFactory,
)
from adapters.yoyo_migration_backend import DatabaseMigrationError, YoyoMigrationBackend
from core.database_config import DatabaseSettings
from domain.analysis_job import (
    AnalysisJob,
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
    NewAnalysisJob,
)
from services.postgres_foundation import MigrationRunner, PostgresFoundation

_OPT_IN = "--slice1-disposable-postgres"
_SKIP_REASON = (
    "Slice 2 disposable PostgreSQL evidence was not executed because the explicit "
    "task-scoped opt-in is absent."
)
_MIGRATION_SOURCE = "package:adapters:migrations/postgres"
_FOUNDATION_REVISION = "sprint2_slice1_foundation"
_ANALYSIS_JOB_REVISION = "sprint2_slice2_analysis_job"
_CATALOG = (_FOUNDATION_REVISION, _ANALYSIS_JOB_REVISION)
_APPLICATION_RELATIONS = frozenset({"public.analysis_jobs"})
_ADVISORY_NAMESPACE = 1_396_113_410
_ADVISORY_RESOURCE = 1
_ACQUIRE_TIMEOUT_SECONDS = 5.0
_OPERATION_TIMEOUT_MS = 2_000
_LOCK_TIMEOUT_MS = 250
_CONCURRENCY_TIMEOUT_SECONDS = 12.0
_RAW_IDEMPOTENCY_MARKER = "raw-idempotency-integration-d-marker"
_EXPECTED_RUNTIME_PRIVILEGES = (
    True,
    True,
    False,
    False,
    False,
    False,
    False,
    False,
    False,
    False,
    False,
)


class _DisposablePostgresSession(Protocol):
    migration_settings: DatabaseSettings
    runtime_settings: DatabaseSettings

    def application_relations(self) -> frozenset[str]: ...


class _DisposablePostgresFactory(Protocol):
    @property
    def active_count(self) -> int: ...

    def __call__(self) -> AbstractContextManager[_DisposablePostgresSession]: ...


def _disposable_factory(request: pytest.FixtureRequest) -> _DisposablePostgresFactory:
    if not request.config.getoption(_OPT_IN, default=False):
        pytest.skip(_SKIP_REASON)
    try:
        fixture = request.getfixturevalue("slice1_disposable_postgres_factory")
    except pytest.FixtureLookupError:
        pytest.fail(
            "Slice 2 opt-in requires the existing task-owned disposable PostgreSQL fixture.",
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


async def _connect_async(
    settings: DatabaseSettings,
) -> psycopg.AsyncConnection[tuple[object, ...]]:
    return await psycopg.AsyncConnection.connect(
        host=settings._host,
        port=settings._port,
        dbname=settings._database,
        user=settings._user,
        password=settings._password,
        connect_timeout=settings.connect_timeout_seconds,
        autocommit=True,
    )


def _runner(settings: DatabaseSettings) -> MigrationRunner:
    return MigrationRunner(YoyoMigrationBackend(settings, (_MIGRATION_SOURCE,)))


def _catalog() -> tuple[str, ...]:
    return tuple(str(migration.id) for migration in read_migrations(_MIGRATION_SOURCE))


def _slice2_settings(settings: DatabaseSettings) -> DatabaseSettings:
    return replace(settings, required_schema_version=_ANALYSIS_JOB_REVISION)


def _grant_runtime_admission_privileges(
    migration_settings: DatabaseSettings,
    runtime_settings: DatabaseSettings,
) -> None:
    with _connect(migration_settings) as connection:
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE analysis_jobs TO {}").format(
                sql.Identifier(runtime_settings._user)
            )
        ).close()
    _assert_runtime_privileges(runtime_settings)


def _runtime_privileges(settings: DatabaseSettings) -> tuple[bool, ...]:
    with _connect(settings) as connection:
        row = connection.execute(
            """
            SELECT
                has_table_privilege(current_user, 'analysis_jobs', 'SELECT'),
                has_table_privilege(current_user, 'analysis_jobs', 'INSERT'),
                has_table_privilege(
                    current_user,
                    'analysis_jobs',
                    'SELECT WITH GRANT OPTION'
                ),
                has_table_privilege(
                    current_user,
                    'analysis_jobs',
                    'INSERT WITH GRANT OPTION'
                ),
                has_table_privilege(current_user, 'analysis_jobs', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_jobs', 'DELETE'),
                has_table_privilege(current_user, 'analysis_jobs', 'TRUNCATE'),
                has_table_privilege(current_user, 'analysis_jobs', 'TRIGGER'),
                has_table_privilege(current_user, 'analysis_jobs', 'REFERENCES'),
                has_schema_privilege(current_user, 'public', 'CREATE'),
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname = 'analysis_jobs'
                      AND pg_get_userbyid(relation.relowner) = current_user
                )
            """
        ).fetchone()
    if row is None or len(row) != 11 or any(type(value) is not bool for value in row):
        pytest.fail("Runtime privilege evidence was unavailable.", pytrace=False)
    return cast(tuple[bool, ...], row)


def _assert_runtime_privileges(settings: DatabaseSettings) -> None:
    if _runtime_privileges(settings) != _EXPECTED_RUNTIME_PRIVILEGES:
        pytest.fail(
            "Runtime privilege boundary did not match the accepted contract.", pytrace=False
        )


def _migration_role_owns_table(settings: DatabaseSettings) -> bool:
    with _connect(settings) as connection:
        row = connection.execute(
            """
            SELECT pg_get_userbyid(relation.relowner) = current_user
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'analysis_jobs'
            """
        ).fetchone()
    return row == (True,)


def _schema_signature(settings: DatabaseSettings) -> bytes:
    with _connect(settings) as connection:
        columns = connection.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'analysis_jobs'
            ORDER BY ordinal_position
            """
        ).fetchall()
        constraints = connection.execute(
            """
            SELECT
                constraint_row.contype,
                pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
            FROM pg_catalog.pg_constraint AS constraint_row
            WHERE constraint_row.conrelid = 'public.analysis_jobs'::regclass
            ORDER BY
                constraint_row.contype,
                pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
            """
        ).fetchall()
        indexes = connection.execute(
            """
            SELECT indexdef
            FROM pg_catalog.pg_indexes
            WHERE schemaname = 'public' AND tablename = 'analysis_jobs'
            ORDER BY indexname
            """
        ).fetchall()
    return hashlib.sha256(repr((columns, constraints, indexes)).encode("utf-8")).digest()


async def _assert_foundation_ready(settings: DatabaseSettings) -> None:
    foundation = PostgresFoundation(
        settings,
        pool_factory=PsycopgPoolFactory(),
        connectivity_probe=PostgresConnectivityProbe(),
        schema_version_probe=PostgresSchemaVersionProbe(),
    )
    await foundation.start()
    try:
        readiness = await foundation.readiness()
        assert readiness.ready is True
        assert readiness.reason == "READY"
        assert readiness.public.database_connectivity is True
        assert readiness.public.database_schema_version is True
    finally:
        await foundation.close()


@asynccontextmanager
async def _repository(
    settings: DatabaseSettings,
    *,
    operation_timeout_ms: int = _OPERATION_TIMEOUT_MS,
    admission_lock_timeout_ms: int = _LOCK_TIMEOUT_MS,
) -> AsyncIterator[tuple[PsycopgAnalysisJobAdmissionRepository, PsycopgDatabasePool]]:
    pool = PsycopgPoolFactory()(settings)
    if not isinstance(pool, PsycopgDatabasePool):
        pytest.fail("The production pool factory returned an unexpected adapter.", pytrace=False)
    await pool.open(timeout_seconds=settings.pool.open_timeout_seconds)
    try:
        yield (
            PsycopgAnalysisJobAdmissionRepository(
                pool,
                connection_acquire_timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS,
                operation_timeout_ms=operation_timeout_ms,
                admission_lock_timeout_ms=admission_lock_timeout_ms,
            ),
            pool,
        )
    finally:
        await pool.close(timeout_seconds=settings.pool.close_timeout_seconds)


async def _fetch_one(
    pool: PsycopgDatabasePool,
    query: str,
    params: tuple[object, ...] | None = None,
) -> tuple[object, ...] | None:
    async with pool.connection(timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS) as connection:
        cursor = await connection.execute(query, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()


async def _row_count(pool: PsycopgDatabasePool) -> int:
    row = await _fetch_one(pool, "SELECT count(*) FROM analysis_jobs")
    if row is None or len(row) != 1 or type(row[0]) is not int:
        pytest.fail("AnalysisJob row-count evidence was unavailable.", pytrace=False)
    return row[0]


async def _queued_row_count(pool: PsycopgDatabasePool) -> int:
    row = await _fetch_one(
        pool,
        "SELECT count(*) FROM analysis_jobs WHERE state = %s",
        ("QUEUED",),
    )
    if row is None or len(row) != 1 or type(row[0]) is not int:
        pytest.fail("Queued AnalysisJob row-count evidence was unavailable.", pytrace=False)
    return row[0]


async def _persisted_winner(
    pool: PsycopgDatabasePool,
    protected_lookup: bytes,
) -> tuple[UUID, bytes]:
    async with pool.connection(timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS) as connection:
        cursor = await connection.execute(
            """
            SELECT job_id, request_fingerprint
            FROM analysis_jobs
            WHERE idempotency_lookup = %s
            """,
            (protected_lookup,),
        )
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
    if len(rows) != 1 or len(rows[0]) != 2:
        pytest.fail("Persisted conflict-winner evidence was unavailable.", pytrace=False)
    job_id, fingerprint = rows[0]
    if not isinstance(job_id, UUID) or type(fingerprint) is not bytes or len(fingerprint) != 32:
        pytest.fail("Persisted conflict-winner evidence was invalid.", pytrace=False)
    return job_id, fingerprint


async def _single_persisted_job_id(pool: PsycopgDatabasePool) -> UUID:
    row = await _fetch_one(pool, "SELECT job_id FROM analysis_jobs")
    if row is None or len(row) != 1 or not isinstance(row[0], UUID):
        pytest.fail("Persisted capacity-winner evidence was unavailable.", pytrace=False)
    return row[0]


async def _connection_row_count(
    connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> int:
    cursor = await connection.execute("SELECT count(*) FROM analysis_jobs")
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        pytest.fail("AnalysisJob row-count evidence was unavailable.", pytrace=False)
    return row[0]


def _protected_lookup(group: str) -> bytes:
    return hashlib.sha256(f"protected:{group}".encode()).digest()


def _fingerprint(group: str) -> bytes:
    return hashlib.sha256(f"fingerprint:{group}".encode()).digest()


def _candidate(
    index: int,
    *,
    lookup_group: str | None = None,
    fingerprint_group: str | None = None,
) -> NewAnalysisJob:
    lookup = lookup_group or f"lookup-{index}"
    fingerprint = fingerprint_group or f"fingerprint-{index}"
    return NewAnalysisJob(
        job_id=UUID(int=10_000 + index),
        idempotency_lookup=_protected_lookup(lookup),
        request_fingerprint=_fingerprint(fingerprint),
        video_id=f"video-{fingerprint}",
        player_id=f"player-{fingerprint}",
        video_reference=f"match-{fingerprint}.mp4",
        callback_url=f"https://callback.invalid/{fingerprint}",
    )


def _jobs_match(left: AnalysisJob, right: AnalysisJob) -> bool:
    return (
        left.job_id == right.job_id
        and left.video_id == right.video_id
        and left.player_id == right.player_id
        and left.video_reference == right.video_reference
        and left.callback_url == right.callback_url
        and left.state is right.state
        and left.accepted_at == right.accepted_at
    )


async def _assert_persisted_job(
    pool: PsycopgDatabasePool,
    candidate: NewAnalysisJob,
    job: AnalysisJob,
) -> None:
    row = await _fetch_one(
        pool,
        """
        SELECT
            job_id = %s,
            idempotency_lookup = %s,
            request_fingerprint = %s,
            video_id = %s,
            player_id = %s,
            video_reference = %s,
            callback_url = %s,
            state = 'QUEUED',
            accepted_at = %s
        FROM analysis_jobs
        WHERE job_id = %s
        """,
        (
            job.job_id,
            candidate.idempotency_lookup,
            candidate.request_fingerprint,
            candidate.video_id,
            candidate.player_id,
            candidate.video_reference,
            candidate.callback_url,
            job.accepted_at,
            job.job_id,
        ),
    )
    if row != (True,) * 9:
        pytest.fail("Persisted AnalysisJob did not match the authoritative result.", pytrace=False)


async def _assert_raw_key_absent(pool: PsycopgDatabasePool) -> None:
    raw_text = _RAW_IDEMPOTENCY_MARKER
    raw_bytes = raw_text.encode()
    row = await _fetch_one(
        pool,
        """
        SELECT count(*)
        FROM analysis_jobs
        WHERE position(%s::bytea IN idempotency_lookup) > 0
           OR position(%s::bytea IN request_fingerprint) > 0
           OR position(%s IN video_id) > 0
           OR position(%s IN player_id) > 0
           OR position(%s IN video_reference) > 0
           OR position(%s IN callback_url) > 0
        """,
        (raw_bytes, raw_bytes, raw_text, raw_text, raw_text, raw_text),
    )
    assert row == (0,)


def _prepare_database(database: _DisposablePostgresSession) -> MigrationRunner:
    runner = _runner(database.migration_settings)
    assert runner.current_version() is None
    assert database.application_relations() == frozenset()
    assert _catalog() == _CATALOG
    runner.apply_pending()
    assert runner.current_version() == _ANALYSIS_JOB_REVISION
    assert database.application_relations() == _APPLICATION_RELATIONS
    return runner


@pytest.mark.integration
def test_real_migration_privileges_and_sequential_admission(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        assert _migration_role_owns_table(database.migration_settings)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)
        _run_database(_assert_foundation_ready(runtime_settings))

        async def verify() -> None:
            candidate = _candidate(1, lookup_group=_RAW_IDEMPOTENCY_MARKER)
            async with _repository(runtime_settings) as (repository, pool):
                accepted = await repository.accept_or_get(candidate, max_queue_size=1)
                assert isinstance(accepted, AnalysisJobAccepted)
                assert accepted.job.accepted_at.tzinfo is not None
                assert accepted.job.accepted_at.utcoffset() is not None
                await _assert_persisted_job(pool, candidate, accepted.job)
                await _assert_raw_key_absent(pool)
                assert await _row_count(pool) == 1

                existing = await repository.accept_or_get(candidate, max_queue_size=1)
                assert isinstance(existing, AnalysisJobExisting)
                assert _jobs_match(existing.job, accepted.job)
                assert await _row_count(pool) == 1

                conflict = await repository.accept_or_get(
                    _candidate(2, lookup_group=_RAW_IDEMPOTENCY_MARKER),
                    max_queue_size=1,
                )
                assert isinstance(conflict, AnalysisJobIdempotencyConflict)
                assert conflict.existing_job_id == accepted.job.job_id
                assert await _row_count(pool) == 1

                rejected = await repository.accept_or_get(_candidate(3), max_queue_size=1)
                assert isinstance(rejected, AnalysisJobCapacityRejected)
                assert await _row_count(pool) == 1

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_empty_slice2_rollback_and_deterministic_reapply(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_database(database)
        first_signature = _schema_signature(database.migration_settings)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )

        runner.rollback_last()
        assert runner.current_version() == _FOUNDATION_REVISION
        assert database.application_relations() == frozenset()

        runner.apply_pending()
        assert runner.current_version() == _ANALYSIS_JOB_REVISION
        assert database.application_relations() == _APPLICATION_RELATIONS
        assert _migration_role_owns_table(database.migration_settings)
        assert _schema_signature(database.migration_settings) == first_signature
        assert _runtime_privileges(database.runtime_settings)[1] is False

        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
    assert factory.active_count == 0


@pytest.mark.integration
def test_nonempty_slice2_rollback_is_refused_without_partial_change(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        runner = _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def insert_one() -> None:
            async with _repository(runtime_settings) as (repository, pool):
                outcome = await repository.accept_or_get(_candidate(10), max_queue_size=2)
                assert isinstance(outcome, AnalysisJobAccepted)
                assert await _row_count(pool) == 1

        _run_database(insert_one())
        assert runner.current_version() == _ANALYSIS_JOB_REVISION
        schema_signature_before_rollback = _schema_signature(database.migration_settings)
        with pytest.raises(DatabaseMigrationError):
            runner.rollback_last()
        assert runner.current_version() == _ANALYSIS_JOB_REVISION
        assert database.application_relations() == _APPLICATION_RELATIONS
        assert _schema_signature(database.migration_settings) == schema_signature_before_rollback

        async def verify_row() -> None:
            async with _repository(runtime_settings) as (_, pool):
                assert await _row_count(pool) == 1

        _run_database(verify_row())
    assert factory.active_count == 0


@pytest.mark.integration
def test_concurrent_identical_admission_has_one_authoritative_job(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def verify() -> None:
            candidates = tuple(
                _candidate(
                    20 + index,
                    lookup_group="concurrent-identical",
                    fingerprint_group="concurrent-identical",
                )
                for index in range(4)
            )
            async with _repository(runtime_settings) as (repository, pool):
                outcomes = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            repository.accept_or_get(candidate, max_queue_size=4)
                            for candidate in candidates
                        )
                    ),
                    timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                )
                accepted = [item for item in outcomes if isinstance(item, AnalysisJobAccepted)]
                existing = [item for item in outcomes if isinstance(item, AnalysisJobExisting)]
                assert len(accepted) == 1
                assert len(existing) == 3
                assert all(_jobs_match(item.job, accepted[0].job) for item in existing)
                assert not any(
                    isinstance(
                        item,
                        (AnalysisJobIdempotencyConflict, AnalysisJobCapacityRejected),
                    )
                    for item in outcomes
                )
                assert await _row_count(pool) == 1

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_concurrent_conflicting_fingerprints_classify_against_the_winner(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def verify() -> None:
            candidates = tuple(
                _candidate(
                    30 + index,
                    lookup_group="concurrent-conflict",
                    fingerprint_group="fingerprint-a" if index < 2 else "fingerprint-b",
                )
                for index in range(4)
            )
            async with _repository(runtime_settings) as (repository, pool):
                outcomes = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            repository.accept_or_get(candidate, max_queue_size=4)
                            for candidate in candidates
                        )
                    ),
                    timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                )
                accepted = [item for item in outcomes if isinstance(item, AnalysisJobAccepted)]
                existing = [item for item in outcomes if isinstance(item, AnalysisJobExisting)]
                conflicts = [
                    item for item in outcomes if isinstance(item, AnalysisJobIdempotencyConflict)
                ]
                assert len(accepted) == 1
                persisted_job_id, persisted_fingerprint = await _persisted_winner(
                    pool,
                    candidates[0].idempotency_lookup,
                )
                winner = accepted[0].job
                winner_candidates = tuple(
                    candidate for candidate in candidates if candidate.job_id == winner.job_id
                )
                if len(winner_candidates) != 1:
                    pytest.fail(
                        "Accepted conflict winner was not a submitted candidate.", pytrace=False
                    )
                winner_candidate = winner_candidates[0]
                assert winner.job_id == persisted_job_id
                assert winner_candidate.request_fingerprint == persisted_fingerprint
                await _assert_persisted_job(pool, winner_candidate, winner)
                assert len(existing) == 1
                assert all(_jobs_match(item.job, winner) for item in existing)
                assert len(conflicts) == 2
                assert all(item.existing_job_id == persisted_job_id for item in conflicts)
                assert all(
                    isinstance(outcome, (AnalysisJobAccepted, AnalysisJobExisting))
                    == (candidate.request_fingerprint == persisted_fingerprint)
                    for candidate, outcome in zip(candidates, outcomes, strict=True)
                )
                assert not any(isinstance(item, AnalysisJobCapacityRejected) for item in outcomes)
                assert await _row_count(pool) == 1

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_concurrent_distinct_admission_never_exceeds_queued_capacity(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def verify() -> None:
            capacity = 1
            candidates = tuple(_candidate(40 + index) for index in range(4))
            async with _repository(runtime_settings) as (repository, pool):
                if runtime_settings.pool.max_size < 2:
                    pytest.fail(
                        "Capacity evidence requires at least two real pool connections.",
                        pytrace=False,
                    )
                start_barrier = asyncio.Barrier(len(candidates))

                async def admit_after_barrier(candidate: NewAnalysisJob) -> object:
                    await start_barrier.wait()
                    return await repository.accept_or_get(
                        candidate,
                        max_queue_size=capacity,
                    )

                # The barrier strengthens evidence against counting outside the
                # advisory lock; PostgreSQL still controls transaction ordering.
                tasks = tuple(
                    asyncio.create_task(admit_after_barrier(candidate)) for candidate in candidates
                )
                try:
                    outcomes = await asyncio.wait_for(
                        asyncio.gather(*tasks),
                        timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                    )
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                assert sum(isinstance(item, AnalysisJobAccepted) for item in outcomes) == capacity
                assert (
                    sum(isinstance(item, AnalysisJobCapacityRejected) for item in outcomes)
                    == len(candidates) - capacity
                )
                assert not any(
                    isinstance(item, (AnalysisJobExisting, AnalysisJobIdempotencyConflict))
                    for item in outcomes
                )
                assert await _row_count(pool) == capacity
                assert await _queued_row_count(pool) == capacity
                persisted_job_id = await _single_persisted_job_id(pool)
                assert persisted_job_id in {candidate.job_id for candidate in candidates}

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_real_advisory_lock_timeout_rolls_back_and_repository_recovers(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repository(
                runtime_settings,
                operation_timeout_ms=1_500,
                admission_lock_timeout_ms=150,
            ) as (repository, pool):
                holder = await _connect_async(database.migration_settings)
                try:
                    async with holder.transaction():
                        cursor = await holder.execute(
                            "SELECT pg_advisory_xact_lock(%s, %s)",
                            (_ADVISORY_NAMESPACE, _ADVISORY_RESOURCE),
                        )
                        await cursor.close()
                        with pytest.raises(AnalysisJobAdmissionLockTimeout):
                            await asyncio.wait_for(
                                repository.accept_or_get(_candidate(50), max_queue_size=2),
                                timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                            )
                        assert await _row_count(pool) == 0
                finally:
                    await holder.close()

                outcome = await asyncio.wait_for(
                    repository.accept_or_get(_candidate(51), max_queue_size=2),
                    timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                )
                assert isinstance(outcome, AnalysisJobAccepted)
                assert await _row_count(pool) == 1

        _run_database(verify())
    assert factory.active_count == 0


@pytest.mark.integration
def test_real_statement_timeout_rolls_back_and_repository_recovers(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_admission_privileges(
            database.migration_settings,
            database.runtime_settings,
        )
        runtime_settings = _slice2_settings(database.runtime_settings)

        async def verify() -> None:
            async with _repository(
                runtime_settings,
                operation_timeout_ms=200,
                admission_lock_timeout_ms=100,
            ) as (repository, pool):
                holder = await _connect_async(database.migration_settings)
                try:
                    async with holder.transaction():
                        cursor = await holder.execute(
                            "LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE"
                        )
                        await cursor.close()
                        with pytest.raises(AnalysisJobPersistenceUnavailable) as captured:
                            await asyncio.wait_for(
                                repository.accept_or_get(_candidate(60), max_queue_size=2),
                                timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                            )
                        assert not isinstance(captured.value, AnalysisJobAdmissionLockTimeout)
                        assert await _connection_row_count(holder) == 0
                finally:
                    await holder.close()

                assert await _row_count(pool) == 0
                outcome = await asyncio.wait_for(
                    repository.accept_or_get(_candidate(61), max_queue_size=2),
                    timeout=_CONCURRENCY_TIMEOUT_SECONDS,
                )
                assert isinstance(outcome, AnalysisJobAccepted)
                assert await _row_count(pool) == 1

        _run_database(verify())
    assert factory.active_count == 0
