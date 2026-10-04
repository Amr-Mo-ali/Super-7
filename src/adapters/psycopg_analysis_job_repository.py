"""Inactive atomic Psycopg repository for durable AnalysisJob admission."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import TracebackType
from typing import Literal, Never, Protocol
from uuid import UUID

from psycopg import InterfaceError, OperationalError
from psycopg.errors import LockNotAvailable
from psycopg_pool import PoolTimeout

from domain.analysis_job import (
    AnalysisJob,
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
    AnalysisJobState,
    NewAnalysisJob,
)

__all__ = [
    "PsycopgAnalysisJobAdmissionRepository",
    "AnalysisJobPersistenceUnavailable",
    "AnalysisJobPersistenceInvariantError",
    "AnalysisJobAdmissionLockTimeout",
]

_MAX_POSTGRES_TIMEOUT_MS = 2_147_483_647
_ADVISORY_LOCK_NAMESPACE = 1_396_113_410
_ADVISORY_LOCK_RESOURCE = 1
_UNAVAILABLE_MESSAGE = "PostgreSQL AnalysisJob persistence is unavailable."
_INVARIANT_MESSAGE = "PostgreSQL AnalysisJob persistence invariant failed."
_LOCK_TIMEOUT_MESSAGE = "PostgreSQL AnalysisJob admission lock timed out."

_TIMEOUT_SQL = "SELECT set_config(%s, %s, %s)"
_LOOKUP_SQL = """SELECT job_id, idempotency_lookup, request_fingerprint, video_id,
    player_id, video_reference, callback_url, state, accepted_at
FROM analysis_jobs
WHERE idempotency_lookup = %s"""
_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s, %s)"
_COUNT_QUEUED_SQL = "SELECT count(*) FROM analysis_jobs WHERE state = %s"
_INSERT_SQL = """INSERT INTO analysis_jobs (
    job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
    video_reference, callback_url, state, accepted_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
RETURNING job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
    video_reference, callback_url, state, accepted_at"""

_Outcome = (
    AnalysisJobAccepted
    | AnalysisJobExisting
    | AnalysisJobIdempotencyConflict
    | AnalysisJobCapacityRejected
)
_FailurePhase = Literal["advisory-execute", "persistence"]


class AnalysisJobPersistenceUnavailable(RuntimeError):
    """Expected PostgreSQL admission persistence is unavailable."""


class AnalysisJobPersistenceInvariantError(RuntimeError):
    """Stored AnalysisJob data violates the accepted durable contract."""


class AnalysisJobAdmissionLockTimeout(AnalysisJobPersistenceUnavailable):
    """The bounded transaction advisory-lock acquisition timed out."""


class _Cursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def close(self) -> None: ...


class _Connection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[object]: ...

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> _Cursor: ...


class _Pool(Protocol):
    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_Connection]: ...


@dataclass(frozen=True, slots=True)
class _StoredAnalysisJob:
    job: AnalysisJob
    idempotency_lookup: bytes
    request_fingerprint: bytes


class PsycopgAnalysisJobAdmissionRepository:
    """Persist one idempotent admission atomically without runtime activation."""

    def __init__(
        self,
        pool: _Pool,
        *,
        connection_acquire_timeout_seconds: float,
        operation_timeout_ms: int,
        admission_lock_timeout_ms: int,
    ) -> None:
        _validate_acquisition_timeout(connection_acquire_timeout_seconds)
        _validate_millisecond_timeout(operation_timeout_ms)
        _validate_millisecond_timeout(admission_lock_timeout_ms)
        if operation_timeout_ms <= admission_lock_timeout_ms:
            raise ValueError("Operation timeout must exceed admission lock timeout.")

        self._pool = pool
        self._connection_acquire_timeout_seconds = float(connection_acquire_timeout_seconds)
        self._operation_timeout_ms = operation_timeout_ms
        self._admission_lock_timeout_ms = admission_lock_timeout_ms

    async def accept_or_get(
        self,
        candidate: NewAnalysisJob,
        *,
        max_queue_size: int,
    ) -> _Outcome:
        """Return one committed admission outcome after connection release."""
        if type(candidate) is not NewAnalysisJob:
            raise TypeError("Candidate must be a NewAnalysisJob.")
        if type(max_queue_size) is not int:
            raise TypeError("Maximum queue size must be an integer.")
        if max_queue_size <= 0:
            raise ValueError("Maximum queue size must be greater than zero.")

        connection_context = self._pool.connection(
            timeout_seconds=self._connection_acquire_timeout_seconds
        )
        connection = await _enter_context(connection_context)
        primary: BaseException | None = None
        outcome: _Outcome | None = None
        try:
            outcome = await self._run_transaction(
                connection,
                candidate,
                max_queue_size=max_queue_size,
            )
        except BaseException as error:
            primary = error
        await _exit_context(connection_context, primary)
        if outcome is None:
            raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
        return outcome

    async def _run_transaction(
        self,
        connection: _Connection,
        candidate: NewAnalysisJob,
        *,
        max_queue_size: int,
    ) -> _Outcome:
        transaction_context = connection.transaction()
        await _enter_context_without_value(transaction_context)
        primary: BaseException | None = None
        outcome: _Outcome | None = None
        try:
            outcome = await self._admit_in_transaction(
                connection,
                candidate,
                max_queue_size=max_queue_size,
            )
        except BaseException as error:
            primary = error
        await _exit_context(transaction_context, primary)
        if outcome is None:
            raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
        return outcome

    async def _admit_in_transaction(
        self,
        connection: _Connection,
        candidate: NewAnalysisJob,
        *,
        max_queue_size: int,
    ) -> _Outcome:
        await _execute_and_close(
            connection,
            _TIMEOUT_SQL,
            ("statement_timeout", str(self._operation_timeout_ms), True),
        )
        stored = await _lookup(connection, candidate.idempotency_lookup)
        if stored is not None:
            return _existing_or_conflict(stored, candidate)

        await _execute_and_close(
            connection,
            _TIMEOUT_SQL,
            ("lock_timeout", str(self._admission_lock_timeout_ms), True),
        )
        await _execute_and_close(
            connection,
            _ADVISORY_LOCK_SQL,
            (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_RESOURCE),
            phase="advisory-execute",
        )

        stored = await _lookup(connection, candidate.idempotency_lookup)
        if stored is not None:
            return _existing_or_conflict(stored, candidate)

        queued = await _queued_count(connection)
        if queued >= max_queue_size:
            return AnalysisJobCapacityRejected()

        stored = await _insert(connection, candidate)
        _validate_insert_consistency(stored, candidate)
        return AnalysisJobAccepted(stored.job)


def _validate_acquisition_timeout(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Connection acquisition timeout must be numeric.")
    if not isfinite(value) or value <= 0:
        raise ValueError("Connection acquisition timeout must be finite and positive.")


def _validate_millisecond_timeout(value: object) -> None:
    if type(value) is not int:
        raise TypeError("PostgreSQL millisecond timeout must be an integer.")
    if value <= 0 or value > _MAX_POSTGRES_TIMEOUT_MS:
        raise ValueError("PostgreSQL millisecond timeout is outside the approved range.")


async def _enter_context[ContextValue](
    context: AbstractAsyncContextManager[ContextValue],
) -> ContextValue:
    captured: BaseException | None = None
    value: ContextValue | None = None
    try:
        value = await context.__aenter__()
    except BaseException as error:
        captured = error
    if captured is not None:
        _raise_classified(captured)
    if value is None:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    return value


async def _enter_context_without_value(
    context: AbstractAsyncContextManager[object],
) -> None:
    captured: BaseException | None = None
    try:
        await context.__aenter__()
    except BaseException as error:
        captured = error
    if captured is not None:
        _raise_classified(captured)


async def _exit_context(
    context: AbstractAsyncContextManager[object],
    primary: BaseException | None,
) -> None:
    cleanup: BaseException | None = None
    exception_type = type(primary) if primary is not None else None
    traceback: TracebackType | None = primary.__traceback__ if primary is not None else None
    try:
        await context.__aexit__(exception_type, primary, traceback)
    except BaseException as error:
        cleanup = error
    _finish_cleanup(primary, cleanup)


def _finish_cleanup(
    primary: BaseException | None,
    cleanup: BaseException | None,
) -> None:
    if cleanup is None:
        if primary is not None:
            raise primary
        return
    if primary is not None:
        if isinstance(cleanup, Exception):
            raise primary
        raise cleanup from primary
    _raise_classified(cleanup)


def _raise_classified(
    error: BaseException,
    *,
    phase: _FailurePhase = "persistence",
) -> Never:
    if (
        phase == "advisory-execute"
        and isinstance(error, LockNotAvailable)
        and error.sqlstate == "55P03"
    ):
        raise AnalysisJobAdmissionLockTimeout(_LOCK_TIMEOUT_MESSAGE)
    if isinstance(error, (PoolTimeout, OperationalError, InterfaceError)):
        raise AnalysisJobPersistenceUnavailable(_UNAVAILABLE_MESSAGE)
    raise error


async def _execute_cursor(
    connection: _Connection,
    sql: str,
    params: tuple[object, ...],
    *,
    phase: _FailurePhase = "persistence",
) -> _Cursor:
    captured: BaseException | None = None
    cursor: _Cursor | None = None
    try:
        cursor = await connection.execute(sql, params)
    except BaseException as error:
        captured = error
    if captured is not None:
        _raise_classified(captured, phase=phase)
    if cursor is None:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    return cursor


async def _execute_and_close(
    connection: _Connection,
    sql: str,
    params: tuple[object, ...],
    *,
    phase: _FailurePhase = "persistence",
) -> None:
    cursor = await _execute_cursor(connection, sql, params, phase=phase)
    await _close_cursor(cursor, None)


async def _fetch_one(
    connection: _Connection,
    sql: str,
    params: tuple[object, ...],
) -> tuple[object, ...] | None:
    cursor = await _execute_cursor(connection, sql, params)
    primary: BaseException | None = None
    row: tuple[object, ...] | None = None
    try:
        row = await cursor.fetchone()
    except BaseException as error:
        primary = _classified(error)
    await _close_cursor(cursor, primary)
    return row


async def _close_cursor(
    cursor: _Cursor,
    primary: BaseException | None,
) -> None:
    cleanup: BaseException | None = None
    try:
        await cursor.close()
    except BaseException as error:
        cleanup = error
    _finish_cleanup(primary, cleanup)


def _classified(error: BaseException) -> BaseException:
    if isinstance(error, (PoolTimeout, OperationalError, InterfaceError)):
        return AnalysisJobPersistenceUnavailable(_UNAVAILABLE_MESSAGE)
    return error


async def _lookup(
    connection: _Connection,
    idempotency_lookup: bytes,
) -> _StoredAnalysisJob | None:
    row = await _fetch_one(connection, _LOOKUP_SQL, (idempotency_lookup,))
    return None if row is None else _decode_row(row)


async def _queued_count(connection: _Connection) -> int:
    row = await _fetch_one(connection, _COUNT_QUEUED_SQL, ("QUEUED",))
    if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    return row[0]


async def _insert(
    connection: _Connection,
    candidate: NewAnalysisJob,
) -> _StoredAnalysisJob:
    row = await _fetch_one(
        connection,
        _INSERT_SQL,
        (
            candidate.job_id,
            candidate.idempotency_lookup,
            candidate.request_fingerprint,
            candidate.video_id,
            candidate.player_id,
            candidate.video_reference,
            candidate.callback_url,
            "QUEUED",
        ),
    )
    if row is None:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    return _decode_row(row)


def _existing_or_conflict(
    stored: _StoredAnalysisJob,
    candidate: NewAnalysisJob,
) -> AnalysisJobExisting | AnalysisJobIdempotencyConflict:
    if stored.request_fingerprint == candidate.request_fingerprint:
        return AnalysisJobExisting(stored.job)
    return AnalysisJobIdempotencyConflict(stored.job.job_id)


def _decode_row(row: tuple[object, ...]) -> _StoredAnalysisJob:
    if type(row) is not tuple or len(row) != 9:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    (
        job_id,
        idempotency_lookup,
        request_fingerprint,
        video_id,
        player_id,
        video_reference,
        callback_url,
        state_value,
        accepted_at,
    ) = row
    if not isinstance(job_id, UUID):
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    if type(idempotency_lookup) is not bytes or not idempotency_lookup:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    if type(request_fingerprint) is not bytes or len(request_fingerprint) != 32:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    video_id_text = _decode_text(video_id)
    player_id_text = _decode_text(player_id)
    video_reference_text = _decode_text(video_reference)
    callback_url_text = _decode_text(callback_url)
    if type(state_value) is not str or state_value not in {
        state.value for state in AnalysisJobState
    }:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    if not isinstance(accepted_at, datetime) or accepted_at.tzinfo is None:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    try:
        aware = accepted_at.utcoffset() is not None
    except Exception:
        aware = False
    if not aware:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)

    lookup = idempotency_lookup
    fingerprint = request_fingerprint
    job = AnalysisJob(
        job_id=job_id,
        video_id=video_id_text,
        player_id=player_id_text,
        video_reference=video_reference_text,
        callback_url=callback_url_text,
        state=AnalysisJobState(state_value),
        accepted_at=accepted_at,
    )
    return _StoredAnalysisJob(
        job=job,
        idempotency_lookup=lookup,
        request_fingerprint=fingerprint,
    )


def _decode_text(value: object) -> str:
    if type(value) is not str or not value:
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
    return value


def _validate_insert_consistency(
    stored: _StoredAnalysisJob,
    candidate: NewAnalysisJob,
) -> None:
    job = stored.job
    if (
        job.job_id != candidate.job_id
        or stored.idempotency_lookup != candidate.idempotency_lookup
        or stored.request_fingerprint != candidate.request_fingerprint
        or job.video_id != candidate.video_id
        or job.player_id != candidate.player_id
        or job.video_reference != candidate.video_reference
        or job.callback_url != candidate.callback_url
        or job.state is not AnalysisJobState.QUEUED
    ):
        raise AnalysisJobPersistenceInvariantError(_INVARIANT_MESSAGE)
