"""Inactive, transaction-owning Worker persistence boundary (Planning-F/H).

Rows are diagnostic observations, not authority until association and committed
consistency have been checked. SQL rechecks eligibility with a separately captured
mutation clock. Nothing here composes a pool, starts a worker, or executes analysis.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from types import (
    CodeType,
    FunctionType,
    GetSetDescriptorType,
    MappingProxyType,
    MemberDescriptorType,
    MethodType,
)
from typing import Never
from uuid import UUID

from psycopg.errors import UniqueViolation

from adapters._analysis_job_persistence import (
    _A,
    _J,
    _UNAVAILABLE,
    _acquisition_timeout,
    _Attempt,
    _clock,
    _decode_attempt,
    _decode_job,
    _eligible,
    _equivalent,
    _execute,
    _expiry,
    _fence,
    _insert_terminal_pair,
    _integer,
    _invalid,
    _job_returned,
    _job_state,
    _millisecond_timeout,
    _optional_attempt,
    _pair,
    _preserved_attempt,
    _query,
    _read_attempt,
    _read_rows,
    _resolve,
    _run_transaction,
    _singleton,
    _terminal_rows,
    _transition_job,
    _uuid,
)
from adapters._analysis_job_persistence import (
    _Connection as _Connection,
)
from adapters._analysis_job_persistence import (
    _Cursor as _Cursor,
)
from adapters._analysis_job_persistence import (
    _Job as _Job,
)
from adapters._analysis_job_persistence import (
    _Pool as _Pool,
)
from adapters._analysis_job_persistence import (
    _read_job as _read_job,
)
from domain.analysis_job import AnalysisJobState
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    AttemptStatus,
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
    FailureCallbackSnapshotV1Builder,
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

_SLOT_INDEX = "analysis_job_attempts_active_worker_slot_id_idx"
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
_RECOVERY = f"""SELECT {", ".join("j." + column.strip() for column in _J.split(","))}
FROM analysis_jobs AS j JOIN analysis_job_attempts AS a
    ON a.job_id = j.job_id AND a.attempt_number = j.attempt_count
WHERE j.state = %s AND a.status = %s AND a.lease_expires_at <= %s
ORDER BY j.job_id ASC LIMIT %s FOR UPDATE OF j SKIP LOCKED"""


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
        failure_snapshot_builder: FailureCallbackSnapshotV1Builder,
    ) -> None:
        acquire = _acquisition_timeout(connection_acquire_timeout_seconds)
        _millisecond_timeout(operation_timeout_ms)
        _millisecond_timeout(lock_timeout_ms)
        if operation_timeout_ms <= lock_timeout_ms:
            raise ValueError("Operation timeout must exceed lock timeout.")
        if not callable(lease_id_factory):
            raise TypeError("Lease UUID factory must be callable.")
        if not callable(failure_snapshot_builder):
            raise TypeError("Failure snapshot builder must be callable.")
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
        self._failure_snapshot_builder = failure_snapshot_builder

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
            pair = await _owner(connection, authority, inspect_terminal=True)
            if pair is None:
                return ClaimLost(authority)
            job, old = pair
            if _replay(job, old, failure_code, terminal=True):
                expected = self._failure_snapshot_builder(
                    job_id=job.payload.job_id,
                    video_id=job.payload.video_id,
                    player_id=job.payload.player_id,
                    safe_code=failure_code.value,
                )
                if await _equivalent(
                    connection, job.payload.job_id, callback_json=expected, result_json=None
                ):
                    return AttemptResolutionAlreadyApplied(authority, old.status, failure_code)
                return ClaimLost(authority)
            if not await _eligible(connection, job, old):
                return ClaimLost(authority)
            await _terminal_rows(connection, job, old, require_absent=True)
            changed = await _resolve(
                connection, job, old, AttemptStatus.TERMINAL_FAILURE, failure_code.value
            )
            if changed is None:
                return ClaimLost(authority)
            terminal_job = await _transition_job(connection, job, changed, AnalysisJobState.FAILED)
            await _insert_terminal_pair(
                connection,
                terminal_job,
                changed,
                result_json=None,
                callback_json=self._failure_snapshot_builder(
                    job_id=job.payload.job_id,
                    video_id=job.payload.video_id,
                    player_id=job.payload.player_id,
                    safe_code=failure_code.value,
                ),
                model_version=None,
                processing_duration_ms=None,
                safe_failure_code=failure_code.value,
                failure_origin="CLASSIFIED_TERMINAL",
            )
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
                if at_limit:
                    await _terminal_rows(connection, job, old, require_absent=True)
                status = AttemptStatus.EXHAUSTED if at_limit else AttemptStatus.INTERRUPTED
                changed = await _resolve(connection, job, old, status, None, recovery=True)
                if changed is None:
                    continue
                updated_job = await _transition_job(
                    connection,
                    job,
                    changed,
                    AnalysisJobState.FAILED if at_limit else AnalysisJobState.QUEUED,
                )
                if at_limit:
                    await _insert_terminal_pair(
                        connection,
                        updated_job,
                        changed,
                        result_json=None,
                        callback_json=self._failure_snapshot_builder(
                            job_id=job.payload.job_id,
                            video_id=job.payload.video_id,
                            player_id=job.payload.player_id,
                            safe_code="attempts_exhausted",
                        ),
                        model_version=None,
                        processing_duration_ms=None,
                        safe_failure_code="attempts_exhausted",
                        failure_origin="EXPIRY_EXHAUSTION",
                    )
                exhausted += int(at_limit)
                requeued += int(not at_limit)
            return RecoverySummary(requeued, exhausted)

        return await self._run(operation)

    async def _run[T](self, operation: Callable[[_Connection], Awaitable[T]]) -> T:
        return await _run_transaction(
            self._pool,
            self._acquire,
            self._operation_timeout_ms,
            self._lock_timeout_ms,
            operation,
        )

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
                if (
                    number == job.count
                    and attempt.status is not AttemptStatus.ACTIVE
                    and job.state not in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED)
                ):
                    _unavailable()

            old = await _read_attempt(connection, job, number, check=locator_check)
            if old is None:
                _invalid()
            if number < job.count:
                replacement = await _read_attempt(connection, job, job.count)
                if replacement is None:
                    _invalid()
                if job.state in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED):
                    await _terminal_rows(connection, job, replacement)
                _unavailable()
            if old.status is not AttemptStatus.ACTIVE:
                if job.state in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED):
                    await _terminal_rows(connection, job, old)
                _unavailable()
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

        attempt = await _query_fresh_insert(
            connection,
            _INSERT,
            (*_fence(authority), "ACTIVE", _interval(duration)),
            inserted,
        )
        return _claim_value(new_job, attempt, authority)

    async def _retryable(
        self, connection: _Connection, authority: AttemptAuthority, code: AttemptFailureCode
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost:
        pair = await _owner(connection, authority, inspect_terminal=True)
        if pair is None:
            return ClaimLost(authority)
        job, old = pair
        if _replay(job, old, code, terminal=False):
            if old.status is AttemptStatus.RETRYABLE_FAILURE:
                return AttemptResolutionAlreadyApplied(authority, old.status, code)
            expected = self._failure_snapshot_builder(
                job_id=job.payload.job_id,
                video_id=job.payload.video_id,
                player_id=job.payload.player_id,
                safe_code="attempts_exhausted",
            )
            if await _equivalent(
                connection, job.payload.job_id, callback_json=expected, result_json=None
            ):
                return AttemptResolutionAlreadyApplied(authority, old.status, code)
            return ClaimLost(authority)
        if not await _eligible(connection, job, old):
            return ClaimLost(authority)
        at_limit = job.count == job.payload.attempt_limit
        if at_limit:
            await _terminal_rows(connection, job, old, require_absent=True)
        status = AttemptStatus.EXHAUSTED if at_limit else AttemptStatus.RETRYABLE_FAILURE
        changed = await _resolve(connection, job, old, status, code.value)
        if changed is None:
            return ClaimLost(authority)
        updated_job = await _transition_job(
            connection,
            job,
            changed,
            AnalysisJobState.FAILED if at_limit else AnalysisJobState.QUEUED,
        )
        if at_limit:
            await _insert_terminal_pair(
                connection,
                updated_job,
                changed,
                result_json=None,
                callback_json=self._failure_snapshot_builder(
                    job_id=job.payload.job_id,
                    video_id=job.payload.video_id,
                    player_id=job.payload.player_id,
                    safe_code="attempts_exhausted",
                ),
                model_version=None,
                processing_duration_ms=None,
                safe_failure_code="attempts_exhausted",
                failure_origin="CLASSIFIED_EXHAUSTION",
            )
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


def _unavailable() -> Never:
    raise AnalysisJobWorkerPersistenceUnavailable(_UNAVAILABLE)


def _optional_job(rows: list[tuple[object, ...]]) -> _Job | None:
    row = _singleton(rows)
    return None if row is None else _decode_job(row)


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


async def _owner(
    connection: _Connection, authority: AttemptAuthority, *, inspect_terminal: bool = False
) -> tuple[_Job, _Attempt] | None:
    job = await _read_job(connection, authority.job_id)
    if job is not None:
        _job_state(job)
    if job is None or job.count == 0:
        return None
    attempt = await _read_attempt(connection, job, job.count)
    if attempt is None:
        _invalid()
    if inspect_terminal and job.state in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED):
        await _terminal_rows(connection, job, attempt)
    if attempt.authority != authority:
        return None
    return job, attempt


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


def _interval(duration: LeaseDuration) -> str:
    value = duration.value
    microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    return f"{microseconds} microseconds"


def _duration(attempt: _Attempt, duration: LeaseDuration) -> None:
    if _instant_key(_expiry(attempt)) - _instant_key(attempt.heartbeat) != duration.value:
        _invalid()


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


async def _query_fresh_insert[T](
    connection: _Connection,
    sql: str,
    params: tuple[object, ...],
    decode: Callable[[list[tuple[object, ...]]], T],
) -> T:
    # Only a conflict at this insert execution has worker-slot semantics.
    conflict: UniqueViolation | None = None
    cursor = None
    try:
        cursor = await _execute(connection, sql, params)
    except UniqueViolation as error:
        conflict = error
    if conflict is not None:
        if conflict.diag.constraint_name == _SLOT_INDEX:
            _unavailable()
        raise conflict
    if cursor is None:
        _invalid()
    return await _read_rows(cursor, decode)
