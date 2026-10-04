"""RED-C contract for inactive PostgreSQL AnalysisJob admission persistence."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, cast, get_args, get_type_hints
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from psycopg import InterfaceError, OperationalError
from psycopg.errors import LockNotAvailable
from psycopg_pool import PoolTimeout

from adapters.psycopg_database import PsycopgDatabasePool
from domain.analysis_job import (
    AnalysisJob,
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
    AnalysisJobState,
    NewAnalysisJob,
)
from services.analysis_job_admission import AnalysisJobAdmissionRepository

if TYPE_CHECKING:
    from adapters.psycopg_analysis_job_repository import (
        PsycopgAnalysisJobAdmissionRepository,
    )

    def _typecheck_real_psycopg_pool_composition(
        pool: PsycopgDatabasePool,
    ) -> PsycopgAnalysisJobAdmissionRepository:
        return PsycopgAnalysisJobAdmissionRepository(
            pool,
            connection_acquire_timeout_seconds=1.0,
            operation_timeout_ms=1_000,
            admission_lock_timeout_ms=100,
        )


_REPOSITORY_MODULE = "adapters.psycopg_analysis_job_repository"
_RED_MISSING = (
    "Slice 2 RED-C: adapters.psycopg_analysis_job_repository is missing; "
    "GREEN-C must implement the approved atomic admission repository."
)
_PUBLIC_SYMBOLS = {
    "PsycopgAnalysisJobAdmissionRepository",
    "AnalysisJobPersistenceUnavailable",
    "AnalysisJobPersistenceInvariantError",
    "AnalysisJobAdmissionLockTimeout",
}
_NAMESPACE = 1_396_113_410
_RESOURCE = 1
_MAX_POSTGRES_TIMEOUT_MS = 2_147_483_647
_ACQUIRE_TIMEOUT_SECONDS = 1.25
_OPERATION_TIMEOUT_MS = 4_321
_LOCK_TIMEOUT_MS = 1_234
_MAX_QUEUE_SIZE = 137
_JOB_ID = UUID("12345678-1234-5678-1234-567812345678")
_OTHER_JOB_ID = UUID("87654321-4321-8765-4321-876543218765")
_LOOKUP = b"protected-lookup-red-c-marker"
_OTHER_LOOKUP = b"other-protected-lookup-red-c"
_FINGERPRINT = bytes(range(32))
_OTHER_FINGERPRINT = bytes(reversed(range(32)))
_VIDEO_ID = "video-red-c-marker"
_PLAYER_ID = "player-red-c-marker"
_VIDEO_REFERENCE = "match-red-c-marker.mp4"
_CALLBACK_URL = "https://callback.invalid/red-c-marker"
_RAW_KEY_MARKER = "raw-idempotency-red-c-marker"
_DRIVER_MARKER = "driver-red-c-marker"
_ACCEPTED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_OTHER_ACCEPTED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_ROW_COLUMNS = (
    "job_id",
    "idempotency_lookup",
    "request_fingerprint",
    "video_id",
    "player_id",
    "video_reference",
    "callback_url",
    "state",
    "accepted_at",
)
_UNAVAILABLE_MESSAGE = "PostgreSQL AnalysisJob persistence is unavailable."
_INVARIANT_MESSAGE = "PostgreSQL AnalysisJob persistence invariant failed."
_LOCK_TIMEOUT_MESSAGE = "PostgreSQL AnalysisJob admission lock timed out."
_CANDIDATE_MARKERS: tuple[str | bytes, ...] = (
    _RAW_KEY_MARKER,
    str(_JOB_ID),
    _LOOKUP,
    _FINGERPRINT,
    _VIDEO_ID,
    _PLAYER_ID,
    _VIDEO_REFERENCE,
    _CALLBACK_URL,
)
_MAX_SCAN_DEPTH = 12
_MAX_SCAN_NODES = 256


class _BytesSubclass(bytes):
    pass


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


class _TextSubclass(str):
    pass


class _FatalFailure(BaseException):
    pass


class _SqlstateProgrammingError(RuntimeError):
    sqlstate = "55P03"


def _require_repository_module() -> ModuleType:
    if find_spec(_REPOSITORY_MODULE) is None:
        pytest.fail(_RED_MISSING, pytrace=False)
    return import_module(_REPOSITORY_MODULE)


def _require_symbol(name: str) -> Any:
    module = _require_repository_module()
    if not hasattr(module, name):
        pytest.fail(
            f"Slice 2 RED-C: {_REPOSITORY_MODULE}.{name} is missing; "
            "GREEN-C must implement the approved public repository boundary.",
            pytrace=False,
        )
    return getattr(module, name)


def _sensitive_representations(value: str | bytes) -> tuple[str | bytes, ...]:
    if type(value) is str:
        text = value
        return (text, repr(text), text.encode("unicode_escape").decode("ascii"))
    if type(value) is bytes:
        binary = value
        return (binary, repr(binary), binary.hex())
    raise AssertionError("sensitive value leaked")


def _assert_values_safe(markers: tuple[str | bytes, ...], *values: object) -> None:
    representations = tuple(
        representation
        for marker in markers
        for representation in _sensitive_representations(marker)
    )
    pending = [(value, 0) for value in values]
    seen: set[int] = set()
    visited = 0
    while pending:
        value, depth = pending.pop()
        value_type = type(value)
        if depth > _MAX_SCAN_DEPTH:
            raise AssertionError("sensitive value leaked")
        visited += 1
        if visited > _MAX_SCAN_NODES:
            raise AssertionError("sensitive value leaked")

        if value_type is str:
            if any(
                representation in cast(str, value)
                for representation in representations
                if type(representation) is str
            ):
                raise AssertionError("sensitive value leaked")
            continue
        if value_type in (bytes, bytearray):
            binary = bytes(cast(bytes | bytearray, value))
            if any(
                representation in binary
                for representation in representations
                if type(representation) is bytes
            ):
                raise AssertionError("sensitive value leaked")
            continue
        if value is None or value_type in (bool, int, float, UUID, datetime):
            continue
        if value_type in (tuple, list, set, frozenset):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            pending.extend((item, depth + 1) for item in cast(Any, value))
            continue
        if value_type is dict:
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            for key, item in cast(dict[object, object], value).items():
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
            continue
        raise AssertionError("sensitive value leaked")


def _assert_exception_safe(
    error: BaseException,
    expected_type: type[BaseException],
    expected_message: str,
    *,
    markers: tuple[str | bytes, ...],
) -> None:
    assert type(error) is expected_type
    assert error.args == (expected_message,)
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        _assert_values_safe(markers, current.args, current.__dict__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    _assert_values_safe(markers, str(error), repr(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return tuple(result)


def _assert_logs_safe(records: list[logging.LogRecord], markers: tuple[str | bytes, ...]) -> None:
    for record in records:
        _assert_values_safe(markers, record.msg, record.args, record.__dict__)
        rendered = logging.LogRecord.getMessage(record)
        _assert_values_safe(markers, rendered)


def _assert_graph_contains_sanitized_primary(
    error: BaseException,
    expected_type: type[BaseException],
    expected_message: str,
    *,
    markers: tuple[str | bytes, ...],
) -> None:
    graph = _exception_graph(error)
    matches = [node for node in graph if type(node) is expected_type]
    assert len(matches) == 1
    assert matches[0].args == (expected_message,)
    for node in graph:
        _assert_values_safe(markers, node.args, node.__dict__)


def _candidate(**overrides: object) -> NewAnalysisJob:
    values: dict[str, object] = {
        "job_id": _JOB_ID,
        "idempotency_lookup": _LOOKUP,
        "request_fingerprint": _FINGERPRINT,
        "video_id": _VIDEO_ID,
        "player_id": _PLAYER_ID,
        "video_reference": _VIDEO_REFERENCE,
        "callback_url": _CALLBACK_URL,
    }
    values.update(overrides)
    return NewAnalysisJob(**cast(Any, values))


def _row(**overrides: object) -> tuple[object, ...]:
    values: dict[str, object] = {
        "job_id": _JOB_ID,
        "idempotency_lookup": _LOOKUP,
        "request_fingerprint": _FINGERPRINT,
        "video_id": _VIDEO_ID,
        "player_id": _PLAYER_ID,
        "video_reference": _VIDEO_REFERENCE,
        "callback_url": _CALLBACK_URL,
        "state": "QUEUED",
        "accepted_at": _ACCEPTED_AT,
    }
    values.update(overrides)
    return tuple(values[column] for column in _ROW_COLUMNS)


def _expected_job(**overrides: object) -> AnalysisJob:
    values: dict[str, object] = {
        "job_id": _JOB_ID,
        "video_id": _VIDEO_ID,
        "player_id": _PLAYER_ID,
        "video_reference": _VIDEO_REFERENCE,
        "callback_url": _CALLBACK_URL,
        "state": AnalysisJobState.QUEUED,
        "accepted_at": _ACCEPTED_AT,
    }
    values.update(overrides)
    return AnalysisJob(
        **cast(Any, values),
    )


@dataclass(frozen=True, slots=True)
class _Operation:
    category: str
    validate: Callable[[str, object], None]
    row: tuple[object, ...] | None = None
    execute_error: BaseException | None = None
    fetch_error: BaseException | None = None
    close_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _Execution:
    category: str
    sql: str
    params: object


def _parameter_values(params: object) -> tuple[object, ...]:
    if params is None:
        return ()
    if type(params) is tuple:
        return cast(tuple[object, ...], params)
    raise AssertionError("unexpected SQL parameter container")


_ASCII_SQL_WHITESPACE = re.compile(r"[ \t\n\r\f\v]+")
_ASCII_SQL_WHITESPACE_CHARACTERS = " \t\n\r\f\v"


def _normalized_sql(sql: str) -> str:
    if type(sql) is not str:
        raise AssertionError("SQL operation must be text")
    normalized = _ASCII_SQL_WHITESPACE.sub(" ", sql.lower()).strip(_ASCII_SQL_WHITESPACE_CHARACTERS)
    return normalized.removesuffix(";").rstrip(_ASCII_SQL_WHITESPACE_CHARACTERS)


_TIMEOUT_SQL = "SELECT set_config(%s, %s, %s)"
_LOOKUP_SQL = """SELECT job_id, idempotency_lookup, request_fingerprint, video_id,
    player_id, video_reference, callback_url, state, accepted_at
FROM analysis_jobs
WHERE idempotency_lookup = %s"""
_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s, %s)"
_COUNT_SQL = "SELECT count(*) FROM analysis_jobs WHERE state = %s"
_TIMEOUT_SQL_SHAPE = _normalized_sql(_TIMEOUT_SQL)
_LOOKUP_SQL_SHAPES = frozenset(
    {
        _normalized_sql(_LOOKUP_SQL),
        _normalized_sql(
            _LOOKUP_SQL.replace(
                "WHERE idempotency_lookup = %s",
                "WHERE (idempotency_lookup = %s)",
            )
        ),
    }
)
_ADVISORY_LOCK_SQL_SHAPE = _normalized_sql(_ADVISORY_LOCK_SQL)
_COUNT_SQL_SHAPE = _normalized_sql(_COUNT_SQL)
_INSERT_PREFIX = _normalized_sql(
    """INSERT INTO analysis_jobs (
        job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
        video_reference, callback_url, state, accepted_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, """
)
_RETURNING_SQL_SHAPE = _normalized_sql(
    """) RETURNING job_id, idempotency_lookup, request_fingerprint, video_id,
        player_id, video_reference, callback_url, state, accepted_at"""
)
_INSERT_SQL_SHAPES = frozenset(
    f"{_INSERT_PREFIX} {database_time}{_RETURNING_SQL_SHAPE}"
    for database_time in ("current_timestamp", "transaction_timestamp()", "now()")
)
_VALID_INSERT_SQL = """INSERT INTO analysis_jobs (
    job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
    video_reference, callback_url, state, accepted_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
RETURNING job_id, idempotency_lookup, request_fingerprint, video_id, player_id,
    video_reference, callback_url, state, accepted_at"""


def _expect_sql(
    shapes: str | frozenset[str],
    parameters: tuple[object, ...],
) -> Callable[[str, object], None]:
    approved = frozenset({shapes}) if isinstance(shapes, str) else shapes

    def validate(sql: str, params: object) -> None:
        assert _normalized_sql(sql) in approved
        assert _parameter_values(params) == parameters

    return validate


def _expect_insert(candidate: NewAnalysisJob) -> Callable[[str, object], None]:
    expected = (
        candidate.job_id,
        candidate.idempotency_lookup,
        candidate.request_fingerprint,
        candidate.video_id,
        candidate.player_id,
        candidate.video_reference,
        candidate.callback_url,
        "QUEUED",
    )

    def validate(sql: str, params: object) -> None:
        assert _normalized_sql(sql) in _INSERT_SQL_SHAPES
        assert _parameter_values(params) == expected
        assert _ACCEPTED_AT not in expected

    return validate


class _ScriptedCursor:
    def __init__(
        self,
        operation: _Operation,
        events: list[str],
    ) -> None:
        self._operation = operation
        self._events = events
        self.close_calls = 0
        self.fetch_calls = 0

    async def fetchone(self) -> tuple[object, ...] | None:
        self.fetch_calls += 1
        self._events.append(f"fetch:{self._operation.category}")
        if self._operation.fetch_error is not None:
            raise self._operation.fetch_error
        return self._operation.row

    async def close(self) -> None:
        self.close_calls += 1
        self._events.append(f"close:{self._operation.category}")
        if self._operation.close_error is not None:
            raise self._operation.close_error


class _TransactionContext:
    def __init__(self, connection: _ScriptedConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        assert self._connection.connection_active is True
        assert self._connection.transaction_active is False
        self._connection.transaction_active = True
        self._connection.transaction_entries += 1
        self._connection.events.append("transaction-enter")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, traceback
        assert self._connection.transaction_active is True
        self._connection.transaction_active = False
        self._connection.transaction_exits += 1
        if exc is None:
            self._connection.commit_attempts += 1
            if self._connection.transaction_exit_error is not None:
                raise self._connection.transaction_exit_error
            self._connection.commits += 1
            self._connection.committed_insert = self._connection.pending_insert
            self._connection.events.append("transaction-commit")
        else:
            self._connection.rollback_attempts += 1
            self._connection.pending_insert = False
            if self._connection.transaction_exit_error is not None:
                raise self._connection.transaction_exit_error
            self._connection.rollbacks += 1
            self._connection.events.append("transaction-rollback")


class _ScriptedConnection:
    def __init__(
        self,
        operations: list[_Operation],
        *,
        transaction_exit_error: BaseException | None = None,
    ) -> None:
        self.operations = list(operations)
        self.transaction_exit_error = transaction_exit_error
        self.executions: list[_Execution] = []
        self.cursors: list[_ScriptedCursor] = []
        self.events: list[str] = []
        self.transaction_entries = 0
        self.transaction_exits = 0
        self.transaction_active = False
        self.connection_active = False
        self.commit_attempts = 0
        self.rollback_attempts = 0
        self.commits = 0
        self.rollbacks = 0
        self.pending_insert = False
        self.committed_insert = False

    def transaction(self) -> AbstractAsyncContextManager[None]:
        self.events.append("transaction-requested")
        return _TransactionContext(self)

    async def execute(self, sql: str, params: object = None) -> _ScriptedCursor:
        if not self.connection_active or not self.transaction_active:
            raise AssertionError("SQL operation executed outside transaction")
        if type(sql) is not str:
            raise AssertionError("SQL operation must be text")
        if not self.operations:
            raise AssertionError("unexpected additional SQL operation")
        expected = self.operations.pop(0)
        expected.validate(sql, params)
        self.executions.append(_Execution(expected.category, sql, params))
        self.events.append(f"execute:{expected.category}")
        if expected.execute_error is not None:
            raise expected.execute_error
        cursor = _ScriptedCursor(expected, self.events)
        self.cursors.append(cursor)
        if expected.category == "insert":
            self.pending_insert = True
        return cursor


class _ConnectionContext:
    def __init__(
        self,
        pool: _ScriptedPool,
        connection: _ScriptedConnection,
        enter_error: BaseException | None,
        exit_error: BaseException | None,
    ) -> None:
        self._pool = pool
        self._connection = connection
        self._enter_error = enter_error
        self._exit_error = exit_error

    async def __aenter__(self) -> _ScriptedConnection:
        self._pool.events.append("connection-enter")
        if self._enter_error is not None:
            self._pool.acquisition_failures += 1
            raise self._enter_error
        assert self._connection.connection_active is False
        self._connection.connection_active = True
        self._pool.connection_entries += 1
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        assert self._connection.connection_active is True
        assert self._connection.transaction_active is False
        self._connection.connection_active = False
        self._pool.connection_exits += 1
        self._pool.events.append("connection-exit")
        if self._exit_error is not None:
            raise self._exit_error


class _ScriptedPool:
    def __init__(
        self,
        operations: list[_Operation],
        *,
        enter_error: BaseException | None = None,
        transaction_exit_error: BaseException | None = None,
        connection_exit_error: BaseException | None = None,
    ) -> None:
        self.connection_value = _ScriptedConnection(
            operations,
            transaction_exit_error=transaction_exit_error,
        )
        self.enter_error = enter_error
        self.connection_exit_error = connection_exit_error
        self.connection_calls: list[float] = []
        self.connection_entries = 0
        self.connection_exits = 0
        self.acquisition_failures = 0
        self.events: list[str] = []

    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_ScriptedConnection]:
        self.connection_calls.append(timeout_seconds)
        self.events.append("connection-requested")
        return _ConnectionContext(
            self,
            self.connection_value,
            self.enter_error,
            self.connection_exit_error,
        )


class _RepositoryConstructor(Protocol):
    def __call__(
        self,
        pool: object,
        *,
        connection_acquire_timeout_seconds: float,
        operation_timeout_ms: int,
        admission_lock_timeout_ms: int,
    ) -> AnalysisJobAdmissionRepository: ...


def _repository(
    pool: _ScriptedPool,
    *,
    connection_acquire_timeout_seconds: object = _ACQUIRE_TIMEOUT_SECONDS,
    operation_timeout_ms: int = _OPERATION_TIMEOUT_MS,
    admission_lock_timeout_ms: int = _LOCK_TIMEOUT_MS,
) -> AnalysisJobAdmissionRepository:
    constructor = cast(
        _RepositoryConstructor,
        _require_symbol("PsycopgAnalysisJobAdmissionRepository"),
    )
    return constructor(
        pool,
        connection_acquire_timeout_seconds=cast(float, connection_acquire_timeout_seconds),
        operation_timeout_ms=operation_timeout_ms,
        admission_lock_timeout_ms=admission_lock_timeout_ms,
    )


def _run(
    repository: AnalysisJobAdmissionRepository,
    candidate: object,
    *,
    max_queue_size: object = _MAX_QUEUE_SIZE,
) -> object:
    return asyncio.run(
        repository.accept_or_get(
            cast(NewAnalysisJob, candidate),
            max_queue_size=cast(int, max_queue_size),
        )
    )


def _capture(
    repository: AnalysisJobAdmissionRepository,
    candidate: object,
    *,
    max_queue_size: object = _MAX_QUEUE_SIZE,
) -> BaseException:
    captured: BaseException | None = None
    try:
        _run(repository, candidate, max_queue_size=max_queue_size)
    except BaseException as error:
        captured = error
    if captured is None:
        raise AssertionError("repository operation did not fail")
    return captured


def _statement_timeout(
    *,
    execute_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "statement-timeout",
        _expect_sql(
            _TIMEOUT_SQL_SHAPE,
            ("statement_timeout", str(_OPERATION_TIMEOUT_MS), True),
        ),
        execute_error=execute_error,
        close_error=close_error,
    )


def _lookup(
    row: tuple[object, ...] | None = None,
    *,
    lookup: bytes = _LOOKUP,
    execute_error: BaseException | None = None,
    fetch_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "lookup",
        _expect_sql(_LOOKUP_SQL_SHAPES, (lookup,)),
        row=row,
        execute_error=execute_error,
        fetch_error=fetch_error,
        close_error=close_error,
    )


def _lock_timeout(
    *,
    execute_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "lock-timeout",
        _expect_sql(
            _TIMEOUT_SQL_SHAPE,
            ("lock_timeout", str(_LOCK_TIMEOUT_MS), True),
        ),
        execute_error=execute_error,
        close_error=close_error,
    )


def _advisory_lock(
    *,
    execute_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "advisory-lock",
        _expect_sql(_ADVISORY_LOCK_SQL_SHAPE, (_NAMESPACE, _RESOURCE)),
        execute_error=execute_error,
        close_error=close_error,
    )


def _count_queued(
    count: int = 0,
    *,
    execute_error: BaseException | None = None,
    fetch_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "count-queued",
        _expect_sql(_COUNT_SQL_SHAPE, ("QUEUED",)),
        row=(count,),
        execute_error=execute_error,
        fetch_error=fetch_error,
        close_error=close_error,
    )


def _insert(
    row: tuple[object, ...] | None,
    *,
    candidate: NewAnalysisJob | None = None,
    execute_error: BaseException | None = None,
    fetch_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> _Operation:
    return _Operation(
        "insert",
        _expect_insert(candidate or _candidate()),
        row=row,
        execute_error=execute_error,
        fetch_error=fetch_error,
        close_error=close_error,
    )


_SUCCESS_SCENARIOS = (
    "initial-existing",
    "initial-conflict",
    "post-lock-existing",
    "post-lock-conflict",
    "capacity-rejected",
    "accepted",
)


def _success_operations(scenario: str) -> list[_Operation]:
    if scenario == "initial-existing":
        return [_statement_timeout(), _lookup(_row())]
    if scenario == "initial-conflict":
        return [
            _statement_timeout(),
            _lookup(_row(request_fingerprint=_OTHER_FINGERPRINT)),
        ]

    operations = [
        _statement_timeout(),
        _lookup(),
        _lock_timeout(),
        _advisory_lock(),
    ]
    if scenario == "post-lock-existing":
        return [*operations, _lookup(_row())]
    if scenario == "post-lock-conflict":
        return [
            *operations,
            _lookup(_row(request_fingerprint=_OTHER_FINGERPRINT)),
        ]
    operations.append(_lookup())
    if scenario == "capacity-rejected":
        return [*operations, _count_queued(_MAX_QUEUE_SIZE)]
    if scenario == "accepted":
        return [*operations, _count_queued(0), _insert(_row())]
    raise AssertionError("unknown success scenario")


def _fetch_failure_operations(phase: str, error: BaseException) -> list[_Operation]:
    if phase == "lookup":
        return [_statement_timeout(), _lookup(fetch_error=error)]
    prefix = [
        _statement_timeout(),
        _lookup(),
        _lock_timeout(),
        _advisory_lock(),
        _lookup(),
    ]
    if phase == "capacity":
        return [*prefix, _count_queued(fetch_error=error)]
    if phase == "insert":
        return [*prefix, _count_queued(0), _insert(_row(), fetch_error=error)]
    raise AssertionError("unknown cursor phase")


def _sqlstate_failure_pool(phase: str, error: BaseException) -> _ScriptedPool:
    if phase == "statement-timeout":
        return _ScriptedPool([_statement_timeout(execute_error=error)])
    if phase == "initial-lookup":
        return _ScriptedPool([_statement_timeout(), _lookup(execute_error=error)])
    prefix = [_statement_timeout(), _lookup()]
    if phase == "lock-timeout":
        return _ScriptedPool([*prefix, _lock_timeout(execute_error=error)])
    if phase == "advisory-cursor-close":
        return _ScriptedPool([*prefix, _lock_timeout(), _advisory_lock(close_error=error)])
    prefix.extend([_lock_timeout(), _advisory_lock(), _lookup()])
    if phase == "capacity":
        return _ScriptedPool([*prefix, _count_queued(execute_error=error)])
    if phase == "insert":
        return _ScriptedPool([*prefix, _count_queued(0), _insert(_row(), execute_error=error)])
    if phase == "transaction-exit":
        return _ScriptedPool(
            [*prefix, _count_queued(_MAX_QUEUE_SIZE)],
            transaction_exit_error=error,
        )
    if phase == "lookup-cursor-close":
        return _ScriptedPool([_statement_timeout(), _lookup(_row(), close_error=error)])
    if phase == "connection-exit":
        return _ScriptedPool(
            [_statement_timeout(), _lookup(_row())],
            connection_exit_error=error,
        )
    raise AssertionError("unknown SQLSTATE phase")


def _assert_success_lifecycle(pool: _ScriptedPool) -> None:
    connection = pool.connection_value
    assert pool.connection_calls == [_ACQUIRE_TIMEOUT_SECONDS]
    assert pool.connection_entries == 1
    assert pool.connection_exits == 1
    assert connection.transaction_entries == 1
    assert connection.transaction_exits == 1
    assert connection.transaction_active is False
    assert connection.connection_active is False
    assert connection.commit_attempts == 1
    assert connection.rollback_attempts == 0
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.events[-1] == "transaction-commit"
    assert pool.events[-1] == "connection-exit"
    assert connection.operations == []
    assert all(cursor.close_calls == 1 for cursor in connection.cursors)
    assert all(
        cursor.fetch_calls
        == (1 if cursor._operation.category in {"lookup", "count-queued", "insert"} else 0)
        for cursor in connection.cursors
    )


def _assert_rollback_lifecycle(pool: _ScriptedPool) -> None:
    connection = pool.connection_value
    assert pool.connection_calls == [_ACQUIRE_TIMEOUT_SECONDS]
    assert pool.connection_entries == 1
    assert pool.connection_exits == 1
    assert connection.transaction_entries == 1
    assert connection.transaction_exits == 1
    assert connection.transaction_active is False
    assert connection.connection_active is False
    assert connection.commit_attempts == 0
    assert connection.rollback_attempts == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.pending_insert is False
    assert connection.committed_insert is False
    assert connection.events[-1] == "transaction-rollback"
    assert pool.events[-1] == "connection-exit"
    assert all(cursor.close_calls == 1 for cursor in connection.cursors)


def _assert_primary_failure_cleanup_lifecycle(
    pool: _ScriptedPool,
    boundary: str,
) -> None:
    if boundary != "transaction-exit":
        _assert_rollback_lifecycle(pool)
        return
    connection = pool.connection_value
    assert pool.connection_calls == [_ACQUIRE_TIMEOUT_SECONDS]
    assert pool.connection_entries == 1
    assert pool.connection_exits == 1
    assert connection.transaction_entries == 1
    assert connection.transaction_exits == 1
    assert connection.transaction_active is False
    assert connection.connection_active is False
    assert connection.commit_attempts == 0
    assert connection.rollback_attempts == 1
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.pending_insert is False
    assert all(cursor.close_calls == 1 for cursor in connection.cursors)


def _assert_no_sensitive_sql(connection: _ScriptedConnection) -> None:
    markers: tuple[str | bytes, ...] = (
        _RAW_KEY_MARKER,
        str(_JOB_ID),
        _LOOKUP,
        _FINGERPRINT,
        _VIDEO_ID,
        _PLAYER_ID,
        _VIDEO_REFERENCE,
        _CALLBACK_URL,
        "QUEUED",
        str(_MAX_QUEUE_SIZE),
        str(_OPERATION_TIMEOUT_MS),
        str(_LOCK_TIMEOUT_MS),
        str(_NAMESPACE),
    )
    for record in connection.executions:
        _assert_values_safe(markers, record.sql)
        compact = "".join(record.sql.lower().split())
        assert f"pg_advisory_xact_lock({_NAMESPACE},{_RESOURCE})" not in compact


def test_guard_existing_psycopg_pool_connection_surface_is_reused() -> None:
    parameters = inspect.signature(PsycopgDatabasePool.connection).parameters
    fake_parameters = inspect.signature(_ScriptedPool.connection).parameters

    assert tuple(parameters) == ("self", "timeout_seconds")
    assert parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tuple(fake_parameters) == tuple(parameters)
    assert fake_parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY


def _assert_application_repository_inactive(application: FastAPI) -> None:
    assert not {
        "analysis_job_repository",
        "postgres_foundation",
        "postgres_pool",
    }.intersection(application.state._state)
    assert all(
        type(value).__module__ != _REPOSITORY_MODULE for value in application.state._state.values()
    )
    routes = [
        nested
        for route in application.routes
        for nested in (
            list(route.original_router.routes) if hasattr(route, "original_router") else [route]
        )
    ]
    dependants = [route.dependant for route in routes if hasattr(route, "dependant")]
    while dependants:
        dependant = dependants.pop()
        call = dependant.call
        if call is not None:
            assert getattr(call, "__module__", "") != _REPOSITORY_MODULE
        dependants.extend(dependant.dependencies)


def test_guard_runtime_composition_remains_repository_free() -> None:
    main = import_module("main")
    assert tuple(inspect.signature(main.create_app).parameters) == (
        "settings",
        "tracker",
        "selector",
        "validator",
        "lifecycle",
        "downloader",
        "path_resolver",
        "callback_service",
        "analysis_queue",
        "process_analysis_pool",
    )
    assert all(
        parameter.default is not inspect.Parameter.empty
        for parameter in inspect.signature(main.create_app).parameters.values()
    )
    _assert_application_repository_inactive(main.app)


def test_guard_runtime_inactivity_ignores_unrelated_routes_and_rejects_repository_calls() -> None:
    application = FastAPI()

    async def unrelated_route() -> dict[str, bool]:
        return {"ok": True}

    application.add_api_route("/unrelated", unrelated_route, methods=["GET"])
    _assert_application_repository_inactive(application)

    async def forbidden_dependency() -> None:
        return None

    forbidden_dependency.__module__ = _REPOSITORY_MODULE

    async def route_with_dependency() -> None:
        return None

    application.add_api_route(
        "/forbidden",
        route_with_dependency,
        methods=["GET"],
        dependencies=[Depends(forbidden_dependency)],
    )
    with pytest.raises(AssertionError):
        _assert_application_repository_inactive(application)


@pytest.mark.parametrize(
    ("separator", "accepted"),
    (
        (" ", True),
        ("   ", True),
        ("\t", True),
        ("\n", True),
        ("\r\n", True),
        ("\f", True),
        ("\v", True),
        ("\u00a0", False),
        ("\u2003", False),
        ("\u202f", False),
    ),
    ids=(
        "space",
        "repeated-space",
        "tab",
        "line-feed",
        "crlf",
        "form-feed",
        "vertical-tab",
        "nbsp",
        "em-space",
        "narrow-nbsp",
    ),
)
def test_sql_normalization_uses_only_approved_ascii_whitespace(
    separator: str,
    accepted: bool,
) -> None:
    sql = f"SELECT{separator}set_config(%s, %s, %s)"
    parameters = ("statement_timeout", str(_OPERATION_TIMEOUT_MS), True)
    normalized = _normalized_sql(sql)

    if accepted:
        assert normalized == _TIMEOUT_SQL_SHAPE
        _statement_timeout().validate(sql, parameters)
        return

    assert normalized != _TIMEOUT_SQL_SHAPE
    with pytest.raises(AssertionError):
        _statement_timeout().validate(sql, parameters)


def test_guard_sql_expectations_reject_pre_correction_false_positives() -> None:
    statement = _statement_timeout()
    lock_timeout = _lock_timeout()
    advisory = _advisory_lock()
    count = _count_queued()
    lookup = _lookup()
    insert = _insert(_row())
    insert_parameters = (
        _JOB_ID,
        _LOOKUP,
        _FINGERPRINT,
        _VIDEO_ID,
        _PLAYER_ID,
        _VIDEO_REFERENCE,
        _CALLBACK_URL,
        "QUEUED",
    )

    statement.validate(
        _TIMEOUT_SQL,
        ("statement_timeout", str(_OPERATION_TIMEOUT_MS), True),
    )
    lock_timeout.validate(
        _TIMEOUT_SQL,
        ("lock_timeout", str(_LOCK_TIMEOUT_MS), True),
    )
    advisory.validate(_ADVISORY_LOCK_SQL, (_NAMESPACE, _RESOURCE))
    lookup.validate(_LOOKUP_SQL, (_LOOKUP,))
    count.validate(_COUNT_SQL, ("QUEUED",))
    insert.validate(_VALID_INSERT_SQL, insert_parameters)
    for sql in (
        _LOOKUP_SQL,
        _LOOKUP_SQL.upper(),
        _LOOKUP_SQL.replace(" ", "  "),
        _LOOKUP_SQL.replace("\n", "\n\t"),
        _LOOKUP_SQL.replace(
            "WHERE idempotency_lookup = %s",
            "WHERE (idempotency_lookup = %s)",
        ),
    ):
        lookup.validate(sql, (_LOOKUP,))

    rejected = (
        (
            statement,
            "SELECT set_config(%s, %s, false) WHERE %s",
            ("statement_timeout", str(_OPERATION_TIMEOUT_MS), True),
        ),
        (
            statement,
            "SELECT set_config(%s, %s, %s)",
            (str(_OPERATION_TIMEOUT_MS), "statement_timeout", True),
        ),
        (
            statement,
            "SELECTset_config(%s, %s, %s)",
            ("statement_timeout", str(_OPERATION_TIMEOUT_MS), True),
        ),
        (lookup, "SELECT 'analysis_jobs'", (_LOOKUP,)),
        (
            lookup,
            "SELECT job_id, idempotency_lookup, request_fingerprint, video_id, "
            "player_id, video_reference, callback_url, state, accepted_at "
            "FROManalysis_jobs WHERE idempotency_lookup = %s",
            (_LOOKUP,),
        ),
        (
            lookup,
            f"{_LOOKUP_SQL} OR TRUE",
            (_LOOKUP,),
        ),
        (
            count,
            "SELECT count(*) FROM analysis_jobs WHERE job_id = %s",
            ("QUEUED",),
        ),
        (count, "SELECT count(*) FROM analysis_jobs", ("QUEUED",)),
        (
            count,
            "SELECT count(*) FROM analysis_jobs WHERE job_id = %s AND 'QUEUED' = 'QUEUED'",
            ("QUEUED",),
        ),
        (count, "SELECTcount(*) FROM analysis_jobs WHERE state = %s", ("QUEUED",)),
        (advisory, _ADVISORY_LOCK_SQL, (_RESOURCE, _NAMESPACE)),
    )
    for operation, sql, parameters in rejected:
        with pytest.raises(AssertionError):
            operation.validate(sql, parameters)
    with pytest.raises(AssertionError):
        insert.validate(
            _VALID_INSERT_SQL,
            (
                _JOB_ID,
                _LOOKUP,
                _FINGERPRINT,
                _PLAYER_ID,
                _VIDEO_ID,
                _VIDEO_REFERENCE,
                _CALLBACK_URL,
                "QUEUED",
            ),
        )
    with pytest.raises(AssertionError):
        insert.validate(
            """INSERT INTO analysis_jobs (
                job_id, idempotency_lookup, request_fingerprint, video_id,
                player_id, video_reference, callback_url, state, accepted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING accepted_at, state, callback_url, video_reference,
                player_id, video_id, request_fingerprint, idempotency_lookup, job_id
            """,
            insert_parameters,
        )
    with pytest.raises(AssertionError):
        insert.validate(
            _VALID_INSERT_SQL.replace(", accepted_at", ""),
            insert_parameters,
        )
    with pytest.raises(AssertionError):
        insert.validate(
            _VALID_INSERT_SQL.replace("CURRENT_TIMESTAMP", "%s"),
            (*insert_parameters, _ACCEPTED_AT),
        )


def test_guard_fake_exposes_cursor_transaction_and_connection_exit_failures() -> None:
    cursor_error = RuntimeError("cursor-close-characterization")
    transaction_error = RuntimeError("transaction-exit-characterization")
    connection_error = RuntimeError("connection-exit-characterization")

    async def exercise() -> None:
        cursor = _ScriptedCursor(
            _lookup(close_error=cursor_error),
            [],
        )
        with pytest.raises(RuntimeError) as cursor_captured:
            await cursor.close()
        assert cursor_captured.value is cursor_error

        transaction_pool = _ScriptedPool([], transaction_exit_error=transaction_error)
        with pytest.raises(RuntimeError) as transaction_captured:
            async with transaction_pool.connection(timeout_seconds=1) as connection:
                async with connection.transaction():
                    pass
        assert transaction_captured.value is transaction_error
        assert transaction_pool.connection_value.commit_attempts == 1

        connection_pool = _ScriptedPool([], connection_exit_error=connection_error)
        with pytest.raises(RuntimeError) as connection_captured:
            async with connection_pool.connection(timeout_seconds=1):
                pass
        assert connection_captured.value is connection_error
        assert connection_pool.connection_exits == 1

    asyncio.run(exercise())


def test_public_repository_surface_signature_and_outcome_taxonomy_are_exact() -> None:
    module = _require_repository_module()
    repository_type = _require_symbol("PsycopgAnalysisJobAdmissionRepository")
    unavailable = _require_symbol("AnalysisJobPersistenceUnavailable")
    invariant = _require_symbol("AnalysisJobPersistenceInvariantError")
    lock_timeout = _require_symbol("AnalysisJobAdmissionLockTimeout")
    pool = _ScriptedPool([])
    repository = _repository(pool)
    constructor_parameters = inspect.signature(repository_type.__init__).parameters
    method_parameters = inspect.signature(repository_type.accept_or_get).parameters
    public_methods = {
        name
        for name, value in vars(repository_type).items()
        if not name.startswith("_") and callable(value)
    }
    forbidden = {
        "get_by_job_id",
        "count",
        "insert",
        "update",
        "claim",
        "cancel",
        "delete",
        "list",
        "transition",
        "transaction",
        "cursor",
    }

    assert set(module.__all__) == _PUBLIC_SYMBOLS
    assert public_methods == {"accept_or_get"}
    assert not any(hasattr(repository, name) for name in forbidden)
    assert tuple(constructor_parameters) == (
        "self",
        "pool",
        "connection_acquire_timeout_seconds",
        "operation_timeout_ms",
        "admission_lock_timeout_ms",
    )
    assert all(
        constructor_parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        and constructor_parameters[name].default is inspect.Parameter.empty
        for name in (
            "connection_acquire_timeout_seconds",
            "operation_timeout_ms",
            "admission_lock_timeout_ms",
        )
    )
    assert tuple(method_parameters) == ("self", "candidate", "max_queue_size")
    assert method_parameters["max_queue_size"].kind is inspect.Parameter.KEYWORD_ONLY
    assert method_parameters["max_queue_size"].default is inspect.Parameter.empty
    assert get_type_hints(repository_type.accept_or_get)["candidate"] is NewAnalysisJob
    assert set(get_args(get_type_hints(repository_type.accept_or_get)["return"])) == {
        AnalysisJobAccepted,
        AnalysisJobExisting,
        AnalysisJobIdempotencyConflict,
        AnalysisJobCapacityRejected,
    }
    assert issubclass(unavailable, RuntimeError)
    assert issubclass(invariant, RuntimeError)
    assert issubclass(lock_timeout, unavailable)
    assert unavailable is not invariant
    assert lock_timeout is not unavailable
    assert "raw_idempotency_key" not in constructor_parameters
    assert "raw_idempotency_key" not in method_parameters
    assert not hasattr(repository, "raw_idempotency_key")
    _assert_values_safe((_RAW_KEY_MARKER,), repr(repository), str(repository))
    assert pool.connection_calls == []


@pytest.mark.parametrize(
    "acquire_timeout",
    (1, 0.125, _ACQUIRE_TIMEOUT_SECONDS, _IntSubclass(2), _FloatSubclass(2.5)),
    ids=(
        "positive-int",
        "positive-float",
        "representative-float",
        "positive-int-subclass",
        "positive-float-subclass",
    ),
)
def test_constructor_accepts_finite_positive_acquisition_timeout_before_pool_io(
    acquire_timeout: int | float,
) -> None:
    constructor = cast(
        _RepositoryConstructor,
        _require_symbol("PsycopgAnalysisJobAdmissionRepository"),
    )
    pool = _ScriptedPool([])
    constructor(
        pool,
        connection_acquire_timeout_seconds=acquire_timeout,
        operation_timeout_ms=2,
        admission_lock_timeout_ms=1,
    )
    assert pool.connection_calls == []


@pytest.mark.parametrize(
    "acquire_timeout",
    (
        True,
        False,
        0,
        0.0,
        -1,
        -1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "1",
        None,
    ),
    ids=(
        "true",
        "false",
        "zero-int",
        "zero-float",
        "negative-int",
        "negative-float",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "text",
        "none",
    ),
)
def test_constructor_rejects_invalid_acquisition_timeout_before_pool_io(
    acquire_timeout: object,
) -> None:
    invalid_type = type(acquire_timeout) not in (int, float)
    expected_exception = TypeError if invalid_type else ValueError
    pool = _ScriptedPool([])

    with pytest.raises(expected_exception):
        _repository(pool, connection_acquire_timeout_seconds=acquire_timeout)

    assert pool.connection_calls == []


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_exception"),
    tuple(
        (field, value, TypeError if type(value) is not int else ValueError)
        for field in ("operation_timeout_ms", "admission_lock_timeout_ms")
        for value in (
            True,
            False,
            0,
            -1,
            1.0,
            1.5,
            "1",
            None,
            _IntSubclass(1),
            _MAX_POSTGRES_TIMEOUT_MS + 1,
        )
    ),
    ids=tuple(
        f"{field}-{identifier}"
        for field in ("operation", "lock")
        for identifier in (
            "true",
            "false",
            "zero",
            "negative",
            "integral-float",
            "fractional-float",
            "text",
            "none",
            "int-subclass",
            "above-postgres-maximum",
        )
    ),
)
def test_constructor_rejects_invalid_millisecond_timeout_before_pool_io(
    field: str,
    invalid_value: object,
    expected_exception: type[BaseException],
) -> None:
    values: dict[str, object] = {
        "operation_timeout_ms": _OPERATION_TIMEOUT_MS,
        "admission_lock_timeout_ms": _LOCK_TIMEOUT_MS,
    }
    values[field] = invalid_value
    pool = _ScriptedPool([])

    with pytest.raises(expected_exception):
        _repository(pool, **cast(Any, values))

    assert pool.connection_calls == []


@pytest.mark.parametrize(
    ("operation_timeout", "lock_timeout"),
    (
        (2, 1),
        (_MAX_POSTGRES_TIMEOUT_MS, 1),
    ),
    ids=("minimum-ordered-boundary", "maximum-operation-boundary"),
)
def test_constructor_accepts_ordered_millisecond_timeout_boundaries(
    operation_timeout: int,
    lock_timeout: int,
) -> None:
    pool = _ScriptedPool([])

    _repository(
        pool,
        operation_timeout_ms=operation_timeout,
        admission_lock_timeout_ms=lock_timeout,
    )

    assert pool.connection_calls == []


@pytest.mark.parametrize(
    ("operation_timeout", "lock_timeout"),
    ((1, 1), (1, 2)),
    ids=("equal", "reversed"),
)
def test_constructor_requires_operation_timeout_greater_than_lock_timeout(
    operation_timeout: int,
    lock_timeout: int,
) -> None:
    pool = _ScriptedPool([])

    with pytest.raises(ValueError):
        _repository(
            pool,
            operation_timeout_ms=operation_timeout,
            admission_lock_timeout_ms=lock_timeout,
        )

    assert pool.connection_calls == []


@pytest.mark.parametrize(
    ("candidate_factory", "capacity"),
    (
        (lambda: object(), 1),
        (_candidate, True),
        (_candidate, False),
        (_candidate, 0),
        (_candidate, -1),
        (_candidate, 1.5),
        (_candidate, _IntSubclass(1)),
        (_candidate, None),
    ),
    ids=(
        "invalid-candidate",
        "capacity-true",
        "capacity-false",
        "capacity-zero",
        "capacity-negative",
        "capacity-float",
        "capacity-int-subclass",
        "capacity-none",
    ),
)
def test_invalid_admission_input_fails_before_connection_io(
    candidate_factory: Any,
    capacity: object,
) -> None:
    pool = _ScriptedPool([])
    repository = _repository(pool)

    with pytest.raises((TypeError, ValueError)):
        _run(repository, candidate_factory(), max_queue_size=capacity)

    assert pool.connection_calls == []


def test_initial_identical_retry_returns_existing_before_lock_or_capacity() -> None:
    stored_fields = {
        "video_id": "stored-video-red-c-marker",
        "player_id": "stored-player-red-c-marker",
        "video_reference": "stored-reference-red-c-marker.mp4",
        "callback_url": "https://stored.invalid/red-c-marker",
        "accepted_at": _OTHER_ACCEPTED_AT,
    }
    pool = _ScriptedPool([_statement_timeout(), _lookup(_row(**stored_fields))])
    repository = _repository(pool)

    outcome = _run(repository, _candidate(), max_queue_size=1)

    assert type(outcome) is AnalysisJobExisting
    assert outcome.job == _expected_job(**stored_fields)
    assert [record.category for record in pool.connection_value.executions] == [
        "statement-timeout",
        "lookup",
    ]
    _assert_no_sensitive_sql(pool.connection_value)
    _assert_success_lifecycle(pool)


def test_initial_conflict_returns_existing_job_id_without_lock_or_capacity() -> None:
    pool = _ScriptedPool(
        [_statement_timeout(), _lookup(_row(request_fingerprint=_OTHER_FINGERPRINT))]
    )
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    assert type(outcome) is AnalysisJobIdempotencyConflict
    assert outcome.existing_job_id == _JOB_ID
    assert [record.category for record in pool.connection_value.executions] == [
        "statement-timeout",
        "lookup",
    ]
    _assert_success_lifecycle(pool)


@pytest.mark.parametrize("conflicting", (False, True), ids=("identical", "conflicting"))
def test_post_lock_recheck_resolves_without_count_or_insert(conflicting: bool) -> None:
    fingerprint = _OTHER_FINGERPRINT if conflicting else _FINGERPRINT
    stored_job_fields: dict[str, object] = {}
    if not conflicting:
        stored_job_fields = {
            "video_id": "stored-video-red-c-marker",
            "player_id": "stored-player-red-c-marker",
            "video_reference": "stored-reference-red-c-marker.mp4",
            "callback_url": "https://stored.invalid/red-c-marker",
            "accepted_at": _OTHER_ACCEPTED_AT,
        }
    stored_fields = {
        "request_fingerprint": fingerprint,
        **stored_job_fields,
    }
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(),
            _lookup(_row(**stored_fields)),
        ]
    )
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    if conflicting:
        assert type(outcome) is AnalysisJobIdempotencyConflict
        assert outcome.existing_job_id == _JOB_ID
    else:
        assert type(outcome) is AnalysisJobExisting
        assert outcome.job == _expected_job(**stored_job_fields)
    assert [record.category for record in pool.connection_value.executions] == [
        "statement-timeout",
        "lookup",
        "lock-timeout",
        "advisory-lock",
        "lookup",
    ]
    _assert_no_sensitive_sql(pool.connection_value)
    _assert_success_lifecycle(pool)


def test_queued_capacity_rejection_counts_only_queued_and_writes_nothing() -> None:
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(),
            _lookup(),
            _count_queued(_MAX_QUEUE_SIZE),
        ]
    )
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    assert type(outcome) is AnalysisJobCapacityRejected
    assert [record.category for record in pool.connection_value.executions] == [
        "statement-timeout",
        "lookup",
        "lock-timeout",
        "advisory-lock",
        "lookup",
        "count-queued",
    ]
    _assert_no_sensitive_sql(pool.connection_value)
    assert pool.connection_value.pending_insert is False
    assert pool.connection_value.committed_insert is False
    _assert_success_lifecycle(pool)


def test_insert_binds_candidate_and_returns_authoritative_job_after_commit() -> None:
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(),
            _lookup(),
            _count_queued(_MAX_QUEUE_SIZE - 1),
            _insert(_row(accepted_at=_OTHER_ACCEPTED_AT)),
        ]
    )
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    assert type(outcome) is AnalysisJobAccepted
    assert outcome.job == _expected_job(accepted_at=_OTHER_ACCEPTED_AT)
    assert [record.category for record in pool.connection_value.executions] == [
        "statement-timeout",
        "lookup",
        "lock-timeout",
        "advisory-lock",
        "lookup",
        "count-queued",
        "insert",
    ]
    _assert_no_sensitive_sql(pool.connection_value)
    _assert_success_lifecycle(pool)
    assert pool.connection_value.committed_insert is True
    assert pool.events[-1] == "connection-exit"


@pytest.mark.parametrize("scenario", _SUCCESS_SCENARIOS)
def test_success_outcome_never_escapes_when_transaction_commit_fails(
    scenario: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    pool = _ScriptedPool(
        _success_operations(scenario),
        transaction_exit_error=OperationalError(_DRIVER_MARKER),
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _UNAVAILABLE_MESSAGE,
        markers=(_DRIVER_MARKER, *_CANDIDATE_MARKERS),
    )
    connection = pool.connection_value
    assert connection.commit_attempts == 1
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert pool.connection_exits == 1
    assert connection.operations == []
    assert all(cursor.close_calls == 1 for cursor in connection.cursors)
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize(
    "error_factory",
    (
        lambda: asyncio.CancelledError("commit-cancel-red-c-marker"),
        lambda: _FatalFailure("commit-fatal-red-c-marker"),
    ),
    ids=("cancellation", "fatal"),
)
def test_transaction_exit_base_exception_propagates_by_identity(
    error_factory: Callable[[], BaseException],
) -> None:
    error = error_factory()
    pool = _ScriptedPool(
        _success_operations("capacity-rejected"),
        transaction_exit_error=error,
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is error
    assert pool.connection_value.commit_attempts == 1
    assert pool.connection_value.commits == 0
    assert pool.connection_exits == 1


@pytest.mark.parametrize("phase", ("lookup", "capacity", "insert"))
@pytest.mark.parametrize(
    ("failure_kind", "error_factory"),
    (
        ("ordinary", lambda: OperationalError(_DRIVER_MARKER)),
        ("cancellation", lambda: asyncio.CancelledError("cursor-cancel-red-c-marker")),
        ("fatal", lambda: _FatalFailure("cursor-fatal-red-c-marker")),
    ),
)
def test_cursor_closes_after_fetch_failure_and_preserves_error_policy(
    phase: str,
    failure_kind: str,
    error_factory: Callable[[], BaseException],
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = error_factory()
    pool = _ScriptedPool(_fetch_failure_operations(phase, error))
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    if failure_kind == "ordinary":
        unavailable = cast(
            type[BaseException],
            _require_symbol("AnalysisJobPersistenceUnavailable"),
        )
        _assert_exception_safe(
            captured,
            unavailable,
            _UNAVAILABLE_MESSAGE,
            markers=(_DRIVER_MARKER, *_CANDIDATE_MARKERS),
        )
    else:
        assert captured is error
    target = [
        cursor
        for cursor in pool.connection_value.cursors
        if cursor._operation.category == ("count-queued" if phase == "capacity" else phase)
    ]
    assert len(target) == 1
    assert target[0].close_calls == 1
    _assert_rollback_lifecycle(pool)
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize(
    "close_error_factory",
    (
        lambda: asyncio.CancelledError("successful-fetch-close-cancel-red-c-marker"),
        lambda: _FatalFailure("successful-fetch-close-fatal-red-c-marker"),
    ),
    ids=("cancellation", "fatal"),
)
def test_successful_fetch_cursor_close_base_exception_propagates_by_identity(
    close_error_factory: Callable[[], BaseException],
) -> None:
    close_error = close_error_factory()
    pool = _ScriptedPool([_statement_timeout(), _lookup(_row(), close_error=close_error)])
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is close_error
    lookup_cursor = pool.connection_value.cursors[-1]
    assert lookup_cursor._operation.category == "lookup"
    assert lookup_cursor.fetch_calls == 1
    assert lookup_cursor.close_calls == 1
    assert pool.connection_value.transaction_exits == 1
    assert pool.connection_value.rollback_attempts == 1
    assert pool.connection_exits == 1


@pytest.mark.parametrize("scenario", _SUCCESS_SCENARIOS)
def test_ordinary_connection_exit_failure_suppresses_every_success_outcome(
    scenario: str,
) -> None:
    cleanup_error = OperationalError("connection-cleanup-red-c-marker")
    pool = _ScriptedPool(
        _success_operations(scenario),
        connection_exit_error=cleanup_error,
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    unavailable = cast(
        type[BaseException],
        _require_symbol("AnalysisJobPersistenceUnavailable"),
    )
    _assert_exception_safe(
        captured,
        unavailable,
        _UNAVAILABLE_MESSAGE,
        markers=("connection-cleanup-red-c-marker", *_CANDIDATE_MARKERS),
    )
    assert pool.connection_exits == 1
    assert pool.connection_value.commits == 1
    assert pool.connection_value.operations == []


@pytest.mark.parametrize(
    "cleanup_factory",
    (
        lambda: asyncio.CancelledError("connection-cancel-red-c-marker"),
        lambda: _FatalFailure("connection-fatal-red-c-marker"),
    ),
    ids=("cancellation", "fatal"),
)
def test_connection_exit_base_exception_after_success_propagates_by_identity(
    cleanup_factory: Callable[[], BaseException],
) -> None:
    cleanup_error = cleanup_factory()
    pool = _ScriptedPool(
        _success_operations("initial-existing"),
        connection_exit_error=cleanup_error,
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is cleanup_error
    assert pool.connection_exits == 1
    assert pool.connection_value.commits == 1


@pytest.mark.parametrize("boundary", ("cursor-close", "transaction-exit", "connection-exit"))
def test_ordinary_cleanup_failure_cannot_replace_active_primary_failure(
    boundary: str,
) -> None:
    primary_marker = "primary-driver-red-c-marker"
    cleanup_marker = "ordinary-cleanup-red-c-marker"
    if boundary == "cursor-close":
        pool = _ScriptedPool(
            [
                _statement_timeout(),
                _lookup(
                    fetch_error=OperationalError(primary_marker),
                    close_error=OperationalError(cleanup_marker),
                ),
            ]
        )
    elif boundary == "transaction-exit":
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=OperationalError(primary_marker))],
            transaction_exit_error=OperationalError(cleanup_marker),
        )
    else:
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=OperationalError(primary_marker))],
            connection_exit_error=OperationalError(cleanup_marker),
        )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    unavailable = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    _assert_exception_safe(
        captured,
        unavailable,
        _UNAVAILABLE_MESSAGE,
        markers=(primary_marker, cleanup_marker, *_CANDIDATE_MARKERS),
    )
    _assert_primary_failure_cleanup_lifecycle(pool, boundary)


@pytest.mark.parametrize("boundary", ("cursor-close", "transaction-exit", "connection-exit"))
@pytest.mark.parametrize(
    "cleanup_factory",
    (
        lambda: asyncio.CancelledError("cancelled-cleanup-red-c-marker"),
        lambda: _FatalFailure("fatal-cleanup-red-c-marker"),
    ),
    ids=("cleanup-cancellation", "cleanup-fatal"),
)
def test_cleanup_base_exception_wins_with_sanitized_primary_reachable(
    boundary: str,
    cleanup_factory: Callable[[], BaseException],
) -> None:
    primary_marker = "primary-driver-red-c-marker"
    cleanup_error = cleanup_factory()
    if boundary == "cursor-close":
        pool = _ScriptedPool(
            [
                _statement_timeout(),
                _lookup(
                    fetch_error=OperationalError(primary_marker),
                    close_error=cleanup_error,
                ),
            ]
        )
    elif boundary == "transaction-exit":
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=OperationalError(primary_marker))],
            transaction_exit_error=cleanup_error,
        )
    else:
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=OperationalError(primary_marker))],
            connection_exit_error=cleanup_error,
        )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is cleanup_error
    unavailable = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    _assert_graph_contains_sanitized_primary(
        captured,
        unavailable,
        _UNAVAILABLE_MESSAGE,
        markers=(primary_marker, *_CANDIDATE_MARKERS),
    )
    _assert_primary_failure_cleanup_lifecycle(pool, boundary)


@pytest.mark.parametrize("boundary", ("cursor-close", "transaction-exit", "connection-exit"))
@pytest.mark.parametrize(
    "primary_factory",
    (
        lambda: asyncio.CancelledError("primary-cancel-red-c-marker"),
        lambda: _FatalFailure("primary-fatal-red-c-marker"),
    ),
    ids=("cancellation", "fatal"),
)
def test_primary_base_exception_survives_ordinary_cleanup_failure(
    boundary: str,
    primary_factory: Callable[[], BaseException],
) -> None:
    primary_error = primary_factory()
    cleanup_error = OperationalError("ordinary-cleanup-red-c-marker")
    if boundary == "cursor-close":
        pool = _ScriptedPool(
            [
                _statement_timeout(),
                _lookup(fetch_error=primary_error, close_error=cleanup_error),
            ]
        )
    elif boundary == "transaction-exit":
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=primary_error)],
            transaction_exit_error=cleanup_error,
        )
    else:
        pool = _ScriptedPool(
            [_statement_timeout(), _lookup(execute_error=primary_error)],
            connection_exit_error=cleanup_error,
        )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is primary_error
    _assert_primary_failure_cleanup_lifecycle(pool, boundary)


def test_advisory_lock_timeout_is_retryable_sanitized_and_rolls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(type[BaseException], _require_symbol("AnalysisJobAdmissionLockTimeout"))
    captured_errors: list[BaseException] = []
    for marker in (_DRIVER_MARKER, "second-driver-red-c-marker"):
        driver_error = LockNotAvailable(marker)
        pool = _ScriptedPool(
            [
                _statement_timeout(),
                _lookup(),
                _lock_timeout(),
                _advisory_lock(execute_error=driver_error),
            ]
        )
        repository = _repository(pool)

        captured = _capture(repository, _candidate())

        _assert_exception_safe(
            captured,
            exception_type,
            _LOCK_TIMEOUT_MESSAGE,
            markers=(marker, *_CANDIDATE_MARKERS),
        )
        captured_errors.append(captured)
        _assert_rollback_lifecycle(pool)
        assert all(record.category != "insert" for record in pool.connection_value.executions)
    assert captured_errors[0].args == captured_errors[1].args
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize(
    "driver_error",
    (
        PoolTimeout(_DRIVER_MARKER),
        OperationalError(_DRIVER_MARKER),
        InterfaceError(_DRIVER_MARKER),
    ),
    ids=("pool-timeout", "operational", "interface"),
)
def test_pool_acquisition_psycopg_failures_map_to_fixed_unavailable_error(
    driver_error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    pool = _ScriptedPool([], enter_error=driver_error)
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _UNAVAILABLE_MESSAGE,
        markers=(_DRIVER_MARKER, *_CANDIDATE_MARKERS),
    )
    assert pool.connection_calls == [_ACQUIRE_TIMEOUT_SECONDS]
    assert pool.connection_entries == 0
    assert pool.connection_exits == 0
    assert pool.acquisition_failures == 1
    assert pool.connection_value.transaction_entries == 0
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize(
    "phase",
    (
        "statement-timeout",
        "initial-lookup",
        "lock-timeout",
        "advisory-cursor-close",
        "capacity",
        "insert",
        "transaction-exit",
        "lookup-cursor-close",
        "connection-exit",
    ),
)
def test_55p03_outside_advisory_execution_is_never_lock_timeout(
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    pool = _sqlstate_failure_pool(phase, LockNotAvailable(_DRIVER_MARKER))
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _UNAVAILABLE_MESSAGE,
        markers=(_DRIVER_MARKER, *_CANDIDATE_MARKERS),
    )
    assert pool.connection_exits == 1
    assert pool.connection_value.operations == []
    if phase in ("transaction-exit", "connection-exit"):
        assert pool.connection_value.commit_attempts == 1
    else:
        assert pool.connection_value.rollback_attempts == 1
    assert all(cursor.close_calls == 1 for cursor in pool.connection_value.cursors)
    if phase == "advisory-cursor-close":
        assert [record.category for record in pool.connection_value.executions] == [
            "statement-timeout",
            "lookup",
            "lock-timeout",
            "advisory-lock",
        ]
        assert all(
            record.category not in {"count-queued", "insert"}
            for record in pool.connection_value.executions
        )
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


def test_non_55p03_psycopg_failure_during_advisory_execution_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(type[BaseException], _require_symbol("AnalysisJobPersistenceUnavailable"))
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(execute_error=OperationalError(_DRIVER_MARKER)),
        ]
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _UNAVAILABLE_MESSAGE,
        markers=(_DRIVER_MARKER, *_CANDIDATE_MARKERS),
    )
    _assert_rollback_lifecycle(pool)
    assert all(record.category != "insert" for record in pool.connection_value.executions)
    _assert_logs_safe(caplog.records, (_DRIVER_MARKER, *_CANDIDATE_MARKERS))


def test_programming_exception_with_55p03_attribute_propagates_by_identity() -> None:
    error = _SqlstateProgrammingError("programming-sqlstate-red-c-marker")
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(execute_error=error),
        ]
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is error
    _assert_rollback_lifecycle(pool)


_MALFORMED_ROWS = (
    (_row()[:-1], "missing-column", "missing-column"),
    (
        _row() + ("extra-row-red-c-marker",),
        "extra-column",
        "extra-row-red-c-marker",
    ),
    (
        _row(job_id="uuid-row-red-c-marker"),
        "invalid-job-id",
        "uuid-row-red-c-marker",
    ),
    (_row(idempotency_lookup=b""), "lookup-empty", b"empty-lookup-marker"),
    (
        _row(idempotency_lookup="lookup-text-red-c-marker"),
        "lookup-text",
        "lookup-text-red-c-marker",
    ),
    (
        _row(idempotency_lookup=bytearray(b"lookup-bytearray-red-c-marker")),
        "lookup-bytearray",
        b"lookup-bytearray-red-c-marker",
    ),
    (
        _row(idempotency_lookup=memoryview(b"lookup-memoryview-red-c-marker")),
        "lookup-memoryview",
        b"lookup-memoryview-red-c-marker",
    ),
    (
        _row(idempotency_lookup=_BytesSubclass(b"lookup-subclass-red-c-marker")),
        "lookup-bytes-subclass",
        b"lookup-subclass-red-c-marker",
    ),
    (_row(request_fingerprint=b""), "fingerprint-empty", b"empty-fingerprint-marker"),
    (
        _row(request_fingerprint=b"short-fingerprint-red-c"),
        "fingerprint-short",
        b"short-fingerprint-red-c",
    ),
    (
        _row(request_fingerprint=b"long-fingerprint-red-c-marker-value-33"),
        "fingerprint-long",
        b"long-fingerprint-red-c-marker-value-33",
    ),
    (
        _row(request_fingerprint="fingerprint-text-red-c-marker"),
        "fingerprint-text",
        "fingerprint-text-red-c-marker",
    ),
    (
        _row(request_fingerprint=bytearray(b"fingerprint-bytearray-red-c-0000")),
        "fingerprint-bytearray",
        b"fingerprint-bytearray-red-c-0000",
    ),
    (
        _row(request_fingerprint=memoryview(b"fingerprint-memoryview-red-c-000")),
        "fingerprint-memoryview",
        b"fingerprint-memoryview-red-c-000",
    ),
    (
        _row(request_fingerprint=_BytesSubclass(b"fingerprint-subclass-red-c-00000")),
        "fingerprint-bytes-subclass",
        b"fingerprint-subclass-red-c-00000",
    ),
    (
        _row(state="STATE_ROW_RED_C_MARKER"),
        "invalid-state",
        "STATE_ROW_RED_C_MARKER",
    ),
    (_row(video_id=""), "video-id-empty", "empty-video-id-marker"),
    (_row(player_id=""), "player-id-empty", "empty-player-id-marker"),
    (_row(video_reference=""), "video-reference-empty", "empty-reference-marker"),
    (_row(callback_url=""), "callback-url-empty", "empty-callback-marker"),
    (_row(video_id=137), "video-id-non-text", "video-id-non-text"),
    (_row(player_id=137), "player-id-non-text", "player-id-non-text"),
    (_row(video_reference=137), "video-reference-non-text", "reference-non-text"),
    (_row(callback_url=137), "callback-url-non-text", "callback-non-text"),
    (
        _row(video_id=_TextSubclass("video-subclass-red-c-marker")),
        "video-id-text-subclass",
        "video-subclass-red-c-marker",
    ),
    (
        _row(player_id=_TextSubclass("player-subclass-red-c-marker")),
        "player-id-text-subclass",
        "player-subclass-red-c-marker",
    ),
    (
        _row(video_reference=_TextSubclass("reference-subclass-red-c-marker")),
        "video-reference-text-subclass",
        "reference-subclass-red-c-marker",
    ),
    (
        _row(callback_url=_TextSubclass("callback-subclass-red-c-marker")),
        "callback-url-text-subclass",
        "callback-subclass-red-c-marker",
    ),
    (
        _row(accepted_at="accepted-at-row-red-c-marker"),
        "timestamp-text",
        "accepted-at-row-red-c-marker",
    ),
    (
        _row(accepted_at=datetime(2026, 10, 1, 12, 0)),
        "timestamp-naive",
        "timestamp-naive-marker",
    ),
    (_row(accepted_at=object()), "timestamp-object", "timestamp-object-marker"),
)


@pytest.mark.parametrize(
    ("malformed_row", "rejected_marker"),
    tuple((value, marker) for value, _, marker in _MALFORMED_ROWS),
    ids=tuple(identifier for _, identifier, _ in _MALFORMED_ROWS),
)
def test_malformed_database_row_maps_to_fixed_invariant_error(
    malformed_row: tuple[object, ...],
    rejected_marker: str | bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(
        type[BaseException], _require_symbol("AnalysisJobPersistenceInvariantError")
    )
    pool = _ScriptedPool([_statement_timeout(), _lookup(malformed_row)])
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _INVARIANT_MESSAGE,
        markers=(rejected_marker, *_CANDIDATE_MARKERS),
    )
    _assert_rollback_lifecycle(pool)
    _assert_logs_safe(caplog.records, (rejected_marker, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize("lookup", (b"x", b"x" * 257), ids=("one-byte", "257-bytes"))
def test_nonempty_protected_lookup_has_no_fixed_or_maximum_length(lookup: bytes) -> None:
    candidate = _candidate(idempotency_lookup=lookup)
    pool = _ScriptedPool(
        [_statement_timeout(), _lookup(_row(idempotency_lookup=lookup), lookup=lookup)]
    )
    repository = _repository(pool)

    outcome = _run(repository, candidate)

    assert type(outcome) is AnalysisJobExisting
    assert outcome.job == _expected_job()
    _assert_success_lifecycle(pool)


@pytest.mark.parametrize(
    "state",
    ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
)
def test_lookup_decodes_every_approved_analysis_job_state(state: str) -> None:
    pool = _ScriptedPool([_statement_timeout(), _lookup(_row(state=state))])
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    assert type(outcome) is AnalysisJobExisting
    assert outcome.job == _expected_job(state=AnalysisJobState(state))
    _assert_success_lifecycle(pool)


def test_lookup_preserves_non_utc_aware_accepted_at() -> None:
    accepted_at = datetime(
        2026,
        10,
        1,
        17,
        30,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    pool = _ScriptedPool([_statement_timeout(), _lookup(_row(accepted_at=accepted_at))])
    repository = _repository(pool)

    outcome = _run(repository, _candidate())

    assert type(outcome) is AnalysisJobExisting
    assert outcome.job.accepted_at is accepted_at
    assert outcome.job == _expected_job(accepted_at=accepted_at)
    _assert_success_lifecycle(pool)


_INCONSISTENT_INSERT_ROWS = (
    (None, "missing-row", "missing-insert-row"),
    (_row(job_id=_OTHER_JOB_ID), "mismatch-job-id", str(_OTHER_JOB_ID)),
    (_row(idempotency_lookup=_OTHER_LOOKUP), "mismatch-lookup", _OTHER_LOOKUP),
    (
        _row(request_fingerprint=_OTHER_FINGERPRINT),
        "mismatch-fingerprint",
        _OTHER_FINGERPRINT,
    ),
    (
        _row(video_id="different-video-red-c-marker"),
        "mismatch-video-id",
        "different-video-red-c-marker",
    ),
    (
        _row(player_id="different-player-red-c-marker"),
        "mismatch-player-id",
        "different-player-red-c-marker",
    ),
    (
        _row(video_reference="different-reference-red-c-marker.mp4"),
        "mismatch-video-reference",
        "different-reference-red-c-marker.mp4",
    ),
    (
        _row(callback_url="https://different.invalid/red-c-marker"),
        "mismatch-callback-url",
        "https://different.invalid/red-c-marker",
    ),
    (_row(state="RUNNING"), "mismatch-state", "RUNNING"),
    (_row()[:-1], "decoder-missing-column", "missing-insert-column"),
    (
        _row() + ("insert-extra-red-c-marker",),
        "decoder-extra-column",
        "insert-extra-red-c-marker",
    ),
    (
        _row(job_id="insert-uuid-red-c-marker"),
        "decoder-invalid-uuid",
        "insert-uuid-red-c-marker",
    ),
    (
        _row(idempotency_lookup=bytearray(b"insert-lookup-red-c-marker")),
        "decoder-invalid-lookup",
        b"insert-lookup-red-c-marker",
    ),
    (
        _row(request_fingerprint=b"insert-short-fingerprint"),
        "decoder-invalid-fingerprint",
        b"insert-short-fingerprint",
    ),
    (
        _row(state="INSERT_STATE_RED_C_MARKER"),
        "decoder-invalid-state",
        "INSERT_STATE_RED_C_MARKER",
    ),
    (_row(video_id=""), "decoder-empty-text", "empty-insert-text"),
    (
        _row(callback_url=b"insert-callback-red-c-marker"),
        "decoder-non-text",
        b"insert-callback-red-c-marker",
    ),
    (
        _row(accepted_at="insert-timestamp-red-c-marker"),
        "decoder-timestamp-text",
        "insert-timestamp-red-c-marker",
    ),
    (
        _row(accepted_at=datetime(2026, 10, 1, 12, 0)),
        "decoder-timestamp-naive",
        "insert-naive-timestamp",
    ),
)


@pytest.mark.parametrize(
    ("returned_row", "rejected_marker"),
    tuple((value, marker) for value, _, marker in _INCONSISTENT_INSERT_ROWS),
    ids=tuple(identifier for _, identifier, _ in _INCONSISTENT_INSERT_ROWS),
)
def test_inconsistent_insert_returning_rolls_back_pending_insert(
    returned_row: tuple[object, ...] | None,
    rejected_marker: str | bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_type = cast(
        type[BaseException], _require_symbol("AnalysisJobPersistenceInvariantError")
    )
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(),
            _lock_timeout(),
            _advisory_lock(),
            _lookup(),
            _count_queued(0),
            _insert(returned_row),
        ]
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    _assert_exception_safe(
        captured,
        exception_type,
        _INVARIANT_MESSAGE,
        markers=(rejected_marker, *_CANDIDATE_MARKERS),
    )
    _assert_rollback_lifecycle(pool)
    assert pool.connection_value.committed_insert is False
    _assert_logs_safe(caplog.records, (rejected_marker, *_CANDIDATE_MARKERS))


@pytest.mark.parametrize(
    "error",
    (
        RuntimeError("programming-runtime-red-c-marker"),
        AttributeError("programming-attribute-red-c-marker"),
        TypeError("programming-type-red-c-marker"),
        AssertionError("programming-assertion-red-c-marker"),
        asyncio.CancelledError("cancelled-red-c-marker"),
        KeyboardInterrupt("keyboard-red-c-marker"),
        SystemExit("system-exit-red-c-marker"),
        MemoryError("memory-red-c-marker"),
    ),
    ids=(
        "runtime",
        "attribute",
        "type",
        "assertion",
        "cancellation",
        "keyboard-interrupt",
        "system-exit",
        "memory-error",
    ),
)
def test_programming_cancellation_and_fatal_errors_propagate_by_identity(
    error: BaseException,
) -> None:
    pool = _ScriptedPool(
        [
            _statement_timeout(),
            _lookup(execute_error=error),
        ]
    )
    repository = _repository(pool)

    captured = _capture(repository, _candidate())

    assert captured is error
    _assert_rollback_lifecycle(pool)
    assert all(record.category != "insert" for record in pool.connection_value.executions)
