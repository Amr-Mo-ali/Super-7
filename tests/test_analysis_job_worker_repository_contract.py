"""RED-H: scripted persistence observations, not a database or Worker implementation.

Authority: Planning-F and Planning-H construction/row contracts (C1-C10).
SQL tokens and supplied parameters are observations, not a SQL parser or proof of
projection origin, locking, clock provenance, or live concurrency. Those require
independent GREEN-H source review and later authorized PostgreSQL evidence.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import partial
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import FunctionType, MethodType, ModuleType
from typing import TYPE_CHECKING, Any, Never, cast, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

import pytest
from psycopg import InterfaceError, OperationalError
from psycopg.errors import CheckViolation, LockNotAvailable, QueryCanceled, UniqueViolation
from psycopg_pool import PoolTimeout

from adapters.psycopg_database import PsycopgDatabasePool
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
)
from services import analysis_job_worker as worker

if TYPE_CHECKING:
    from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository

    def _real_pool_composition_witness(
        pool: PsycopgDatabasePool, factory: Callable[[], UUID]
    ) -> worker.AnalysisJobWorkerRepository:
        repository: worker.AnalysisJobWorkerRepository = PsycopgAnalysisJobWorkerRepository(
            pool=pool,
            connection_acquire_timeout_seconds=1.25,
            operation_timeout_ms=4321,
            lock_timeout_ms=1234,
            lease_id_factory=factory,
        )
        return repository


_MODULE = "adapters.psycopg_analysis_job_worker_repository"
_MISSING = (
    "Slice 3 RED-H: adapters.psycopg_analysis_job_worker_repository is missing; "
    "GREEN-H must implement the approved worker persistence adapter."
)
_UNAVAILABLE = "PostgreSQL AnalysisJob worker persistence is unavailable."
_INVARIANT = "PostgreSQL AnalysisJob worker persistence invariant failed."
_SLOT_INDEX = "analysis_job_attempts_active_worker_slot_id_idx"
_JOB = UUID("12345678-1234-5678-1234-567812345678")
_OTHER_JOB = UUID("87654321-4321-8765-4321-876543218765")
_SLOT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OTHER = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
_START = _NOW - timedelta(seconds=10)
_EXPIRY = _NOW + timedelta(seconds=10)
_DURATION = LeaseDuration(timedelta(seconds=30, microseconds=1))
_AUTHORITY = AttemptAuthority(_JOB, 2, WorkerSlotId(_SLOT), LeaseId(_LEASE))
_J_COLUMNS = (
    "job_id",
    "video_id",
    "player_id",
    "video_reference",
    "callback_url",
    "analysis_version",
    "state",
    "attempt_limit",
    "attempt_count",
    "accepted_at",
)
_A_COLUMNS = (
    "job_id",
    "attempt_number",
    "worker_slot_id",
    "lease_id",
    "status",
    "started_at",
    "heartbeat_at",
    "lease_expires_at",
    "finished_at",
    "safe_failure_code",
)
_METHODS = (
    "claim_next",
    "renew_lease",
    "finalize_retryable_failure",
    "finalize_terminal_failure",
    "recover_expired",
)


def _module() -> ModuleType:
    if find_spec(_MODULE) is None:
        pytest.fail(_MISSING, pytrace=False)
    return import_module(_MODULE)


def _j(**changes: object) -> tuple[object, ...]:
    values: dict[str, object] = dict(
        job_id=_JOB,
        video_id="Video_123",
        player_id="Player_456",
        video_reference="Match-é.MP4",
        callback_url="https://example.com/callback",
        analysis_version="Version_1.0",
        state="RUNNING",
        attempt_limit=3,
        attempt_count=2,
        accepted_at=_NOW + timedelta(days=1),
    )
    values.update(changes)
    return tuple(values[name] for name in _J_COLUMNS)


def _a(**changes: object) -> tuple[object, ...]:
    values: dict[str, object] = dict(
        job_id=_JOB,
        attempt_number=2,
        worker_slot_id=_SLOT,
        lease_id=_LEASE,
        status="ACTIVE",
        started_at=_START,
        heartbeat_at=_NOW,
        lease_expires_at=_EXPIRY,
        finished_at=None,
        safe_failure_code=None,
    )
    values.update(changes)
    return tuple(values[name] for name in _A_COLUMNS)


@dataclass(frozen=True)
class _Step:
    label: str
    rows: object = None
    tokens: tuple[str, ...] = ()
    bound: tuple[object, ...] = ()
    execute_error: BaseException | None = None
    fetch_error: BaseException | None = None
    close_error: BaseException | None = None


class _Cursor:
    def __init__(self, step: _Step, events: list[str]) -> None:
        self.step, self.events = step, events
        self.fetches = 0
        self.closes = 0

    async def fetchone(self) -> tuple[object, ...] | None:
        raise AssertionError("C2 requires fetchall; singleton truncation is forbidden")

    async def fetchall(self) -> list[tuple[object, ...]]:
        self.fetches += 1
        self.events.append(f"fetch:{self.step.label}")
        if self.step.fetch_error is not None:
            raise self.step.fetch_error
        return cast(list[tuple[object, ...]], self.step.rows)

    async def close(self) -> None:
        self.closes += 1
        self.events.append(f"close:{self.step.label}")
        if self.step.close_error is not None:
            raise self.step.close_error


class _Transaction(AbstractAsyncContextManager[object]):
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    async def __aenter__(self) -> object:
        self.pool.events.append("transaction-enter")
        if self.pool.transaction_enter_error is not None:
            raise self.pool.transaction_enter_error
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        self.pool.events.append("rollback" if exc is not None else "commit")
        self.pool.exit_primaries.append(exc)
        if self.pool.transaction_exit_error is not None:
            raise self.pool.transaction_exit_error


class _Connection:
    def __init__(self, pool: _Pool, steps: list[_Step]) -> None:
        self.pool, self.steps = pool, list(steps)
        self.cursors: list[_Cursor] = []
        self.executions: list[tuple[str, str, tuple[object, ...] | None]] = []

    def transaction(self) -> AbstractAsyncContextManager[object]:
        return _Transaction(self.pool)

    async def execute(self, query: str, params: tuple[object, ...] | None = None) -> _Cursor:
        assert self.steps, "Unscripted statement/internal retry"
        assert type(query) is str
        assert params is None or type(params) is tuple
        step = self.steps.pop(0)
        normalized = " ".join(query.lower().split()).removesuffix(";")
        assert "pg_advisory" not in normalized
        assert not any(marker in query for marker in (str(_JOB), str(_LEASE), "example.com"))
        assert all(token in normalized for token in step.tokens), step.label
        if step.label in ("statement-timeout", "lock-timeout"):
            assert normalized == "select set_config(%s, %s, %s)"
            assert params == step.bound
        else:
            assert all(value in (params or ()) for value in step.bound), step.label
        self.executions.append((step.label, query, params))
        self.pool.events.append(f"execute:{step.label}")
        if step.execute_error is not None:
            raise step.execute_error
        cursor = _Cursor(step, self.pool.events)
        self.cursors.append(cursor)
        return cursor


class _ConnectionContext(AbstractAsyncContextManager[_Connection]):
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _Connection:
        self.pool.events.append("connection-enter")
        if self.pool.acquire_error is not None:
            raise self.pool.acquire_error
        return self.pool.value

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        self.pool.events.append("release")
        self.pool.exit_primaries.append(exc)
        if self.pool.release_error is not None:
            raise self.pool.release_error


class _Pool:
    """Rows/errors are a finite script. No state, clock, SQL or policy is executed."""

    def __init__(self, steps: list[_Step]) -> None:
        self.events: list[str] = []
        self.calls: list[float] = []
        self.exit_primaries: list[BaseException | None] = []
        self.acquire_error: BaseException | None = None
        self.transaction_enter_error: BaseException | None = None
        self.transaction_exit_error: BaseException | None = None
        self.release_error: BaseException | None = None
        self.value = _Connection(self, steps)

    def connection(self, *, timeout_seconds: float) -> AbstractAsyncContextManager[_Connection]:
        self.calls.append(timeout_seconds)
        self.events.append("acquire")
        return _ConnectionContext(self)


class _Factory:
    def __init__(
        self, pool: _Pool, value: object = _LEASE, error: BaseException | None = None
    ) -> None:
        self.pool, self.value, self.error = pool, value, error
        self.calls = 0

    def __call__(self) -> UUID:
        self.calls += 1
        self.pool.events.append("factory")
        if self.error is not None:
            raise self.error
        return cast(UUID, self.value)


def _repository(pool: _Pool, factory: object | None = None, **changes: object) -> Any:
    values: dict[str, object] = dict(
        pool=pool,
        connection_acquire_timeout_seconds=1.25,
        operation_timeout_ms=4321,
        lock_timeout_ms=1234,
        lease_id_factory=_Factory(pool) if factory is None else factory,
    )
    values.update(changes)
    return _module().PsycopgAnalysisJobWorkerRepository(**values)


def _settings() -> list[_Step]:
    return [
        _Step("statement-timeout", bound=("statement_timeout", "4321", True)),
        _Step("lock-timeout", bound=("lock_timeout", "1234", True)),
    ]


def _read(label: str, rows: object, *bound: object) -> _Step:
    # These bounded token checks record intended statement boundaries, not semantics.
    table = (
        "analysis_job_attempts"
        if label in ("locator", "attempt", "recovery-attempt", "active")
        else "analysis_jobs"
    )
    tokens: tuple[str, ...] = ("clock_timestamp",) if label == "clock" else ("select", table)
    if label in ("job", "attempt", "recovery-attempt", "candidate"):
        tokens += ("for update",)
    if label == "recovery-attempt":
        tokens += ("skip locked",)
    if label in ("candidate", "recovery"):
        tokens += ("for update", "skip locked", "order by", "limit")
    return _Step(label, rows, tokens, tuple(bound))


def _write(label: str, rows: object, *bound: object) -> _Step:
    table = "analysis_jobs" if label == "job-write" else "analysis_job_attempts"
    verb = "insert" if label == "insert" else "update"
    return _Step(label, rows, (verb, table, "returning"), tuple(bound))


def _slot(job: object = None, attempt: object = None, locator: object = None) -> list[_Step]:
    return [
        *_settings(),
        _read("locator", [(_JOB, 2)] if locator is None else locator, _SLOT),
        _read("job", [_j()] if job is None else job, _JOB),
        _read("attempt", [_a()] if attempt is None else attempt, _JOB, 2),
    ]


def _owner(job: object = None, attempt: object = None) -> list[_Step]:
    return [
        *_settings(),
        _read("job", [_j()] if job is None else job, _JOB),
        _read("attempt", [_a()] if attempt is None else attempt, _JOB, 2),
    ]


def _fresh() -> list[_Step]:
    return [
        *_settings(),
        _read("locator", [], _SLOT),
        _read("candidate", [_j(state="QUEUED", attempt_count=1)]),
        _read(
            "attempt",
            [_a(attempt_number=1, status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW)],
            _JOB,
            1,
        ),
        _read("active", [], _JOB),
    ]


def _fresh_writes(job: object = None, attempt: object = None) -> list[_Step]:
    return [
        _write("job-write", [_j()] if job is None else job, _JOB),
        _write(
            "insert",
            [_a(started_at=_NOW, lease_expires_at=_NOW + _DURATION.value)]
            if attempt is None
            else attempt,
            _JOB,
            2,
            _SLOT,
            _LEASE,
        ),
    ]


def _run(repository: Any, method: str = "claim_next", **changes: object) -> object:
    operations: dict[str, dict[str, object]] = {
        "claim_next": dict(worker_slot_id=WorkerSlotId(_SLOT), lease_duration=_DURATION),
        "renew_lease": dict(authority=_AUTHORITY, lease_duration=_DURATION),
        "finalize_retryable_failure": dict(
            authority=_AUTHORITY, failure_code=AttemptFailureCode("retry")
        ),
        "finalize_terminal_failure": dict(
            authority=_AUTHORITY, failure_code=AttemptFailureCode("retry")
        ),
        "recover_expired": dict(batch_size=RecoveryBatchSize(2)),
    }
    values = operations[method]
    values.update(changes)
    return asyncio.run(getattr(repository, method)(**values))


def _capture(repository: Any, method: str = "claim_next", **changes: object) -> BaseException:
    captured: BaseException | None = None
    try:
        _run(repository, method, **changes)
    except BaseException as error:
        captured = error
    assert captured is not None, "Expected failure, not a successful outcome"
    return captured


def _done(pool: _Pool, *, rollback: bool = False, remaining: bool = False) -> None:
    assert pool.calls == [1.25]
    assert pool.events.count("transaction-enter") == 1
    assert pool.events.count("rollback") == int(rollback)
    assert pool.events.count("commit") == int(not rollback)
    assert pool.events[-1] == "release"
    if not remaining:
        assert pool.value.steps == []
    assert all(cursor.closes == 1 for cursor in pool.value.cursors)
    assert all(
        cursor.fetches == (0 if "timeout" in cursor.step.label else 1)
        for cursor in pool.value.cursors
    )


def _fixed(error: BaseException, unavailable: bool = False) -> None:
    cls = (
        worker.AnalysisJobWorkerPersistenceUnavailable
        if unavailable
        else worker.AnalysisJobWorkerPersistenceInvariantError
    )
    assert type(error) is cls
    assert error.args == (_UNAVAILABLE if unavailable else _INVARIANT,)
    assert error.__dict__ == {}
    assert error.__cause__ is None
    assert error.__context__ is None


class _Int(int):
    pass


class _Text(str):
    pass


class _Uuid(UUID):
    pass


class _Fatal(BaseException):
    pass


class _Fold(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=0 if dt is not None and dt.fold else 1)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "local-fold"


def _instant(value: datetime) -> timedelta:
    offset = value.utcoffset()
    assert offset is not None
    return (value.replace(tzinfo=None) - datetime.min) - offset


def _fold_fixture_expiry(heartbeat: datetime) -> datetime:
    """Add elapsed time in the bounded fold fixture, retaining its offset/fold."""
    offset = heartbeat.utcoffset()
    assert offset is not None
    absolute = heartbeat.replace(tzinfo=None) - offset + timedelta(microseconds=1)
    return (absolute + offset).replace(tzinfo=heartbeat.tzinfo, fold=heartbeat.fold)


def _claimable_fixture(accepted_at: datetime) -> ClaimableAnalysisJob:
    return ClaimableAnalysisJob(
        _JOB,
        "Video_123",
        "Player_456",
        "Match-é.MP4",
        "https://example.com/callback",
        "Version_1.0",
        3,
        accepted_at,
    )


def _assert_claim_payload(job: ClaimableAnalysisJob, row: tuple[object, ...]) -> None:
    assert job.job_id is row[0]
    assert job.video_id == row[1]
    assert job.player_id == row[2]
    assert job.video_reference == row[3]
    assert job.callback_url == row[4]
    assert job.analysis_version == row[5]
    assert job.attempt_limit == row[7]
    assert job.accepted_at is row[9]


def _assert_renewal_factory_unused(factory: _Factory) -> None:
    assert factory.calls == 0


def _assert_finalization_limit(
    outcome: worker.AttemptRequeued | worker.AttemptExhausted, limit: int
) -> None:
    assert outcome.attempt_limit == limit


@pytest.mark.parametrize("fold", (0, 1))
def test_helper_recovery_fold_fixture_is_valid_active(fold: int) -> None:
    heartbeat = datetime(2026, 10, 25, 1, 30, tzinfo=_Fold(), fold=fold)
    expiry = _fold_fixture_expiry(heartbeat)
    claim = WorkerClaim(
        _AUTHORITY,
        _claimable_fixture(_NOW),
        datetime(2026, 10, 25, tzinfo=UTC),
        heartbeat,
        expiry,
    )
    assert _instant(expiry) - _instant(heartbeat) == timedelta(microseconds=1)
    assert expiry.tzinfo is heartbeat.tzinfo
    assert expiry.utcoffset() == heartbeat.utcoffset() and expiry.fold == heartbeat.fold
    assert claim.heartbeat_at is heartbeat and claim.lease_expires_at is expiry


@pytest.mark.parametrize("extra_call", (False, True))
def test_helper_renewal_factory_assertion_rejects_an_extra_call(extra_call: bool) -> None:
    factory = _Factory(_Pool([]))
    if extra_call:
        factory()
        with pytest.raises(AssertionError):
            _assert_renewal_factory_unused(factory)
    else:
        _assert_renewal_factory_unused(factory)


@pytest.mark.parametrize(
    "field",
    (
        None,
        "job_id",
        "video_id",
        "player_id",
        "video_reference",
        "callback_url",
        "analysis_version",
        "attempt_limit",
        "accepted_at",
    ),
)
def test_helper_claim_payload_assertion_checks_every_j_field(field: str | None) -> None:
    row = _j()
    job = _claimable_fixture(cast(datetime, row[9]))
    if field is None:
        _assert_claim_payload(job, row)
    else:
        alternatives = {
            "job_id": replace(job, job_id=_OTHER_JOB),
            "video_id": replace(job, video_id="OtherVideo"),
            "player_id": replace(job, player_id="OtherPlayer"),
            "video_reference": replace(job, video_reference="other.mp4"),
            "callback_url": replace(job, callback_url="https://example.com/other"),
            "analysis_version": replace(job, analysis_version="Version_2.0"),
            "attempt_limit": replace(job, attempt_limit=7),
            "accepted_at": replace(
                job, accepted_at=job.accepted_at.astimezone(timezone(timedelta(hours=3)))
            ),
        }
        altered = alternatives[field]
        with pytest.raises(AssertionError):
            _assert_claim_payload(altered, row)


@pytest.mark.parametrize("kind", ("requeued", "exhausted"))
@pytest.mark.parametrize("changed", (False, True))
def test_helper_finalization_limit_assertion_rejects_valid_wrong_policy(
    kind: str, changed: bool
) -> None:
    limit = 3 if kind == "requeued" else 2
    supplied = 1 if changed else limit
    outcome: worker.AttemptRequeued | worker.AttemptExhausted = (
        worker.AttemptRequeued(_AUTHORITY, supplied)
        if kind == "requeued"
        else worker.AttemptExhausted(_AUTHORITY, supplied, AttemptFailureCode("retry"))
    )
    if changed:
        with pytest.raises(AssertionError):
            _assert_finalization_limit(outcome, limit)
    else:
        _assert_finalization_limit(outcome, limit)


def test_helper_exact_absence_and_import_defects(monkeypatch: pytest.MonkeyPatch) -> None:
    target = sys.modules[__name__]
    monkeypatch.setattr(target, "find_spec", lambda name: None)
    with pytest.raises(pytest.fail.Exception, match="^" + _MISSING.replace(".", r"\.") + "$"):
        _module()
    monkeypatch.setattr(target, "find_spec", lambda name: object())
    for error in (
        ModuleNotFoundError("transitive", name="dependency"),
        SyntaxError("defect"),
        ImportError("defect"),
    ):

        def broken(name: str, failure: Exception = error) -> ModuleType:
            raise failure

        monkeypatch.setattr(target, "import_module", broken)
        with pytest.raises(type(error)) as caught:
            _module()
        assert caught.value is error


def test_helper_ports_record_only_supplied_observations_and_errors() -> None:
    error = OperationalError("driver-marker")
    pool = _Pool([_read("job", [_j()], _JOB), _Step("failure", execute_error=error)])

    async def observe() -> None:
        async with pool.connection(timeout_seconds=1.25) as connection:
            async with connection.transaction():
                cursor = await connection.execute("SELECT * FROM analysis_jobs FOR UPDATE", (_JOB,))
                assert await cursor.fetchall() == [_j()]
                await cursor.close()
                await connection.execute("SELECT 1")

    with pytest.raises(OperationalError) as caught:
        asyncio.run(observe())
    assert caught.value is error
    _done(pool, rollback=True)
    assert pool.events == [
        "acquire",
        "connection-enter",
        "transaction-enter",
        "execute:job",
        "fetch:job",
        "close:job",
        "execute:failure",
        "rollback",
        "release",
    ]


@pytest.mark.parametrize("upper", (False, True))
def test_helper_exact_absolute_arithmetic_extremes_and_folds(upper: bool) -> None:
    zone = _Fold()
    left = datetime(2026, 10, 25, 1, 45, tzinfo=zone, fold=0)
    right = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    assert right - left == timedelta(minutes=-15)
    assert _instant(right) - _instant(left) == timedelta(minutes=45)
    extreme = (datetime.max if upper else datetime.min).replace(
        tzinfo=timezone(timedelta(hours=-23 if upper else 23))
    )
    with pytest.raises(OverflowError):
        extreme.astimezone(UTC)
    assert isinstance(_instant(extreme), timedelta)


def test_helper_real_psycopg_diagnostic_and_error_hierarchy() -> None:
    error = UniqueViolation("driver-marker", info={ord("n"): _SLOT_INDEX.encode()})
    assert error.diag.constraint_name == _SLOT_INDEX
    assert not isinstance(error, OperationalError)
    assert isinstance(QueryCanceled(), OperationalError)
    assert isinstance(LockNotAvailable(), OperationalError)
    assert hasattr(import_module("adapters.psycopg_database")._QueryResult, "fetchall")


def test_exact_exports_constructor_private_ports_and_service_error_identity() -> None:
    module = _module()
    assert tuple(module.__all__) == (
        "PsycopgAnalysisJobWorkerRepository",
        "AnalysisJobWorkerPersistenceUnavailable",
        "AnalysisJobWorkerPersistenceInvariantError",
    )
    assert (
        module.AnalysisJobWorkerPersistenceUnavailable
        is worker.AnalysisJobWorkerPersistenceUnavailable
    )
    assert (
        module.AnalysisJobWorkerPersistenceInvariantError
        is worker.AnalysisJobWorkerPersistenceInvariantError
    )
    cls = module.PsycopgAnalysisJobWorkerRepository
    params = inspect.signature(cls.__init__).parameters
    assert tuple(params) == (
        "self",
        "pool",
        "connection_acquire_timeout_seconds",
        "operation_timeout_ms",
        "lock_timeout_ms",
        "lease_id_factory",
    )
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
        for name, p in params.items()
        if name != "self"
    )
    hints = get_type_hints(cls.__init__)
    assert set(hints) == {
        "pool",
        "connection_acquire_timeout_seconds",
        "operation_timeout_ms",
        "lock_timeout_ms",
        "lease_id_factory",
        "return",
    }
    assert hints["return"] is type(None)
    assert hints["pool"] is module._Pool
    assert hints["connection_acquire_timeout_seconds"] is float
    assert hints["operation_timeout_ms"] is int and hints["lock_timeout_ms"] is int
    assert hints["lease_id_factory"] == Callable[[], UUID]
    for name in ("_Pool", "_Connection", "_Cursor"):
        assert getattr(getattr(module, name), "_is_protocol", False)
    assert get_type_hints(module._Cursor.fetchall) == {"return": list[tuple[object, ...]]}
    assert get_type_hints(module._Cursor.fetchone) == {"return": tuple[object, ...] | None}
    assert get_type_hints(module._Cursor.close) == {"return": type(None)}
    for operation in (
        module._Cursor.fetchall,
        module._Cursor.fetchone,
        module._Cursor.close,
        module._Connection.transaction,
    ):
        assert tuple(inspect.signature(operation).parameters) == ("self",)
        assert inspect.iscoroutinefunction(operation) is (
            operation != module._Connection.transaction
        )
    pool_hints = get_type_hints(module._Pool.connection)
    assert set(pool_hints) == {"timeout_seconds", "return"}
    assert pool_hints["timeout_seconds"] is float
    assert get_origin(pool_hints["return"]) is AbstractAsyncContextManager
    assert get_args(pool_hints["return"]) == (module._Connection,)
    pool_parameters = inspect.signature(module._Pool.connection).parameters
    assert tuple(pool_parameters) == ("self", "timeout_seconds")
    assert pool_parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert pool_parameters["timeout_seconds"].default is inspect.Parameter.empty
    assert get_type_hints(module._Connection.transaction) == {
        "return": AbstractAsyncContextManager[object]
    }
    assert get_type_hints(module._Connection.execute) == {
        "query": str,
        "params": tuple[object, ...] | None,
        "return": module._Cursor,
    }
    execute_parameters = inspect.signature(module._Connection.execute).parameters
    assert tuple(execute_parameters) == ("self", "query", "params")
    assert execute_parameters["query"].default is inspect.Parameter.empty
    assert execute_parameters["params"].default is None
    assert all(
        p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in execute_parameters.values()
    )
    assert {
        name for name, value in vars(cls).items() if not name.startswith("_") and callable(value)
    } == set(_METHODS)


@pytest.mark.parametrize("method", _METHODS)
def test_concrete_operations_match_complete_approved_protocol(method: str) -> None:
    concrete = getattr(_module().PsycopgAnalysisJobWorkerRepository, method)
    approved = getattr(worker.AnalysisJobWorkerRepository, method)
    assert inspect.iscoroutinefunction(concrete)
    assert (
        inspect.signature(concrete).parameters.keys()
        == inspect.signature(approved).parameters.keys()
    )
    assert get_type_hints(concrete) == get_type_hints(approved)
    for name, p in inspect.signature(concrete).parameters.items():
        assert p.default is inspect.Parameter.empty
        if name != "self":
            assert p.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "missing",
    (
        "pool",
        "connection_acquire_timeout_seconds",
        "operation_timeout_ms",
        "lock_timeout_ms",
        "lease_id_factory",
    ),
)
def test_constructor_requires_every_keyword(missing: str) -> None:
    module = _module()
    pool = _Pool([])
    factory = _Factory(pool)
    values = dict(
        pool=pool,
        connection_acquire_timeout_seconds=1.25,
        operation_timeout_ms=4321,
        lock_timeout_ms=1234,
        lease_id_factory=factory,
    )
    del values[missing]
    with pytest.raises(TypeError):
        module.PsycopgAnalysisJobWorkerRepository(**values)
    with pytest.raises(TypeError):
        module.PsycopgAnalysisJobWorkerRepository(pool, 1.25, 4321, 1234, factory)
    assert pool.events == [] and factory.calls == 0


_BAD_TIMEOUTS = (
    (
        "connection_acquire_timeout_seconds",
        True,
        TypeError,
        "Connection acquisition timeout must be numeric.",
    ),
    (
        "connection_acquire_timeout_seconds",
        "1",
        TypeError,
        "Connection acquisition timeout must be numeric.",
    ),
    *(
        (
            "connection_acquire_timeout_seconds",
            value,
            ValueError,
            "Connection acquisition timeout must be finite and positive.",
        )
        for value in (0, -1, float("inf"), float("-inf"), float("nan"), 10**1000)
    ),
    *(
        (
            field,
            value,
            TypeError if type(value) is not int else ValueError,
            "PostgreSQL millisecond timeout must be an integer."
            if type(value) is not int
            else "PostgreSQL millisecond timeout is outside the approved range.",
        )
        for field in ("operation_timeout_ms", "lock_timeout_ms")
        for value in (True, _Int(1), 1.0, "1", None, 0, -1, 2_147_483_648)
    ),
)


@pytest.mark.parametrize(
    ("field", "bad", "error_type", "message"),
    _BAD_TIMEOUTS,
    ids=[f"{field}-{i}" for i, (field, *_) in enumerate(_BAD_TIMEOUTS)],
)
def test_timeout_boundaries_are_fixed_and_have_zero_side_effects(
    field: str, bad: object, error_type: type[Exception], message: str
) -> None:
    pool = _Pool([])
    factory = _Factory(pool)
    with pytest.raises(error_type) as caught:
        _repository(pool, factory, **{field: bad})
    assert caught.value.args == (message,)
    assert caught.value.__context__ is None
    assert pool.events == [] and factory.calls == 0


@pytest.mark.parametrize(("operation", "lock"), ((1, 1), (1, 2)))
def test_timeout_ordering_precedes_factory_validation(operation: int, lock: int) -> None:
    pool = _Pool([])
    with pytest.raises(ValueError, match="^Operation timeout must exceed lock timeout\\.$"):
        _repository(pool, operation_timeout_ms=operation, lock_timeout_ms=lock, lease_id_factory=3)
    assert pool.events == []


@pytest.mark.parametrize("acquire", (1, 0.125, _Int(2), 2.5))
def test_constructor_positive_boundaries_normalize_acquisition_without_probing(
    acquire: float,
) -> None:
    pool = _Pool([])
    factory = _Factory(pool)
    _repository(
        pool,
        factory,
        connection_acquire_timeout_seconds=acquire,
        operation_timeout_ms=2_147_483_647,
        lock_timeout_ms=2_147_483_646,
    )
    assert pool.events == [] and factory.calls == 0


@pytest.mark.parametrize("method", _METHODS)
def test_public_argument_validation_precedes_acquisition(method: str) -> None:
    pool = _Pool([])
    factory = _Factory(pool)
    repo = _repository(pool, factory)
    names = tuple(inspect.signature(getattr(repo, method)).parameters)
    for name in names:
        with pytest.raises((TypeError, ValueError)):
            _run(repo, method, **{name: None})
    assert pool.events == [] and factory.calls == 0


def test_same_slot_retry_preserves_complete_persisted_generation_and_timestamps() -> None:
    job = _j()
    pool = _Pool([*_slot(job=[job]), _read("clock", [(_NOW,)])])
    factory = _Factory(pool, value=_OTHER)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is WorkerClaim
    assert outcome.authority == _AUTHORITY
    assert outcome.job.analysis_version == "Version_1.0"
    assert outcome.job.attempt_limit == 3
    assert outcome.job.accepted_at is job[9]
    _assert_claim_payload(outcome.job, job)
    assert outcome.started_at is _START and outcome.heartbeat_at is _NOW
    assert outcome.lease_expires_at is _EXPIRY
    assert not hasattr(outcome.job, "attempt_count") and not hasattr(outcome.job, "state")
    assert factory.calls == 0
    _done(pool)


@pytest.mark.parametrize("delta", (0, -1))
def test_same_slot_expiry_boundary_has_no_authority_or_writes(delta: int) -> None:
    pool = _Pool(
        [
            *_slot(
                attempt=[_a(lease_expires_at=_NOW + timedelta(seconds=delta), heartbeat_at=_START)]
            ),
            _read("clock", [(_NOW,)]),
        ]
    )
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is worker.WorkerSlotUnavailable
    assert outcome.worker_slot_id.value is _SLOT
    assert not hasattr(outcome, "authority")
    assert factory.calls == 0
    _done(pool)


def test_same_slot_cancelled_active_is_valid_but_non_authoritative() -> None:
    pool = _Pool(_slot(job=[_j(state="CANCELLED")]))
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is worker.WorkerSlotUnavailable
    assert factory.calls == 0
    _done(pool)


def test_fresh_claim_preserves_generation_two_and_factory_uuid_object() -> None:
    job = _j()
    equivalent_lease = UUID(str(_LEASE))
    assert equivalent_lease is not _LEASE
    pool = _Pool(
        [
            *_fresh(),
            *_fresh_writes(
                job=[job],
                attempt=[
                    _a(
                        started_at=_NOW,
                        lease_id=equivalent_lease,
                        lease_expires_at=_NOW + _DURATION.value,
                    )
                ],
            ),
        ]
    )
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is WorkerClaim
    assert outcome.authority == _AUTHORITY
    assert outcome.authority.lease_id.value is _LEASE
    _assert_claim_payload(outcome.job, job)
    assert factory.calls == 1
    assert (
        pool.events.index("close:active")
        < pool.events.index("factory")
        < pool.events.index("execute:job-write")
    )
    _done(pool)


def test_fresh_count_zero_does_not_require_an_attempt() -> None:
    pool = _Pool(
        [
            *_settings(),
            _read("locator", [], _SLOT),
            _read("candidate", [_j(state="QUEUED", attempt_count=0)]),
            _read("active", [], _JOB),
            _write("job-write", [_j(attempt_count=1)], _JOB),
            _write(
                "insert",
                [_a(attempt_number=1, started_at=_NOW, lease_expires_at=_NOW + _DURATION.value)],
                _JOB,
                1,
                _SLOT,
                _LEASE,
            ),
        ]
    )
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is WorkerClaim
    assert outcome.authority.attempt_number == 1 and factory.calls == 1
    _done(pool)


def test_no_candidate_commits_releases_and_never_calls_factory() -> None:
    pool = _Pool([*_settings(), _read("locator", [], _SLOT), _read("candidate", [])])
    factory = _Factory(pool)
    assert type(_run(_repository(pool, factory))) is worker.NoJobAvailable
    assert factory.calls == 0
    _done(pool)


_BAD_J = (
    ("job_id", UUID(int=0)),
    ("job_id", str(_JOB)),
    ("job_id", _Uuid(str(_JOB))),
    *(
        (name, value)
        for name in ("video_id", "player_id")
        for value in (None, _Text("safe"), "", "a/b", "é", "a\x00")
    ),
    *(
        ("video_reference", value)
        for value in (None, _Text("file.mp4"), "", "../file.mp4", "C:file.mp4", "file.mp4/")
    ),
    *(
        ("callback_url", value)
        for value in (
            None,
            _Text("https://example.com/"),
            "",
            "HTTPS://EXAMPLE.COM/",
            "https://example.com/#marker",
        )
    ),
    *(
        ("analysis_version", value)
        for value in (None, _Text("v1"), "", "v" * 65, "_v1", "é", "v+1")
    ),
    ("state", None),
    ("state", _Text("RUNNING")),
    ("state", "UNKNOWN"),
    *(("attempt_limit", value) for value in (True, _Int(3), 0, -1, 3.0, None)),
    *(("attempt_count", value) for value in (True, _Int(2), -1, 4, 2.0, None)),
    ("accepted_at", None),
    ("accepted_at", datetime(2026, 10, 10)),
    ("accepted_at", "timestamp-marker"),
)
_BAD_A = (
    ("job_id", UUID(int=0)),
    ("job_id", _Uuid(str(_JOB))),
    ("job_id", "uuid-marker"),
    *(("attempt_number", value) for value in (True, _Int(2), 0, -1, 2.0, None)),
    *(
        (name, value)
        for name in ("worker_slot_id", "lease_id")
        for value in (None, UUID(int=0), _Uuid(str(_SLOT)), "uuid-marker")
    ),
    ("status", None),
    ("status", _Text("ACTIVE")),
    ("status", "COMPLETED"),
    *(
        (name, value)
        for name in ("started_at", "heartbeat_at", "lease_expires_at")
        for value in (None, datetime(2026, 10, 10), "timestamp-marker")
    ),
    ("heartbeat_at", _START - timedelta(microseconds=1)),
    ("lease_expires_at", _NOW),
    ("lease_expires_at", _NOW - timedelta(microseconds=1)),
    ("finished_at", _NOW),
    ("safe_failure_code", "retry"),
)


@pytest.mark.parametrize(
    ("field", "bad"), _BAD_J, ids=[f"{f}-{i}" for i, (f, _) in enumerate(_BAD_J)]
)
def test_every_job_projection_field_is_defensively_validated(field: str, bad: object) -> None:
    pool = _Pool(_slot(job=[_j(**{field: bad})]))
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 0
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize(
    ("field", "bad"), _BAD_A, ids=[f"{f}-{i}" for i, (f, _) in enumerate(_BAD_A)]
)
def test_every_attempt_projection_field_is_defensively_validated(field: str, bad: object) -> None:
    pool = _Pool(_slot(attempt=[_a(**{field: bad})]))
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 0
    _done(pool, rollback=True)


@pytest.mark.parametrize("layout", ("J", "A", "L", "T"))
@pytest.mark.parametrize(
    "defect", ("short", "long", "list-row", "tuple-result", "none-result", "duplicate")
)
def test_exact_rows_and_result_cardinality_fail_closed(layout: str, defect: str) -> None:
    row = {"J": _j(), "A": _a(), "L": (_JOB, 2), "T": (_NOW,)}[layout]
    rows: object = {
        "short": [row[:-1]],
        "long": [(*row, "extra-marker")],
        "list-row": [list(row)],
        "tuple-result": (row,),
        "none-result": None,
        "duplicate": [row, row],
    }[defect]
    steps = [*_slot(), _read("clock", [(_NOW,)])]
    index = {"L": 2, "J": 3, "A": 4, "T": 5}[layout]
    steps[index] = replace(steps[index], rows=rows)
    pool = _Pool(steps)
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 0
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize(
    "row", ((UUID(int=0), 2), (str(_JOB), 2), (_JOB, True), (_JOB, _Int(2)), (_JOB, 0), (_JOB, 2.0))
)
def test_locator_fields_are_exact_before_required_reads(row: tuple[object, ...]) -> None:
    pool = _Pool([*_settings(), _read("locator", [row], _SLOT)])
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


@pytest.mark.parametrize("bad", (None, "clock-marker", datetime(2026, 10, 10)))
def test_clock_observation_requires_valid_aware_timestamp(bad: object) -> None:
    pool = _Pool([*_slot(), _read("clock", [(bad,)])])
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


@pytest.mark.parametrize("side", ("job", "attempt"))
def test_locator_requires_both_rows_even_if_the_discovery_is_obsolete(side: str) -> None:
    pool = _Pool(_slot(**{side: []}))
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize("method", _METHODS[1:4])
@pytest.mark.parametrize(
    "missing", ("job", "count-zero", "current-attempt", "stale-missing-current")
)
def test_owner_absence_distinguishes_claim_loss_from_required_current_corruption(
    method: str, missing: str
) -> None:
    steps = _owner()
    supplied = _AUTHORITY
    if missing == "job":
        steps = [*_settings(), _read("job", [], _JOB)]
    elif missing == "count-zero":
        steps = [*_settings(), _read("job", [_j(state="QUEUED", attempt_count=0)], _JOB)]
    else:
        steps[-1] = _read("attempt", [], _JOB, 2)
        if missing == "stale-missing-current":
            supplied = replace(_AUTHORITY, attempt_number=1)
    pool = _Pool(steps)
    repo = _repository(pool)
    if missing in ("job", "count-zero"):
        outcome = _run(repo, method, authority=supplied)
        assert type(outcome) is worker.ClaimLost and outcome.authority is supplied
        _done(pool)
    else:
        _fixed(_capture(repo, method, authority=supplied))
        _done(pool, rollback=True)


@pytest.mark.parametrize("method", _METHODS[1:4])
@pytest.mark.parametrize("component", ("job_id", "attempt_number", "worker_slot_id", "lease_id"))
def test_all_four_supplied_fence_components_are_authoritative(method: str, component: str) -> None:
    changes = dict(
        job_id=_OTHER_JOB,
        attempt_number=1,
        worker_slot_id=WorkerSlotId(_OTHER),
        lease_id=LeaseId(_OTHER),
    )
    supplied = replace(_AUTHORITY, **cast(Any, {component: changes[component]}))
    steps = _owner()
    if component == "job_id":
        # Lookup by the supplied ID found no job; never reuse another job's row.
        steps = [*_settings(), _read("job", [], _OTHER_JOB)]
    pool = _Pool(steps)
    result = _run(_repository(pool), method, authority=supplied)
    assert type(result) is worker.ClaimLost and result.authority is supplied
    _done(pool)


@pytest.mark.parametrize("method", _METHODS[1:4])
def test_stale_authority_cannot_hide_contradictory_committed_pair(method: str) -> None:
    pool = _Pool(_owner(job=[_j(state="QUEUED")]))
    _fixed(_capture(_repository(pool), method, authority=replace(_AUTHORITY, attempt_number=1)))
    _done(pool, rollback=True)


_RESOLUTIONS = (
    ("INTERRUPTED", None),
    ("RETRYABLE_FAILURE", "retry"),
    ("TERMINAL_FAILURE", "retry"),
    ("EXHAUSTED", "retry"),
    ("EXHAUSTED", None),
)


@pytest.mark.parametrize(("status", "code"), _RESOLUTIONS)
@pytest.mark.parametrize(
    "defect", ("expiry", "missing-finish", "before-heartbeat", "bad-finish", "bad-code")
)
def test_resolved_shape_nullability_codes_and_timestamp_order(
    status: str, code: str | None, defect: str
) -> None:
    job = _j(
        state="QUEUED" if status in ("INTERRUPTED", "RETRYABLE_FAILURE") else "FAILED",
        attempt_limit=2 if status == "EXHAUSTED" else 3,
    )
    changes: dict[str, object] = dict(
        status=status, lease_expires_at=None, finished_at=_NOW, safe_failure_code=code
    )
    defects: dict[str, dict[str, object]] = {
        "expiry": dict(lease_expires_at=_EXPIRY),
        "missing-finish": dict(finished_at=None),
        "before-heartbeat": dict(finished_at=_START),
        "bad-finish": dict(finished_at="marker"),
        "bad-code": dict(safe_failure_code="Bad-code"),
    }
    changes.update(defects[defect])
    pool = _Pool(_slot(job=[job], attempt=[_a(**changes)]))
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    "status", ("INTERRUPTED", "RETRYABLE_FAILURE", "TERMINAL_FAILURE", "EXHAUSTED")
)
def test_failure_code_nullability_is_status_specific(status: str) -> None:
    interrupted = status == "INTERRUPTED"
    job = _j(
        state="QUEUED" if status in ("INTERRUPTED", "RETRYABLE_FAILURE") else "FAILED",
        attempt_limit=2 if status == "EXHAUSTED" else 3,
    )
    row = _a(
        status=status,
        lease_expires_at=None,
        finished_at=_NOW,
        safe_failure_code="retry" if interrupted else None,
    )
    pool = _Pool(_slot(job=[job], attempt=[row]))
    if status == "EXHAUSTED":
        # Null-code exhaustion is valid expiry history, not malformed classification.
        _fixed(_capture(_repository(pool)), unavailable=True)
    else:
        _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


@pytest.mark.parametrize("relation", ("current", "historical"))
@pytest.mark.parametrize(("status", "code"), (("ACTIVE", None), *_RESOLUTIONS))
@pytest.mark.parametrize("state", ("QUEUED", "RUNNING", "FAILED", "CANCELLED", "COMPLETED"))
def test_complete_committed_pair_consistency_table(
    relation: str, status: str, code: str | None, state: str
) -> None:
    number = 2 if relation == "current" else 1
    limit = 2 if status == "EXHAUSTED" else 3
    row = _a(
        attempt_number=number,
        status=status,
        lease_expires_at=_EXPIRY if status == "ACTIVE" else None,
        finished_at=None if status == "ACTIVE" else _NOW,
        safe_failure_code=code,
    )
    job = _j(state=state, attempt_limit=limit)
    steps = _slot(job=[job], attempt=[row], locator=[(_JOB, number)])
    steps[-1] = _read("attempt", [row], _JOB, number)
    retry = status in ("INTERRUPTED", "RETRYABLE_FAILURE")
    if relation == "historical":
        current = _a()
        if state in ("QUEUED", "CANCELLED"):
            current = _a(status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW)
        elif state == "FAILED":
            current = _a(
                status="TERMINAL_FAILURE",
                lease_expires_at=None,
                finished_at=_NOW,
                safe_failure_code="retry",
            )
        steps.append(_read("attempt", [current], _JOB, 2))
        # COMPLETED has no approved current successful-attempt representation in Slice 3.
        valid = retry and state != "COMPLETED"
    else:
        valid = (
            (status == "ACTIVE" and state in ("RUNNING", "CANCELLED"))
            or (retry and state in ("QUEUED", "CANCELLED"))
            or (status in ("TERMINAL_FAILURE", "EXHAUSTED") and state == "FAILED")
        )
    if valid and status == "ACTIVE" and state == "RUNNING":
        steps.append(_read("clock", [(_NOW,)]))
    pool = _Pool(steps)
    factory = _Factory(pool)
    repo = _repository(pool, factory)
    if valid and status == "ACTIVE":
        expected = WorkerClaim if state == "RUNNING" else worker.WorkerSlotUnavailable
        assert type(_run(repo)) is expected
        _done(pool)
    else:
        _fixed(_capture(repo), unavailable=valid)
        _done(pool, rollback=True, remaining=True)
    assert factory.calls == 0


@pytest.mark.parametrize("status", ("INTERRUPTED", "RETRYABLE_FAILURE", "EXHAUSTED"))
def test_status_attempt_number_and_policy_limit_association(status: str) -> None:
    at_limit = status != "EXHAUSTED"
    row = _a(
        status=status,
        lease_expires_at=None,
        finished_at=_NOW,
        safe_failure_code="retry" if status == "RETRYABLE_FAILURE" else None,
    )
    pool = _Pool(
        _slot(
            job=[_j(state="QUEUED" if at_limit else "FAILED", attempt_limit=2 if at_limit else 3)],
            attempt=[row],
        )
    )
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    ("status", "state", "method", "limit"),
    (
        ("RETRYABLE_FAILURE", "QUEUED", "finalize_retryable_failure", 3),
        ("EXHAUSTED", "FAILED", "finalize_retryable_failure", 2),
        ("TERMINAL_FAILURE", "FAILED", "finalize_terminal_failure", 3),
    ),
)
@pytest.mark.parametrize(
    "conflict", ("exact", "code", "kind", "slot", "lease", "generation", "cancelled")
)
def test_resolution_replay_requires_exact_current_fence_code_kind_and_state(
    status: str, state: str, method: str, limit: int, conflict: str
) -> None:
    if conflict == "cancelled" and status != "RETRYABLE_FAILURE":
        # Terminal -> CANCELLED is contradictory, never an ordinary replay conflict.
        state = "CANCELLED"
    elif conflict == "cancelled":
        state = "CANCELLED"
    pool = _Pool(
        _owner(
            job=[_j(state=state, attempt_limit=limit)],
            attempt=[
                _a(
                    status=status,
                    lease_expires_at=None,
                    finished_at=_NOW,
                    safe_failure_code="retry",
                )
            ],
        )
    )
    changes: dict[str, object] = {}
    if conflict == "code":
        changes["failure_code"] = AttemptFailureCode("different")
    elif conflict == "kind":
        method = (
            "finalize_terminal_failure"
            if method == "finalize_retryable_failure"
            else "finalize_retryable_failure"
        )
    elif conflict in ("slot", "lease", "generation"):
        field = {"slot": "worker_slot_id", "lease": "lease_id", "generation": "attempt_number"}[
            conflict
        ]
        value: object = {"slot": WorkerSlotId(_OTHER), "lease": LeaseId(_OTHER), "generation": 1}[
            conflict
        ]
        changes["authority"] = replace(_AUTHORITY, **cast(Any, {field: value}))
    repo = _repository(pool)
    if conflict == "cancelled" and status != "RETRYABLE_FAILURE":
        _fixed(_capture(repo, method, **changes))
        _done(pool, rollback=True)
        return
    outcome = _run(repo, method, **changes)
    if conflict == "exact":
        assert type(outcome) is worker.AttemptResolutionAlreadyApplied
        assert outcome.status is AttemptStatus(status)
        assert outcome.failure_code.value == "retry"
    else:
        assert type(outcome) is worker.ClaimLost
    assert outcome.authority == changes.get("authority", _AUTHORITY)
    _done(pool)


@pytest.mark.parametrize("method", _METHODS[1:4])
@pytest.mark.parametrize("boundary", ("observed-equal", "observed-past", "mutation-zero"))
def test_owner_expiry_observation_and_later_zero_gate_never_write_job(
    method: str, boundary: str
) -> None:
    clock = _EXPIRY if boundary == "observed-equal" else _EXPIRY + timedelta(microseconds=1)
    if boundary == "mutation-zero":
        clock = _NOW
    steps = [*_owner(), _read("clock", [(clock,)])]
    if boundary == "mutation-zero":
        steps.append(_write("attempt-write", [], _JOB, 2, _SLOT, _LEASE))
    pool = _Pool(steps)
    outcome = _run(_repository(pool), method)
    assert type(outcome) is worker.ClaimLost and outcome.authority is _AUTHORITY
    assert not any(label == "job-write" for label, _, _ in pool.value.executions)
    _done(pool)


def test_renewal_uses_returned_later_sample_not_earlier_observation() -> None:
    heartbeat = _NOW + timedelta(seconds=1)
    expiry = heartbeat + _DURATION.value
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write(
                "attempt-write",
                [_a(heartbeat_at=heartbeat, lease_expires_at=expiry)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
        ]
    )
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory), "renew_lease")
    assert type(outcome) is worker.LeaseRenewed
    assert outcome.authority is _AUTHORITY
    assert outcome.heartbeat_at is heartbeat and outcome.lease_expires_at is expiry
    _assert_renewal_factory_unused(factory)
    _done(pool)


@pytest.mark.parametrize(
    ("method", "status", "state", "limit", "outcome_type"),
    (
        ("finalize_retryable_failure", "RETRYABLE_FAILURE", "QUEUED", 3, worker.AttemptRequeued),
        ("finalize_retryable_failure", "EXHAUSTED", "FAILED", 2, worker.AttemptExhausted),
        ("finalize_terminal_failure", "TERMINAL_FAILURE", "FAILED", 3, worker.AttemptFailed),
    ),
)
def test_failure_finalization_attempt_then_job_commits_only_complete_pair(
    method: str, status: str, state: str, limit: int, outcome_type: type[object]
) -> None:
    finish = _NOW + timedelta(seconds=1)
    pool = _Pool(
        [
            *_owner(job=[_j(attempt_limit=limit)]),
            _read("clock", [(_NOW,)]),
            _write(
                "attempt-write",
                [
                    _a(
                        status=status,
                        lease_expires_at=None,
                        finished_at=finish,
                        safe_failure_code="retry",
                    )
                ],
                _JOB,
                2,
                _SLOT,
                _LEASE,
                "retry",
            ),
            _write("job-write", [_j(state=state, attempt_limit=limit)], _JOB),
        ]
    )
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory), method)
    assert type(outcome) is outcome_type
    assert cast(Any, outcome).authority is _AUTHORITY
    if outcome_type is not worker.AttemptRequeued:
        assert cast(Any, outcome).failure_code.value == "retry"
    if outcome_type in (worker.AttemptRequeued, worker.AttemptExhausted):
        _assert_finalization_limit(
            cast(worker.AttemptRequeued | worker.AttemptExhausted, outcome), limit
        )
    assert factory.calls == 0
    assert pool.events.index("close:attempt-write") < pool.events.index("execute:job-write")
    _done(pool)


@pytest.mark.parametrize(
    ("state", "count", "limit"),
    (
        ("QUEUED", 3, 3),
        ("RUNNING", 0, 3),
        ("FAILED", 0, 3),
    ),
)
def test_worker_job_state_count_invariants_precede_stale_authority(
    state: str, count: int, limit: int
) -> None:
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state=state, attempt_count=count, attempt_limit=limit)], _JOB),
        ]
    )
    _fixed(
        _capture(_repository(pool), "renew_lease", authority=replace(_AUTHORITY, attempt_number=1))
    )
    _done(pool, rollback=True)


@pytest.mark.parametrize("defect", ("job", "generation", "slot", "required-current"))
def test_c9a_one_condition_wrong_locator_or_historical_current_is_invariant(defect: str) -> None:
    job, attempt, locator = (
        _j(state="QUEUED"),
        _a(status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW),
        (_JOB, 2),
    )
    if defect == "job":
        job = _j(job_id=_OTHER_JOB, state="QUEUED")
    elif defect == "generation":
        attempt = _a(
            attempt_number=1, status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW
        )
    elif defect == "slot":
        attempt = _a(
            worker_slot_id=_OTHER, status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW
        )
    else:
        locator = (_JOB, 1)
        attempt = _a(
            attempt_number=1, status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW
        )
    steps = _slot(job=[job], attempt=[attempt], locator=[locator])
    steps[-1] = _read("attempt", [attempt], _JOB, locator[1])
    if defect == "required-current":
        steps.append(_read("attempt", [], _JOB, 2))
    pool = _Pool(steps)
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 0
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize(
    "bad", (None, UUID(int=0), str(_LEASE), _Uuid(str(_LEASE)), 1, LeaseId(_LEASE))
)
def test_factory_invalid_results_rollback_before_mutations(bad: object) -> None:
    pool = _Pool(_fresh())
    factory = _Factory(pool, value=bad)
    repo = _repository(pool, factory)
    error = _capture(repo)
    expected = ValueError if type(bad) is UUID else TypeError
    message = (
        "Lease UUID factory must not return a nil UUID."
        if type(bad) is UUID
        else "Lease UUID factory must return an exact UUID."
    )
    assert type(error) is expected and error.args == (message,)
    assert error.__context__ is None
    assert factory.calls == 1
    _done(pool, rollback=True)


@pytest.mark.parametrize("version", (1, 3, 4, 5))
def test_factory_uuid_versions_have_no_extra_restriction(version: int) -> None:
    value = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", version=version)
    pool = _Pool(
        [
            *_fresh(),
            *_fresh_writes(
                attempt=[
                    _a(
                        lease_id=UUID(str(value)),
                        started_at=_NOW,
                        lease_expires_at=_NOW + _DURATION.value,
                    )
                ]
            ),
        ]
    )
    # Bound values need the generated lease, not the representative fixture UUID.
    pool.value.steps[-1] = replace(pool.value.steps[-1], bound=(_JOB, 2, _SLOT, value))
    factory = _Factory(pool, value=value)
    outcome = _run(_repository(pool, factory))
    assert type(outcome) is WorkerClaim and outcome.authority.lease_id.value is value
    assert factory.calls == 1
    _done(pool)


def test_factory_lease_may_equal_slot_without_extra_inequality() -> None:
    pool = _Pool(
        [
            *_fresh(),
            *_fresh_writes(
                attempt=[
                    _a(lease_id=_SLOT, started_at=_NOW, lease_expires_at=_NOW + _DURATION.value)
                ]
            ),
        ]
    )
    pool.value.steps[-1] = replace(pool.value.steps[-1], bound=(_JOB, 2, _SLOT))
    outcome = _run(_repository(pool, _Factory(pool, value=_SLOT)))
    assert type(outcome) is WorkerClaim and outcome.authority.lease_id.value is _SLOT
    _done(pool)


@pytest.mark.parametrize(
    "kind", ("function", "generator", "callable", "generator-callable", "noncallable")
)
def test_factory_configuration_rejects_known_async_before_interaction(kind: str) -> None:
    async def asynchronous() -> UUID:
        raise AssertionError("Must not run")

    async def generator() -> Any:
        yield _LEASE

    class AsyncCallable:
        async def __call__(self) -> UUID:
            raise AssertionError("Must not run")

    class AsyncGeneratorCallable:
        async def __call__(self) -> Any:
            yield _LEASE

    pool = _Pool([])
    value = {
        "function": asynchronous,
        "generator": generator,
        "callable": AsyncCallable(),
        "generator-callable": AsyncGeneratorCallable(),
        "noncallable": 3,
    }[kind]
    with pytest.raises(TypeError) as caught:
        _repository(pool, lease_id_factory=value)
    assert caught.value.args == (
        "Lease UUID factory must be callable."
        if kind == "noncallable"
        else "Lease UUID factory must be synchronous.",
    )
    assert pool.events == []


def test_async_type_call_rejects_synchronous_instance_shadow_before_interaction() -> None:
    calls: list[str] = []

    class AsyncFactory:
        async def __call__(self) -> UUID:
            calls.append("effective")
            raise AssertionError("Async factory must not run")

    def synchronous_shadow() -> UUID:
        calls.append("shadow")
        raise AssertionError("Instance shadow must not run")

    factory = AsyncFactory()
    factory.__dict__["__call__"] = synchronous_shadow
    pool = _Pool([])
    with pytest.raises(TypeError) as caught:
        _repository(pool, factory)
    assert caught.value.args == ("Lease UUID factory must be synchronous.",)
    assert pool.events == [] and pool.calls == [] and calls == []


def test_sync_type_call_accepts_async_instance_shadow_and_preserves_uuid() -> None:
    shadow_calls: list[str] = []

    async def asynchronous_shadow() -> UUID:
        shadow_calls.append("shadow")
        raise AssertionError("Instance shadow must not run")

    pool = _Pool([*_fresh(), *_fresh_writes()])
    factory = _Factory(pool)
    factory.__dict__["__call__"] = asynchronous_shadow
    repository = _repository(pool, factory)
    assert pool.events == [] and pool.calls == [] and factory.calls == 0
    assert shadow_calls == []
    outcome = _run(repository)
    assert type(outcome) is WorkerClaim
    assert outcome.authority.lease_id.value is _LEASE
    assert factory.calls == 1 and shadow_calls == []
    _done(pool)


@pytest.mark.parametrize(
    "field",
    (
        "__name__",
        "__code__",
        "__defaults__",
        "__kwdefaults__",
        "__annotations__",
        "_is_coroutine_marker",
        "__class__",
    ),
)
def test_sync_factory_metadata_descriptors_are_not_executed(field: str) -> None:
    hooks: list[str] = []

    def metadata_property(self: _Factory) -> object:
        hooks.append(field)
        raise RuntimeError("factory-metadata-marker")

    factory_type = type("MetadataFactory", (_Factory,), {field: property(metadata_property)})
    pool = _Pool([*_fresh(), *_fresh_writes()])
    factory = factory_type(pool)
    repository = _repository(pool, factory)
    assert hooks == [] and factory.calls == 0 and pool.events == [] and pool.calls == []
    outcome = _run(repository)
    assert type(outcome) is WorkerClaim
    assert outcome.authority.lease_id.value is _LEASE
    assert hooks == [] and factory.calls == 1
    _done(pool)


def test_async_factory_rejects_without_executing_metadata_descriptor() -> None:
    hooks: list[str] = []
    calls: list[str] = []

    class AsyncFactory:
        @property
        def __name__(self) -> str:
            hooks.append("name")
            raise RuntimeError("factory-metadata-marker")

        async def __call__(self) -> UUID:
            calls.append("body")
            raise AssertionError("Async factory must not run")

    pool = _Pool([])
    with pytest.raises(TypeError) as caught:
        _repository(pool, AsyncFactory())
    assert caught.value.args == ("Lease UUID factory must be synchronous.",)
    assert hooks == [] and calls == [] and pool.events == [] and pool.calls == []


@pytest.mark.parametrize("kind", ("unstarted-native", "started-native", "arbitrary-awaitable"))
def test_invalid_factory_awaitables_are_not_awaited_or_given_user_cleanup_hooks(kind: str) -> None:
    _module()  # Do not allocate a coroutine when the intentional RED is module absence.
    hooks: list[str] = []

    class AwaitableProbe:
        def __await__(self) -> Any:
            hooks.append("await")
            yield

        def close(self) -> None:
            hooks.append("close")

        async def aclose(self) -> None:
            hooks.append("aclose")

    async def native() -> UUID:
        hooks.append("body")
        await asyncio.sleep(0)
        return _LEASE

    coroutine = native()
    if kind == "started-native":
        coroutine.send(None)
    value: object = AwaitableProbe() if kind == "arbitrary-awaitable" else coroutine
    pool = _Pool(_fresh())
    factory = _Factory(pool, value=value)
    repo = _repository(pool, factory)
    try:
        error = _capture(repo)
        assert type(error) is TypeError and error.args == (
            "Lease UUID factory must return an exact UUID.",
        )
        if kind == "unstarted-native":
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
            assert hooks == []
        elif kind == "started-native":
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_SUSPENDED
            assert hooks == ["body"]
        else:
            assert hooks == []
        assert factory.calls == 1
        _done(pool, rollback=True)
    finally:
        # Test-owned native coroutine only. No arbitrary user-object hooks.
        coroutine.close()


@pytest.mark.parametrize(
    "error",
    (
        RuntimeError("factory-marker"),
        OperationalError("factory-marker"),
        InterfaceError("factory-marker"),
        PoolTimeout("factory-marker"),
        UniqueViolation("factory-marker", info={ord("n"): _SLOT_INDEX.encode()}),
        asyncio.CancelledError("factory-marker"),
        MemoryError("factory-marker"),
        _Fatal("factory-marker"),
    ),
)
def test_factory_exceptions_are_not_translated_by_outer_driver_handlers(
    error: BaseException,
) -> None:
    pool = _Pool(_fresh())
    factory = _Factory(pool, error=error)
    assert _capture(_repository(pool, factory)) is error
    assert factory.calls == 1
    _done(pool, rollback=True)


def test_factory_wrong_arity_is_not_probed_at_construction() -> None:
    def wrong(required: object) -> UUID:
        raise AssertionError("Wrong arity must fail before the body")

    pool = _Pool(_fresh())
    repo = _repository(pool, wrong)
    assert pool.events == []
    assert type(_capture(repo)) is TypeError
    _done(pool, rollback=True)


_BAD_FRESH_A = (
    ("job_id", _OTHER_JOB),
    ("attempt_number", 1),
    ("worker_slot_id", _OTHER),
    ("lease_id", _OTHER),
    ("started_at", _START),
    ("lease_expires_at", _NOW + _DURATION.value + timedelta(microseconds=1)),
    ("status", "INTERRUPTED"),
    ("safe_failure_code", "retry"),
)
_BAD_JOB_WRITE = (
    ("job_id", _OTHER_JOB),
    ("video_id", "Other_123"),
    ("player_id", "Other_456"),
    ("video_reference", "Other.MP4"),
    ("callback_url", "https://other.invalid/callback"),
    ("analysis_version", "Other_2.0"),
    ("attempt_limit", 7),
    ("accepted_at", _NOW),
    ("state", "QUEUED"),
    ("attempt_count", 1),
)


@pytest.mark.parametrize(("field", "bad"), _BAD_FRESH_A, ids=[f for f, _ in _BAD_FRESH_A])
def test_fresh_insert_returning_preserves_full_authority_shape_and_exact_duration(
    field: str, bad: object
) -> None:
    row = (
        _a(started_at=_NOW, lease_expires_at=_NOW + _DURATION.value, **{field: bad})
        if field not in ("started_at", "lease_expires_at")
        else _a(**dict(started_at=_NOW, lease_expires_at=_NOW + _DURATION.value) | {field: bad})
    )
    pool = _Pool([*_fresh(), *_fresh_writes(attempt=[row])])
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 1
    _done(pool, rollback=True)


@pytest.mark.parametrize(("field", "bad"), _BAD_JOB_WRITE, ids=[f for f, _ in _BAD_JOB_WRITE])
def test_fresh_job_returning_preserves_immutable_inputs_policy_and_generation(
    field: str, bad: object
) -> None:
    pool = _Pool([*_fresh(), *_fresh_writes(job=[_j(**{field: bad})])])
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 1
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize("boundary", ("job-write", "insert"))
@pytest.mark.parametrize("cardinality", (0, 2))
def test_fresh_required_returning_is_singleton_and_rolls_back_all_writes(
    boundary: str, cardinality: int
) -> None:
    steps = [*_fresh(), *_fresh_writes()]
    index = -2 if boundary == "job-write" else -1
    steps[index] = replace(steps[index], rows=cast(list[object], steps[index].rows) * cardinality)
    pool = _Pool(steps)
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True, remaining=True)


_BAD_RENEW = (
    ("job_id", _OTHER_JOB),
    ("attempt_number", 1),
    ("worker_slot_id", _OTHER),
    ("lease_id", _OTHER),
    ("started_at", _START + timedelta(microseconds=1)),
    ("heartbeat_at", _NOW - timedelta(microseconds=1)),
    ("heartbeat_at", _EXPIRY),
    ("lease_expires_at", _NOW + timedelta(seconds=31)),
    ("finished_at", _NOW),
    ("safe_failure_code", "retry"),
)


@pytest.mark.parametrize(
    ("field", "bad"), _BAD_RENEW, ids=[f"{f}-{i}" for i, (f, _) in enumerate(_BAD_RENEW)]
)
def test_renewal_returning_requires_preserved_fields_old_expiry_and_elapsed_duration(
    field: str, bad: object
) -> None:
    row = _a(
        **(
            dict(
                heartbeat_at=_NOW + timedelta(seconds=1),
                lease_expires_at=_NOW + timedelta(seconds=1) + _DURATION.value,
            )
            | {field: bad}
        )
    )
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write("attempt-write", [row], _JOB, 2, _SLOT, _LEASE),
        ]
    )
    _fixed(_capture(_repository(pool), "renew_lease"))
    _done(pool, rollback=True)


@pytest.mark.parametrize("method", ("finalize_retryable_failure", "finalize_terminal_failure"))
@pytest.mark.parametrize(
    "field",
    (
        "job_id",
        "attempt_number",
        "worker_slot_id",
        "lease_id",
        "started_at",
        "heartbeat_at",
        "lease_expires_at",
        "finished_at",
        "safe_failure_code",
        "status",
    ),
)
def test_failure_returning_requires_full_preservation_and_mutation_gate(
    method: str, field: str
) -> None:
    status = "RETRYABLE_FAILURE" if method == "finalize_retryable_failure" else "TERMINAL_FAILURE"
    bad: dict[str, object] = dict(
        job_id=_OTHER_JOB,
        attempt_number=1,
        worker_slot_id=_OTHER,
        lease_id=_OTHER,
        started_at=_START + timedelta(microseconds=1),
        heartbeat_at=_NOW + timedelta(microseconds=1),
        lease_expires_at=_EXPIRY,
        finished_at=_EXPIRY,
        safe_failure_code="different",
        status="EXHAUSTED",
    )
    row = _a(
        **(
            dict(status=status, lease_expires_at=None, finished_at=_NOW, safe_failure_code="retry")
            | {field: bad[field]}
        )
    )
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write("attempt-write", [row], _JOB, 2, _SLOT, _LEASE, "retry"),
        ]
    )
    _fixed(_capture(_repository(pool), method))
    _done(pool, rollback=True)


@pytest.mark.parametrize("method", ("finalize_retryable_failure", "finalize_terminal_failure"))
@pytest.mark.parametrize(
    "defect", ("missing", "duplicate", "wrong-policy", "wrong-count", "wrong-state")
)
def test_failure_second_write_evidence_rolls_back_first_write(method: str, defect: str) -> None:
    retry = method == "finalize_retryable_failure"
    status, state = ("RETRYABLE_FAILURE", "QUEUED") if retry else ("TERMINAL_FAILURE", "FAILED")
    rows = {
        "missing": [],
        "duplicate": [_j(state=state), _j(state=state)],
        "wrong-policy": [_j(state=state, analysis_version="Other_2")],
        "wrong-count": [_j(state=state, attempt_count=1)],
        "wrong-state": [_j()],
    }[defect]
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write(
                "attempt-write",
                [
                    _a(
                        status=status,
                        lease_expires_at=None,
                        finished_at=_NOW,
                        safe_failure_code="retry",
                    )
                ],
                _JOB,
                2,
                _SLOT,
                _LEASE,
                "retry",
            ),
            _write("job-write", rows, _JOB),
        ]
    )
    _fixed(_capture(_repository(pool), method))
    _done(pool, rollback=True)


@pytest.mark.parametrize("layout", ("active", "candidate"))
def test_fresh_selection_invalid_or_active_state_never_generates_a_lease(layout: str) -> None:
    steps = _fresh()
    if layout == "candidate":
        steps[3] = replace(steps[3], rows=[_j()])
    else:
        steps[-1] = replace(steps[-1], rows=[_a()])
    pool = _Pool(steps)
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)))
    assert factory.calls == 0
    _done(pool, rollback=True, remaining=True)


def _recovery(
    job: tuple[object, ...], attempt: object, *, clock: datetime = _EXPIRY
) -> list[_Step]:
    return [
        *_settings(),
        _read("clock", [(clock,)]),
        _read("recovery", [job], 2),
        _read("recovery-attempt", attempt, job[0], job[8]),
    ]


@pytest.mark.parametrize("limit", (2, 3))
def test_expiry_recovery_uses_null_code_and_does_not_increment_generation(limit: int) -> None:
    exhausted = limit == 2
    job = _j(attempt_limit=limit)
    steps = [
        *_recovery(job, [_a()]),
        _read("clock", [(_EXPIRY,)]),
        _write(
            "attempt-write",
            [
                _a(
                    status="EXHAUSTED" if exhausted else "INTERRUPTED",
                    lease_expires_at=None,
                    finished_at=_EXPIRY,
                )
            ],
            _JOB,
            2,
            _SLOT,
            _LEASE,
        ),
        _write(
            "job-write", [_j(state="FAILED" if exhausted else "QUEUED", attempt_limit=limit)], _JOB
        ),
    ]
    pool = _Pool(steps)
    factory = _Factory(pool)
    outcome = _run(_repository(pool, factory), "recover_expired")
    assert outcome == worker.RecoverySummary(int(not exhausted), int(exhausted))
    assert factory.calls == 0
    _done(pool)


@pytest.mark.parametrize(
    "branch", ("no-candidate", "attempt-skipped", "clock-reversed", "guard-zero", "resolved")
)
def test_recovery_weak_absence_revalidation_and_zero_gate_produce_no_counts(branch: str) -> None:
    steps = _recovery(_j(), [_a()])
    if branch == "no-candidate":
        steps = [*_settings(), _read("clock", [(_EXPIRY,)]), _read("recovery", [], 2)]
    elif branch == "attempt-skipped":
        steps[-1] = replace(steps[-1], rows=[])
    elif branch == "resolved":
        steps = _recovery(
            _j(state="QUEUED"),
            [_a(status="INTERRUPTED", lease_expires_at=None, finished_at=_EXPIRY)],
        )
        steps.append(_read("clock", [(_EXPIRY,)]))
    else:
        steps.append(_read("clock", [(_NOW if branch == "clock-reversed" else _EXPIRY,)]))
        if branch == "guard-zero":
            steps.append(_write("attempt-write", [], _JOB, 2, _SLOT, _LEASE))
    pool = _Pool(steps)
    factory = _Factory(pool)
    assert _run(_repository(pool, factory), "recover_expired") == worker.RecoverySummary(0, 0)
    assert factory.calls == 0
    _done(pool)


@pytest.mark.parametrize(
    "defect", ("over-bound", "duplicate", "malformed-job", "contradictory", "bad-attempt")
)
def test_recovery_invalid_selection_or_current_pair_rolls_back_batch(defect: str) -> None:
    steps = _recovery(_j(), [_a()])
    if defect in ("over-bound", "duplicate", "malformed-job"):
        rows = [_j()] * (3 if defect == "over-bound" else 2)
        if defect == "malformed-job":
            rows = [_j(attempt_count=True)]
        steps[3] = replace(steps[3], rows=rows)
    elif defect == "contradictory":
        steps[3] = replace(steps[3], rows=[_j(state="QUEUED")])
    else:
        steps[-1] = replace(steps[-1], rows=[_a(heartbeat_at=_START - timedelta(seconds=1))])
    pool = _Pool(steps)
    _fixed(_capture(_repository(pool), "recover_expired"))
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize(
    "field",
    (
        "job_id",
        "attempt_number",
        "worker_slot_id",
        "lease_id",
        "started_at",
        "heartbeat_at",
        "lease_expires_at",
        "finished_at",
        "safe_failure_code",
        "status",
    ),
)
def test_recovery_returning_preserves_authority_and_enforces_expired_mutation_time(
    field: str,
) -> None:
    bad: dict[str, object] = dict(
        job_id=_OTHER_JOB,
        attempt_number=1,
        worker_slot_id=_OTHER,
        lease_id=_OTHER,
        started_at=_START + timedelta(microseconds=1),
        heartbeat_at=_NOW + timedelta(microseconds=1),
        lease_expires_at=_EXPIRY,
        finished_at=_EXPIRY - timedelta(microseconds=1),
        safe_failure_code="retry",
        status="RETRYABLE_FAILURE",
    )
    row = _a(
        **(
            dict(status="INTERRUPTED", lease_expires_at=None, finished_at=_EXPIRY)
            | {field: bad[field]}
        )
    )
    pool = _Pool(
        [
            *_recovery(_j(), [_a()]),
            _read("clock", [(_EXPIRY,)]),
            _write("attempt-write", [row], _JOB, 2, _SLOT, _LEASE),
        ]
    )
    _fixed(_capture(_repository(pool), "recover_expired"))
    _done(pool, rollback=True)


def test_recovery_second_candidate_failure_rolls_back_entire_batch_without_partial_summary() -> (
    None
):
    first, second = _j(), _j(job_id=_OTHER_JOB)
    pool = _Pool(
        [
            *_settings(),
            _read("clock", [(_EXPIRY,)]),
            _read("recovery", [first, second], 2),
            _read("recovery-attempt", [_a()], _JOB, 2),
            _read("clock", [(_EXPIRY,)]),
            _write(
                "attempt-write",
                [_a(status="INTERRUPTED", lease_expires_at=None, finished_at=_EXPIRY)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
            _write("job-write", [_j(state="QUEUED")], _JOB),
            _read(
                "recovery-attempt",
                [_a(job_id=_OTHER_JOB, status="ACTIVE", safe_failure_code="retry")],
                _OTHER_JOB,
                2,
            ),
        ]
    )
    _fixed(_capture(_repository(pool), "recover_expired"))
    _done(pool, rollback=True)


def test_recovery_second_job_write_failure_cannot_expose_summary() -> None:
    pool = _Pool(
        [
            *_recovery(_j(), [_a()]),
            _read("clock", [(_EXPIRY,)]),
            _write(
                "attempt-write",
                [_a(status="INTERRUPTED", lease_expires_at=None, finished_at=_EXPIRY)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
            _write("job-write", [], _JOB),
        ]
    )
    _fixed(_capture(_repository(pool), "recover_expired"))
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    "boundary", ("claim-heartbeat", "claim-expiry", "fresh-duration", "renew-duration")
)
@pytest.mark.parametrize("ordering", ("valid", "reversed", "equal"))
def test_fold_sensitive_order_and_exact_duration_are_absolute(boundary: str, ordering: str) -> None:
    zone = _Fold()  # Always available; no ZoneInfo download or skip.
    earlier = datetime(2026, 10, 25, 1, 45, tzinfo=zone, fold=0)
    later = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    left, right = earlier, later
    if ordering == "reversed":
        left, right = later, earlier
    elif ordering == "equal":
        left, right = later, later.astimezone(UTC)
    method = "claim_next"
    changes: dict[str, object] = {}
    if boundary == "claim-heartbeat":
        row = _a(started_at=left, heartbeat_at=right, lease_expires_at=right + timedelta(hours=3))
        steps = [*_slot(attempt=[row]), _read("clock", [(right,)])]
        accepted = ordering != "reversed"
    elif boundary == "claim-expiry":
        row = _a(
            started_at=datetime(2026, 10, 25, tzinfo=UTC), heartbeat_at=left, lease_expires_at=right
        )
        steps = [*_slot(attempt=[row]), _read("clock", [(left,)])]
        accepted = ordering == "valid"
    elif boundary == "fresh-duration":
        row = _a(started_at=left, heartbeat_at=left, lease_expires_at=right)
        steps = [*_fresh(), *_fresh_writes(attempt=[row])]
        changes["lease_duration"] = LeaseDuration(timedelta(minutes=45))
        accepted = ordering == "valid"
    else:
        method = "renew_lease"
        row = _a(
            started_at=datetime(2026, 10, 25, tzinfo=UTC), heartbeat_at=left, lease_expires_at=right
        )
        old = _a(
            started_at=datetime(2026, 10, 25, tzinfo=UTC),
            heartbeat_at=earlier,
            lease_expires_at=datetime(2026, 10, 25, 3, tzinfo=UTC),
        )
        steps = [
            *_owner(attempt=[old]),
            _read("clock", [(earlier,)]),
            _write("attempt-write", [row], _JOB, 2, _SLOT, _LEASE),
        ]
        changes["lease_duration"] = LeaseDuration(timedelta(minutes=45))
        accepted = ordering == "valid"
    pool = _Pool(steps)
    repo = _repository(pool)
    if accepted:
        outcome = _run(repo, method, **changes)
        assert type(outcome) is (worker.LeaseRenewed if method == "renew_lease" else WorkerClaim)
        for name, value in zip(_A_COLUMNS[5:8], row[5:8], strict=True):
            if hasattr(outcome, name):
                assert getattr(outcome, name) is value
                assert getattr(outcome, name).fold == cast(datetime, value).fold
        _done(pool)
    else:
        _fixed(_capture(repo, method, **changes))
        _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize("upper", (False, True))
def test_decoder_preserves_extreme_aware_dates_without_utc_overflow(upper: bool) -> None:
    left = (datetime.max - timedelta(microseconds=1) if upper else datetime.min).replace(
        tzinfo=timezone(timedelta(hours=-23 if upper else 23))
    )
    right = left + timedelta(microseconds=1)
    job = _j(accepted_at=left)
    row = _a(started_at=left, heartbeat_at=left, lease_expires_at=right)
    pool = _Pool([*_slot(job=[job], attempt=[row]), _read("clock", [(left,)])])
    outcome = _run(_repository(pool))
    assert type(outcome) is WorkerClaim
    assert outcome.started_at is left and outcome.heartbeat_at is left
    assert outcome.lease_expires_at is right and outcome.job.accepted_at is left
    _done(pool)


def test_renewal_may_shorten_expiry_and_accept_equal_old_heartbeat() -> None:
    duration = LeaseDuration(timedelta(microseconds=1))
    expiry = _NOW + duration.value
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write("attempt-write", [_a(lease_expires_at=expiry)], _JOB, 2, _SLOT, _LEASE),
        ]
    )
    outcome = _run(_repository(pool), "renew_lease", lease_duration=duration)
    assert type(outcome) is worker.LeaseRenewed
    assert outcome.heartbeat_at is _NOW and outcome.lease_expires_at is expiry
    _done(pool)


@pytest.mark.parametrize("method", ("finalize_retryable_failure", "recover_expired"))
@pytest.mark.parametrize("order", ("valid-fold", "reversed-fold", "equal-heartbeat"))
def test_resolved_finish_order_uses_absolute_instants(method: str, order: str) -> None:
    zone = _Fold()
    earlier = datetime(2026, 10, 25, 1, 45, tzinfo=zone, fold=0)
    later = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    hb, finish = (earlier, later) if order == "valid-fold" else (later, earlier)
    if order == "equal-heartbeat":
        hb, finish = later, later.astimezone(UTC)
    start = datetime(2026, 10, 25, tzinfo=UTC)
    recovery = method == "recover_expired"
    expiry = _fold_fixture_expiry(hb) if recovery else datetime(2026, 10, 25, 3, tzinfo=UTC)
    # The observed ACTIVE row must be valid before testing mutation RETURNING.
    WorkerClaim(_AUTHORITY, _claimable_fixture(_NOW), start, hb, expiry)
    old = _a(started_at=start, heartbeat_at=hb, lease_expires_at=expiry)
    returned = _a(
        started_at=start,
        heartbeat_at=hb,
        lease_expires_at=None,
        finished_at=finish,
        status="INTERRUPTED" if recovery else "RETRYABLE_FAILURE",
        safe_failure_code=None if recovery else "retry",
    )
    steps = _recovery(_j(), [old], clock=expiry) if recovery else _owner(attempt=[old])
    steps += [
        _read("clock", [(expiry if recovery else hb,)]),
        _write("attempt-write", [returned], _JOB, 2, _SLOT, _LEASE),
    ]
    accepted = order == "valid-fold" or (order == "equal-heartbeat" and not recovery)
    if accepted:
        steps.append(_write("job-write", [_j(state="QUEUED")], _JOB))
    pool = _Pool(steps)
    if accepted:
        result = _run(_repository(pool), method)
        assert type(result) is (worker.RecoverySummary if recovery else worker.AttemptRequeued)
        _done(pool)
    else:
        _fixed(_capture(_repository(pool), method))
        _done(pool, rollback=True)


@pytest.mark.parametrize("recognized", (True, False), ids=("active-slot", "other-constraint"))
def test_fresh_insert_unique_violation_recognition_is_narrow(recognized: bool) -> None:
    error = UniqueViolation(
        "driver-marker", info={ord("n"): (_SLOT_INDEX if recognized else "other_idx").encode()}
    )
    steps = [*_fresh(), *_fresh_writes()]
    steps[-1] = replace(steps[-1], execute_error=error)
    pool = _Pool(steps)
    factory = _Factory(pool)
    captured = _capture(_repository(pool, factory))
    if recognized:
        _fixed(captured, unavailable=True)
        assert pool.exit_primaries == [captured, captured]
    else:
        assert captured is error
    assert factory.calls == 1
    assert pool.events.count("execute:job-write") == 1
    assert pool.events.count("execute:insert") == 1
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    "diagnostic",
    (
        None,
        b"",
        b"analysis_job_attempts_active_job_id_idx",
        b"analysis_job_attempts_active_worker_slot_id_idx ",
    ),
)
def test_unique_violation_missing_null_or_nonexact_diagnostics_propagate(
    diagnostic: bytes | None,
) -> None:
    info: dict[int, bytes | None] = {} if diagnostic is None else {ord("n"): diagnostic}
    error = UniqueViolation("driver-marker", info=info)
    steps = [*_fresh(), *_fresh_writes()]
    steps[-1] = replace(steps[-1], execute_error=error)
    pool = _Pool(steps)
    factory = _Factory(pool)
    assert _capture(_repository(pool, factory)) is error
    assert factory.calls == 1
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    "boundary", ("job-execute", "insert-fetch", "insert-close", "commit", "release", "factory")
)
def test_matching_unique_violation_outside_insert_execution_is_not_c9b(boundary: str) -> None:
    error = UniqueViolation("driver-marker", info={ord("n"): _SLOT_INDEX.encode()})
    steps = [*_fresh(), *_fresh_writes()]
    if boundary == "job-execute":
        steps[-2] = replace(steps[-2], execute_error=error)
    elif boundary == "insert-fetch":
        steps[-1] = replace(steps[-1], fetch_error=error)
    elif boundary == "insert-close":
        steps[-1] = replace(steps[-1], close_error=error)
    pool = _Pool(steps)
    if boundary == "commit":
        pool.transaction_exit_error = error
    elif boundary == "release":
        pool.release_error = error
    factory = _Factory(pool, error=error if boundary == "factory" else None)
    assert _capture(_repository(pool, factory)) is error
    assert factory.calls == 1
    _done(pool, rollback=boundary not in ("commit", "release"), remaining=True)


class _LookalikeError(RuntimeError):
    sqlstate = "23505"

    @property
    def diag(self) -> Any:
        raise AssertionError("Programming failure must not be inspected as a driver error")


@pytest.mark.parametrize(
    "error", (CheckViolation("driver-marker"), _LookalikeError("driver-marker"))
)
def test_unrecognized_integrity_and_programming_errors_are_not_contention(
    error: BaseException,
) -> None:
    steps = [*_fresh(), *_fresh_writes()]
    steps[-1] = replace(steps[-1], execute_error=error)
    pool = _Pool(steps)
    assert _capture(_repository(pool)) is error
    _done(pool, rollback=True)


_IO_ERRORS = (PoolTimeout, OperationalError, InterfaceError, QueryCanceled, LockNotAvailable)
_IO_BOUNDARIES = (
    "acquire",
    "transaction-enter",
    "setting-execute",
    "setting-close",
    "execute",
    "fetch",
    "close",
    "commit",
    "release",
)


def _failure_pool(boundary: str, error: BaseException) -> _Pool:
    steps = [*_settings(), _read("locator", [], _SLOT), _read("candidate", [])]
    if boundary in ("setting-execute", "setting-close"):
        field = "execute_error" if boundary == "setting-execute" else "close_error"
        steps[0] = replace(steps[0], **cast(Any, {field: error}))
    elif boundary in ("execute", "fetch", "close"):
        field = {"execute": "execute_error", "fetch": "fetch_error", "close": "close_error"}[
            boundary
        ]
        steps[2] = replace(steps[2], **cast(Any, {field: error}))
    pool = _Pool(steps)
    attribute = {
        "acquire": "acquire_error",
        "transaction-enter": "transaction_enter_error",
        "commit": "transaction_exit_error",
        "release": "release_error",
    }.get(boundary)
    if attribute is not None:
        setattr(pool, attribute, error)
    return pool


@pytest.mark.parametrize("boundary", _IO_BOUNDARIES)
@pytest.mark.parametrize("error_type", _IO_ERRORS)
def test_actual_driver_io_failures_are_fixed_unavailable_with_cleanup(
    boundary: str, error_type: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    pool = _failure_pool(boundary, error_type("driver-marker"))
    factory = _Factory(pool)
    captured = _capture(_repository(pool, factory))
    _fixed(captured, unavailable=True)
    assert factory.calls == 0 and pool.calls == [1.25]
    if boundary != "acquire":
        assert pool.events[-1] == "release"
    if boundary not in ("acquire", "transaction-enter", "commit", "release"):
        assert pool.events.count("rollback") == 1
    assert all(cursor.closes == 1 for cursor in pool.value.cursors)
    assert all(
        "driver-marker" not in record.getMessage() and record.exc_info is None
        for record in caplog.records
    )


@pytest.mark.parametrize("boundary", _IO_BOUNDARIES)
@pytest.mark.parametrize(
    "kind", ("runtime", "type", "attribute", "cancelled", "memory", "fatal", "check")
)
def test_unexpected_boundary_failures_propagate_by_identity(boundary: str, kind: str) -> None:
    error = {
        "runtime": RuntimeError("marker"),
        "type": TypeError("marker"),
        "attribute": AttributeError("marker"),
        "cancelled": asyncio.CancelledError("marker"),
        "memory": MemoryError("marker"),
        "fatal": _Fatal("marker"),
        "check": CheckViolation("marker"),
    }[kind]
    pool = _failure_pool(boundary, error)
    assert _capture(_repository(pool)) is error
    if boundary != "acquire":
        assert pool.events[-1] == "release"
    assert all(cursor.closes == 1 for cursor in pool.value.cursors)


@pytest.mark.parametrize(
    ("branch", "boundary"),
    tuple(
        (branch, boundary)
        for branch in ("driver", "invariant", "c9a", "c9b")
        for boundary in ("cursor", "transaction", "release")
        if (branch, boundary) != ("c9b", "cursor")
    ),
)
@pytest.mark.parametrize("cleanup_kind", ("ordinary", "fatal", "cancelled", "memory"))
def test_classified_primary_is_sanitized_before_cleanup_precedence(
    branch: str, boundary: str, cleanup_kind: str
) -> None:
    cleanup: BaseException = {
        "ordinary": OperationalError("cleanup-marker"),
        "fatal": _Fatal("cleanup-marker"),
        "cancelled": asyncio.CancelledError("cleanup-marker"),
        "memory": MemoryError("cleanup-marker"),
    }[cleanup_kind]
    unavailable = branch != "invariant"
    if branch == "driver":
        steps = [
            *_settings(),
            replace(_read("locator", [], _SLOT), fetch_error=OperationalError("driver-marker")),
        ]
    elif branch == "invariant":
        steps = _slot(attempt=[_a(safe_failure_code="rejected-marker")])
    elif branch == "c9a":
        steps = _slot(
            job=[_j(state="QUEUED")],
            attempt=[_a(status="INTERRUPTED", lease_expires_at=None, finished_at=_NOW)],
        )
    else:
        steps = [*_fresh(), *_fresh_writes()]
        steps[-1] = replace(
            steps[-1],
            execute_error=UniqueViolation("driver-marker", info={ord("n"): _SLOT_INDEX.encode()}),
        )
    if boundary == "cursor":
        steps[-1] = replace(steps[-1], close_error=cleanup)
    pool = _Pool(steps)
    if boundary == "transaction":
        pool.transaction_exit_error = cleanup
    elif boundary == "release":
        pool.release_error = cleanup
    factory = _Factory(pool)
    captured = _capture(_repository(pool, factory))
    if cleanup_kind != "ordinary":
        assert captured is cleanup
        assert captured.__cause__ is not None
        _fixed(captured.__cause__, unavailable=unavailable)
        assert captured.__context__ is None or captured.__context__ is captured.__cause__
    else:
        _fixed(captured, unavailable=unavailable)
    assert factory.calls == int(branch == "c9b")
    assert pool.events.count("rollback") == 1 and pool.events[-1] == "release"
    assert all(cursor.closes == 1 for cursor in pool.value.cursors)


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("boundary", ("commit", "release"))
def test_no_operation_outcome_escapes_failed_commit_or_release(method: str, boundary: str) -> None:
    if method == "claim_next":
        steps = [*_settings(), _read("locator", [], _SLOT), _read("candidate", [])]
    elif method == "recover_expired":
        steps = [*_settings(), _read("clock", [(_NOW,)]), _read("recovery", [], 2)]
    else:
        steps = [*_settings(), _read("job", [], _JOB)]
    pool = _Pool(steps)
    if boundary == "commit":
        pool.transaction_exit_error = OperationalError("driver-marker")
    else:
        pool.release_error = OperationalError("driver-marker")
    _fixed(_capture(_repository(pool), method), unavailable=True)
    assert pool.events.count("commit") == 1 and pool.events[-1] == "release"
    assert pool.value.steps == []


@pytest.mark.parametrize("leak", ("args", "attributes", "cause", "context"))
def test_helper_fixed_error_assertion_rejects_hidden_sensitive_graph(leak: str) -> None:
    error = worker.AnalysisJobWorkerPersistenceInvariantError(_INVARIANT)
    if leak == "args":
        error.args = (_INVARIANT, "marker")
    elif leak == "attributes":
        error.__dict__["row"] = "marker"
    elif leak == "cause":
        error.__cause__ = RuntimeError("marker")
    else:
        error.__context__ = RuntimeError("marker")
    with pytest.raises(AssertionError):
        _fixed(error)


def test_current_runtime_remains_worker_repository_free() -> None:
    import main
    from core.config import Settings

    assert not {"worker_repository", "analysis_job_repository"}.intersection(
        inspect.signature(main.create_app).parameters
    )
    assert "attempt_limit" not in Settings.__dataclass_fields__
    assert not main.app.state.analysis_queue.metrics().worker_running
    assert not {"worker_repository", "analysis_job_repository", "postgres_pool"}.intersection(
        main.app.state._state
    )
    assert all(type(value).__module__ != _MODULE for value in main.app.state._state.values())
    root = Path(__file__).resolve().parents[1]
    for relative in ("src/main.py", "src/core/config.py", "src/api/routes.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert _MODULE not in imports


def test_concrete_adapter_has_no_success_outbox_or_runtime_surface() -> None:
    module = _module()
    assert not {"finalize_success", "run_worker", "worker_loop", "dispatch_callbacks"}.intersection(
        vars(module)
    )
    assert {
        name
        for name, value in vars(module.PsycopgAnalysisJobWorkerRepository).items()
        if not name.startswith("_") and callable(value)
    } == set(_METHODS)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        (
            dict(
                connection_acquire_timeout_seconds=False,
                operation_timeout_ms=False,
                lock_timeout_ms=False,
                lease_id_factory=3,
            ),
            "Connection acquisition timeout must be numeric.",
        ),
        (
            dict(operation_timeout_ms=False, lock_timeout_ms=False, lease_id_factory=3),
            "PostgreSQL millisecond timeout must be an integer.",
        ),
        (
            dict(operation_timeout_ms=0, lock_timeout_ms=False, lease_id_factory=3),
            "PostgreSQL millisecond timeout is outside the approved range.",
        ),
        (
            dict(operation_timeout_ms=2, lock_timeout_ms=False, lease_id_factory=3),
            "PostgreSQL millisecond timeout must be an integer.",
        ),
        (
            dict(operation_timeout_ms=1, lock_timeout_ms=1, lease_id_factory=3),
            "Operation timeout must exceed lock timeout.",
        ),
    ),
)
def test_complete_constructor_validation_precedence(
    changes: dict[str, object], message: str
) -> None:
    pool = _Pool([])
    with pytest.raises((TypeError, ValueError)) as caught:
        _repository(pool, **changes)
    assert caught.value.args == (message,) and pool.events == []


def test_constructor_does_not_inspect_pool_and_acquisition_conversion_occurs_once() -> None:
    module = _module()
    probes: list[str] = []

    class NoProbe:
        def __getattr__(self, name: str) -> object:
            probes.append(name)
            raise AssertionError("Construction must not inspect a pool")

    class Numeric(float):
        def __float__(self) -> float:
            probes.append("float")
            return 1.25

    module.PsycopgAnalysisJobWorkerRepository(
        pool=NoProbe(),
        connection_acquire_timeout_seconds=1.25,
        operation_timeout_ms=2,
        lock_timeout_ms=1,
        lease_id_factory=lambda: _LEASE,
    )
    assert probes == []
    pool = _Pool([*_settings(), _read("locator", [], _SLOT), _read("candidate", [])])
    repo = _repository(pool, connection_acquire_timeout_seconds=Numeric(1.25))
    assert probes == ["float"] and pool.events == []
    assert type(_run(repo)) is worker.NoJobAvailable
    assert probes == ["float"] and type(pool.calls[0]) is float
    _done(pool)


@pytest.mark.parametrize("defect", ("different-job", "future-generation"))
def test_required_attempt_association_is_validated_before_authority(defect: str) -> None:
    row = _a(job_id=_OTHER_JOB) if defect == "different-job" else _a(attempt_number=3)
    pool = _Pool(_owner(attempt=[row]))
    _fixed(
        _capture(_repository(pool), "renew_lease", authority=replace(_AUTHORITY, attempt_number=1))
    )
    _done(pool, rollback=True)


@pytest.mark.parametrize("method", ("renew_lease", "finalize_retryable_failure"))
def test_mutation_sample_may_precede_earlier_observation_when_clock_reverses(method: str) -> None:
    mutation_time = _NOW + timedelta(seconds=1)
    observation_time = _NOW + timedelta(seconds=2)
    row = _a(heartbeat_at=mutation_time, lease_expires_at=mutation_time + _DURATION.value)
    if method == "finalize_retryable_failure":
        row = _a(
            status="RETRYABLE_FAILURE",
            lease_expires_at=None,
            finished_at=mutation_time,
            safe_failure_code="retry",
        )
    steps = [
        *_owner(),
        _read("clock", [(observation_time,)]),
        _write("attempt-write", [row], _JOB, 2, _SLOT, _LEASE),
    ]
    if method == "finalize_retryable_failure":
        steps.append(_write("job-write", [_j(state="QUEUED")], _JOB))
    pool = _Pool(steps)
    result = _run(_repository(pool), method)
    assert type(result) is (
        worker.LeaseRenewed if method == "renew_lease" else worker.AttemptRequeued
    )
    _done(pool)


@pytest.mark.parametrize("extra_microsecond", (False, True))
def test_elapsed_duration_does_not_round_away_large_interval_microseconds(
    extra_microsecond: bool,
) -> None:
    duration = LeaseDuration(timedelta(days=365000, microseconds=2))
    expiry = _NOW + duration.value + timedelta(microseconds=int(extra_microsecond))
    assert (
        duration.value.total_seconds()
        == (duration.value + timedelta(microseconds=1)).total_seconds()
    )
    pool = _Pool(
        [*_fresh(), *_fresh_writes(attempt=[_a(started_at=_NOW, lease_expires_at=expiry)])]
    )
    repo = _repository(pool)
    if extra_microsecond:
        _fixed(_capture(repo, lease_duration=duration))
        _done(pool, rollback=True)
    else:
        result = _run(repo, lease_duration=duration)
        assert type(result) is WorkerClaim and result.lease_expires_at is expiry
        _done(pool)


@pytest.mark.parametrize("method", _METHODS[1:4])
def test_owner_guard_duplicate_rows_are_invariant_not_success(method: str) -> None:
    row = _a(
        heartbeat_at=_NOW + timedelta(seconds=1),
        lease_expires_at=_NOW + timedelta(seconds=1) + _DURATION.value,
    )
    if method != "renew_lease":
        row = _a(
            status="RETRYABLE_FAILURE"
            if method == "finalize_retryable_failure"
            else "TERMINAL_FAILURE",
            lease_expires_at=None,
            finished_at=_NOW,
            safe_failure_code="retry",
        )
    pool = _Pool(
        [
            *_owner(),
            _read("clock", [(_NOW,)]),
            _write("attempt-write", [row, row], _JOB, 2, _SLOT, _LEASE),
        ]
    )
    _fixed(_capture(_repository(pool), method))
    _done(pool, rollback=True)


@pytest.mark.parametrize("method", ("finalize_retryable_failure", "recover_expired"))
@pytest.mark.parametrize(
    ("field", "bad"), _BAD_JOB_WRITE[:8], ids=[f for f, _ in _BAD_JOB_WRITE[:8]]
)
def test_resolving_job_returning_cannot_change_immutable_request_or_policy(
    method: str, field: str, bad: object
) -> None:
    recovery = method == "recover_expired"
    steps = _recovery(_j(), [_a()]) if recovery else _owner()
    finish = _EXPIRY if recovery else _NOW
    steps += [
        _read("clock", [(finish,)]),
        _write(
            "attempt-write",
            [
                _a(
                    status="INTERRUPTED" if recovery else "RETRYABLE_FAILURE",
                    lease_expires_at=None,
                    finished_at=finish,
                    safe_failure_code=None if recovery else "retry",
                )
            ],
            _JOB,
            2,
            _SLOT,
            _LEASE,
        ),
        _write("job-write", [_j(state="QUEUED", **{field: bad})], _JOB),
    ]
    pool = _Pool(steps)
    _fixed(_capture(_repository(pool), method))
    _done(pool, rollback=True)


@pytest.mark.parametrize("side", ("job", "attempt"))
def test_rejected_row_data_is_not_logged_or_retained(
    side: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "private-red-h-row-marker"
    steps = (
        _slot(job=[_j(callback_url=f"https://example.com/#{marker}")])
        if side == "job"
        else _slot(attempt=[_a(safe_failure_code=f"bad+{marker}")])
    )
    pool = _Pool(steps)
    _fixed(_capture(_repository(pool)))
    for record in caplog.records:
        assert record.exc_info is None
        assert marker not in record.getMessage()
        for value in record.__dict__.values():
            if isinstance(value, (str, bytes)):
                assert marker.encode() not in (value.encode() if isinstance(value, str) else value)
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize(
    ("boundary", "primary_kind"),
    tuple(
        (boundary, kind)
        for boundary in ("cursor", "transaction", "release")
        for kind in ("factory-driver", "cancelled", "fatal")
        if (boundary, kind) != ("cursor", "factory-driver")
    ),
)
def test_untranslated_primary_identity_survives_ordinary_cleanup(
    boundary: str, primary_kind: str
) -> None:
    primary: BaseException = {
        "factory-driver": OperationalError("factory-marker"),
        "cancelled": asyncio.CancelledError("primary-marker"),
        "fatal": _Fatal("primary-marker"),
    }[primary_kind]
    cleanup = RuntimeError("cleanup-marker")
    if primary_kind == "factory-driver":
        # Factory raises without producing a cursor: transaction/release are the cleanup boundaries.
        steps = _fresh()
    else:
        steps = [*_settings(), replace(_read("locator", [], _SLOT), fetch_error=primary)]
        if boundary == "cursor":
            steps[-1] = replace(steps[-1], close_error=cleanup)
    pool = _Pool(steps)
    if boundary == "transaction":
        pool.transaction_exit_error = cleanup
    elif boundary == "release":
        pool.release_error = cleanup
    factory = _Factory(pool, error=primary if primary_kind == "factory-driver" else None)
    assert _capture(_repository(pool, factory)) is primary
    assert factory.calls == int(primary_kind == "factory-driver")
    assert pool.events.count("rollback") == 1 and pool.events[-1] == "release"
    assert all(cursor.closes == 1 for cursor in pool.value.cursors)


def test_recovery_mixed_complete_pairs_account_once_without_refill() -> None:
    pool = _Pool(
        [
            *_settings(),
            _read("clock", [(_EXPIRY,)]),
            _read("recovery", [_j(), _j(job_id=_OTHER_JOB, attempt_limit=2)], 2),
            _read("recovery-attempt", [_a()], _JOB, 2),
            _read("clock", [(_EXPIRY,)]),
            _write(
                "attempt-write",
                [_a(status="INTERRUPTED", lease_expires_at=None, finished_at=_EXPIRY)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
            _write("job-write", [_j(state="QUEUED")], _JOB),
            _read(
                "recovery-attempt",
                [_a(job_id=_OTHER_JOB, worker_slot_id=_OTHER, lease_id=_OTHER)],
                _OTHER_JOB,
                2,
            ),
            _read("clock", [(_EXPIRY,)]),
            _write(
                "attempt-write",
                [
                    _a(
                        job_id=_OTHER_JOB,
                        worker_slot_id=_OTHER,
                        lease_id=_OTHER,
                        status="EXHAUSTED",
                        lease_expires_at=None,
                        finished_at=_EXPIRY,
                    )
                ],
                _OTHER_JOB,
                2,
                _OTHER,
            ),
            _write(
                "job-write", [_j(job_id=_OTHER_JOB, attempt_limit=2, state="FAILED")], _OTHER_JOB
            ),
        ]
    )
    factory = _Factory(pool)
    assert _run(_repository(pool, factory), "recover_expired") == worker.RecoverySummary(1, 1)
    assert factory.calls == 0
    _done(pool)


@pytest.mark.parametrize("method", _METHODS[2:4])
@pytest.mark.parametrize("status", ("INTERRUPTED", "EXHAUSTED"))
def test_unclassified_resolution_is_never_classified_replay(method: str, status: str) -> None:
    job = _j(
        state="QUEUED" if status == "INTERRUPTED" else "FAILED",
        attempt_limit=3 if status == "INTERRUPTED" else 2,
    )
    pool = _Pool(
        _owner(job=[job], attempt=[_a(status=status, lease_expires_at=None, finished_at=_EXPIRY)])
    )
    result = _run(_repository(pool), method)
    assert type(result) is worker.ClaimLost and result.authority is _AUTHORITY
    _done(pool)


@pytest.mark.parametrize("method", _METHODS[1:4])
def test_cancelled_active_owner_has_no_mutation_authority(method: str) -> None:
    pool = _Pool(_owner(job=[_j(state="CANCELLED")]))
    result = _run(_repository(pool), method)
    assert type(result) is worker.ClaimLost and result.authority is _AUTHORITY
    _done(pool)


@pytest.mark.parametrize("boundary", ("job-execute", "insert-execute", "insert-fetch"))
def test_post_generation_driver_failure_rolls_back_with_one_factory_call(boundary: str) -> None:
    steps = [*_fresh(), *_fresh_writes()]
    index = -2 if boundary == "job-execute" else -1
    field = "fetch_error" if boundary == "insert-fetch" else "execute_error"
    steps[index] = replace(steps[index], **cast(Any, {field: OperationalError("driver-marker")}))
    pool = _Pool(steps)
    factory = _Factory(pool)
    _fixed(_capture(_repository(pool, factory)), unavailable=True)
    assert factory.calls == 1
    _done(pool, rollback=True, remaining=True)


@pytest.mark.parametrize("code", (_Text("retry"), b"retry", 1, ""))
def test_resolved_code_requires_exact_safe_builtin_text(code: object) -> None:
    pool = _Pool(
        _slot(
            job=[_j(state="QUEUED")],
            attempt=[
                _a(
                    status="RETRYABLE_FAILURE",
                    lease_expires_at=None,
                    finished_at=_NOW,
                    safe_failure_code=code,
                )
            ],
        )
    )
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True)


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize("layout", ("J", "A", "T"))
def test_timestamp_with_tzinfo_but_no_offset_is_invalid(layout: str) -> None:
    bad = datetime(2026, 10, 10, tzinfo=_NoOffset())
    steps = [*_slot(), _read("clock", [(_NOW,)])]
    index, rows = {
        "J": (3, [_j(accepted_at=bad)]),
        "A": (4, [_a(heartbeat_at=bad)]),
        "T": (5, [(bad,)]),
    }[layout]
    steps[index] = replace(steps[index], rows=rows)
    pool = _Pool(steps)
    _fixed(_capture(_repository(pool)))
    _done(pool, rollback=True, remaining=True)


_RECOGNITION_CONTROLS = (
    "raising-metadata",
    "recording-metaclass-sync",
    "recording-metaclass-async",
    "raising-metaclass-sync",
    "meta-metaclass-hash",
    "raising-meta-metaclass-hash",
    "sync-function",
    "async-function",
    "async-generator",
    "uuid4",
    "class-factory",
    "marked-sync",
    "recording-marker-key",
    "raising-marker-key",
    "recording-namespace-key",
    "raising-namespace-key",
    "wrong-hash-marker-key",
    "false-equality-marker-key",
    "unrelated-interceptor",
    "coexisting-marker-keys",
    "integer-plus-marker",
    "nonstring-marker-key",
    "wrong-marker-value",
    "native-coroutine-opaque",
    "native-generator-opaque",
    "earlier-opaque-namespace",
    "function-dict-subclass",
)


@pytest.mark.parametrize("case", _RECOGNITION_CONTROLS, ids=_RECOGNITION_CONTROLS)
def test_factory_native_observation_controls(case: str) -> None:
    hooks: list[str] = []
    calls: list[str] = []
    armed = [False]

    def synchronous() -> UUID:
        calls.append("factory")
        return _LEASE

    async def asynchronous() -> UUID:
        calls.append("factory")
        return _LEASE

    async def generator() -> AsyncIterator[object]:
        calls.append("factory")
        yield _LEASE

    def hook(name: str) -> None:
        if armed[0]:
            hooks.append(name)
            if case.startswith("raising"):
                raise RuntimeError("recognition-hook-marker")

    class Key(str):
        def __hash__(self) -> int:
            hook("hash")
            target = "__call__" if "namespace" in case else marker
            return hash(target) + int(case == "wrong-hash-marker-key")

        def __eq__(self, other: object) -> bool:
            hook("equality")
            return case == "unrelated-interceptor" and other == marker

    class NonStringKey:
        def __hash__(self) -> int:
            hook("hash")
            return hash(marker)

        def __eq__(self, other: object) -> bool:
            hook("equality")
            if armed[0] and case == "wrong-marker-value":
                raise RuntimeError("wrong-marker-value-equality")
            return case == "nonstring-marker-key" and other == marker

    class Metadata(dict[str, object]):
        def items(self) -> Never:
            raise AssertionError("User dictionary method must not execute")

    class MetaMeta(type):
        def __hash__(self) -> int:
            hook("meta-hash")
            return type.__hash__(self)

    class Meta(type, metaclass=MetaMeta):
        def __getattribute__(self, name: str) -> object:
            if "metaclass-hash" not in case:
                hook("meta-attribute")
            return type.__getattribute__(self, name)

    # Derive test instrumentation from the public marker API, not an adapter-private name.
    inspect.markcoroutinefunction(synchronous)
    marker, token = next(iter(synchronous.__dict__.items()))
    synchronous.__dict__.clear()
    factory: object = synchronous
    rejected = case in (
        "recording-metaclass-async",
        "async-function",
        "async-generator",
        "marked-sync",
        "native-coroutine-opaque",
        "native-generator-opaque",
        "function-dict-subclass",
    )
    if case == "raising-metadata":

        class MetadataFactory:
            def __getattribute__(self, name: str) -> object:
                hook("instance-attribute")
                return object.__getattribute__(self, name)

            def __getattr__(self, name: str) -> object:
                hook("getattr")
                raise AttributeError(name)

            def __call__(self) -> UUID:
                return synchronous()

        factory = MetadataFactory()
    elif "metaclass" in case:
        if case == "recording-metaclass-async":
            target: Callable[[], object] = asynchronous
        else:
            target = synchronous
        factory = Meta("Factory", (), {"__call__": staticmethod(target)})()
    elif case == "async-function":
        factory = asynchronous
    elif case == "async-generator":
        factory = generator
    elif case == "uuid4":
        factory = uuid4
    elif case == "class-factory":

        class ClassFactory:
            def __init__(self) -> None:
                calls.append("factory")

        factory = ClassFactory
    elif case in ("recording-namespace-key", "raising-namespace-key", "earlier-opaque-namespace"):
        namespace: dict[str, object] = {Key("unrelated"): object()}
        if case != "earlier-opaque-namespace":
            namespace["__call__"] = staticmethod(synchronous)
            factory = type("NamespaceFactory", (), namespace)()
        else:
            base = type("AsyncBase", (), {"__call__": staticmethod(asynchronous)})
            factory = type("OpaqueFactory", (base,), namespace)()
    else:
        function: Callable[[], object] = (
            asynchronous if case == "native-coroutine-opaque" else synchronous
        )
        if case == "native-generator-opaque":
            function = generator
        metadata: dict[object, object] = {}
        if case in ("marked-sync", "integer-plus-marker", "coexisting-marker-keys"):
            metadata[marker] = token
        if case == "integer-plus-marker":
            metadata[1] = "harmless"
        elif case == "wrong-marker-value":
            metadata[marker] = NonStringKey()
        elif case == "nonstring-marker-key":
            metadata[NonStringKey()] = token
        elif case not in ("sync-function", "marked-sync", "integer-plus-marker"):
            metadata[Key("unrelated" if case == "unrelated-interceptor" else marker)] = token
        if case == "function-dict-subclass":
            function.__dict__ = Metadata({marker: token})
        else:
            FunctionType.__dict__["__dict__"].__set__(function, metadata)
        factory = function
    pool = _Pool([*_fresh(), *_fresh_writes()])
    armed[0] = True
    if rejected:
        with pytest.raises(TypeError) as caught:
            _repository(pool, factory)
        assert caught.value.args == ("Lease UUID factory must be synchronous.",)
    else:
        repository = _repository(pool, factory)
    assert hooks == [] and calls == [] and pool.calls == [] and pool.events == []
    armed[0] = False  # Invocation may legitimately run normal Python lookup/binding.
    if not rejected:
        if case in ("uuid4", "class-factory", "earlier-opaque-namespace"):
            # Independent normal invocation; UUID4 is intentionally not replaced by a policy fake.
            if case == "uuid4":
                assert type(uuid4()) is UUID
            elif case == "class-factory":
                error = _capture(repository)
                assert type(error) is TypeError
                assert calls == ["factory"]
                _done(pool, rollback=True, remaining=True)
            else:
                error = _capture(repository)
                assert type(error) is TypeError
                assert calls == []  # Rejected unstarted coroutine; body never ran.
                _done(pool, rollback=True, remaining=True)
        else:
            result = _run(repository)
            assert type(result) is WorkerClaim and result.authority.lease_id.value is _LEASE
            assert calls == ["factory"]
            _done(pool)


_BOOTSTRAP_SCRIPT = r"""
import asyncio
import importlib
import importlib.util
import inspect
import sys

def check(condition, label):
    if not condition:
        raise AssertionError(label)

sys.path.insert(0, sys.argv[2])
import pydantic
import psycopg
import psycopg_pool
import domain.analysis_job
import domain.analysis_job_attempt
import services.analysis_job_worker
module_name = "adapters.psycopg_analysis_job_worker_repository"
check(module_name not in sys.modules, "adapter imported during preflight")
check(importlib.util.find_spec(module_name) is not None, "source path preflight")
print("PREFLIGHT_OK", flush=True)
case = sys.argv[1]
original_mark = inspect.markcoroutinefunction
original_predicate = inspect.iscoroutinefunction
calls = []
predicates = []
caller = ValueError("caller-context")
api_error = RuntimeError("api-marker")
api_cause = ValueError("api-cause")
api_error.__cause__ = api_cause
fatal = asyncio.CancelledError("fatal-api-marker")

class Key(str):
    def __hash__(self):
        return str.__hash__(self)
    def __eq__(self, other):
        raise AssertionError("custom bootstrap key compared")

def mark(probe):
    calls.append(probe)
    if case in ("api-error", "api-context"):
        raise api_error
    if case == "fatal-api":
        raise fatal
    if case in ("missing", "retry"):
        return probe
    original_mark(probe)
    if case == "derived-profile":
        probe.__dict__.clear()
        probe.__dict__["controlled_derived_name"] = None
    elif case == "ambiguous":
        probe.__dict__["extra"] = object()
    elif case == "nonexact-key":
        name, value = next(iter(probe.__dict__.items()))
        probe.__dict__.clear()
        probe.__dict__[Key(name)] = value
    elif case == "wrong-entry":
        probe.__dict__.clear()
        probe.__dict__["wrong"] = object()
    elif case == "native-flags":
        probe.__code__ = probe.__code__.replace(co_flags=probe.__code__.co_flags | inspect.CO_COROUTINE)
    elif case == "body":
        probe()
    # The return value is deliberately unrelated: the original probe must be inspected.
    return object()

def predicate(probe):
    predicates.append(probe)
    if case == "marked-control":
        return False
    if case == "marked-truthy":
        return 1
    if case == "unmarked-control":
        return True
    if case == "unmarked-falsy":
        return True if len(predicates) == 1 else 0
    if case == "derived-profile":
        return "controlled_derived_name" in probe.__dict__
    if case == "predicate-error":
        raise api_error
    return original_predicate(probe)

inspect.markcoroutinefunction = mark
inspect.iscoroutinefunction = predicate
failures = {
    "missing", "ambiguous", "nonexact-key", "wrong-entry", "marked-control",
    "unmarked-control", "native-flags", "body", "retry", "profile-context",
    "marked-truthy", "unmarked-falsy",
}
if case == "profile-context":
    def missing(probe):
        calls.append(probe)
        return probe
    inspect.markcoroutinefunction = missing

def attempt():
    try:
        return importlib.import_module(module_name), None
    except BaseException as error:
        return None, error

if case in ("api-context", "profile-context"):
    try:
        raise caller
    except ValueError:
        module, error = attempt()
else:
    module, error = attempt()
check(len(calls) == 1, "exact marker API call count")
if case in failures:
    check(type(error) is RuntimeError, "exact profile error type")
    check(error.args == ("Lease UUID factory coroutine-marker bootstrap failed.",), "profile message")
    check(error.__cause__ is None, "no explicit profile cause")
    check(error.__context__ is (caller if case == "profile-context" else None), "normal context")
    check(module_name not in sys.modules, "failed first import removed")
    if case in ("ambiguous", "nonexact-key", "native-flags"):
        check(predicates == [], "metadata/flag controls precede predicate")
elif case in ("api-error", "api-context", "predicate-error", "fatal-api"):
    expected = fatal if case == "fatal-api" else api_error
    check(error is expected, "API exception identity")
    check(error.__cause__ is (None if case == "fatal-api" else api_cause), "cause preserved")
    check(error.__context__ is (caller if case == "api-context" else None), "API context preserved")
    check(module_name not in sys.modules, "API failed first import removed")
else:
    check(error is None and module is not None, "valid production bootstrap")
    profile = module._COROUTINE_PROFILE
    check(type(profile) is tuple and type(profile[0]) is str, "private profile shape")
    check(len(predicates) == 2, "marked and unmarked controls")
    check(profile[1] is next(iter(calls[0].__dict__.values())), "token identity")
    check(importlib.import_module(module_name) is module, "cached import identity")
    class Pool:
        def __getattr__(self, name):
            raise AssertionError("pool probed")
    def factory():
        raise AssertionError("factory invoked")
    module.PsycopgAnalysisJobWorkerRepository(
        pool=Pool(), connection_acquire_timeout_seconds=1.25,
        operation_timeout_ms=2, lock_timeout_ms=1, lease_id_factory=factory,
    )
    check(len(calls) == 1, "cached import/construction never bootstrap")
    if case in ("reload", "failed-reload", "reload-recovery"):
        if case == "reload":
            importlib.reload(module)
            check(len(calls) == 2, "successful reload bootstrap once")
            check(module._COROUTINE_PROFILE is not profile, "new reload tuple")
        else:
            def missing(probe):
                calls.append(probe)
                return probe
            inspect.markcoroutinefunction = missing
            try:
                importlib.reload(module)
            except RuntimeError as failure:
                check(failure.args == ("Lease UUID factory coroutine-marker bootstrap failed.",), "reload failure")
                check(failure.__cause__ is None and failure.__context__ is None, "reload context")
            else:
                raise AssertionError("failed reload accepted")
            check(len(calls) == 2 and module._COROUTINE_PROFILE is profile, "pre-publication identity")
            check(importlib.import_module(module_name) is module, "cached failed-reload module")
            check(len(calls) == 2, "no repair on cached import")
            if case == "reload-recovery":
                inspect.markcoroutinefunction = mark
                importlib.reload(module)
                check(len(calls) == 3 and module._COROUTINE_PROFILE is not profile, "explicit recovery")
if case == "retry":
    def valid(probe):
        calls.append(probe)
        return original_mark(probe)
    inspect.markcoroutinefunction = valid
    inspect.iscoroutinefunction = original_predicate
    module = importlib.import_module(module_name)
    check(module is sys.modules[module_name], "fresh import retry succeeds")
    check(len(calls) == 2, "one bootstrap per fresh import attempt")
print("BOOTSTRAP_OK:" + case, flush=True)
"""


_BOOTSTRAP_CASES = (
    "valid",
    "derived-profile",
    "missing",
    "ambiguous",
    "nonexact-key",
    "wrong-entry",
    "marked-control",
    "unmarked-control",
    "marked-truthy",
    "unmarked-falsy",
    "native-flags",
    "body",
    "api-error",
    "fatal-api",
    "retry",
    "reload",
    "failed-reload",
    "reload-recovery",
    "api-context",
    "profile-context",
    "predicate-error",
)


@pytest.mark.parametrize("case", _BOOTSTRAP_CASES, ids=_BOOTSTRAP_CASES)
@pytest.mark.parametrize("optimized", (False, True), ids=("normal", "optimized"))
def test_factory_marker_production_bootstrap_isolated(case: str, optimized: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-B"]
    if optimized:
        command.append("-O")
    result = subprocess.run(
        [*command, "-c", _BOOTSTRAP_SCRIPT, case, str(root / "src")],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert "PREFLIGHT_OK" in result.stdout, (result.stdout, result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"BOOTSTRAP_OK:{case}" in result.stdout


_DESCRIPTOR_CASES = (
    "direct-static-async",
    "direct-class",
    "namespace-class-async",
    "namespace-static-async",
    "static-bound-async",
    "subclass-binds-sync",
    "subclass-binds-async",
    "subclass-direct-sync",
    "subclass-direct-async",
    "inherited-static-subclass",
    "nested-static",
    "static-partial-async",
    "partial-static-async",
    "static-method-partial",
    "namespace-none",
)
_PARTIAL_CASES = (
    "exact-sync",
    "exact-async",
    "nested-exact-async",
    "bound-method-async",
    "inherited-partial-sync",
    "inherited-partial-async",
    "partial-override-sync",
    "partial-override-async",
    "partial-metadata-sync",
    "partial-metadata-async",
    "partial-binds-sync",
    "partial-binds-async",
    "exact-opaque-partial",
    "namespace-exact-partial",
    "namespace-subclass-partial",
)


@pytest.mark.parametrize("case", _DESCRIPTOR_CASES, ids=_DESCRIPTOR_CASES)
def test_factory_descriptor_call_paths(case: str) -> None:
    hooks: list[str] = []
    calls: list[str] = []

    def sync(*args: object) -> UUID:
        calls.append("factory")
        return _LEASE

    async def asynchronous(*args: object) -> UUID:
        calls.append("factory")
        return _LEASE

    def bind_sync(self: object, instance: object, owner: object) -> Callable[[], object]:
        hooks.append("binding")
        return sync

    def bind_async(self: object, instance: object, owner: object) -> Callable[[], object]:
        hooks.append("binding")
        return asynchronous

    static_subclass = type("StaticSubclass", (staticmethod,), {})
    binding_sync = type("SyncBinding", (staticmethod,), {"__get__": bind_sync})
    binding_async = type("AsyncBinding", (staticmethod,), {"__get__": bind_async})
    direct_sync = type("SyncDirect", (staticmethod,), {"__call__": sync})
    direct_async = type("AsyncDirect", (staticmethod,), {"__call__": asynchronous})
    factories: dict[str, object] = {
        "direct-static-async": staticmethod(asynchronous),
        "direct-class": classmethod(asynchronous),
        "namespace-class-async": type("ClassCall", (), {"__call__": classmethod(asynchronous)})(),
        "namespace-static-async": type(
            "StaticCall", (), {"__call__": staticmethod(asynchronous)}
        )(),
        "static-bound-async": staticmethod(MethodType(asynchronous, object())),
        "subclass-binds-sync": type("Binding", (), {"__call__": binding_sync(asynchronous)})(),
        "subclass-binds-async": type("Binding", (), {"__call__": binding_async(sync)})(),
        "subclass-direct-sync": direct_sync(asynchronous),
        "subclass-direct-async": direct_async(sync),
        "inherited-static-subclass": type(
            "Inherited", (), {"__call__": static_subclass(asynchronous)}
        )(),
        "nested-static": staticmethod(staticmethod(asynchronous)),
        "static-partial-async": staticmethod(partial(asynchronous)),
        "partial-static-async": partial(staticmethod(asynchronous)),
        "static-method-partial": staticmethod(MethodType(partial(asynchronous), object())),
        "namespace-none": type("NoCall", (), {"__call__": None})(),
    }
    expected = "C" if case == "direct-class" else "A"
    if case in (
        "direct-static-async",
        "namespace-class-async",
        "namespace-static-async",
        "static-bound-async",
        "subclass-direct-async",
        "partial-static-async",
    ):
        expected = "S"
    pool = _Pool([*_fresh(), *_fresh_writes()])
    if expected != "A":
        with pytest.raises(TypeError) as caught:
            _repository(pool, factories[case])
        assert caught.value.args == (
            "Lease UUID factory must be callable."
            if expected == "C"
            else "Lease UUID factory must be synchronous.",
        )
    else:
        repository = _repository(pool, factories[case])
    assert hooks == [] and calls == [] and pool.events == [] and pool.calls == []
    if expected == "A":
        if case in ("subclass-binds-sync", "subclass-direct-sync"):
            result = _run(repository)
            assert type(result) is WorkerClaim and result.authority.lease_id.value is _LEASE
            assert calls == ["factory"]
            _done(pool)
        else:
            error = _capture(repository)
            assert type(error) is TypeError
            if case != "namespace-none":
                assert error.args == ("Lease UUID factory must return an exact UUID.",)
            assert calls == []
            _done(pool, rollback=True, remaining=True)
        assert hooks == (["binding"] if case.startswith("subclass-binds") else [])


@pytest.mark.parametrize("case", _PARTIAL_CASES, ids=_PARTIAL_CASES)
def test_factory_partial_call_paths(case: str) -> None:
    hooks: list[str] = []
    calls: list[str] = []

    def sync(*args: object) -> UUID:
        calls.append("factory")
        return _LEASE

    async def asynchronous(*args: object) -> UUID:
        calls.append("factory")
        return _LEASE

    def metadata(self: object, name: str) -> object:
        hooks.append("metadata")
        raise AssertionError("Partial metadata hook")

    def bind_sync(self: object, instance: object, owner: object) -> Callable[[], object]:
        hooks.append("binding")
        return sync

    def bind_async(self: object, instance: object, owner: object) -> Callable[[], object]:
        hooks.append("binding")
        return asynchronous

    inherited = type("InheritedPartial", (partial,), {})
    sync_override = type("SyncPartial", (partial,), {"__call__": sync})
    async_override = type("AsyncPartial", (partial,), {"__call__": asynchronous})
    metadata_override = type("MetadataPartial", (partial,), {"__getattribute__": metadata})
    sync_binding = type("SyncBindingPartial", (partial,), {"__get__": bind_sync})
    async_binding = type("AsyncBindingPartial", (partial,), {"__get__": bind_async})
    nested = partial(sync)
    # Native partial construction may flatten chains; explicit native state retains nesting.
    partial.__dict__["__setstate__"](nested, (partial(asynchronous), (), {}, {}))
    opaque = inherited(asynchronous)
    outer = partial(sync)
    partial.__dict__["__setstate__"](outer, (opaque, (), {}, {}))
    assert partial.__dict__["func"].__get__(outer) is opaque
    factories: dict[str, object] = {
        "exact-sync": partial(sync),
        "exact-async": partial(asynchronous),
        "nested-exact-async": nested,
        "bound-method-async": MethodType(asynchronous, object()),
        "inherited-partial-sync": inherited(sync),
        "inherited-partial-async": inherited(asynchronous),
        "partial-override-sync": sync_override(asynchronous),
        "partial-override-async": async_override(sync),
        "partial-metadata-sync": metadata_override(sync),
        "partial-metadata-async": metadata_override(asynchronous),
        "partial-binds-sync": type("Binding", (), {"__call__": sync_binding(asynchronous)})(),
        "partial-binds-async": type("Binding", (), {"__call__": async_binding(sync)})(),
        "exact-opaque-partial": outer,
        "namespace-exact-partial": type("Call", (), {"__call__": partial(asynchronous)})(),
        "namespace-subclass-partial": type("Call", (), {"__call__": inherited(asynchronous)})(),
    }
    rejected = case in (
        "exact-async",
        "nested-exact-async",
        "bound-method-async",
        "partial-override-async",
        "namespace-exact-partial",
    )
    pool = _Pool([*_fresh(), *_fresh_writes()])
    if rejected:
        with pytest.raises(TypeError) as caught:
            _repository(pool, factories[case])
        assert caught.value.args == ("Lease UUID factory must be synchronous.",)
    else:
        repository = _repository(pool, factories[case])
    assert hooks == [] and calls == [] and pool.events == [] and pool.calls == []
    if not rejected:
        if case in (
            "exact-sync",
            "inherited-partial-sync",
            "partial-override-sync",
            "partial-metadata-sync",
            "partial-binds-sync",
        ):
            result = _run(repository)
            assert type(result) is WorkerClaim and result.authority.lease_id.value is _LEASE
            assert calls == ["factory"]
            _done(pool)
        else:
            error = _capture(repository)
            assert type(error) is TypeError
            assert error.args == ("Lease UUID factory must return an exact UUID.",)
            assert calls == []
            _done(pool, rollback=True, remaining=True)
        assert hooks == (["binding"] if case.startswith("partial-binds") else [])


@pytest.mark.parametrize("cycle", ("self", "pair", "method", "acyclic-long"))
def test_factory_partial_graph_constructor_terminates_in_bounded_child(cycle: str) -> None:
    script = (
        _BOOTSTRAP_SCRIPT.split("case = sys.argv[1]", 1)[0]
        + r"""
from functools import partial
from types import MethodType
from uuid import UUID
case = sys.argv[1]
calls = []
def sync(*args):
    calls.append("factory")
    return UUID(int=1)
first = partial(sync)
if case == "self":
    first.__setstate__((first, (), {}, {}))
elif case == "pair":
    second = partial(sync)
    first.__setstate__((second, (), {}, {}))
    second.__setstate__((first, (), {}, {}))
elif case == "method":
    first.__setstate__((MethodType(first, object()), (), {}, {}))
else:
    for _ in range(1200):
        outer = partial(sync)
        outer.__setstate__((first, (), {}, {}))
        first = outer
class Pool:
    def __getattr__(self, name):
        raise AssertionError("pool probed")
module = importlib.import_module(module_name)
module.PsycopgAnalysisJobWorkerRepository(
    pool=Pool(), connection_acquire_timeout_seconds=1.25,
    operation_timeout_ms=2, lock_timeout_ms=1, lease_id_factory=first,
)
check(calls == [], "cyclic factory never invoked")
print("GRAPH_OK:" + case, flush=True)
"""
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, cycle, str(root / "src")],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert "PREFLIGHT_OK" in result.stdout, (result.stdout, result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"GRAPH_OK:{cycle}" in result.stdout


@pytest.mark.parametrize(
    "kind", ("direct-static", "namespace-static", "namespace-class", "method", "partial")
)
@pytest.mark.parametrize("async_form", ("generator", "marked"))
def test_factory_native_wrapper_async_evidence(kind: str, async_form: str) -> None:
    calls: list[str] = []

    def synchronous(*args: object) -> UUID:
        calls.append("factory")
        return _LEASE

    async def generator(*args: object) -> AsyncIterator[UUID]:
        calls.append("factory")
        yield _LEASE

    inspect.markcoroutinefunction(synchronous)
    function: Callable[..., object] = generator if async_form == "generator" else synchronous
    factory = {
        "direct-static": staticmethod(function),
        "namespace-static": type("Static", (), {"__call__": staticmethod(function)})(),
        "namespace-class": type("Class", (), {"__call__": classmethod(function)})(),
        "method": MethodType(function, object()),
        "partial": partial(function),
    }[kind]
    pool = _Pool([])
    with pytest.raises(TypeError) as caught:
        _repository(pool, factory)
    assert caught.value.args == ("Lease UUID factory must be synchronous.",)
    assert calls == [] and pool.events == [] and pool.calls == []


@pytest.mark.parametrize("form", ("staticmethod", "classmethod"))
@pytest.mark.parametrize("binding", ("sync", "async", "raising"))
def test_factory_subclass_binding_is_opaque_and_errors_keep_identity(
    form: str, binding: str
) -> None:
    hooks: list[str] = []
    calls: list[str] = []
    error = OperationalError("binding-marker")

    def sync() -> UUID:
        calls.append("factory")
        return _LEASE

    async def asynchronous() -> UUID:
        calls.append("factory")
        return _LEASE

    def bind(self: object, instance: object, owner: object) -> Callable[[], object]:
        hooks.append("binding")
        if binding == "raising":
            raise error
        return sync if binding == "sync" else asynchronous

    descriptor_type = type(
        "OpaqueBinding",
        (staticmethod if form == "staticmethod" else classmethod,),
        {"__get__": bind},
    )
    stored = asynchronous if binding == "sync" else sync
    factory = type("Factory", (), {"__call__": descriptor_type(stored)})()
    pool = _Pool([*_fresh(), *_fresh_writes()])
    repository = _repository(pool, factory)
    assert hooks == [] and calls == [] and pool.events == [] and pool.calls == []
    if binding == "sync":
        result = _run(repository)
        assert type(result) is WorkerClaim and result.authority.lease_id.value is _LEASE
        assert calls == ["factory"]
        _done(pool)
    else:
        result = _capture(repository)
        if binding == "raising":
            assert result is error
        else:
            assert type(result) is TypeError
            assert result.args == ("Lease UUID factory must return an exact UUID.",)
        assert calls == []
        _done(pool, rollback=True, remaining=True)
    assert hooks == ["binding"]
