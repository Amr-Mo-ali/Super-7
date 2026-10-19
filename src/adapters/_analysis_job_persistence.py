"""Shared AnalysisJob PostgreSQL row, fence, and transaction primitives.

Both worker failure/retry/recovery and success finalization use these exact
database checks. Operation selection and worker-only claim policy stay local.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite
from typing import Never, Protocol
from uuid import UUID

from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from domain.analysis_job import AnalysisJobState, _validate_datetime
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    AttemptStatus,
    ClaimableAnalysisJob,
    LeaseId,
    WorkerSlotId,
    _instant_key,
)
from services.analysis_job_finalization import (
    _AMBIGUOUS_RESULT,
    _CALLBACK_FIELDS,
    _COMMON_RESULT,
    _COMPLETED_RESULT,
    _NONCOMPLETED_RESULT,
    _strict_json_object,
    _validate_json_value,
    _validate_retained_result_fields,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AnalysisJobWorkerPersistenceUnavailable,
)

_UNAVAILABLE = "PostgreSQL AnalysisJob worker persistence is unavailable."

_INVARIANT = "PostgreSQL AnalysisJob worker persistence invariant failed."

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

_R = (
    "job_id, attempt_number, terminal_kind, finalized_at, terminal_schema_version, "
    "result_schema_version, result_json, model_version, processing_duration_ms, "
    "safe_failure_code, failure_origin"
)

_O = (
    "callback_event_id, job_id, payload_schema_version, payload_json, delivery_state, "
    "created_at, next_attempt_at"
)

_JOB_READ = f"SELECT {_J} FROM analysis_jobs WHERE job_id = %s FOR UPDATE"

_ATTEMPT_READ = (
    f"SELECT {_A} FROM analysis_job_attempts WHERE job_id = %s AND attempt_number = %s FOR UPDATE"
)

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

_TERMINAL_READ = f"SELECT {_R} FROM analysis_job_terminals WHERE job_id = %s"

_OUTBOX_READ = f"SELECT {_O} FROM analysis_callback_outbox WHERE job_id = %s"

_TERMINAL_INSERT = f"""INSERT INTO analysis_job_terminals (
    {_R}
) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
RETURNING {_R}"""

_OUTBOX_INSERT = f"""INSERT INTO analysis_callback_outbox (
    job_id, payload_schema_version, payload_json, delivery_state, created_at, next_attempt_at
) VALUES (%s, %s, %s::jsonb, %s, %s, %s)
RETURNING {_O}"""

_FAILURE_Q = """SELECT o.payload_json = %s::jsonb AS equivalent
FROM analysis_job_terminals AS r JOIN analysis_callback_outbox AS o ON o.job_id = r.job_id
WHERE r.job_id = %s"""

_SUCCESS_Q = """SELECT
    (r.result_json - 'processing_time_ms') = (%s::jsonb - 'processing_time_ms')
    AND o.payload_json = %s::jsonb AS equivalent
FROM analysis_job_terminals AS r JOIN analysis_callback_outbox AS o ON o.job_id = r.job_id
WHERE r.job_id = %s"""


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


@dataclass(frozen=True, slots=True)
class _Terminal:
    job_id: UUID
    attempt_number: int
    kind: str
    finalized_at: datetime
    schema_version: str
    result_schema_version: str | None
    result_json: dict[str, object] | None
    model_version: str | None
    processing_duration_ms: int | None
    safe_failure_code: str | None
    failure_origin: str | None


@dataclass(frozen=True, slots=True)
class _Outbox:
    event_id: UUID
    job_id: UUID
    schema_version: str
    payload_json: dict[str, object]
    delivery_state: str
    created_at: datetime
    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class _TerminalPair:
    terminal: _Terminal
    outbox: _Outbox


def _invalid() -> Never:
    raise AnalysisJobWorkerPersistenceInvariantError(_INVARIANT)


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


async def _execute(connection: _Connection, sql: str, params: tuple[object, ...]) -> _Cursor:
    primary: BaseException | None = None
    result: _Cursor | None = None
    try:
        result = await connection.execute(sql, params)
    except BaseException as error:
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
) -> T:
    cursor = await _execute(connection, sql, params)
    return await _read_rows(cursor, decode)


async def _read_rows[T](cursor: _Cursor, decode: Callable[[list[tuple[object, ...]]], T]) -> T:
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
    if count > limit or (state in ("RUNNING", "COMPLETED", "FAILED") and count == 0):
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
        or (status in (AttemptStatus.INTERRUPTED, AttemptStatus.SUCCEEDED) and code is not None)
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


def _json_object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _invalid()
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            _invalid()
        try:
            _validate_json_value(key)
            _validate_json_value(item)
        except ValueError:
            _invalid()
        result[key] = item
    return result


def _returned_jsonb_equal(left: object, right: object) -> bool:
    """Check INSERT RETURNING evidence only; replay equality remains PostgreSQL Q."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _returned_jsonb_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _returned_jsonb_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if type(left) in (int, float, Decimal) and type(right) in (int, float, Decimal):
        return Decimal(str(left)) == Decimal(str(right))
    return type(left) is type(right) and left == right


def _nullable_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _decode_terminal(row: tuple[object, ...]) -> _Terminal:
    if len(row) != 11:
        _invalid()
    job_id, number = _uuid(row[0]), _integer(row[1], positive=True)
    kind, finished, schema = _text(row[2]), _timestamp(row[3]), _text(row[4])
    result_schema = _nullable_text(row[5])
    result_json = None if row[6] is None else _json_object(row[6])
    model = _nullable_text(row[7])
    duration = None if row[8] is None else _integer(row[8])
    code, origin = _nullable_text(row[9]), _nullable_text(row[10])
    if schema != "analysis_terminal_v1":
        _invalid()
    if kind == "COMPLETED":
        if (
            result_schema != "analysis_result_v1"
            or result_json is None
            or model is None
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model)
            or duration is None
            or duration < 0
            or code is not None
            or origin is not None
        ):
            _invalid()
    elif kind == "FAILED":
        if (
            result_schema is not None
            or result_json is not None
            or model is not None
            or duration is not None
            or code is None
            or origin
            not in (
                "CLASSIFIED_TERMINAL",
                "CLASSIFIED_EXHAUSTION",
                "EXPIRY_EXHAUSTION",
            )
        ):
            _invalid()
        try:
            AttemptFailureCode(code)
        except (TypeError, ValueError):
            _invalid()
    else:
        _invalid()
    return _Terminal(
        job_id,
        number,
        kind,
        finished,
        schema,
        result_schema,
        result_json,
        model,
        duration,
        code,
        origin,
    )


def _decode_outbox(row: tuple[object, ...]) -> _Outbox:
    if len(row) != 7:
        _invalid()
    event_id, job_id = _uuid(row[0]), _uuid(row[1])
    schema, payload, state = _text(row[2]), _json_object(row[3]), _text(row[4])
    created, next_at = _timestamp(row[5]), _timestamp(row[6])
    if schema != "analysis_callback_snapshot_v1" or state not in (
        "PENDING",
        "RETRYING",
        "DELIVERED",
        "EXHAUSTED",
    ):
        _invalid()
    return _Outbox(event_id, job_id, schema, payload, state, created, next_at)


def _validate_terminal_pair(job: _Job, attempt: _Attempt, pair: _TerminalPair) -> None:
    terminal, outbox = pair.terminal, pair.outbox
    payload = outbox.payload_json
    if (
        terminal.job_id != job.payload.job_id
        or terminal.attempt_number != job.count
        or attempt.authority.attempt_number != job.count
        or terminal.kind != job.state.value
        or attempt.finished is None
        or _instant_key(terminal.finalized_at) != _instant_key(attempt.finished)
        or outbox.job_id != job.payload.job_id
        or _instant_key(outbox.created_at) != _instant_key(terminal.finalized_at)
        or payload.get("request_id") != str(job.payload.job_id)
        or payload.get("video_id") != job.payload.video_id
        or payload.get("player_id") != job.payload.player_id
        or not all(isinstance(payload.get(key), dict) for key in ("summary", "ratings", "events"))
    ):
        _invalid()
    if terminal.kind == "COMPLETED":
        result = terminal.result_json
        if (
            attempt.status is not AttemptStatus.SUCCEEDED
            or result is None
            or result.get("analysis_id") != str(job.payload.job_id)
            or result.get("status")
            not in (
                "completed",
                "ambiguous_target",
                "invalid_video",
                "no_players_detected",
                "no_valid_tracks",
                "failed",
                "player_detection_completed_tracking_not_available",
            )
            or payload.keys() != _CALLBACK_FIELDS
            or payload.get("error") is not None
            or not isinstance(payload.get("detailed"), dict)
            or payload.get("status")
            != ("COMPLETED" if result.get("status") == "completed" else result.get("status"))
        ):
            _invalid()
        projected_status = result["status"]
        expected_fields = _COMMON_RESULT | (
            _COMPLETED_RESULT
            if projected_status == "completed"
            else _AMBIGUOUS_RESULT
            if projected_status == "ambiguous_target"
            else _NONCOMPLETED_RESULT
        )
        if result.keys() != expected_fields or (
            projected_status != "completed" and result.get("selected_player") is not None
        ):
            _invalid()
        versions = result.get("algorithm_versions")
        pipeline_duration = result.get("processing_time_ms")
        if result.get("status") == "completed" and (
            result.get("analysis_version") != job.payload.analysis_version
            or result.get("model_version") != terminal.model_version
            or not isinstance(versions, dict)
            or versions.get("player_detection") != terminal.model_version
            or type(pipeline_duration) is not int
            or pipeline_duration < 0
        ):
            _invalid()
        try:
            _validate_retained_result_fields(result)
        except (TypeError, ValueError):
            _invalid()
    else:
        expected_origin = (
            "CLASSIFIED_TERMINAL"
            if attempt.status is AttemptStatus.TERMINAL_FAILURE
            else "EXPIRY_EXHAUSTION"
            if attempt.status is AttemptStatus.EXHAUSTED and attempt.code is None
            else "CLASSIFIED_EXHAUSTION"
            if attempt.status is AttemptStatus.EXHAUSTED
            else None
        )
        expected_code = (
            attempt.code if expected_origin == "CLASSIFIED_TERMINAL" else "attempts_exhausted"
        )
        error = payload.get("error")
        if (
            expected_origin is None
            or terminal.failure_origin != expected_origin
            or terminal.safe_failure_code != expected_code
            or payload.keys()
            != frozenset(
                {
                    "request_id",
                    "video_id",
                    "player_id",
                    "status",
                    "summary",
                    "ratings",
                    "overall",
                    "events",
                    "error",
                }
            )
            or payload.get("status") != "failed"
            or payload.get("overall") is not None
            or not isinstance(error, dict)
            or error.keys() != {"code", "message"}
            or error.get("code") != expected_code
            or error.get("message") != "Analysis could not be completed."
        ):
            _invalid()


def _required_row(rows: list[tuple[object, ...]]) -> tuple[object, ...]:
    row = _singleton(rows, required=True)
    if row is None:
        _invalid()
    return row


async def _terminal_rows(
    connection: _Connection, job: _Job, attempt: _Attempt, *, require_absent: bool = False
) -> _TerminalPair | None:
    terminal_pair = job.state in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED)
    if not terminal_pair and not require_absent:
        return None
    terminal = await _query(
        connection,
        _TERMINAL_READ,
        (job.payload.job_id,),
        lambda rows: None if (row := _singleton(rows)) is None else _decode_terminal(row),
    )
    outbox = await _query(
        connection,
        _OUTBOX_READ,
        (job.payload.job_id,),
        lambda rows: None if (row := _singleton(rows)) is None else _decode_outbox(row),
    )
    if require_absent:
        if terminal is not None or outbox is not None:
            _invalid()
        return None
    if not terminal_pair or terminal is None or outbox is None:
        _invalid()
    pair = _TerminalPair(terminal, outbox)
    _validate_terminal_pair(job, attempt, pair)
    return pair


async def _insert_terminal_pair(
    connection: _Connection,
    job: _Job,
    attempt: _Attempt,
    *,
    result_json: bytes | None,
    callback_json: bytes,
    model_version: str | None,
    processing_duration_ms: int | None,
    safe_failure_code: str | None,
    failure_origin: str | None,
) -> _TerminalPair:
    if attempt.finished is None:
        _invalid()
    terminal = await _query(
        connection,
        _TERMINAL_INSERT,
        (
            job.payload.job_id,
            job.count,
            job.state.value,
            attempt.finished,
            "analysis_terminal_v1",
            "analysis_result_v1" if result_json is not None else None,
            result_json.decode("utf-8") if result_json is not None else None,
            model_version,
            processing_duration_ms,
            safe_failure_code,
            failure_origin,
        ),
        lambda rows: _decode_terminal(_required_row(rows)),
    )
    outbox = await _query(
        connection,
        _OUTBOX_INSERT,
        (
            job.payload.job_id,
            "analysis_callback_snapshot_v1",
            callback_json.decode("utf-8"),
            "PENDING",
            attempt.finished,
            attempt.finished,
        ),
        lambda rows: _decode_outbox(_required_row(rows)),
    )
    pair = _TerminalPair(terminal, outbox)
    _validate_terminal_pair(job, attempt, pair)
    if (
        _instant_key(outbox.next_attempt_at) != _instant_key(attempt.finished)
        or terminal.model_version != model_version
        or terminal.processing_duration_ms != processing_duration_ms
        or terminal.safe_failure_code != safe_failure_code
        or terminal.failure_origin != failure_origin
        or not _returned_jsonb_equal(outbox.payload_json, _strict_json_object(callback_json))
        or (
            result_json is not None
            and not _returned_jsonb_equal(terminal.result_json, _strict_json_object(result_json))
        )
    ):
        _invalid()
    return pair


async def _equivalent(
    connection: _Connection, job_id: UUID, *, callback_json: bytes, result_json: bytes | None
) -> bool:
    def decode(rows: list[tuple[object, ...]]) -> bool:
        row = _required_row(rows)
        if len(row) != 1 or type(row[0]) is not bool:
            _invalid()
        return row[0]

    if result_json is None:
        return await _query(connection, _FAILURE_Q, (callback_json.decode("utf-8"), job_id), decode)
    return await _query(
        connection,
        _SUCCESS_Q,
        (result_json.decode("utf-8"), callback_json.decode("utf-8"), job_id),
        decode,
    )


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
    elif attempt.status is AttemptStatus.SUCCEEDED:
        if job.state is not AnalysisJobState.COMPLETED:
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


async def _eligible(connection: _Connection, job: _Job, attempt: _Attempt) -> bool:
    if job.state is not AnalysisJobState.RUNNING or attempt.status is not AttemptStatus.ACTIVE:
        return False
    observed = await _clock(connection)
    return _instant_key(_expiry(attempt)) > _instant_key(observed)


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


async def _run_transaction[T](
    pool: _Pool,
    acquire: float,
    operation_timeout_ms: int,
    lock_timeout_ms: int,
    operation: Callable[[_Connection], Awaitable[T]],
) -> T:
    # Classify only persistence boundaries; user-supplied factory errors retain identity.
    context = _io_call(lambda: pool.connection(timeout_seconds=acquire))
    connection = await _enter(context)
    primary: BaseException | None = None
    outcome: T | None = None
    try:
        transaction = _io_call(connection.transaction)
        await _enter(transaction)
        transaction_primary: BaseException | None = None
        try:
            for setting, value in (
                ("statement_timeout", operation_timeout_ms),
                ("lock_timeout", lock_timeout_ms),
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
