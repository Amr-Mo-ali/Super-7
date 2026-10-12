"""Inactive, transaction-owning Worker persistence boundary (Planning-F/H).

Rows are diagnostic observations, not authority until association and committed
consistency have been checked. SQL rechecks eligibility with a separately captured
mutation clock. Nothing here composes a pool, starts a worker, or executes analysis.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from math import isfinite
from types import (
    CodeType,
    FunctionType,
    GetSetDescriptorType,
    MappingProxyType,
    MemberDescriptorType,
    MethodType,
)
from typing import Never, Protocol
from uuid import UUID

from psycopg import InterfaceError, OperationalError
from psycopg.errors import UniqueViolation
from psycopg_pool import PoolTimeout

from domain.analysis_job import AnalysisJobState, _validate_datetime
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    AttemptStatus,
    ClaimableAnalysisJob,
    LeaseDuration,
    LeaseId,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
    _instant_key,
    _validate_authority,
    _validate_failure_code,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AnalysisJobWorkerPersistenceUnavailable,
    AttemptExhausted,
    AttemptFailed,
    AttemptRequeued,
    AttemptResolutionAlreadyApplied,
    ClaimLost,
    LeaseRenewed,
    NoJobAvailable,
    RecoverySummary,
    WorkerSlotUnavailable,
    _validate_duration,
    _validate_slot,
)

__all__ = [
    "PsycopgAnalysisJobWorkerRepository",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerPersistenceInvariantError",
]

_UNAVAILABLE = "PostgreSQL AnalysisJob worker persistence is unavailable."
_INVARIANT = "PostgreSQL AnalysisJob worker persistence invariant failed."
_SLOT_INDEX = "analysis_job_attempts_active_worker_slot_id_idx"
_SETTINGS = "SELECT set_config(%s, %s, %s)"
_CLOCK = "SELECT clock_timestamp()"
_J = (
    "job_id, video_id, player_id, video_reference, callback_url, analysis_version, "
    "state, attempt_limit, attempt_count, accepted_at"
)
_A = (
    "job_id, attempt_number, worker_slot_id, lease_id, status, started_at, "
    "heartbeat_at, lease_expires_at, finished_at, safe_failure_code"
)
_JOB_READ = f"SELECT {_J} FROM analysis_jobs WHERE job_id = %s FOR UPDATE"
_ATTEMPT_READ = (
    f"SELECT {_A} FROM analysis_job_attempts WHERE job_id = %s AND attempt_number = %s FOR UPDATE"
)
_LOCATOR = (
    "SELECT job_id, attempt_number FROM analysis_job_attempts "
    "WHERE status = %s AND worker_slot_id = %s"
)
_CANDIDATE = (
    f"SELECT {_J} FROM analysis_jobs "
    "WHERE state = %s AND attempt_count < attempt_limit "
    "ORDER BY accepted_at ASC, job_id ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
)
_ACTIVE = f"SELECT {_A} FROM analysis_job_attempts WHERE job_id = %s AND status = %s"
_CLAIM_JOB = (
    "UPDATE analysis_jobs SET state = %s, attempt_count = attempt_count + 1 "
    "WHERE job_id = %s AND state = %s AND attempt_count = %s "
    f"AND attempt_count < attempt_limit RETURNING {_J}"
)
# A pure microsecond interval has no calendar-day component. Its bound text is
# built with integer arithmetic, not total_seconds(), interval multiplication by
# floating point, or Psycopg's calendar-day timedelta adaptation.
_INSERT = f"""WITH mutation_clock AS MATERIALIZED (SELECT clock_timestamp() AS t_m)
INSERT INTO analysis_job_attempts (
    {_A}
) SELECT %s, %s, %s, %s, %s, t_m, t_m, t_m + %s::interval, NULL, NULL
FROM mutation_clock
RETURNING {_A}"""
_RENEW = f"""WITH mutation_clock AS MATERIALIZED (SELECT clock_timestamp() AS t_m)
UPDATE analysis_job_attempts AS a
SET heartbeat_at = m.t_m, lease_expires_at = m.t_m + %s::interval
FROM mutation_clock AS m
WHERE a.job_id = %s AND a.attempt_number = %s
    AND a.worker_slot_id = %s AND a.lease_id = %s AND a.status = %s
    AND a.lease_expires_at > m.t_m
    AND EXISTS (SELECT 1 FROM analysis_jobs AS j
        WHERE j.job_id = a.job_id AND j.attempt_count = a.attempt_number AND j.state = %s)
RETURNING {", ".join("a." + column.strip() for column in _A.split(","))}"""
_RESOLVE_OWNER = f"""WITH mutation_clock AS MATERIALIZED (SELECT clock_timestamp() AS t_m)
UPDATE analysis_job_attempts AS a
SET status = %s, lease_expires_at = NULL, finished_at = m.t_m, safe_failure_code = %s
FROM mutation_clock AS m
WHERE a.job_id = %s AND a.attempt_number = %s
    AND a.worker_slot_id = %s AND a.lease_id = %s AND a.status = %s
    AND a.lease_expires_at > m.t_m
    AND EXISTS (SELECT 1 FROM analysis_jobs AS j
        WHERE j.job_id = a.job_id AND j.attempt_count = a.attempt_number AND j.state = %s)
RETURNING {", ".join("a." + column.strip() for column in _A.split(","))}"""
_RESOLVE_EXPIRED = _RESOLVE_OWNER.replace(
    "a.lease_expires_at > m.t_m", "a.lease_expires_at <= m.t_m"
)
_RESOLVE_JOB = f"""UPDATE analysis_jobs AS j SET state = %s
WHERE j.job_id = %s AND j.attempt_count = %s AND j.state = %s
    AND EXISTS (SELECT 1 FROM analysis_job_attempts AS a
        WHERE a.job_id = j.job_id AND a.attempt_number = j.attempt_count
            AND a.worker_slot_id = %s AND a.lease_id = %s AND a.status = %s)
RETURNING {", ".join("j." + column.strip() for column in _J.split(","))}"""
_RECOVERY = f"""SELECT {", ".join("j." + column.strip() for column in _J.split(","))}
FROM analysis_jobs AS j JOIN analysis_job_attempts AS a
    ON a.job_id = j.job_id AND a.attempt_number = j.attempt_count
WHERE j.state = %s AND a.status = %s AND a.lease_expires_at <= %s
ORDER BY j.job_id ASC LIMIT %s FOR UPDATE OF j SKIP LOCKED"""


class _Cursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...

    async def close(self) -> None: ...


class _Connection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[object]: ...

    async def execute(self, query: str, params: tuple[object, ...] | None = None) -> _Cursor: ...


class _Pool(Protocol):
    def connection(self, *, timeout_seconds: float) -> AbstractAsyncContextManager[_Connection]: ...


@dataclass(frozen=True, slots=True)
class _Job:
    payload: ClaimableAnalysisJob
    state: AnalysisJobState
    count: int


@dataclass(frozen=True, slots=True)
class _Attempt:
    authority: AttemptAuthority
    status: AttemptStatus
    started: datetime
    heartbeat: datetime
    expiry: datetime | None
    finished: datetime | None
    code: str | None


def _native_descriptor(owner: type, name: str) -> GetSetDescriptorType | MemberDescriptorType:
    # Only trusted built-in type dictionaries are passed here.
    value: object = owner.__dict__[name]
    if isinstance(value, (GetSetDescriptorType, MemberDescriptorType)):
        return value
    raise RuntimeError("Unexpected native factory inspection descriptor.")


def _native_read(descriptor: GetSetDescriptorType | MemberDescriptorType, value: object) -> object:
    result: object = descriptor.__get__(value)
    return result


_TYPE_MRO = _native_descriptor(type, "__mro__")
_TYPE_DICT = _native_descriptor(type, "__dict__")
_FUNCTION_CODE = _native_descriptor(FunctionType, "__code__")
_FUNCTION_DICT = _native_descriptor(FunctionType, "__dict__")
_CODE_FLAGS = _native_descriptor(CodeType, "co_flags")
_METHOD_FUNCTION = _native_descriptor(MethodType, "__func__")
_STATIC_FUNCTION = _native_descriptor(staticmethod, "__func__")
_CLASS_FUNCTION = _native_descriptor(classmethod, "__func__")
_PARTIAL_FUNCTION = _native_descriptor(partial, "func")
_ABSENT, _PRESENT, _OPAQUE = object(), object(), object()
_ASYNC_FLAGS = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR


def _entry(namespace: object, name: str) -> tuple[object, object]:
    entries: Iterable[tuple[object, object]]
    if isinstance(namespace, dict):
        entries = dict.items(namespace)
    elif isinstance(namespace, MappingProxyType):
        entries = MappingProxyType.items(namespace)
    else:
        return _OPAQUE, None
    observation: tuple[object, object] = (_ABSENT, None)
    for key, value in entries:
        if type(key) is not str:
            return _OPAQUE, None
        if key == name:
            observation = (_PRESENT, value)
    return observation


def _raw_call(factory: object) -> object:
    mro = _native_read(_TYPE_MRO, type(factory))
    if isinstance(mro, tuple):
        for owner in mro:
            state, value = _entry(_native_read(_TYPE_DICT, owner), "__call__")
            if state is _PRESENT:
                return value
            if state is _OPAQUE:
                break
    return None


def _native_async_flags(function: object) -> bool:
    flags = _native_read(_CODE_FLAGS, _native_read(_FUNCTION_CODE, function))
    return isinstance(flags, int) and bool(flags & _ASYNC_FLAGS)


def _marker_bootstrap() -> tuple[str, object]:
    executions = [0, 0]

    def marked() -> None:
        executions[0] += 1

    def unmarked() -> None:
        executions[1] += 1

    def failed() -> Never:
        raise RuntimeError("Lease UUID factory coroutine-marker bootstrap failed.")

    for probe in (marked, unmarked):
        metadata = _native_read(_FUNCTION_DICT, probe)
        if not isinstance(metadata, dict) or len(metadata) != 0 or _native_async_flags(probe):
            failed()
    inspect.markcoroutinefunction(marked)
    metadata = _native_read(_FUNCTION_DICT, marked)
    other = _native_read(_FUNCTION_DICT, unmarked)
    if not isinstance(metadata, dict) or not isinstance(other, dict):
        failed()
    entries = tuple(dict.items(metadata))
    if (
        len(entries) != 1
        or type(entries[0][0]) is not str
        or len(other) != 0
        or _native_async_flags(marked)
        or _native_async_flags(unmarked)
    ):
        failed()
    name, token = entries[0]
    if (
        inspect.iscoroutinefunction(marked) is not True
        or inspect.iscoroutinefunction(unmarked) is not False
        or executions != [0, 0]
    ):
        failed()
    return name, token


_COROUTINE_PROFILE = _marker_bootstrap()


def _native_async_function(function: object) -> bool:
    if _native_async_flags(function):
        return True
    name, token = _COROUTINE_PROFILE
    state, value = _entry(_native_read(_FUNCTION_DICT, function), name)
    return state is _PRESENT and value is token


class PsycopgAnalysisJobWorkerRepository:
    """One short transaction per call, no defaults, retries, or runtime wiring."""

    def __init__(
        self,
        *,
        pool: _Pool,
        connection_acquire_timeout_seconds: float,
        operation_timeout_ms: int,
        lock_timeout_ms: int,
        lease_id_factory: Callable[[], UUID],
    ) -> None:
        acquire = _acquisition_timeout(connection_acquire_timeout_seconds)
        _millisecond_timeout(operation_timeout_ms)
        _millisecond_timeout(lock_timeout_ms)
        if operation_timeout_ms <= lock_timeout_ms:
            raise ValueError("Operation timeout must exceed lock timeout.")
        if not callable(lease_id_factory):
            raise TypeError("Lease UUID factory must be callable.")
        for surface in (
            lease_id_factory,
            _raw_call(lease_id_factory),
        ):
            if _known_async_factory_target(surface):
                raise TypeError("Lease UUID factory must be synchronous.")
        self._pool = pool
        self._acquire = acquire
        self._operation_timeout_ms = operation_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._factory = lease_id_factory

    async def claim_next(
        self, *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
    ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable:
        _validate_slot(worker_slot_id)
        _validate_duration(lease_duration)
        return await self._run(
            lambda connection: self._claim(connection, worker_slot_id, lease_duration)
        )

    async def renew_lease(
        self, *, authority: AttemptAuthority, lease_duration: LeaseDuration
    ) -> LeaseRenewed | ClaimLost:
        _validate_authority(authority)
        _validate_duration(lease_duration)

        async def operation(connection: _Connection) -> LeaseRenewed | ClaimLost:
            pair = await _owner(connection, authority)
            if pair is None:
                return ClaimLost(authority)
            job, old = pair
            if not await _eligible(connection, job, old):
                return ClaimLost(authority)

            def decode(rows: list[tuple[object, ...]]) -> _Attempt | None:
                result = _optional_attempt(rows)
                if result is not None:
                    _preserved_attempt(old, result, heartbeat=False)
                    _pair(job, result)
                    if (
                        result.status is not AttemptStatus.ACTIVE
                        or _instant_key(result.heartbeat) < _instant_key(old.heartbeat)
                        or _instant_key(result.heartbeat) >= _instant_key(_expiry(old))
                    ):
                        _invalid()
                    _duration(result, lease_duration)
                return result

            result = await _query(
                connection,
                _RENEW,
                (_interval(lease_duration), *_fence(authority), "ACTIVE", "RUNNING"),
                decode,
            )
            if result is None:
                return ClaimLost(authority)
            return LeaseRenewed(authority, result.heartbeat, _expiry(result))

        return await self._run(operation)

    async def finalize_retryable_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost:
        _validate_authority(authority)
        _validate_failure_code(failure_code)
        return await self._run(
            lambda connection: self._retryable(connection, authority, failure_code)
        )

    async def finalize_terminal_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost:
        _validate_authority(authority)
        _validate_failure_code(failure_code)

        async def operation(
            connection: _Connection,
        ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost:
            pair = await _owner(connection, authority)
            if pair is None:
                return ClaimLost(authority)
            job, old = pair
            if _replay(job, old, failure_code, terminal=True):
                return AttemptResolutionAlreadyApplied(authority, old.status, failure_code)
            if not await _eligible(connection, job, old):
                return ClaimLost(authority)
            changed = await _resolve(
                connection, job, old, AttemptStatus.TERMINAL_FAILURE, failure_code.value
            )
            if changed is None:
                return ClaimLost(authority)
            await _transition_job(connection, job, changed, AnalysisJobState.FAILED)
            return AttemptFailed(authority, failure_code)

        return await self._run(operation)

    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary:
        if not isinstance(batch_size, RecoveryBatchSize):
            raise TypeError("Recovery batch must be a RecoveryBatchSize.")

        async def operation(connection: _Connection) -> RecoverySummary:
            selection_time = await _clock(connection)

            def selection(rows: list[tuple[object, ...]]) -> list[_Job]:
                if len(rows) > batch_size.value:
                    _invalid()
                jobs = [_decode_job(row) for row in rows]
                if len({job.payload.job_id for job in jobs}) != len(jobs):
                    _invalid()
                return jobs

            jobs = await _query(
                connection,
                _RECOVERY,
                ("RUNNING", "ACTIVE", selection_time, batch_size.value),
                selection,
            )
            requeued = exhausted = 0
            for job in jobs:
                old = await _read_attempt(connection, job, job.count, skip_locked=True)
                if old is None:
                    continue
                observed = await _clock(connection)
                if (
                    job.state is not AnalysisJobState.RUNNING
                    or old.status is not AttemptStatus.ACTIVE
                    or _instant_key(_expiry(old)) > _instant_key(observed)
                ):
                    continue
                at_limit = job.count == job.payload.attempt_limit
                status = AttemptStatus.EXHAUSTED if at_limit else AttemptStatus.INTERRUPTED
                changed = await _resolve(connection, job, old, status, None, recovery=True)
                if changed is None:
                    continue
                await _transition_job(
                    connection,
                    job,
                    changed,
                    AnalysisJobState.FAILED if at_limit else AnalysisJobState.QUEUED,
                )
                exhausted += int(at_limit)
                requeued += int(not at_limit)
            return RecoverySummary(requeued, exhausted)

        return await self._run(operation)

    async def _run[T](self, operation: Callable[[_Connection], Awaitable[T]]) -> T:
        # Classify only persistence boundaries. In particular this is not an
        # outer driver-error handler that could translate factory exceptions.
        context = _io_call(lambda: self._pool.connection(timeout_seconds=self._acquire))
        connection = await _enter(context)
        primary: BaseException | None = None
        outcome: T | None = None
        try:
            transaction = _io_call(connection.transaction)
            await _enter(transaction)
            transaction_primary: BaseException | None = None
            try:
                for setting, value in (
                    ("statement_timeout", self._operation_timeout_ms),
                    ("lock_timeout", self._lock_timeout_ms),
                ):
                    cursor = await _execute(connection, _SETTINGS, (setting, str(value), True))
                    await _close(cursor, None)
                outcome = await operation(connection)
            except BaseException as error:
                transaction_primary = error
            await _exit(transaction, transaction_primary)
        except BaseException as error:
            primary = error
        await _exit(context, primary)
        if outcome is None:
            _invalid()
        return outcome

    async def _claim(
        self, connection: _Connection, slot: WorkerSlotId, duration: LeaseDuration
    ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable:
        locator = await _query(connection, _LOCATOR, ("ACTIVE", slot.value), _decode_locator)
        if locator is not None:
            job_id, number = locator
            observed_job = await _read_job(connection, job_id, required=True)
            if observed_job is None:
                _invalid()
            job = observed_job

            def locator_check(attempt: _Attempt) -> None:
                if attempt.authority.worker_slot_id.value != slot.value:
                    _invalid()
                if number == job.count and attempt.status is not AttemptStatus.ACTIVE:
                    _unavailable()

            old = await _read_attempt(connection, job, number, check=locator_check)
            if old is None:
                _invalid()
            if number < job.count:

                def replacement_check(attempt: _Attempt) -> None:
                    _unavailable()

                await _read_attempt(connection, job, job.count, check=replacement_check)
                _invalid()
            if job.state is AnalysisJobState.CANCELLED:
                return WorkerSlotUnavailable(slot)
            observed = await _clock(connection)
            if _instant_key(_expiry(old)) <= _instant_key(observed):
                return WorkerSlotUnavailable(slot)
            return _claim_value(job, old)

        candidate = await _query(connection, _CANDIDATE, ("QUEUED",), _optional_job)
        if candidate is None:
            return NoJobAvailable()
        job = candidate
        if job.state is not AnalysisJobState.QUEUED or job.count >= job.payload.attempt_limit:
            _invalid()
        if job.count:
            await _read_attempt(connection, job, job.count)

        def no_active(rows: list[tuple[object, ...]]) -> None:
            for row in rows:
                _pair(job, _decode_attempt(row))
            if rows:
                _invalid()

        await _query(connection, _ACTIVE, (job.payload.job_id, "ACTIVE"), no_active)
        lease = self._factory()  # No persistence handler surrounds user code.
        if type(lease) is not UUID:
            if (
                inspect.iscoroutine(lease)
                and inspect.getcoroutinestate(lease) == inspect.CORO_CREATED
            ):
                lease.close()
            raise TypeError("Lease UUID factory must return an exact UUID.")
        if lease.int == 0:
            raise ValueError("Lease UUID factory must not return a nil UUID.")
        authority = AttemptAuthority(job.payload.job_id, job.count + 1, slot, LeaseId(lease))
        new_job = await _query(
            connection,
            _CLAIM_JOB,
            ("RUNNING", job.payload.job_id, "QUEUED", job.count),
            lambda rows: _job_returned(rows, job, AnalysisJobState.RUNNING, job.count + 1),
        )

        def inserted(rows: list[tuple[object, ...]]) -> _Attempt:
            attempt = _required_attempt(rows)
            _pair(new_job, attempt)
            if attempt.authority != authority or attempt.status is not AttemptStatus.ACTIVE:
                _invalid()
            if _instant_key(attempt.started) != _instant_key(attempt.heartbeat):
                _invalid()
            _duration(attempt, duration)
            return attempt

        attempt = await _query(
            connection,
            _INSERT,
            (*_fence(authority), "ACTIVE", _interval(duration)),
            inserted,
            fresh_insert=True,
        )
        return _claim_value(new_job, attempt, authority)

    async def _retryable(
        self, connection: _Connection, authority: AttemptAuthority, code: AttemptFailureCode
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost:
        pair = await _owner(connection, authority)
        if pair is None:
            return ClaimLost(authority)
        job, old = pair
        if _replay(job, old, code, terminal=False):
            return AttemptResolutionAlreadyApplied(authority, old.status, code)
        if not await _eligible(connection, job, old):
            return ClaimLost(authority)
        at_limit = job.count == job.payload.attempt_limit
        status = AttemptStatus.EXHAUSTED if at_limit else AttemptStatus.RETRYABLE_FAILURE
        changed = await _resolve(connection, job, old, status, code.value)
        if changed is None:
            return ClaimLost(authority)
        await _transition_job(
            connection,
            job,
            changed,
            AnalysisJobState.FAILED if at_limit else AnalysisJobState.QUEUED,
        )
        if at_limit:
            return AttemptExhausted(authority, job.payload.attempt_limit, code)
        return AttemptRequeued(authority, job.payload.attempt_limit)


def _known_async_factory_target(surface: object) -> bool:
    # Retain objects while hashing only owned IDs, never supplied objects.
    partials: list[object] = []
    visited: set[int] = set()
    while True:
        kind = type(surface)
        if kind is FunctionType:
            return _native_async_function(surface)
        if kind is MethodType:
            surface = _native_read(_METHOD_FUNCTION, surface)
        elif kind is staticmethod or kind is classmethod:
            target = _native_read(
                _STATIC_FUNCTION if kind is staticmethod else _CLASS_FUNCTION, surface
            )
            if type(target) is MethodType:
                target = _native_read(_METHOD_FUNCTION, target)
            return type(target) is FunctionType and _native_async_function(target)
        elif kind is partial:
            identity = id(surface)
            if identity in visited:
                return False
            visited.add(identity)
            partials.append(surface)
            surface = _native_read(_PARTIAL_FUNCTION, surface)
        else:
            return False


def _invalid() -> Never:
    raise AnalysisJobWorkerPersistenceInvariantError(_INVARIANT)


def _unavailable() -> Never:
    raise AnalysisJobWorkerPersistenceUnavailable(_UNAVAILABLE)


def _classified(error: BaseException) -> BaseException:
    if isinstance(error, (PoolTimeout, OperationalError, InterfaceError)):
        return AnalysisJobWorkerPersistenceUnavailable(_UNAVAILABLE)
    return error


def _finish(primary: BaseException | None, cleanup: BaseException | None) -> None:
    if primary is not None:
        if cleanup is not None and (
            not isinstance(cleanup, Exception) or isinstance(cleanup, MemoryError)
        ):
            raise cleanup from primary
        raise primary
    if cleanup is not None:
        raise _classified(cleanup)


def _io_call[T](action: Callable[[], T]) -> T:
    primary: BaseException | None = None
    result: T | None = None
    try:
        result = action()
    except BaseException as error:
        primary = _classified(error)
    if primary is not None:
        raise primary
    if result is None:
        _invalid()
    return result


async def _enter[T](context: AbstractAsyncContextManager[T]) -> T:
    primary: BaseException | None = None
    values: list[T] = []
    try:
        values.append(await context.__aenter__())
    except BaseException as error:
        primary = _classified(error)
    if primary is not None:
        raise primary
    # Transaction context entry may legitimately return None.
    return values[0]


async def _exit(
    context: AbstractAsyncContextManager[object], primary: BaseException | None
) -> None:
    cleanup: BaseException | None = None
    try:
        await context.__aexit__(
            type(primary) if primary is not None else None,
            primary,
            primary.__traceback__ if primary is not None else None,
        )
    except BaseException as error:
        cleanup = error
    _finish(primary, cleanup)


async def _execute(
    connection: _Connection, sql: str, params: tuple[object, ...], *, fresh_insert: bool = False
) -> _Cursor:
    primary: BaseException | None = None
    result: _Cursor | None = None
    try:
        result = await connection.execute(sql, params)
    except BaseException as error:
        if (
            fresh_insert
            and isinstance(error, UniqueViolation)
            and error.diag.constraint_name == _SLOT_INDEX
        ):
            primary = AnalysisJobWorkerPersistenceUnavailable(_UNAVAILABLE)
        else:
            primary = _classified(error)
    if primary is not None:
        raise primary
    if result is None:
        _invalid()
    return result


async def _close(cursor: _Cursor, primary: BaseException | None) -> None:
    cleanup: BaseException | None = None
    try:
        await cursor.close()
    except BaseException as error:
        cleanup = error
    _finish(primary, cleanup)


async def _query[T](
    connection: _Connection,
    sql: str,
    params: tuple[object, ...],
    decode: Callable[[list[tuple[object, ...]]], T],
    *,
    fresh_insert: bool = False,
) -> T:
    cursor = await _execute(connection, sql, params, fresh_insert=fresh_insert)
    primary: BaseException | None = None
    values: list[T] = []
    try:
        rows: list[tuple[object, ...]] | None = None
        try:
            rows = await cursor.fetchall()
        except BaseException as error:
            primary = _classified(error)
        if primary is not None:
            raise primary
        if type(rows) is not list or any(type(row) is not tuple for row in rows):
            _invalid()
        values.append(decode(rows))
    except BaseException as error:
        primary = error
    await _close(cursor, primary)
    # None is a valid optional observation/setting decoder result.
    return values[0]


def _acquisition_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Connection acquisition timeout must be numeric.")
    normalized: float | None = None
    try:
        normalized = float(value)
    except OverflowError:
        pass
    if normalized is None or not isfinite(normalized) or normalized <= 0:
        raise ValueError("Connection acquisition timeout must be finite and positive.")
    return normalized


def _millisecond_timeout(value: int) -> None:
    if type(value) is not int:
        raise TypeError("PostgreSQL millisecond timeout must be an integer.")
    if not 1 <= value <= 2_147_483_647:
        raise ValueError("PostgreSQL millisecond timeout is outside the approved range.")


def _singleton(
    rows: list[tuple[object, ...]], *, required: bool = False
) -> tuple[object, ...] | None:
    if len(rows) > 1 or (required and not rows):
        _invalid()
    return rows[0] if rows else None


def _uuid(value: object) -> UUID:
    if type(value) is not UUID or value.int == 0:
        _invalid()
    return value


def _integer(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        _invalid()
    return value


def _text(value: object) -> str:
    if type(value) is not str:
        _invalid()
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        _invalid()
    valid = False
    try:
        _validate_datetime(value)
        valid = True
    except (TypeError, ValueError):
        pass
    if not valid:
        _invalid()
    return value


def _decode_job(row: tuple[object, ...]) -> _Job:
    if len(row) != 10:
        _invalid()
    job_id = _uuid(row[0])
    version = _text(row[5])
    state = _text(row[6])
    if state not in AnalysisJobState.__members__:
        _invalid()
    limit, count = _integer(row[7], positive=True), _integer(row[8])
    if count > limit or (state in ("RUNNING", "FAILED") and count == 0):
        _invalid()
    payload: ClaimableAnalysisJob | None = None
    video, player, reference, callback = (_text(row[i]) for i in (1, 2, 3, 4))
    accepted = _timestamp(row[9])
    try:
        payload = ClaimableAnalysisJob(
            job_id, video, player, reference, callback, version, limit, accepted
        )
    except (TypeError, ValueError):
        pass
    if payload is None:
        _invalid()
    return _Job(payload, AnalysisJobState(state), count)


def _optional_job(rows: list[tuple[object, ...]]) -> _Job | None:
    row = _singleton(rows)
    return None if row is None else _decode_job(row)


def _decode_attempt(row: tuple[object, ...]) -> _Attempt:
    if len(row) != 10:
        _invalid()
    authority = AttemptAuthority(
        _uuid(row[0]),
        _integer(row[1], positive=True),
        WorkerSlotId(_uuid(row[2])),
        LeaseId(_uuid(row[3])),
    )
    status_text = _text(row[4])
    if status_text not in AttemptStatus.__members__:
        _invalid()
    status = AttemptStatus(status_text)
    started, heartbeat = _timestamp(row[5]), _timestamp(row[6])
    expiry = None if row[7] is None else _timestamp(row[7])
    finish = None if row[8] is None else _timestamp(row[8])
    code = None if row[9] is None else _text(row[9])
    valid_code = code is None
    if code is not None:
        try:
            AttemptFailureCode(code)
            valid_code = True
        except (TypeError, ValueError):
            pass
    if not valid_code or _instant_key(heartbeat) < _instant_key(started):
        _invalid()
    if status is AttemptStatus.ACTIVE:
        if (
            expiry is None
            or finish is not None
            or code is not None
            or _instant_key(expiry) <= _instant_key(heartbeat)
        ):
            _invalid()
    elif (
        expiry is not None
        or finish is None
        or _instant_key(finish) < _instant_key(heartbeat)
        or (status is AttemptStatus.INTERRUPTED and code is not None)
        or (
            status in (AttemptStatus.RETRYABLE_FAILURE, AttemptStatus.TERMINAL_FAILURE)
            and code is None
        )
    ):
        _invalid()
    return _Attempt(authority, status, started, heartbeat, expiry, finish, code)


def _optional_attempt(rows: list[tuple[object, ...]]) -> _Attempt | None:
    row = _singleton(rows)
    return None if row is None else _decode_attempt(row)


def _required_attempt(rows: list[tuple[object, ...]]) -> _Attempt:
    row = _singleton(rows, required=True)
    if row is None:
        _invalid()
    return _decode_attempt(row)


def _decode_locator(rows: list[tuple[object, ...]]) -> tuple[UUID, int] | None:
    row = _singleton(rows)
    if row is None:
        return None
    if len(row) != 2:
        _invalid()
    return _uuid(row[0]), _integer(row[1], positive=True)


def _pair(job: _Job, attempt: _Attempt) -> None:
    _job_state(job)
    number, limit = attempt.authority.attempt_number, job.payload.attempt_limit
    if attempt.authority.job_id != job.payload.job_id or not number <= job.count <= limit:
        _invalid()

    retry = attempt.status in (AttemptStatus.INTERRUPTED, AttemptStatus.RETRYABLE_FAILURE)
    if (retry and number >= limit) or (
        attempt.status is AttemptStatus.EXHAUSTED and number != limit
    ):
        _invalid()
    if number < job.count:
        if not retry:
            _invalid()
    elif attempt.status is AttemptStatus.ACTIVE:
        if job.state not in (AnalysisJobState.RUNNING, AnalysisJobState.CANCELLED):
            _invalid()
    elif retry:
        if job.state not in (AnalysisJobState.QUEUED, AnalysisJobState.CANCELLED):
            _invalid()
    elif job.state is not AnalysisJobState.FAILED:
        _invalid()


def _job_state(job: _Job) -> None:
    if job.state is AnalysisJobState.QUEUED and job.count >= job.payload.attempt_limit:
        _invalid()


async def _read_job(
    connection: _Connection, job_id: UUID, *, required: bool = False
) -> _Job | None:
    def decode(rows: list[tuple[object, ...]]) -> _Job | None:
        row = _singleton(rows, required=required)
        job = None if row is None else _decode_job(row)
        if job is not None and job.payload.job_id != job_id:
            _invalid()
        return job

    return await _query(connection, _JOB_READ, (job_id,), decode)


async def _read_attempt(
    connection: _Connection,
    job: _Job,
    number: int,
    *,
    skip_locked: bool = False,
    check: Callable[[_Attempt], None] | None = None,
) -> _Attempt | None:
    def decode(rows: list[tuple[object, ...]]) -> _Attempt | None:
        row = _singleton(rows, required=not skip_locked)
        if row is None:
            return None
        attempt = _decode_attempt(row)
        if attempt.authority.attempt_number != number:
            _invalid()
        _pair(job, attempt)
        if check is not None:
            check(attempt)
        return attempt

    return await _query(
        connection,
        _ATTEMPT_READ + (" SKIP LOCKED" if skip_locked else ""),
        (job.payload.job_id, number),
        decode,
    )


async def _clock(connection: _Connection) -> datetime:
    def decode(rows: list[tuple[object, ...]]) -> datetime:
        row = _singleton(rows, required=True)
        if row is None or len(row) != 1:
            _invalid()
        return _timestamp(row[0])

    return await _query(connection, _CLOCK, (), decode)


async def _owner(
    connection: _Connection, authority: AttemptAuthority
) -> tuple[_Job, _Attempt] | None:
    job = await _read_job(connection, authority.job_id)
    if job is not None:
        _job_state(job)
    if job is None or job.count == 0:
        return None
    attempt = await _read_attempt(connection, job, job.count)
    if attempt is None:
        _invalid()
    if attempt.authority != authority:
        return None
    return job, attempt


async def _eligible(connection: _Connection, job: _Job, attempt: _Attempt) -> bool:
    if job.state is not AnalysisJobState.RUNNING or attempt.status is not AttemptStatus.ACTIVE:
        return False
    observed = await _clock(connection)
    return _instant_key(_expiry(attempt)) > _instant_key(observed)


def _replay(job: _Job, attempt: _Attempt, code: AttemptFailureCode, *, terminal: bool) -> bool:
    statuses = (
        (AttemptStatus.TERMINAL_FAILURE,)
        if terminal
        else (AttemptStatus.RETRYABLE_FAILURE, AttemptStatus.EXHAUSTED)
    )
    expected_state = (
        AnalysisJobState.QUEUED
        if attempt.status is AttemptStatus.RETRYABLE_FAILURE
        else AnalysisJobState.FAILED
    )
    return attempt.status in statuses and attempt.code == code.value and job.state is expected_state


def _fence(authority: AttemptAuthority) -> tuple[object, ...]:
    return (
        authority.job_id,
        authority.attempt_number,
        authority.worker_slot_id.value,
        authority.lease_id.value,
    )


def _expiry(attempt: _Attempt) -> datetime:
    if attempt.expiry is None:
        _invalid()
    return attempt.expiry


def _interval(duration: LeaseDuration) -> str:
    value = duration.value
    microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    return f"{microseconds} microseconds"


def _duration(attempt: _Attempt, duration: LeaseDuration) -> None:
    if _instant_key(_expiry(attempt)) - _instant_key(attempt.heartbeat) != duration.value:
        _invalid()


def _preserved_attempt(old: _Attempt, new: _Attempt, *, heartbeat: bool = True) -> None:
    if (
        old.authority != new.authority
        or _instant_key(old.started) != _instant_key(new.started)
        or (heartbeat and _instant_key(old.heartbeat) != _instant_key(new.heartbeat))
    ):
        _invalid()


def _job_returned(
    rows: list[tuple[object, ...]], old: _Job, state: AnalysisJobState, count: int
) -> _Job:
    row = _singleton(rows, required=True)
    if row is None:
        _invalid()
    new = _decode_job(row)
    p, q = old.payload, new.payload
    if (
        p.job_id != q.job_id
        or p.video_id != q.video_id
        or p.player_id != q.player_id
        or p.video_reference != q.video_reference
        or p.callback_url != q.callback_url
        or p.analysis_version != q.analysis_version
        or p.attempt_limit != q.attempt_limit
        or _instant_key(p.accepted_at) != _instant_key(q.accepted_at)
        or new.state is not state
        or new.count != count
    ):
        _invalid()
    return new


async def _resolve(
    connection: _Connection,
    job: _Job,
    old: _Attempt,
    status: AttemptStatus,
    code: str | None,
    *,
    recovery: bool = False,
) -> _Attempt | None:
    def decode(rows: list[tuple[object, ...]]) -> _Attempt | None:
        new = _optional_attempt(rows)
        if new is None:
            return None
        _preserved_attempt(old, new)
        if new.status is not status or new.code != code or new.finished is None:
            _invalid()
        finish, expiry = _instant_key(new.finished), _instant_key(_expiry(old))
        if (recovery and finish < expiry) or (not recovery and finish >= expiry):
            _invalid()
        return new

    return await _query(
        connection,
        _RESOLVE_EXPIRED if recovery else _RESOLVE_OWNER,
        (status.value, code, *_fence(old.authority), "ACTIVE", "RUNNING"),
        decode,
    )


async def _transition_job(
    connection: _Connection, old: _Job, attempt: _Attempt, state: AnalysisJobState
) -> _Job:
    def decode(rows: list[tuple[object, ...]]) -> _Job:
        job = _job_returned(rows, old, state, old.count)
        _pair(job, attempt)
        return job

    return await _query(
        connection,
        _RESOLVE_JOB,
        (
            state.value,
            old.payload.job_id,
            old.count,
            "RUNNING",
            attempt.authority.worker_slot_id.value,
            attempt.authority.lease_id.value,
            attempt.status.value,
        ),
        decode,
    )


def _claim_value(
    job: _Job, attempt: _Attempt, authority: AttemptAuthority | None = None
) -> WorkerClaim:
    return WorkerClaim(
        authority if authority is not None else attempt.authority,
        job.payload,
        attempt.started,
        attempt.heartbeat,
        _expiry(attempt),
    )
