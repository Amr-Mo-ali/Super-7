"""RED contract for inactive Slice 1 PostgreSQL adapters and migrations.

Concrete runtime and migration adapters belong under ``adapters``. The service
boundary retains lifecycle and readiness policy, and application composition
remains unwired until the separately reviewed activation slice.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import selectors
import subprocess
import tomllib
import warnings
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import fields
from importlib import import_module, resources
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import conftest as disposable_postgres
import pytest

from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
    PsycopgPoolFactory,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from services.postgres_foundation import (
    ConnectivityProbe,
    DatabaseConnectivityUnavailable,
    DatabasePool,
    DatabaseSchemaVersionUnavailable,
    PostgresFoundation,
    SchemaVersionProbe,
)
from services.process_contracts import ChildAnalysisRequest

_RUNTIME_ADAPTER_MODULE = "adapters.psycopg_database"
_MIGRATION_ADAPTER_MODULE = "adapters.yoyo_migration_backend"
_EXPECTED_FOUNDATION_REVISION = "sprint2_slice1_foundation"
_EXPECTED_ANALYSIS_JOB_REVISION = "sprint2_slice2_analysis_job"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_DIRECTORY = _REPOSITORY_ROOT / "src" / "adapters" / "migrations" / "postgres"
_STALE_MIGRATION = _REPOSITORY_ROOT / "migrations" / "postgres" / "sprint2_slice1_foundation.py"
_PASSWORD_MARKER = "adapter-password-secret-marker"
_HOST_MARKER = "adapter-host.invalid"
_DATABASE_MARKER = "adapter-database-secret-marker"
_USER_MARKER = "adapter-user-secret-marker"
_DRIVER_MARKER = "raw-driver-secret-marker"
_SQL_MARKER = "raw-sql-secret-marker"
_MIGRATION_PATH_MARKER = "migration-path-secret-marker"
_SECRET_MARKERS = (
    _PASSWORD_MARKER,
    _HOST_MARKER,
    _DATABASE_MARKER,
    _USER_MARKER,
    _DRIVER_MARKER,
    _SQL_MARKER,
    _MIGRATION_PATH_MARKER,
)
_DOMAIN_RELATION_MARKERS = (
    "analysis_job",
    "callback",
    "outbox",
    "lease",
    "idempotency",
    "worker",
    "scoring",
    "player",
    "video",
)

_RED_POOL = "Slice 1 RED: concrete Psycopg runtime adapter contract is not implemented yet"
_RED_CONNECTIVITY = "Slice 1 RED: concrete connectivity probe is not implemented yet"
_RED_SCHEMA = "Slice 1 RED: concrete schema-version probe is not implemented yet"
_RED_MIGRATION = "Slice 1 RED: concrete Yoyo migration backend is not implemented yet"
_RED_DISCOVERY = "Slice 1 RED: foundation-only migration catalog is not implemented yet"
_RED_DISPOSABLE = "Slice 1 RED: disposable PostgreSQL verification is not implemented yet"
_RED_PACKAGING = "Slice 1 RED: concrete adapter package discovery is not implemented yet"

_GREEN_C2_OPTION = "--slice1-disposable-postgres"

with warnings.catch_warnings():
    # RED-C cannot edit pytest configuration; the packaging contract below
    # requires registration before these future integration nodes turn GREEN.
    warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
    _INTEGRATION = pytest.mark.integration


def _run[T](operation: Coroutine[object, object, T]) -> T:
    return asyncio.run(operation)


def _run_real_database[T](operation: Coroutine[object, object, T]) -> T:
    return asyncio.run(
        operation,
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def _require_module(module_name: str, message: str) -> ModuleType:
    if find_spec(module_name) is None:
        pytest.fail(message, pytrace=False)
    return import_module(module_name)


def _require_symbol(module_name: str, name: str, message: str) -> object:
    module = _require_module(module_name, message)
    if not hasattr(module, name):
        pytest.fail(f"{message}: missing public symbol {name}", pytrace=False)
    return cast(object, getattr(module, name))


def _settings() -> DatabaseSettings:
    return DatabaseSettings.from_mapping(
        {
            "POSTGRES_HOST": _HOST_MARKER,
            "POSTGRES_PORT": "55432",
            "POSTGRES_DATABASE": _DATABASE_MARKER,
            "POSTGRES_USER": _USER_MARKER,
            "POSTGRES_PASSWORD": _PASSWORD_MARKER,
            "POSTGRES_POOL_MIN_SIZE": "0",
            "POSTGRES_POOL_MAX_SIZE": "3",
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": "4",
            "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "1.25",
            "POSTGRES_POOL_MAX_WAITERS": "6",
            "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "2.5",
            "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "3.5",
            "POSTGRES_REQUIRED_SCHEMA_VERSION": _EXPECTED_FOUNDATION_REVISION,
        }
    )


def _exception_graph(root: BaseException) -> tuple[BaseException, ...]:
    pending = [root]
    seen: set[int] = set()
    graph: list[BaseException] = []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        graph.append(error)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return tuple(graph)


def _assert_secret_safe(*values: object) -> None:
    rendered = " ".join(str(value) for value in values)
    assert not any(marker in rendered for marker in _SECRET_MARKERS)


def _assert_exception_secret_safe(error: BaseException) -> None:
    graph = _exception_graph(error)
    rendered = " ".join(part for node in graph for part in (str(node), repr(node), repr(node.args)))
    _assert_secret_safe(rendered)
    assert all(not isinstance(node, _DriverFailure) for node in graph)


def _optional_diagnostics(value: object) -> object:
    diagnostics = getattr(value, "safe_diagnostics", None)
    if not callable(diagnostics):
        return {}
    return cast(object, diagnostics())


def _capturing_logger() -> tuple[logging.Logger, StringIO]:
    stream = StringIO()
    logger = logging.Logger("slice1.postgres.adapters.contract")
    logger.addHandler(logging.StreamHandler(stream))
    logger.setLevel(logging.INFO)
    return logger, stream


class _LoggingFailure(RuntimeError):
    pass


class _ExplodingLogger:
    def info(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logging failed")

    def warning(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logging failed")

    def error(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logging failed")

    def exception(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logging failed")


class _FatalDriverFailure(BaseException):
    pass


class _DriverFailure(RuntimeError):
    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeResult:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row
        self.fetchone_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.close_calls = 0

    async def fetchone(self) -> tuple[object, ...] | None:
        if self.fetchone_error is not None:
            raise self.fetchone_error
        return self._row

    async def fetchall(self) -> list[tuple[object, ...]]:
        if self.fetchone_error is not None:
            raise self.fetchone_error
        return [] if self._row is None else [self._row]

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakeConnection:
    def __init__(self, *, version: str | None = _EXPECTED_FOUNDATION_REVISION) -> None:
        self.version = version
        self.execute_error: BaseException | None = None
        self.queries: list[str] = []
        self.results: list[_FakeResult] = []

    async def execute(self, query: str) -> _FakeResult:
        self.queries.append(query)
        if self.execute_error is not None:
            raise self.execute_error
        result = _FakeResult(None if self.version is None else (self.version,))
        self.results.append(result)
        return result


class _FakeConnectionContext:
    def __init__(self, pool: _FakeDriverPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        if self._pool.acquire_error is not None:
            raise self._pool.acquire_error
        return self._pool.connection_value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._pool.connection_exit_calls += 1


class _FakeDriverPool:
    def __init__(self) -> None:
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls: list[float] = []
        self.connection_timeouts: list[float | None] = []
        self.connection_exit_calls = 0
        self.open_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.acquire_error: BaseException | None = None
        self.connection_value = _FakeConnection()

    async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        self.open_calls.append((wait, timeout))
        if self.open_error is not None:
            raise self.open_error

    async def close(self, timeout: float = 5.0) -> None:
        self.close_calls.append(timeout)
        if self.close_error is not None:
            raise self.close_error

    def connection(self, timeout: float | None = None) -> _FakeConnectionContext:
        self.connection_timeouts.append(timeout)
        return _FakeConnectionContext(self)


class _FakePoolBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.pools: list[_FakeDriverPool] = []

    def __call__(self, conninfo: str = "", **kwargs: object) -> _FakeDriverPool:
        self.calls.append((conninfo, dict(kwargs)))
        pool = _FakeDriverPool()
        self.pools.append(pool)
        return pool


class _PoolBoundary(Protocol):
    async def open(self, *, timeout_seconds: float) -> None: ...

    async def close(self, *, timeout_seconds: float) -> None: ...

    def connection(self, *, timeout_seconds: float) -> AbstractAsyncContextManager[object]: ...


class _PoolConstructor(Protocol):
    def __call__(
        self,
        settings: DatabaseSettings,
        *,
        pool_builder: object,
        logger: logging.Logger | None = None,
    ) -> _PoolBoundary: ...


class _PoolFactoryConstructor(Protocol):
    def __call__(
        self,
        *,
        pool_builder: object,
        logger: logging.Logger | None = None,
    ) -> Callable[[DatabaseSettings], DatabasePool]: ...


class _ProbeBoundary(Protocol):
    async def __call__(self, pool: DatabasePool, *, timeout_seconds: float) -> object: ...


class _ProbeConstructor(Protocol):
    def __call__(self, *, logger: logging.Logger | None = None) -> _ProbeBoundary: ...


def _pool(
    builder: _FakePoolBuilder,
    *,
    logger: logging.Logger | None = None,
) -> tuple[_PoolBoundary, _FakeDriverPool]:
    constructor = cast(
        _PoolConstructor,
        _require_symbol(_RUNTIME_ADAPTER_MODULE, "PsycopgDatabasePool", _RED_POOL),
    )
    adapter = constructor(_settings(), pool_builder=builder, logger=logger)
    assert builder.pools, "pool construction must create one lazy driver pool"
    return adapter, builder.pools[-1]


def test_pool_adapter_maps_validated_settings_lazily_and_secret_safely() -> None:
    builder = _FakePoolBuilder()
    adapter, driver_pool = _pool(builder)

    assert driver_pool.open_calls == []
    assert len(builder.calls) == 1
    conninfo, options = builder.calls[0]
    assert conninfo == ""
    assert options["open"] is False
    assert options["min_size"] == 0
    assert options["max_size"] == 3
    assert options["timeout"] == 1.25
    assert options["max_waiting"] == 6
    connection_options = cast(Mapping[str, object], options["kwargs"])
    assert connection_options == {
        "host": _HOST_MARKER,
        "port": 55432,
        "dbname": _DATABASE_MARKER,
        "user": _USER_MARKER,
        "password": _PASSWORD_MARKER,
        "connect_timeout": 4,
    }
    _assert_secret_safe(repr(adapter), str(adapter), _optional_diagnostics(adapter))


def test_pool_adapter_wraps_explicit_lifecycle_and_bounded_acquisition() -> None:
    adapter, driver_pool = _pool(_FakePoolBuilder())

    async def scenario() -> None:
        await adapter.open(timeout_seconds=2.5)
        context = cast(_FakeConnectionContext, adapter.connection(timeout_seconds=1.25))
        async with context as connection:
            assert connection is driver_pool.connection_value
        await adapter.close(timeout_seconds=3.5)

    _run(scenario())

    assert driver_pool.open_calls == [(True, 2.5)]
    assert driver_pool.connection_timeouts == [1.25]
    assert driver_pool.close_calls == [3.5]


def test_pool_adapter_sanitizes_driver_failure_and_logging_cannot_replace_it() -> None:
    logger, stream = _capturing_logger()
    adapter, driver_pool = _pool(_FakePoolBuilder(), logger=logger)
    driver_pool.open_error = _DriverFailure(f"{_DRIVER_MARKER} {_PASSWORD_MARKER} {_HOST_MARKER}")

    with pytest.raises(DatabaseConnectivityUnavailable) as captured:
        _run(adapter.open(timeout_seconds=2.5))

    _assert_exception_secret_safe(captured.value)
    _assert_secret_safe(stream.getvalue())

    adapter, driver_pool = _pool(
        _FakePoolBuilder(),
        logger=cast(logging.Logger, _ExplodingLogger()),
    )
    driver_pool.open_error = _DriverFailure(f"{_DRIVER_MARKER} {_PASSWORD_MARKER} {_HOST_MARKER}")

    with pytest.raises(DatabaseConnectivityUnavailable) as captured:
        _run(adapter.open(timeout_seconds=2.5))

    _assert_exception_secret_safe(captured.value)


@pytest.mark.parametrize(
    "failure",
    (asyncio.CancelledError("pool-cancelled"), _FatalDriverFailure("pool-fatal")),
    ids=("cancellation", "base-exception"),
)
def test_pool_adapter_preserves_cancellation_and_fatal_exceptions(failure: BaseException) -> None:
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.open_error = failure

    with pytest.raises(type(failure)) as captured:
        _run(adapter.open(timeout_seconds=2.5))

    assert captured.value is failure


def test_pool_factory_is_lazy_and_creates_distinct_protocol_compatible_pools() -> None:
    constructor = cast(
        _PoolFactoryConstructor,
        _require_symbol(_RUNTIME_ADAPTER_MODULE, "PsycopgPoolFactory", _RED_POOL),
    )
    builder = _FakePoolBuilder()
    factory = constructor(pool_builder=builder)
    assert builder.pools == []

    first = factory(_settings())
    second = factory(_settings())

    assert first is not second
    assert len(builder.pools) == 2
    assert all(pool.open_calls == [] for pool in builder.pools)
    assert callable(first.open)
    assert callable(first.close)
    _assert_secret_safe(repr(factory), str(factory), repr(first), repr(second))


def _probe(name: str, message: str, logger: logging.Logger | None = None) -> _ProbeBoundary:
    constructor = cast(
        _ProbeConstructor,
        _require_symbol(_RUNTIME_ADAPTER_MODULE, name, message),
    )
    return constructor(logger=logger)


def test_connectivity_probe_uses_bounded_acquisition_and_one_read_only_operation() -> None:
    probe = _probe("PostgresConnectivityProbe", _RED_CONNECTIVITY)
    adapter, driver_pool = _pool(_FakePoolBuilder())

    result = _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert result is None
    assert driver_pool.connection_timeouts == [1.25]
    assert len(driver_pool.connection_value.queries) == 1
    assert driver_pool.connection_value.results[0].close_calls == 1
    assert driver_pool.connection_exit_calls == 1
    query = driver_pool.connection_value.queries[0]
    normalized_query = " ".join(query.upper().split())
    assert normalized_query.startswith(("SELECT ", "SHOW "))
    assert not any(
        operation in normalized_query
        for operation in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
    )
    assert not any(marker in query.lower() for marker in _DOMAIN_RELATION_MARKERS)
    _assert_secret_safe(query)


def test_connectivity_probe_sanitizes_failure_without_logging_precedence() -> None:
    logger, stream = _capturing_logger()
    probe = _probe("PostgresConnectivityProbe", _RED_CONNECTIVITY, logger)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.acquire_error = _DriverFailure(
        f"{_DRIVER_MARKER} {_PASSWORD_MARKER} {_SQL_MARKER}", sqlstate="08006"
    )

    with pytest.raises(DatabaseConnectivityUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)
    _assert_secret_safe(stream.getvalue())

    probe = _probe(
        "PostgresConnectivityProbe",
        _RED_CONNECTIVITY,
        cast(logging.Logger, _ExplodingLogger()),
    )
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.acquire_error = _DriverFailure(
        f"{_DRIVER_MARKER} {_PASSWORD_MARKER} {_SQL_MARKER}", sqlstate="08006"
    )

    with pytest.raises(DatabaseConnectivityUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)


@pytest.mark.parametrize(
    "failure",
    (_DriverFailure("cursor-close-ordinary"), _FatalDriverFailure("cursor-close-fatal")),
    ids=("ordinary", "base-exception"),
)
def test_connectivity_probe_closes_cursor_and_releases_connection_when_close_fails(
    failure: BaseException,
) -> None:
    probe = _probe("PostgresConnectivityProbe", _RED_CONNECTIVITY)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    result = _FakeResult(None)
    result.close_error = failure

    async def execute(_: str) -> _FakeResult:
        driver_pool.connection_value.results.append(result)
        return result

    driver_pool.connection_value.execute = execute  # type: ignore[assignment]
    expected = DatabaseConnectivityUnavailable if isinstance(failure, Exception) else type(failure)

    with pytest.raises(expected):
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert result.close_calls == 1
    assert driver_pool.connection_exit_calls == 1


@pytest.mark.parametrize(
    "failure",
    (asyncio.CancelledError("probe-cancelled"), _FatalDriverFailure("probe-fatal")),
    ids=("cancellation", "base-exception"),
)
def test_connectivity_probe_preserves_cancellation_and_fatal_exceptions(
    failure: BaseException,
) -> None:
    probe = _probe("PostgresConnectivityProbe", _RED_CONNECTIVITY)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.acquire_error = failure

    with pytest.raises(type(failure)) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert captured.value is failure


def test_schema_probe_reads_only_stable_version_through_bounded_acquisition() -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())

    version = _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert version == _EXPECTED_FOUNDATION_REVISION
    assert driver_pool.connection_timeouts == [1.25]
    assert len(driver_pool.connection_value.queries) == 1
    assert driver_pool.connection_value.results[0].close_calls == 1
    assert driver_pool.connection_exit_calls == 1
    query = driver_pool.connection_value.queries[0]
    assert query.lstrip().upper().startswith("SELECT")
    assert not any(marker in query.lower() for marker in _DOMAIN_RELATION_MARKERS)
    _assert_secret_safe(query)


def test_schema_probe_maps_missing_metadata_to_schema_version_unavailable() -> None:
    logger, stream = _capturing_logger()
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.connection_value.execute_error = _DriverFailure(
        f"{_DRIVER_MARKER} {_SQL_MARKER}", sqlstate="42P01"
    )
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA, logger)

    with pytest.raises(DatabaseSchemaVersionUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)
    _assert_secret_safe(stream.getvalue())
    assert driver_pool.connection_value.results == []
    assert driver_pool.connection_exit_calls == 1


def test_schema_probe_distinguishes_missing_version() -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.connection_value.version = None

    with pytest.raises(DatabaseSchemaVersionUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)
    assert driver_pool.connection_value.results[0].close_calls == 1
    assert driver_pool.connection_exit_calls == 1


@pytest.mark.parametrize(
    "failure",
    (
        _DriverFailure("fetch-ordinary-secret-marker"),
        asyncio.CancelledError("fetch-cancelled"),
        _FatalDriverFailure("fetch-fatal"),
    ),
    ids=("ordinary", "cancellation", "base-exception"),
)
def test_schema_probe_closes_cursor_and_releases_connection_after_fetch_failure(
    failure: BaseException,
) -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    result = _FakeResult((_EXPECTED_FOUNDATION_REVISION,))
    result.fetchone_error = failure

    async def execute(_: str) -> _FakeResult:
        driver_pool.connection_value.results.append(result)
        return result

    driver_pool.connection_value.execute = execute  # type: ignore[assignment]

    expected = DatabaseSchemaVersionUnavailable if isinstance(failure, Exception) else type(failure)
    with pytest.raises(expected):
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert result.close_calls == 1
    assert driver_pool.connection_exit_calls == 1


def test_cursor_close_failure_cannot_replace_primary_fetch_failure() -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    result = _FakeResult((_EXPECTED_FOUNDATION_REVISION,))
    result.fetchone_error = _DriverFailure(f"fetch-primary {_DRIVER_MARKER}")
    result.close_error = _DriverFailure("cursor-close-secondary")

    async def execute(_: str) -> _FakeResult:
        driver_pool.connection_value.results.append(result)
        return result

    driver_pool.connection_value.execute = execute  # type: ignore[assignment]

    with pytest.raises(DatabaseSchemaVersionUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)
    assert result.close_calls == 1
    assert driver_pool.connection_exit_calls == 1


def test_cursor_baseexception_during_primary_fetch_failure_has_cleanup_precedence() -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    primary = _DriverFailure("fetch-primary")
    cleanup = _FatalDriverFailure("cursor-close-fatal")
    result = _FakeResult((_EXPECTED_FOUNDATION_REVISION,))
    result.fetchone_error = primary
    result.close_error = cleanup

    async def execute(_: str) -> _FakeResult:
        driver_pool.connection_value.results.append(result)
        return result

    driver_pool.connection_value.execute = execute  # type: ignore[assignment]

    with pytest.raises(_FatalDriverFailure) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert captured.value is cleanup
    assert primary in _exception_graph(captured.value)
    assert result.close_calls == 1
    assert driver_pool.connection_exit_calls == 1


@pytest.mark.parametrize(
    "row",
    (("revision", "extra"), (42,), ("",)),
    ids=("wrong-width", "non-string", "empty-string"),
)
def test_schema_probe_closes_cursor_before_safely_rejecting_malformed_revision(
    row: tuple[object, ...],
) -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    result = _FakeResult(row)

    async def execute(_: str) -> _FakeResult:
        driver_pool.connection_value.results.append(result)
        return result

    driver_pool.connection_value.execute = execute  # type: ignore[assignment]

    with pytest.raises(DatabaseSchemaVersionUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)
    assert result.close_calls == 1
    assert driver_pool.connection_exit_calls == 1


def test_schema_probe_distinguishes_connectivity_unavailable() -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.acquire_error = _DriverFailure(_DRIVER_MARKER, sqlstate="08006")

    with pytest.raises(DatabaseConnectivityUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    _assert_exception_secret_safe(captured.value)


def test_schema_probe_integrates_with_foundation_mismatch_classification() -> None:
    schema_probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    connectivity_probe = _probe("PostgresConnectivityProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    settings = _settings()
    foundation = PostgresFoundation(
        settings,
        pool_factory=lambda _: cast(DatabasePool, adapter),
        connectivity_probe=cast(ConnectivityProbe, connectivity_probe),
        schema_version_probe=cast(SchemaVersionProbe, schema_probe),
    )

    async def scenario() -> str:
        await foundation.start()
        driver_pool.connection_value.version = "different-foundation-version"
        readiness = await foundation.readiness()
        await foundation.close()
        return readiness.reason

    assert _run(scenario()) == "SCHEMA_VERSION_MISMATCH"


@pytest.mark.parametrize(
    "failure",
    (asyncio.CancelledError("schema-cancelled"), _FatalDriverFailure("schema-fatal")),
    ids=("cancellation", "base-exception"),
)
def test_schema_probe_preserves_cancellation_and_fatal_exceptions(failure: BaseException) -> None:
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    driver_pool.acquire_error = failure

    with pytest.raises(type(failure)) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    assert captured.value is failure


class _FakeMigration:
    def __init__(self, identifier: str) -> None:
        self.id = identifier


class _FakeYoyoBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.applied: set[str] = set()
        self.failure: BaseException | None = None

    def to_apply(self, migrations: list[_FakeMigration]) -> list[_FakeMigration]:
        self.events.append("to_apply")
        return [migration for migration in migrations if migration.id not in self.applied]

    def apply_migrations(self, migrations: list[_FakeMigration]) -> None:
        self.events.append("apply_migrations")
        if self.failure is not None:
            raise self.failure
        self.applied.update(migration.id for migration in migrations)

    def to_rollback(self, migrations: list[_FakeMigration]) -> list[_FakeMigration]:
        self.events.append("to_rollback")
        return [migration for migration in reversed(migrations) if migration.id in self.applied]

    def rollback_migrations(self, migrations: list[_FakeMigration]) -> None:
        self.events.append("rollback_migrations")
        if self.failure is not None:
            raise self.failure
        for migration in migrations:
            self.applied.discard(migration.id)

    def is_applied(self, migration: _FakeMigration) -> bool:
        self.events.append("is_applied")
        return migration.id in self.applied


class _MigrationBoundary(Protocol):
    def apply_pending(self) -> None: ...

    def rollback_last(self) -> None: ...

    def current_version(self) -> str | None: ...


class _MigrationConstructor(Protocol):
    def __call__(
        self,
        settings: DatabaseSettings,
        migration_sources: tuple[Path, ...],
        *,
        backend_factory: object | None = None,
        migration_reader: object | None = None,
        logger: logging.Logger | None = None,
    ) -> _MigrationBoundary: ...


def _migration_backend(
    fake: _FakeYoyoBackend,
    *,
    logger: logging.Logger | None = None,
) -> _MigrationBoundary:
    constructor = cast(
        _MigrationConstructor,
        _require_symbol(
            _MIGRATION_ADAPTER_MODULE,
            "YoyoMigrationBackend",
            _RED_MIGRATION,
        ),
    )

    def backend_factory(_: str) -> _FakeYoyoBackend:
        return fake

    def migration_reader(*_: Path) -> list[_FakeMigration]:
        return [_FakeMigration(_EXPECTED_FOUNDATION_REVISION)]

    return constructor(
        _settings(),
        (Path(_MIGRATION_PATH_MARKER),),
        backend_factory=backend_factory,
        migration_reader=migration_reader,
        logger=logger,
    )


def test_yoyo_backend_implements_existing_operations_without_composite_workflow() -> None:
    backend = _migration_backend(_FakeYoyoBackend())

    assert callable(backend.apply_pending)
    assert callable(backend.rollback_last)
    assert callable(backend.current_version)
    assert not hasattr(backend, "verify_apply_rollback_reapply")


def test_yoyo_backend_delegates_apply_inspect_rollback_inspect_reapply() -> None:
    fake = _FakeYoyoBackend()
    backend = _migration_backend(fake)

    assert backend.current_version() is None
    backend.apply_pending()
    first_version = backend.current_version()
    backend.rollback_last()
    rolled_back_version = backend.current_version()
    backend.apply_pending()
    reapplied_version = backend.current_version()

    assert first_version == _EXPECTED_FOUNDATION_REVISION
    assert rolled_back_version is None
    assert reapplied_version == first_version
    assert fake.events.count("apply_migrations") == 2
    assert fake.events.count("rollback_migrations") == 1
    assert fake.applied == {_EXPECTED_FOUNDATION_REVISION}


def test_yoyo_backend_sanitizes_failure_without_logging_precedence() -> None:
    migration_error = cast(
        type[Exception],
        _require_symbol(
            _MIGRATION_ADAPTER_MODULE,
            "DatabaseMigrationError",
            _RED_MIGRATION,
        ),
    )
    logger, stream = _capturing_logger()
    fake = _FakeYoyoBackend()
    fake.failure = _DriverFailure(f"{_DRIVER_MARKER} {_PASSWORD_MARKER} {_MIGRATION_PATH_MARKER}")
    backend = _migration_backend(fake, logger=logger)

    with pytest.raises(migration_error) as captured:
        backend.apply_pending()

    _assert_exception_secret_safe(captured.value)
    _assert_secret_safe(stream.getvalue())
    _assert_secret_safe(repr(backend), str(backend), _optional_diagnostics(backend))

    fake = _FakeYoyoBackend()
    fake.failure = _DriverFailure(f"{_DRIVER_MARKER} {_PASSWORD_MARKER}")
    backend = _migration_backend(fake, logger=cast(logging.Logger, _ExplodingLogger()))

    with pytest.raises(migration_error) as logging_captured:
        backend.apply_pending()

    _assert_exception_secret_safe(logging_captured.value)


@pytest.mark.parametrize(
    "failure",
    (asyncio.CancelledError("migration-cancelled"), _FatalDriverFailure("migration-fatal")),
    ids=("cancellation", "base-exception"),
)
def test_yoyo_backend_preserves_cancellation_and_fatal_exceptions(
    failure: BaseException,
) -> None:
    fake = _FakeYoyoBackend()
    fake.failure = failure
    backend = _migration_backend(fake)

    with pytest.raises(type(failure)) as captured:
        backend.apply_pending()

    assert captured.value is failure


def _migration_sources(message: str) -> tuple[Path, ...]:
    if not _MIGRATION_DIRECTORY.is_dir():
        pytest.fail(message, pytrace=False)
    sources = tuple(sorted(_MIGRATION_DIRECTORY.glob("*.py")))
    if not sources:
        pytest.fail(message, pytrace=False)
    return sources


def _exact_foundation_migrations(migrations: list[object]) -> list[object]:
    matches = [
        migration
        for migration in migrations
        if str(getattr(migration, "id", "")) == _EXPECTED_FOUNDATION_REVISION
    ]
    if len(matches) != 1:
        raise AssertionError("foundation migration catalog must contain exactly one revision")
    return [matches[0]]


def test_foundation_migration_is_stable_and_domain_free() -> None:
    sources = _migration_sources(_RED_DISCOVERY)
    foundation_source = _MIGRATION_DIRECTORY / f"{_EXPECTED_FOUNDATION_REVISION}.py"

    assert sources
    assert len(sources) == len(set(sources))
    assert all(path.is_file() for path in sources)
    assert foundation_source in sources
    source = foundation_source.read_text(encoding="utf-8").lower()
    created_relations = re.findall(
        r"\bcreate\s+table(?:\s+if\s+not\s+exists)?\s+([^\s(]+)",
        source,
    )
    assert not any(
        marker in relation for relation in created_relations for marker in _DOMAIN_RELATION_MARKERS
    )
    foundation = _FakeMigration(_EXPECTED_FOUNDATION_REVISION)
    assert _exact_foundation_migrations([foundation]) == [foundation]
    with pytest.raises(
        AssertionError,
        match=r"^foundation migration catalog must contain exactly one revision$",
    ):
        _exact_foundation_migrations([])
    with pytest.raises(
        AssertionError,
        match=r"^foundation migration catalog must contain exactly one revision$",
    ):
        _exact_foundation_migrations(
            [
                _FakeMigration(_EXPECTED_FOUNDATION_REVISION),
                _FakeMigration(_EXPECTED_FOUNDATION_REVISION),
            ]
        )


def test_canonical_migration_resource_is_source_discoverable_and_unique() -> None:
    yoyo = _require_module("yoyo", _RED_DISCOVERY)
    migrations = yoyo.read_migrations("package:adapters:migrations/postgres")
    migration_ids = [str(migration.id) for migration in migrations]
    resource = resources.files("adapters").joinpath(
        "migrations/postgres/sprint2_slice1_foundation.py"
    )

    assert migration_ids == [
        _EXPECTED_FOUNDATION_REVISION,
        _EXPECTED_ANALYSIS_JOB_REVISION,
    ]
    assert resource.is_file()
    assert not _STALE_MIGRATION.exists()
    assert (
        hashlib.sha256(resource.read_bytes()).hexdigest()
        == hashlib.sha256(_migration_sources(_RED_DISCOVERY)[0].read_bytes()).hexdigest()
    )


def test_yoyo_private_metadata_compatibility_is_exactly_pinned_and_deterministic() -> None:
    module = _require_module(_MIGRATION_ADAPTER_MODULE, _RED_SCHEMA)
    configuration = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependency = next(
        item
        for item in configuration["project"]["dependencies"]
        if item.startswith("yoyo-migrations")
    )
    compatibility_factory = getattr(module, "_yoyo_metadata_compatibility", None)

    assert dependency == "yoyo-migrations==9.0.0"
    assert module.yoyo_version == "9.0.0"
    assert module.default_migration_table == "_yoyo_migration"
    assert callable(compatibility_factory)
    compatibility = compatibility_factory(
        migration_reader=lambda *_: [
            _FakeMigration("revision-001"),
            _FakeMigration("revision-002"),
        ]
    )
    assert "ORDER BY" not in compatibility.select_applied_revisions_query.upper()
    assert '"migration_id"' in compatibility.select_applied_revisions_query
    assert '"_yoyo_migration"' in compatibility.select_applied_revisions_query
    assert compatibility.current_version([("revision-002",), ("revision-001",)]) == "revision-002"
    assert compatibility.current_version([("revision-001",), ("revision-002",)]) == "revision-002"
    psycopg_source = inspect.getsource(_require_module(_RUNTIME_ADAPTER_MODULE, _RED_SCHEMA))
    assert "_yoyo_migration" not in psycopg_source
    assert "applied_at_utc" not in psycopg_source


def test_yoyo_private_metadata_identifiers_do_not_escape_schema_failure() -> None:
    logger, stream = _capturing_logger()
    probe = _probe("PostgresSchemaVersionProbe", _RED_SCHEMA, logger)
    adapter, driver_pool = _pool(_FakePoolBuilder())
    private_markers = ("_yoyo_migration", "migration_id", "applied_at_utc")
    driver_pool.connection_value.execute_error = _DriverFailure(" ".join(private_markers))

    with pytest.raises(DatabaseSchemaVersionUnavailable) as captured:
        _run(probe(cast(DatabasePool, adapter), timeout_seconds=1.25))

    rendered = " ".join((str(captured.value), repr(captured.value), stream.getvalue()))
    assert all(marker not in rendered for marker in private_markers)


def test_concrete_adapters_are_packaged_and_integration_marker_is_registered() -> None:
    _require_module(_RUNTIME_ADAPTER_MODULE, _RED_PACKAGING)
    _require_module(_MIGRATION_ADAPTER_MODULE, _RED_PACKAGING)
    configuration = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    wheel_configuration = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_configuration["only-include"] == ["src"]
    assert wheel_configuration["sources"] == ["src"]
    assert "packages" not in wheel_configuration
    registered_markers = configuration["tool"]["pytest"]["ini_options"].get("markers", ())
    assert any(
        str(marker).partition(":")[0].strip() == "integration" for marker in registered_markers
    )


class _DisposableSession(Protocol):
    migration_settings: DatabaseSettings
    runtime_settings: DatabaseSettings

    def application_relations(self) -> frozenset[str]: ...


class _DisposableFactory(Protocol):
    active_count: int

    def __call__(self) -> AbstractContextManager[_DisposableSession]: ...


_RECOVERY_CONTAINER_ID = "a" * 64
_RECOVERY_OTHER_CONTAINER_ID = "b" * 64
_RECOVERY_TASK_ID = "c" * 24
_RECOVERY_CONTAINER_NAME = f"super7-slice1-green-c2-{_RECOVERY_TASK_ID}"


class _RecoveryDocker:
    def __init__(
        self,
        *,
        name_output: str = _RECOVERY_CONTAINER_ID,
        actual_id: str = _RECOVERY_CONTAINER_ID,
        actual_name: str = f"/{_RECOVERY_CONTAINER_NAME}",
        actual_label: str = _RECOVERY_TASK_ID,
        exists: bool | None = None,
        fail_run: bool = False,
    ) -> None:
        self.name_output = name_output
        self.actual_id = actual_id
        self.actual_name = actual_name
        self.actual_label = actual_label
        self.exists = bool(name_output) if exists is None else exists
        self.fail_run = fail_run
        self.calls: list[tuple[str, ...]] = []
        self.removed_ids: list[str] = []

    def __call__(
        self,
        *arguments: str,
        preserve_raw_output: bool = False,
    ) -> str:
        del preserve_raw_output
        self.calls.append(arguments)
        command = arguments[0]
        if command == "ps":
            filter_value = arguments[arguments.index("--filter") + 1]
            if filter_value.startswith("name="):
                return self.name_output if self.exists else ""
            if filter_value.startswith("id="):
                return self.actual_id if self.exists else ""
        elif command == "inspect":
            template = arguments[arguments.index("--format") + 1]
            if template == "{{.Id}}":
                return self.actual_id
            if template == "{{.Name}}":
                return self.actual_name
            if ".Config.Labels" in template:
                return self.actual_label
        elif command == "rm":
            container_id = arguments[-1]
            self.removed_ids.append(container_id)
            self.exists = False
            return container_id
        elif command == "run":
            self.exists = True
            if self.fail_run:
                raise disposable_postgres._DisposablePostgresError(
                    "Disposable PostgreSQL Docker operation failed."
                )
            return self.actual_id
        raise AssertionError("unexpected deterministic Docker operation")


class _RecoverySubprocess:
    def __init__(self, raw_name_output: str) -> None:
        self.raw_name_output = raw_name_output
        self.exists = True
        self.calls: list[tuple[str, ...]] = []
        self.removed_ids: list[str] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        command = arguments[1]
        output: str
        if command == "ps":
            filter_value = arguments[arguments.index("--filter") + 1]
            if filter_value.startswith("name="):
                output = self.raw_name_output if self.exists else ""
            elif filter_value.startswith("id="):
                output = f"{_RECOVERY_CONTAINER_ID}\n" if self.exists else ""
            else:
                raise AssertionError("unexpected deterministic Docker filter")
        elif command == "inspect":
            template = arguments[arguments.index("--format") + 1]
            if template == "{{.Id}}":
                output = f"{_RECOVERY_CONTAINER_ID}\n"
            elif template == "{{.Name}}":
                output = f"/{_RECOVERY_CONTAINER_NAME}\n"
            elif ".Config.Labels" in template:
                output = f"{_RECOVERY_TASK_ID}\n"
            else:
                raise AssertionError("unexpected deterministic Docker inspection")
        elif command == "rm":
            container_id = arguments[-1]
            self.removed_ids.append(container_id)
            self.exists = False
            output = f"{container_id}\n"
        else:
            raise AssertionError("unexpected deterministic Docker operation")
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")


def _recovery_resource(secret_file: Path) -> disposable_postgres._OwnedResource:
    secret_file.write_text("task-only-password-marker", encoding="utf-8")
    return disposable_postgres._OwnedResource(
        task_id=_RECOVERY_TASK_ID,
        name=_RECOVERY_CONTAINER_NAME,
        password_file=secret_file,
    )


def _patch_recovery_docker(
    monkeypatch: pytest.MonkeyPatch,
    docker: _RecoveryDocker,
) -> None:
    monkeypatch.setattr(disposable_postgres, "_docker_checked", docker)


def test_disposable_postgres_recovers_exact_owned_container_after_ambiguous_run_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = _RecoveryDocker(exists=False, fail_run=True)
    resource = _recovery_resource(tmp_path / "task.secret")
    factory = disposable_postgres._DisposablePostgresFactory("unused-test-image")
    _patch_recovery_docker(monkeypatch, docker)
    monkeypatch.setattr(disposable_postgres, "_new_owned_resource", lambda: resource)

    with pytest.raises(
        disposable_postgres._DisposablePostgresError,
        match=r"^Disposable PostgreSQL Docker operation failed\.$",
    ):
        with factory():
            pytest.fail("ambiguous startup must not enter the context")

    expected_name_filter = f"name=^/{_RECOVERY_CONTAINER_NAME}$"
    assert any(expected_name_filter in call for call in docker.calls)
    assert ("inspect", "--format", "{{.Id}}", _RECOVERY_CONTAINER_ID) in docker.calls
    assert docker.removed_ids == [_RECOVERY_CONTAINER_ID]
    assert resource.container_id == _RECOVERY_CONTAINER_ID
    assert factory.active_count == 0
    assert not resource.password_file.exists()


def test_disposable_postgres_zero_result_recovery_performs_no_inspection_or_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = _RecoveryDocker(name_output="")
    resource = _recovery_resource(tmp_path / "task.secret")
    _patch_recovery_docker(monkeypatch, docker)

    disposable_postgres._remove_exact_owned_container(resource)

    assert len(docker.calls) == 1
    assert docker.calls[0][0] == "ps"
    assert f"name=^/{_RECOVERY_CONTAINER_NAME}$" in docker.calls[0]
    assert docker.removed_ids == []
    assert resource.container_id is None


@pytest.mark.parametrize(
    "raw_output",
    (
        f" {_RECOVERY_CONTAINER_ID}\n",
        f"{_RECOVERY_CONTAINER_ID} \n",
        f"\n{_RECOVERY_CONTAINER_ID}\n",
        f"{_RECOVERY_CONTAINER_ID}\n\n",
        f"\t{_RECOVERY_CONTAINER_ID}\n",
        f"{_RECOVERY_CONTAINER_ID}\t\n",
    ),
    ids=(
        "leading-space",
        "trailing-space",
        "leading-empty-line",
        "additional-trailing-empty-line",
        "leading-tab",
        "trailing-tab",
    ),
)
def test_disposable_postgres_recovery_rejects_whitespace_or_empty_line_pollution_without_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw_output: str,
) -> None:
    docker = _RecoverySubprocess(raw_output)
    resource = _recovery_resource(tmp_path / "task.secret")
    factory = disposable_postgres._DisposablePostgresFactory("unused-test-image")
    factory._register(resource)
    monkeypatch.setattr(subprocess, "run", docker)

    with pytest.raises(
        disposable_postgres._DisposablePostgresError,
        match=r"^Disposable PostgreSQL container identity is ambiguous\.$",
    ) as captured:
        disposable_postgres._remove_exact_owned_container(resource)

    rendered = " ".join(
        part
        for error in _exception_graph(captured.value)
        for part in (str(error), repr(error), repr(error.args))
    )
    assert raw_output not in rendered
    assert raw_output not in caplog.text
    assert not any(call[1] == "inspect" for call in docker.calls)
    assert not any(call[1] == "rm" for call in docker.calls)
    assert docker.removed_ids == []
    assert resource.container_id is None
    assert factory.active_count == 1
    assert resource.password_file.exists()


@pytest.mark.parametrize(
    ("field", "actual_value"),
    (
        ("id", _RECOVERY_OTHER_CONTAINER_ID),
        ("name", f"/{_RECOVERY_CONTAINER_NAME}-unrelated"),
        ("label", "unrelated-owner-token"),
        ("label", ""),
    ),
    ids=("id-mismatch", "name-mismatch", "label-mismatch", "missing-label"),
)
def test_disposable_postgres_recovery_rejects_ownership_mismatch_without_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    actual_value: str,
) -> None:
    if field == "id":
        docker = _RecoveryDocker(actual_id=actual_value)
    elif field == "name":
        docker = _RecoveryDocker(actual_name=actual_value)
    else:
        docker = _RecoveryDocker(actual_label=actual_value)
    resource = _recovery_resource(tmp_path / "task.secret")
    _patch_recovery_docker(monkeypatch, docker)

    with pytest.raises(
        disposable_postgres._DisposablePostgresError,
        match=r"^Disposable PostgreSQL ownership verification failed\.$",
    ) as captured:
        disposable_postgres._remove_exact_owned_container(resource)

    rendered = " ".join(
        part
        for error in _exception_graph(captured.value)
        for part in (str(error), repr(error), repr(error.args))
    )
    if actual_value:
        assert actual_value not in rendered
    assert docker.removed_ids == []
    assert resource.container_id is None


@pytest.mark.parametrize(
    "name_output",
    (
        f"{_RECOVERY_CONTAINER_ID}\n{_RECOVERY_OTHER_CONTAINER_ID}",
        "malformed-container-identity-marker",
        f"{_RECOVERY_CONTAINER_ID}\nraw-docker-output-marker",
    ),
    ids=("multiple-results", "malformed-id", "mixed-malformed-output"),
)
def test_disposable_postgres_recovery_rejects_ambiguous_or_malformed_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name_output: str,
) -> None:
    docker = _RecoveryDocker(name_output=name_output)
    resource = _recovery_resource(tmp_path / "task.secret")
    _patch_recovery_docker(monkeypatch, docker)

    with pytest.raises(
        disposable_postgres._DisposablePostgresError,
        match=r"^Disposable PostgreSQL container identity is ambiguous\.$",
    ) as captured:
        disposable_postgres._remove_exact_owned_container(resource)

    rendered = " ".join(
        part
        for error in _exception_graph(captured.value)
        for part in (str(error), repr(error), repr(error.args))
    )
    assert "malformed-container-identity-marker" not in rendered
    assert "raw-docker-output-marker" not in rendered
    assert docker.removed_ids == []
    assert resource.container_id is None


def test_disposable_postgres_startup_failure_retains_precedence_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = _RecoveryDocker(actual_label="unrelated-owner-token")
    resource = _recovery_resource(tmp_path / "task.secret")
    factory = disposable_postgres._DisposablePostgresFactory("unused-test-image")
    _patch_recovery_docker(monkeypatch, docker)
    monkeypatch.setattr(disposable_postgres, "_new_owned_resource", lambda: resource)

    def failing_start(*_: object) -> None:
        raise _DriverFailure(f"{_DRIVER_MARKER} {_PASSWORD_MARKER}")

    monkeypatch.setattr(disposable_postgres, "_start_disposable_postgres", failing_start)

    with pytest.raises(
        disposable_postgres._DisposablePostgresError,
        match=r"^Disposable PostgreSQL setup failed\.$",
    ) as captured:
        with factory():
            pytest.fail("failed startup must not enter the context")

    _assert_exception_secret_safe(captured.value)
    assert docker.removed_ids == []
    assert factory.active_count == 1
    assert not resource.password_file.exists()


@pytest.mark.parametrize(
    "failure",
    (
        asyncio.CancelledError("startup-cancellation-marker"),
        _FatalDriverFailure("startup-fatal-marker"),
    ),
    ids=("cancellation", "base-exception"),
)
def test_disposable_postgres_primary_baseexception_survives_ordinary_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    docker = _RecoveryDocker(actual_label="unrelated-owner-token")
    resource = _recovery_resource(tmp_path / "task.secret")
    factory = disposable_postgres._DisposablePostgresFactory("unused-test-image")
    _patch_recovery_docker(monkeypatch, docker)
    monkeypatch.setattr(disposable_postgres, "_new_owned_resource", lambda: resource)

    def failing_start(*_: object) -> None:
        raise failure

    monkeypatch.setattr(disposable_postgres, "_start_disposable_postgres", failing_start)

    with pytest.raises(type(failure)) as captured:
        with factory():
            pytest.fail("failed startup must not enter the context")

    assert captured.value is failure
    assert docker.removed_ids == []
    assert factory.active_count == 1
    assert not resource.password_file.exists()


def test_disposable_postgres_cleanup_baseexception_retains_sanitized_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_failure = _FatalDriverFailure("cleanup-fatal-marker")
    docker = _RecoveryDocker()
    resource = _recovery_resource(tmp_path / "task.secret")
    factory = disposable_postgres._DisposablePostgresFactory("unused-test-image")
    _patch_recovery_docker(monkeypatch, docker)
    monkeypatch.setattr(disposable_postgres, "_new_owned_resource", lambda: resource)

    def failing_start(*_: object) -> None:
        raise disposable_postgres._DisposablePostgresError(
            "Disposable PostgreSQL Docker operation failed."
        )

    def fatal_inspection(*_: str, **__: object) -> str:
        raise cleanup_failure

    monkeypatch.setattr(disposable_postgres, "_start_disposable_postgres", failing_start)
    monkeypatch.setattr(disposable_postgres, "_docker_checked", fatal_inspection)

    with pytest.raises(_FatalDriverFailure) as captured:
        with factory():
            pytest.fail("failed startup must not enter the context")

    assert captured.value is cleanup_failure
    assert any(
        isinstance(error, disposable_postgres._DisposablePostgresError)
        and str(error) == "Disposable PostgreSQL Docker operation failed."
        for error in _exception_graph(captured.value)
    )
    assert factory.active_count == 1
    assert not resource.password_file.exists()


def _disposable_factory(request: pytest.FixtureRequest) -> _DisposableFactory:
    _require_module(_MIGRATION_ADAPTER_MODULE, _RED_DISPOSABLE)
    try:
        fixture = request.getfixturevalue("slice1_disposable_postgres_factory")
    except pytest.FixtureLookupError:
        if request.config.getoption(_GREEN_C2_OPTION, default=False):
            pytest.fail(
                "GREEN-C2 opt-in requires the task-owned disposable PostgreSQL fixture.",
                pytrace=False,
            )
        pytest.skip(
            "Slice 1 integration requires the explicitly authorized task-scoped "
            "slice1_disposable_postgres_factory fixture; no fallback database is permitted."
        )
    return cast(_DisposableFactory, fixture)


async def _assert_real_schema_unavailable(settings: DatabaseSettings) -> None:
    factory = PsycopgPoolFactory()
    pool = factory(settings)
    assert isinstance(pool, PsycopgDatabasePool)
    connectivity_probe = PostgresConnectivityProbe()
    schema_probe = PostgresSchemaVersionProbe()

    await pool.open(timeout_seconds=settings.pool.open_timeout_seconds)
    try:
        await connectivity_probe(
            pool,
            timeout_seconds=settings.pool.acquire_timeout_seconds,
        )
        with pytest.raises(DatabaseSchemaVersionUnavailable):
            await schema_probe(
                pool,
                timeout_seconds=settings.pool.acquire_timeout_seconds,
            )
    finally:
        await pool.close(timeout_seconds=settings.pool.close_timeout_seconds)


async def _assert_real_foundation_ready(settings: DatabaseSettings) -> None:
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
        assert readiness.affects_liveness is False
    finally:
        await foundation.close()


@_INTEGRATION
def test_disposable_postgres_apply_rollback_reapply_contract(
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    constructor = cast(
        _MigrationConstructor,
        _require_symbol(
            _MIGRATION_ADAPTER_MODULE,
            "YoyoMigrationBackend",
            _RED_DISPOSABLE,
        ),
    )
    foundation_source = _MIGRATION_DIRECTORY / f"{_EXPECTED_FOUNDATION_REVISION}.py"
    factory = _disposable_factory(request)

    def foundation_reader(*sources: str) -> object:
        yoyo = _require_module("yoyo", _RED_DISPOSABLE)
        migrations = yoyo.read_migrations(*sources)
        _exact_foundation_migrations(list(migrations))
        return migrations.filter(
            lambda migration: str(migration.id) == _EXPECTED_FOUNDATION_REVISION
        )

    with factory() as database:
        canonical_resource = resources.files("adapters").joinpath(
            "migrations/postgres/sprint2_slice1_foundation.py"
        )
        assert foundation_source.is_file()
        assert canonical_resource.is_file()
        assert (
            hashlib.sha256(foundation_source.read_bytes()).digest()
            == hashlib.sha256(canonical_resource.read_bytes()).digest()
        )
        backend = constructor(
            database.migration_settings,
            (_MIGRATION_DIRECTORY,),
            migration_reader=foundation_reader,
        )
        assert backend.current_version() is None
        assert database.application_relations() == frozenset()
        _run_real_database(_assert_real_schema_unavailable(database.runtime_settings))
        backend.apply_pending()
        first_version = backend.current_version()
        assert first_version == _EXPECTED_FOUNDATION_REVISION
        _run_real_database(_assert_real_foundation_ready(database.runtime_settings))
        assert database.application_relations() == frozenset()
        backend.rollback_last()
        assert backend.current_version() is None
        _run_real_database(_assert_real_schema_unavailable(database.runtime_settings))
        assert database.application_relations() == frozenset()
        backend.apply_pending()
        assert backend.current_version() == first_version
        _run_real_database(_assert_real_foundation_ready(database.runtime_settings))
        assert database.application_relations() == frozenset()
        _assert_secret_safe(caplog.text, repr(backend), str(backend))

    assert factory.active_count == 0


class _VerificationFailure(RuntimeError):
    pass


@_INTEGRATION
@pytest.mark.parametrize(
    "failure",
    (
        _VerificationFailure("intentional ordinary verification failure"),
        AssertionError("intentional assertion failure"),
        asyncio.CancelledError("intentional verification cancellation"),
    ),
    ids=("ordinary-failure", "assertion-failure", "cancellation"),
)
def test_disposable_postgres_teardown_runs_after_interruption(
    request: pytest.FixtureRequest,
    failure: BaseException,
) -> None:
    factory = _disposable_factory(request)

    with pytest.raises(type(failure)) as captured:
        with factory():
            raise failure

    assert captured.value is failure
    assert factory.active_count == 0


def test_guard_application_composition_has_no_postgres_activation() -> None:
    import main

    source = inspect.getsource(main)
    parameters = inspect.signature(main.create_app).parameters
    assert _RUNTIME_ADAPTER_MODULE not in source
    assert _MIGRATION_ADAPTER_MODULE not in source
    assert "postgres_foundation" not in parameters
    assert "database_settings" not in parameters
    assert not hasattr(main.app.state, "postgres_foundation")
    assert "/analyze" in main.app.openapi()["paths"]


def test_guard_public_health_has_no_database_readiness() -> None:
    from api import health

    source = inspect.getsource(health)
    parameters = tuple(inspect.signature(health.create_health_router).parameters)
    assert parameters == ("lifecycle", "path_resolver", "analysis_queue")
    assert "postgres" not in source.lower()
    assert "database_connectivity" not in source
    assert "database_schema_version" not in source


def test_guard_spawned_analysis_child_remains_database_free() -> None:
    from services.process_entrypoint import initialize_analysis_child

    prohibited = ("database", "postgres", "pool", "dsn", "credential", "password")
    request_fields = {field.name.lower() for field in fields(ChildAnalysisRequest)}
    child_settings = {field.name.lower() for field in fields(Settings)}
    assert not any(marker in name for marker in prohibited for name in request_fields)
    assert not any(marker in name for marker in prohibited for name in child_settings)
    assert tuple(inspect.signature(initialize_analysis_child).parameters) == ("settings",)


def test_guard_active_route_queue_callback_and_scoring_remain_adapter_free() -> None:
    module_names = (
        "api.routes",
        "services.analysis_queue",
        "services.callback_service",
        "services.player_rating.engine",
    )
    for module_name in module_names:
        source = inspect.getsource(import_module(module_name))
        assert _RUNTIME_ADAPTER_MODULE not in source
        assert _MIGRATION_ADAPTER_MODULE not in source
        assert "PsycopgDatabasePool" not in source
        assert "YoyoMigrationBackend" not in source
