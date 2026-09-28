"""Contract for the inactive Slice 1 PostgreSQL configuration and foundation.

The implementation remains dependency-injected and unwired: concrete database
adapters, real connectivity, and completion of Slice 1 are outside this module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Coroutine, Mapping
from dataclasses import FrozenInstanceError, asdict, dataclass, fields
from importlib import import_module
from importlib.util import find_spec
from io import StringIO
from types import ModuleType
from typing import Any, cast, get_args, get_origin, get_type_hints

import pytest

_CONFIG_MODULE = "core.database_config"
_FOUNDATION_MODULE = "services.postgres_foundation"
_RAW_PASSWORD = "slice1-contract-password-marker"
_REQUIRED_SCHEMA_VERSION = "slice1-foundation-head"
_DOMAIN_RELATION_MARKERS = (
    "analysis_job",
    "idempotency",
    "attempt",
    "result",
    "callback",
    "outbox",
    "cancellation",
    "artifact",
)
_READINESS_MATRIX_CASES = (
    ("NOT_STARTED", False, False, False),
    ("CLEANUP_PENDING", False, False, False),
    ("CLOSED", False, False, False),
    ("CONNECTIVITY_UNAVAILABLE", False, False, False),
    ("SCHEMA_VERSION_UNAVAILABLE", False, True, False),
    ("SCHEMA_VERSION_MISMATCH", False, True, False),
    ("READY", True, True, True),
)


def _require_module(module_name: str, purpose: str) -> ModuleType:
    spec = find_spec(module_name)
    assert spec is not None, f"Slice 1 is missing {module_name}, required for {purpose}."
    return import_module(module_name)


def _require_symbol(module_name: str, symbol: str, purpose: str) -> Any:
    module = _require_module(module_name, purpose)
    assert hasattr(module, symbol), (
        f"Slice 1 is missing {module_name}.{symbol}, required for {purpose}."
    )
    return getattr(module, symbol)


def _local_environment(**overrides: str) -> dict[str, str]:
    values = {
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DATABASE": "super7_slice1_contract",
        "POSTGRES_USER": "super7_slice1_contract",
        "POSTGRES_PASSWORD": _RAW_PASSWORD,
        "POSTGRES_POOL_MIN_SIZE": "0",
        "POSTGRES_POOL_MAX_SIZE": "3",
        "POSTGRES_CONNECT_TIMEOUT_SECONDS": "5",
        "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "2.5",
        "POSTGRES_POOL_MAX_WAITERS": "7",
        "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "4",
        "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "6",
        "POSTGRES_REQUIRED_SCHEMA_VERSION": _REQUIRED_SCHEMA_VERSION,
    }
    values.update(overrides)
    return values


def _load_settings(values: Mapping[str, str], purpose: str) -> Any:
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    return settings_type.from_mapping(values)


def _assert_redacted(*values: object) -> None:
    rendered = " ".join(str(value) for value in values)
    if _RAW_PASSWORD in rendered:
        raise AssertionError("A raw database credential escaped the protected boundary.")


def _walk_exception_graph(root: BaseException) -> tuple[BaseException, ...]:
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


def _render_exception_graph(root: BaseException) -> str:
    return " ".join(
        rendered
        for error in _walk_exception_graph(root)
        for rendered in (str(error), repr(error), repr(error.args))
    )


class _FakePool:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.open_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.connectivity_error: BaseException | None = None
        self.schema_error: BaseException | None = None
        self.schema_version = _REQUIRED_SCHEMA_VERSION
        self.open_timeouts: list[float] = []
        self.close_timeouts: list[float] = []
        self.acquisition_timeouts: list[float] = []

    async def open(self, *, timeout_seconds: float) -> None:
        self.events.append("database.open")
        self.open_timeouts.append(timeout_seconds)
        if self.open_error is not None:
            raise self.open_error

    async def close(self, *, timeout_seconds: float) -> None:
        self.events.append("database.close")
        self.close_timeouts.append(timeout_seconds)
        if self.close_error is not None:
            raise self.close_error


class _PoolFactory:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool
        self.calls = 0
        self.settings: Any = None

    def __call__(self, settings: object) -> _FakePool:
        self.calls += 1
        self.settings = settings
        return self.pool


async def _connectivity_probe(pool: _FakePool, *, timeout_seconds: float) -> None:
    pool.acquisition_timeouts.append(timeout_seconds)
    if pool.connectivity_error is not None:
        raise pool.connectivity_error


async def _schema_version_probe(pool: _FakePool, *, timeout_seconds: float) -> str:
    pool.acquisition_timeouts.append(timeout_seconds)
    if pool.schema_error is not None:
        raise pool.schema_error
    return pool.schema_version


def _capturing_logger() -> tuple[logging.Logger, StringIO]:
    stream = StringIO()
    logger = logging.Logger("slice1.postgres.contract")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, stream


class _LoggingFailure(RuntimeError):
    pass


class _ExplodingLogger:
    def debug(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logger failed")

    def info(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logger failed")

    def warning(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logger failed")

    def error(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logger failed")

    def exception(self, *_: object, **__: object) -> None:
        raise _LoggingFailure("diagnostic logger failed")


class _PrimaryFailure(RuntimeError):
    pass


class _CleanupFailure(RuntimeError):
    pass


class _FatalProbeFailure(BaseException):
    pass


class _FatalLoggingFailure(BaseException):
    pass


class _FatalEventLogger:
    def __init__(self, target_event: str, failure: BaseException | None = None) -> None:
        self.target_event = target_event
        self.failure = failure or _FatalLoggingFailure(f"fatal logger failure for {target_event}")
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.raised = False

    def _record(self, method: str, args: tuple[object, ...]) -> None:
        self.calls.append((method, args))
        if not self.raised and self.target_event in args:
            self.raised = True
            raise self.failure

    def info(self, *args: object, **__: object) -> None:
        self._record("info", args)

    def error(self, *args: object, **__: object) -> None:
        self._record("error", args)


def _foundation(
    *,
    pool: _FakePool | None = None,
    logger: object | None = None,
    purpose: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[Any, _FakePool, _PoolFactory]:
    settings = _load_settings(environment or _local_environment(), purpose)
    foundation_type = _require_symbol(_FOUNDATION_MODULE, "PostgresFoundation", purpose)
    resolved_pool = pool or _FakePool([])
    factory = _PoolFactory(resolved_pool)
    resolved_logger = logger or _capturing_logger()[0]
    foundation = foundation_type(
        settings,
        pool_factory=factory,
        connectivity_probe=_connectivity_probe,
        schema_version_probe=_schema_version_probe,
        logger=resolved_logger,
    )
    return foundation, resolved_pool, factory


def _run(operation: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(operation)


def test_complete_local_database_configuration_is_typed() -> None:
    purpose = "typed loading of complete local PostgreSQL configuration"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    settings = settings_type.from_mapping(_local_environment())

    assert isinstance(settings, settings_type)
    assert isinstance(settings.pool.min_size, int)
    assert isinstance(settings.pool.max_size, int)
    assert isinstance(settings.connect_timeout_seconds, int)
    assert settings.connect_timeout_seconds == 5
    assert settings.required_schema_version == _REQUIRED_SCHEMA_VERSION


def test_missing_required_database_configuration_fails_safely() -> None:
    purpose = "safe rejection of missing required PostgreSQL configuration"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)
    values = _local_environment()
    del values["POSTGRES_PASSWORD"]

    try:
        settings_type.from_mapping(values)
    except error_type as error:
        _assert_redacted(str(error), repr(error))
    except Exception as error:
        _assert_redacted(str(error), repr(error))
        raise AssertionError("Missing configuration raised the wrong safe error type.") from None
    else:
        raise AssertionError("Missing required PostgreSQL configuration was accepted.")


@pytest.mark.parametrize(
    ("field", "marker"),
    (
        ("POSTGRES_PORT", "malformed-integer-secret-marker-7e91"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "malformed-timeout-secret-marker-4c28"),
    ),
    ids=("malformed-integer", "malformed-connect-timeout"),
)
def test_malformed_numeric_configuration_has_no_secret_bearing_exception_context(
    field: str,
    marker: str,
) -> None:
    purpose = f"secret-safe rejection of malformed numeric configuration in {field}"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)

    with pytest.raises(error_type) as captured:
        settings_type.from_mapping(_local_environment(**{field: marker}))

    error = captured.value
    assert marker not in str(error)
    assert marker not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    graph = _walk_exception_graph(error)
    assert all(marker not in " ".join((str(node), repr(node), repr(node.args))) for node in graph)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    (("-1", "1"), ("1", "0"), ("4", "3")),
    ids=("negative-minimum", "non-positive-maximum", "minimum-exceeds-maximum"),
)
def test_invalid_pool_bounds_are_rejected(minimum: str, maximum: str) -> None:
    purpose = f"rejection of invalid pool bounds ({minimum}, {maximum})"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)

    with pytest.raises(error_type):
        settings_type.from_mapping(
            _local_environment(
                POSTGRES_POOL_MIN_SIZE=minimum,
                POSTGRES_POOL_MAX_SIZE=maximum,
            )
        )


def test_zero_pool_minimum_with_positive_maximum_is_valid() -> None:
    purpose = "a zero-minimum pool with a finite positive maximum"
    settings = _load_settings(
        _local_environment(POSTGRES_POOL_MIN_SIZE="0", POSTGRES_POOL_MAX_SIZE="2"), purpose
    )

    assert settings.pool.min_size == 0
    assert settings.pool.max_size == 2


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "0"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "1"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "-1"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "0.25"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "1.5"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "2.75"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "nan"),
        ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "inf"),
        ("POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS", "0"),
        ("POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS", "-1"),
        ("POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS", "nan"),
        ("POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS", "inf"),
        ("POSTGRES_POOL_MAX_WAITERS", "0"),
        ("POSTGRES_POOL_MAX_WAITERS", "-1"),
        ("POSTGRES_POOL_OPEN_TIMEOUT_SECONDS", "0"),
        ("POSTGRES_POOL_OPEN_TIMEOUT_SECONDS", "-1"),
        ("POSTGRES_POOL_OPEN_TIMEOUT_SECONDS", "nan"),
        ("POSTGRES_POOL_OPEN_TIMEOUT_SECONDS", "inf"),
        ("POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS", "0"),
        ("POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS", "-1"),
        ("POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS", "nan"),
        ("POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS", "inf"),
    ),
    ids=(
        "zero-connect-timeout",
        "below-driver-minimum-connect-timeout",
        "negative-connect-timeout",
        "quarter-second-connect-timeout",
        "fractional-one-and-half-connect-timeout",
        "fractional-two-and-three-quarters-connect-timeout",
        "nan-connect-timeout",
        "positive-infinity-connect-timeout",
        "zero-acquisition-timeout",
        "negative-acquisition-timeout",
        "nan-acquisition-timeout",
        "positive-infinity-acquisition-timeout",
        "zero-max-waiters",
        "negative-max-waiters",
        "zero-open-timeout",
        "negative-open-timeout",
        "nan-open-timeout",
        "positive-infinity-open-timeout",
        "zero-close-timeout",
        "negative-close-timeout",
        "nan-close-timeout",
        "positive-infinity-close-timeout",
    ),
)
def test_invalid_bounded_wait_and_lifecycle_controls_are_rejected(
    field: str,
    value: str,
) -> None:
    purpose = f"rejection of invalid bounded pool control {field}={value}"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)

    with pytest.raises(error_type):
        settings_type.from_mapping(_local_environment(**{field: value}))


@pytest.mark.parametrize("value", (2, 3), ids=("two-seconds", "three-seconds"))
def test_connect_timeout_accepts_exact_driver_integer_seconds(value: int) -> None:
    purpose = f"exact integer driver connect timeout of {value} seconds"
    settings = _load_settings(
        _local_environment(POSTGRES_CONNECT_TIMEOUT_SECONDS=str(value)),
        purpose,
    )

    assert settings.connect_timeout_seconds == value
    assert type(settings.connect_timeout_seconds) is int


@pytest.mark.parametrize("value", (False, True), ids=("false", "true"))
def test_connect_timeout_rejects_boolean_objects_safely(value: bool) -> None:
    purpose = "safe rejection of a Boolean driver connect timeout"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)
    values: dict[str, object] = dict(_local_environment())
    values["POSTGRES_CONNECT_TIMEOUT_SECONDS"] = value

    with pytest.raises(error_type) as captured:
        settings_type.from_mapping(cast(Mapping[str, str], values))

    assert str(value) not in _render_exception_graph(captured.value)


def test_pool_controls_are_typed_configurable_and_bounded() -> None:
    purpose = "typed configurable connection, waiter, and lifecycle bounds"
    environment = _local_environment(
        POSTGRES_POOL_MIN_SIZE="0",
        POSTGRES_POOL_MAX_SIZE="2",
        POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS="1.25",
        POSTGRES_POOL_MAX_WAITERS="5",
        POSTGRES_POOL_OPEN_TIMEOUT_SECONDS="3.5",
        POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS="4.5",
    )
    foundation, _, factory = _foundation(purpose=purpose, environment=environment)

    _run(foundation.start())
    _run(foundation.close())

    assert factory.calls == 1
    assert factory.settings.pool.min_size == 0
    assert factory.settings.pool.max_size == 2
    assert factory.settings.pool.acquire_timeout_seconds == 1.25
    assert factory.settings.pool.max_waiters == 5
    assert factory.settings.pool.open_timeout_seconds == 3.5
    assert factory.settings.pool.close_timeout_seconds == 4.5


def test_credentials_are_redacted_from_repr_logs_diagnostics_and_readiness() -> None:
    purpose = "credential-safe representations and readiness diagnostics"
    settings = _load_settings(_local_environment(), purpose)
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseConnectivityUnavailable",
        purpose,
    )
    logger, stream = _capturing_logger()
    foundation, pool, _ = _foundation(logger=logger, purpose=purpose)

    _run(foundation.start())
    pool.connectivity_error = unavailable_type(_RAW_PASSWORD)
    readiness = _run(foundation.readiness())
    _run(foundation.close())

    _assert_redacted(
        repr(settings),
        settings.safe_diagnostics(),
        repr(readiness),
        json.dumps(asdict(readiness.public), sort_keys=True),
        stream.getvalue(),
    )


def test_owned_startup_opens_one_pool_exactly_once() -> None:
    purpose = "single parent-owned pool startup"
    events: list[str] = []
    pool = _FakePool(events)
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)

    _run(foundation.start())
    _run(foundation.close())

    assert factory.calls == 1
    assert events.count("database.open") == 1
    assert pool.open_timeouts == [factory.settings.pool.open_timeout_seconds]


def test_owned_shutdown_closes_once_after_dependents_stop() -> None:
    purpose = "shutdown ordering that closes PostgreSQL after dependents"
    events: list[str] = []
    pool = _FakePool(events)
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)

    async def scenario() -> None:
        await foundation.start()
        events.append("dependent.start")
        events.append("dependent.stop")
        await foundation.close()

    _run(scenario())

    assert events == [
        "database.open",
        "dependent.start",
        "dependent.stop",
        "database.close",
    ]
    assert pool.close_timeouts == [factory.settings.pool.close_timeout_seconds]


def test_readiness_probes_use_the_bounded_acquisition_timeout() -> None:
    purpose = "bounded acquisition waits for connectivity and schema probes"
    pool = _FakePool([])
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)

    _run(foundation.start())
    _run(foundation.readiness())
    _run(foundation.close())

    expected = factory.settings.pool.acquire_timeout_seconds
    assert pool.acquisition_timeouts
    assert all(timeout == expected for timeout in pool.acquisition_timeouts)


def test_partial_startup_failure_closes_an_open_pool() -> None:
    purpose = "cleanup after a partial PostgreSQL startup failure"
    events: list[str] = []
    pool = _FakePool(events)
    pool.schema_version = "unexpected-version"
    foundation, _, _ = _foundation(pool=pool, purpose=purpose)
    startup_error = _require_symbol(_FOUNDATION_MODULE, "DatabaseStartupError", purpose)

    with pytest.raises(startup_error):
        _run(foundation.start())

    assert events == ["database.open", "database.close"]


def test_connectivity_failure_has_a_safe_distinct_classification() -> None:
    purpose = "safe connectivity-failure classification"
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseConnectivityUnavailable",
        purpose,
    )
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    pool.connectivity_error = unavailable_type(_RAW_PASSWORD)

    readiness = _run(foundation.readiness())
    _run(foundation.close())

    assert readiness.ready is False
    assert readiness.reason == "CONNECTIVITY_UNAVAILABLE"
    _assert_redacted(repr(readiness), readiness.public)


def test_schema_version_mismatch_is_distinct_from_connectivity_failure() -> None:
    purpose = "a schema-version-mismatch readiness classification"
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    pool.schema_version = "unexpected-version"

    readiness = _run(foundation.readiness())
    _run(foundation.close())

    assert readiness.ready is False
    assert readiness.reason == "SCHEMA_VERSION_MISMATCH"
    assert readiness.public.database_connectivity is True
    assert readiness.public.database_schema_version is False


def test_required_database_unavailability_makes_readiness_false() -> None:
    purpose = "database unavailability making application readiness false"
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseConnectivityUnavailable",
        purpose,
    )
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    pool.connectivity_error = unavailable_type("disposable database unavailable")

    readiness = _run(foundation.readiness())
    _run(foundation.close())

    assert readiness.ready is False
    assert isinstance(readiness.public.database_connectivity, bool)
    assert isinstance(readiness.public.database_schema_version, bool)


def test_database_readiness_does_not_redefine_liveness() -> None:
    purpose = "separation of database readiness from process liveness"
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseConnectivityUnavailable",
        purpose,
    )
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    pool.connectivity_error = unavailable_type("disposable database unavailable")

    readiness = _run(foundation.readiness())
    _run(foundation.close())

    assert readiness.ready is False
    assert readiness.affects_liveness is False


def test_analysis_child_contract_remains_database_free() -> None:
    from core.config import Settings
    from services.process_contracts import ChildAnalysisRequest
    from services.process_entrypoint import initialize_analysis_child

    prohibited = ("database", "postgres", "pool", "dsn", "credential", "password")
    request_fields = {field.name.lower() for field in fields(ChildAnalysisRequest)}
    child_settings = {field.name.lower() for field in fields(Settings)}
    initializer_parameters = tuple(inspect.signature(initialize_analysis_child).parameters)

    assert not any(marker in name for marker in prohibited for name in request_fields)
    assert not any(marker in name for marker in prohibited for name in child_settings)
    assert initializer_parameters == ("settings",)


@dataclass(frozen=True, slots=True)
class _MigrationState:
    current_version: str | None
    applied_slice_one_revisions: int
    application_relations: frozenset[str]
    migration_tool_metadata_present: bool


class _MigrationBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.apply_error: BaseException | None = None
        self._version: str | None = None
        self._applied_slice_one_revisions = 0
        self._application_relations: frozenset[str] = frozenset()
        self._migration_tool_metadata_present = True

    def apply_pending(self) -> None:
        self.events.append("apply_pending")
        if self.apply_error is not None:
            raise self.apply_error
        self._version = _REQUIRED_SCHEMA_VERSION
        self._applied_slice_one_revisions = 1

    def rollback_last(self) -> None:
        self.events.append("rollback_last")
        self._version = None
        self._applied_slice_one_revisions = 0

    def current_version(self) -> str | None:
        self.events.append("current_version")
        return self._version

    def inspect(self) -> _MigrationState:
        return _MigrationState(
            current_version=self._version,
            applied_slice_one_revisions=self._applied_slice_one_revisions,
            application_relations=self._application_relations,
            migration_tool_metadata_present=self._migration_tool_metadata_present,
        )


def test_contract_composes_operational_migration_methods_for_round_trip_verification() -> None:
    purpose = "operational apply, version, rollback, and deterministic reapply capabilities"
    runner_type = _require_symbol(_FOUNDATION_MODULE, "MigrationRunner", purpose)
    backend = _MigrationBackend()
    runner = runner_type(backend, logger=_capturing_logger()[0])

    runner.apply_pending()
    first_version = runner.current_version()
    first_state = backend.inspect()

    runner.rollback_last()
    rolled_back_version = runner.current_version()
    rolled_back_state = backend.inspect()

    runner.apply_pending()
    reapplied_version = runner.current_version()
    reapplied_state = backend.inspect()

    assert first_version == _REQUIRED_SCHEMA_VERSION
    assert first_state.applied_slice_one_revisions == 1
    assert first_state.application_relations == frozenset()
    assert rolled_back_version is None
    assert rolled_back_state.applied_slice_one_revisions == 0
    assert rolled_back_state.application_relations == frozenset()
    assert rolled_back_state.migration_tool_metadata_present is True
    assert reapplied_version == first_version
    assert reapplied_state == first_state


def test_slice_one_migration_policy_authorizes_only_tool_metadata() -> None:
    purpose = "migration-tool metadata without Super-7 domain-table authorization"
    policy = _require_symbol(_FOUNDATION_MODULE, "FOUNDATION_MIGRATION_POLICY", purpose)

    assert policy.allows_migration_tool_metadata is True
    assert policy.allows_domain_relations is False


def test_slice_one_migration_state_contains_no_domain_relations() -> None:
    purpose = "absence of AnalysisJob, idempotency, attempt, result, and delivery relations"
    runner_type = _require_symbol(_FOUNDATION_MODULE, "MigrationRunner", purpose)
    backend = _MigrationBackend()
    runner = runner_type(backend, logger=_capturing_logger()[0])

    runner.apply_pending()
    normalized = tuple(relation.lower() for relation in backend.inspect().application_relations)

    assert all(
        marker not in relation for marker in _DOMAIN_RELATION_MARKERS for relation in normalized
    )


def test_configuration_failure_remains_logging_free_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purpose = "logging-free preservation of a primary configuration failure"
    settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)
    error_type = _require_symbol(_CONFIG_MODULE, "DatabaseConfigurationError", purpose)
    values = _local_environment()
    del values["POSTGRES_PASSWORD"]
    logging_calls = 0

    def fail_logging(*_: object, **__: object) -> None:
        nonlocal logging_calls
        logging_calls += 1
        raise _LoggingFailure("diagnostic logger failed")

    monkeypatch.setattr(logging.Logger, "_log", fail_logging)
    with pytest.raises(error_type, match="POSTGRES_PASSWORD is required"):
        settings_type.from_mapping(values)

    assert logging_calls == 0


def test_logging_failure_cannot_replace_startup_failure() -> None:
    purpose = "preservation of a primary pool-startup failure"
    pool = _FakePool([])
    pool.open_error = _PrimaryFailure("primary startup failure")
    foundation, _, _ = _foundation(pool=pool, logger=_ExplodingLogger(), purpose=purpose)

    with pytest.raises(_PrimaryFailure, match="primary startup failure"):
        _run(foundation.start())


def test_logging_failure_cannot_replace_connectivity_classification() -> None:
    purpose = "preservation of connectivity classification when diagnostics fail"
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseConnectivityUnavailable",
        purpose,
    )
    foundation, pool, _ = _foundation(logger=_ExplodingLogger(), purpose=purpose)
    _run(foundation.start())
    pool.connectivity_error = unavailable_type("primary connectivity failure")

    readiness = _run(foundation.readiness())
    _run(foundation.close())

    assert readiness.reason == "CONNECTIVITY_UNAVAILABLE"


def test_logging_failure_cannot_replace_migration_failure() -> None:
    purpose = "preservation of a primary migration failure"
    runner_type = _require_symbol(_FOUNDATION_MODULE, "MigrationRunner", purpose)
    backend = _MigrationBackend()
    backend.apply_error = _PrimaryFailure("primary migration failure")
    runner = runner_type(backend, logger=_ExplodingLogger())

    with pytest.raises(_PrimaryFailure, match="primary migration failure"):
        runner.apply_pending()


def test_logging_failure_cannot_replace_shutdown_failure() -> None:
    purpose = "preservation of a primary pool-shutdown failure"
    pool = _FakePool([])
    foundation, _, _ = _foundation(pool=pool, logger=_ExplodingLogger(), purpose=purpose)
    _run(foundation.start())
    pool.close_error = _PrimaryFailure("primary shutdown failure")

    with pytest.raises(_PrimaryFailure, match="primary shutdown failure"):
        _run(foundation.close())


@pytest.mark.parametrize(
    "failure",
    (asyncio.CancelledError(), _FatalProbeFailure("fatal probe failure")),
    ids=("cancellation", "base-exception"),
)
def test_startup_diagnostics_do_not_swallow_cancellation_or_base_exception(
    failure: BaseException,
) -> None:
    purpose = f"propagation of {type(failure).__name__} from startup"
    pool = _FakePool([])
    pool.connectivity_error = failure
    foundation, _, _ = _foundation(pool=pool, logger=_ExplodingLogger(), purpose=purpose)

    with pytest.raises(type(failure)):
        _run(foundation.start())

    assert pool.events == ["database.open", "database.close"]


@pytest.mark.parametrize(
    "stage",
    ("open", "connectivity", "schema", "mismatch"),
    ids=("open", "connectivity", "schema", "schema-mismatch"),
)
def test_logger_baseexception_after_successful_startup_cleanup_is_recoverable(
    stage: str,
) -> None:
    purpose = f"recoverable state before fatal startup logging after {stage} failure"
    events: list[str] = []
    pool = _FakePool(events)
    primary = _PrimaryFailure(f"{stage}-primary-marker")
    expected_primary_type: type[BaseException] = _PrimaryFailure
    if stage == "open":
        pool.open_error = primary
    elif stage == "connectivity":
        pool.connectivity_error = primary
    elif stage == "schema":
        pool.schema_error = primary
    else:
        pool.schema_version = "unexpected-version-marker"
        expected_primary_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseStartupError", purpose)

    logger = _FatalEventLogger("postgres_foundation_start_failed")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.start())

    assert captured.value is logger.failure
    graph = _walk_exception_graph(captured.value)
    assert any(isinstance(error, expected_primary_type) for error in graph)
    if stage != "mismatch":
        assert primary in graph
    assert foundation._state.value == "NEW"
    assert foundation._pool is None
    assert factory.calls == 1
    assert events.count("database.close") == 1
    assert _run(foundation.readiness()).reason == "NOT_STARTED"

    pool.open_error = None
    pool.connectivity_error = None
    pool.schema_error = None
    pool.schema_version = _REQUIRED_SCHEMA_VERSION
    _run(foundation.start())
    assert foundation._state.value == "STARTED"
    assert foundation._pool is pool
    assert factory.calls == 2
    _run(foundation.close())
    assert events.count("database.close") == 2


def test_logger_baseexception_after_failed_startup_cleanup_retains_retryable_pool() -> None:
    purpose = "recoverable retained ownership before fatal failed-cleanup logging"
    primary = _PrimaryFailure("startup-primary-marker")
    cleanup = _CleanupFailure("startup-cleanup-marker")
    pool = _FakePool([])
    pool.open_error = primary
    pool.close_error = cleanup
    logger = _FatalEventLogger("postgres_foundation_start_failed")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.start())

    assert captured.value is logger.failure
    graph = _walk_exception_graph(captured.value)
    assert primary in graph
    assert foundation._state.value == "CLEANUP_PENDING"
    assert foundation._pool is pool
    assert factory.calls == 1
    assert pool.events.count("database.close") == 1
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        _run(foundation.start())
    assert factory.calls == 1

    pool.close_error = None
    _run(foundation.close())
    assert foundation._state.value == "CLOSED"
    assert foundation._pool is None
    assert pool.events.count("database.close") == 2


def test_logger_baseexception_from_cleanup_failure_diagnostic_retains_retryable_pool() -> None:
    purpose = "recoverable retained ownership before fatal cleanup-failure logging"
    primary = _PrimaryFailure("startup-primary-marker")
    cleanup = _CleanupFailure("startup-cleanup-marker")
    pool = _FakePool([])
    pool.open_error = primary
    pool.close_error = cleanup
    logger = _FatalEventLogger("postgres_foundation_start_cleanup_failed")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.start())

    assert captured.value is logger.failure
    graph = _walk_exception_graph(captured.value)
    assert cleanup in graph
    assert primary in graph
    assert foundation._state.value == "CLEANUP_PENDING"
    assert foundation._pool is pool
    assert factory.calls == 1
    assert pool.events.count("database.close") == 1

    pool.close_error = None
    _run(foundation.close())
    assert foundation._state.value == "CLOSED"
    assert foundation._pool is None
    assert pool.events.count("database.close") == 2


def test_logger_cancellation_after_successful_startup_cleanup_is_not_swallowed() -> None:
    purpose = "logger cancellation after resource-safe startup cleanup"
    primary = _PrimaryFailure("startup-primary-marker")
    cancellation = asyncio.CancelledError("logger-cancellation-marker")
    pool = _FakePool([])
    pool.open_error = primary
    logger = _FatalEventLogger("postgres_foundation_start_failed", cancellation)
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(asyncio.CancelledError) as captured:
        _run(foundation.start())

    assert captured.value is cancellation
    assert primary in _walk_exception_graph(captured.value)
    assert foundation._state.value == "NEW"
    assert foundation._pool is None
    assert factory.calls == 1
    assert pool.events.count("database.close") == 1

    pool.open_error = None
    _run(foundation.start())
    assert foundation._state.value == "STARTED"
    assert foundation._pool is pool
    assert factory.calls == 2
    _run(foundation.close())


def test_logger_baseexception_after_normal_close_failure_retains_retryable_pool() -> None:
    purpose = "recoverable retained ownership before fatal close-failure logging"
    close_failure = _CleanupFailure("close-primary-marker")
    pool = _FakePool([])
    logger = _FatalEventLogger("postgres_foundation_close_failed")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)
    _run(foundation.start())
    pool.close_error = close_failure

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.close())

    assert captured.value is logger.failure
    assert close_failure in _walk_exception_graph(captured.value)
    assert foundation._state.value == "CLEANUP_PENDING"
    assert foundation._pool is pool
    assert factory.calls == 1
    assert pool.events.count("database.close") == 1
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        _run(foundation.start())
    assert factory.calls == 1

    pool.close_error = None
    _run(foundation.close())
    assert foundation._state.value == "CLOSED"
    assert foundation._pool is None
    assert pool.events.count("database.close") == 2


def test_logger_baseexception_after_successful_start_observes_started_state() -> None:
    purpose = "committed STARTED state before fatal successful-start logging"
    pool = _FakePool([])
    logger = _FatalEventLogger("postgres_foundation_started")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.start())

    assert captured.value is logger.failure
    assert captured.value.__context__ is None
    assert foundation._state.value == "STARTED"
    assert foundation._pool is pool
    assert factory.calls == 1
    assert pool.events.count("database.open") == 1
    assert pool.events.count("database.close") == 0
    _run(foundation.start())
    assert factory.calls == 1
    assert pool.events.count("database.open") == 1
    _run(foundation.close())


def test_logger_baseexception_after_successful_close_observes_closed_state() -> None:
    purpose = "committed CLOSED state before fatal successful-close logging"
    pool = _FakePool([])
    logger = _FatalEventLogger("postgres_foundation_closed")
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)
    _run(foundation.start())

    with pytest.raises(_FatalLoggingFailure) as captured:
        _run(foundation.close())

    assert captured.value is logger.failure
    assert captured.value.__context__ is None
    assert foundation._state.value == "CLOSED"
    assert foundation._pool is None
    assert factory.calls == 1
    assert pool.events.count("database.close") == 1
    _run(foundation.close())
    assert pool.events.count("database.close") == 1
    with pytest.raises(RuntimeError, match="has been closed"):
        _run(foundation.start())
    assert factory.calls == 1


@pytest.mark.parametrize(
    "stage",
    ("open", "connectivity", "schema", "mismatch"),
    ids=("open", "connectivity", "schema", "schema-mismatch"),
)
def test_startup_failure_with_cleanup_failure_retains_owned_pool(stage: str) -> None:
    purpose = f"retained pool ownership after {stage} failure and failed cleanup"
    events: list[str] = []
    pool = _FakePool(events)
    primary = _PrimaryFailure(f"{stage}-primary-marker")
    cleanup_marker = f"{stage}-cleanup-marker"
    pool.close_error = _CleanupFailure(cleanup_marker)
    expected_type: type[BaseException] = _PrimaryFailure
    if stage == "open":
        pool.open_error = primary
    elif stage == "connectivity":
        pool.connectivity_error = primary
    elif stage == "schema":
        pool.schema_error = primary
    else:
        pool.schema_version = "unexpected-version-marker"
        expected_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseStartupError", purpose)

    logger, stream = _capturing_logger()
    foundation, _, factory = _foundation(pool=pool, logger=logger, purpose=purpose)

    with pytest.raises(expected_type) as captured:
        _run(foundation.start())

    if stage != "mismatch":
        assert captured.value is primary
    assert cleanup_marker not in _render_exception_graph(captured.value)
    assert foundation._pool is pool
    assert factory.calls == 1

    pending = _run(foundation.readiness())
    assert pending.reason == "CLEANUP_PENDING"
    assert pending.ready is False
    assert pending.public.database_connectivity is False
    assert pending.public.database_schema_version is False

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        _run(foundation.start())
    assert factory.calls == 1

    pool.close_error = None
    _run(foundation.close())
    assert events.count("database.close") == 2
    assert foundation._pool is None
    assert _run(foundation.readiness()).reason == "CLOSED"
    assert cleanup_marker not in stream.getvalue()


def test_normal_close_failure_retains_pool_and_retries_same_instance() -> None:
    purpose = "retryable close of the same retained pool"
    pool = _FakePool([])
    failure = _CleanupFailure("standalone-close-marker")
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)
    _run(foundation.start())
    pool.close_error = failure

    with pytest.raises(_CleanupFailure) as captured:
        _run(foundation.close())

    assert captured.value is failure
    assert foundation._pool is pool
    assert factory.calls == 1
    assert _run(foundation.readiness()).reason == "CLEANUP_PENDING"

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        _run(foundation.start())
    assert factory.calls == 1

    pool.close_error = None
    _run(foundation.close())
    assert pool.events.count("database.close") == 2
    assert foundation._pool is None
    assert _run(foundation.readiness()).reason == "CLOSED"


def test_repeated_successful_start_is_a_no_op() -> None:
    purpose = "idempotent repeated successful startup"
    foundation, pool, factory = _foundation(purpose=purpose)

    _run(foundation.start())
    _run(foundation.start())

    assert factory.calls == 1
    assert pool.events.count("database.open") == 1
    _run(foundation.close())


def test_repeated_successful_close_is_a_no_op() -> None:
    purpose = "idempotent repeated successful shutdown"
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())

    _run(foundation.close())
    _run(foundation.close())

    assert pool.events.count("database.close") == 1


def test_close_before_start_creates_no_pool_and_rejects_later_start() -> None:
    purpose = "close before startup and rejection of startup after close"
    foundation, pool, factory = _foundation(purpose=purpose)

    _run(foundation.close())

    assert factory.calls == 0
    assert pool.events == []
    assert _run(foundation.readiness()).reason == "CLOSED"
    with pytest.raises(RuntimeError, match="has been closed"):
        _run(foundation.start())
    assert factory.calls == 0


def test_readiness_before_start_is_fixed_and_false() -> None:
    purpose = "fixed readiness before startup"
    foundation, _, factory = _foundation(purpose=purpose)

    readiness = _run(foundation.readiness())

    assert factory.calls == 0
    assert readiness.ready is False
    assert readiness.reason == "NOT_STARTED"
    assert readiness.public.database_connectivity is False
    assert readiness.public.database_schema_version is False
    assert readiness.affects_liveness is False


def test_readiness_after_close_is_fixed_and_false() -> None:
    purpose = "fixed readiness after shutdown"
    foundation, _, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    _run(foundation.close())

    readiness = _run(foundation.readiness())

    assert readiness.ready is False
    assert readiness.reason == "CLOSED"
    assert readiness.public.database_connectivity is False
    assert readiness.public.database_schema_version is False


def test_schema_probe_operational_failure_preserves_connectivity_success() -> None:
    purpose = "distinct safe schema-probe unavailability"
    unavailable_type = _require_symbol(
        _FOUNDATION_MODULE,
        "DatabaseSchemaVersionUnavailable",
        purpose,
    )
    marker = "schema-operational-secret-marker"
    logger, stream = _capturing_logger()
    foundation, pool, _ = _foundation(logger=logger, purpose=purpose)
    _run(foundation.start())
    pool.schema_error = unavailable_type(marker)

    readiness = _run(foundation.readiness())
    pool.schema_error = None
    _run(foundation.close())

    assert readiness.ready is False
    assert readiness.reason == "SCHEMA_VERSION_UNAVAILABLE"
    assert readiness.public.database_connectivity is True
    assert readiness.public.database_schema_version is False
    assert marker not in repr(readiness)
    assert marker not in stream.getvalue()


@pytest.mark.parametrize(
    ("stage", "failure_type"),
    (
        ("connectivity", AssertionError),
        ("schema", TypeError),
        ("connectivity", AttributeError),
    ),
    ids=("connectivity-assertion", "schema-type-error", "connectivity-attribute-error"),
)
def test_readiness_propagates_programming_defects(
    stage: str,
    failure_type: type[Exception],
) -> None:
    purpose = f"propagation of {failure_type.__name__} from the {stage} readiness probe"
    failure = failure_type("programming-defect-marker")
    foundation, pool, _ = _foundation(purpose=purpose)
    _run(foundation.start())
    if stage == "connectivity":
        pool.connectivity_error = failure
    else:
        pool.schema_error = failure

    with pytest.raises(failure_type) as captured:
        _run(foundation.readiness())

    assert captured.value is failure
    pool.connectivity_error = None
    pool.schema_error = None
    _run(foundation.close())


@pytest.mark.parametrize(
    ("reason", "ready", "connectivity", "schema"),
    _READINESS_MATRIX_CASES,
    ids=(
        "not-started",
        "cleanup-pending",
        "closed",
        "connectivity-unavailable",
        "schema-version-unavailable",
        "schema-version-mismatch",
        "ready",
    ),
)
def test_readiness_constructor_and_runtime_helper_accept_exact_matrix_rows(
    reason: str,
    ready: bool,
    connectivity: bool,
    schema: bool,
) -> None:
    purpose = f"exact readiness matrix row for {reason}"
    module = _require_module(_FOUNDATION_MODULE, purpose)
    checks_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePublicChecks", purpose)
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)
    public = checks_type(
        database_connectivity=connectivity,
        database_schema_version=schema,
    )

    constructed = readiness_type(ready=ready, reason=reason, public=public)
    runtime = module._readiness(
        ready,
        reason,
        connectivity=connectivity,
        schema=schema,
    )

    assert constructed == runtime
    assert constructed.affects_liveness is False


@pytest.mark.parametrize(
    ("reason", "ready", "connectivity", "schema"),
    (
        ("READY", False, True, True),
        ("READY", True, False, True),
        ("READY", True, True, False),
        ("NOT_STARTED", True, False, False),
        ("CLEANUP_PENDING", True, False, False),
        ("CLOSED", True, False, False),
        ("CONNECTIVITY_UNAVAILABLE", True, False, False),
        ("SCHEMA_VERSION_UNAVAILABLE", True, True, False),
        ("SCHEMA_VERSION_MISMATCH", True, True, False),
        ("NOT_STARTED", False, True, False),
        ("NOT_STARTED", False, False, True),
        ("CLEANUP_PENDING", False, True, False),
        ("CLEANUP_PENDING", False, False, True),
        ("CLOSED", False, True, False),
        ("CLOSED", False, False, True),
        ("CONNECTIVITY_UNAVAILABLE", False, True, False),
        ("CONNECTIVITY_UNAVAILABLE", False, False, True),
        ("SCHEMA_VERSION_UNAVAILABLE", False, False, False),
        ("SCHEMA_VERSION_UNAVAILABLE", False, True, True),
        ("SCHEMA_VERSION_MISMATCH", False, False, False),
        ("SCHEMA_VERSION_MISMATCH", False, True, True),
    ),
    ids=(
        "ready-with-ready-false",
        "ready-with-connectivity-false",
        "ready-with-schema-false",
        "not-started-with-ready-true",
        "cleanup-pending-with-ready-true",
        "closed-with-ready-true",
        "connectivity-unavailable-with-ready-true",
        "schema-unavailable-with-ready-true",
        "schema-mismatch-with-ready-true",
        "not-started-with-connectivity-true",
        "not-started-with-schema-true",
        "cleanup-pending-with-connectivity-true",
        "cleanup-pending-with-schema-true",
        "closed-with-connectivity-true",
        "closed-with-schema-true",
        "connectivity-unavailable-with-connectivity-true",
        "connectivity-unavailable-with-schema-true",
        "schema-unavailable-with-connectivity-false",
        "schema-unavailable-with-schema-true",
        "schema-mismatch-with-connectivity-false",
        "schema-mismatch-with-schema-true",
    ),
)
def test_readiness_constructor_rejects_every_contradictory_matrix_form(
    reason: str,
    ready: bool,
    connectivity: bool,
    schema: bool,
) -> None:
    purpose = f"rejection of contradictory readiness fields for {reason}"
    checks_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePublicChecks", purpose)
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)

    with pytest.raises(ValueError) as captured:
        readiness_type(
            ready=ready,
            reason=reason,
            public=checks_type(
                database_connectivity=connectivity,
                database_schema_version=schema,
            ),
        )

    assert str(captured.value) == "database readiness fields must match the reason."
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_readiness_constructor_rejects_unknown_reason_without_exposing_it() -> None:
    purpose = "safe rejection of an unknown readiness reason"
    marker = "unknown-reason-secret-marker"
    checks_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePublicChecks", purpose)
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)

    with pytest.raises(ValueError) as captured:
        readiness_type(
            ready=False,
            reason=marker,
            public=checks_type(
                database_connectivity=False,
                database_schema_version=False,
            ),
        )

    assert str(captured.value) == "reason must be a supported database readiness reason."
    assert marker not in _render_exception_graph(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("field", "marker"),
    (
        ("ready", "non-boolean-ready-secret-marker"),
        ("connectivity", "non-boolean-connectivity-secret-marker"),
        ("schema", "non-boolean-schema-secret-marker"),
    ),
    ids=("ready", "connectivity", "schema"),
)
def test_readiness_constructor_rejects_non_boolean_fields_without_exposing_them(
    field: str,
    marker: str,
) -> None:
    purpose = f"safe rejection of non-Boolean readiness field {field}"
    checks_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePublicChecks", purpose)
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)
    ready: object = False
    connectivity: object = False
    schema: object = False
    if field == "ready":
        ready = marker
    elif field == "connectivity":
        connectivity = marker
    else:
        schema = marker

    with pytest.raises(TypeError) as captured:
        public = checks_type(
            database_connectivity=connectivity,
            database_schema_version=schema,
        )
        readiness_type(ready=ready, reason="NOT_STARTED", public=public)

    assert marker not in _render_exception_graph(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_public_readiness_shape_is_fixed_immutable_and_non_liveness_affecting() -> None:
    purpose = "fixed immutable public readiness checks"
    checks_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePublicChecks", purpose)
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)
    foundation, _, _ = _foundation(purpose=purpose)
    readiness = _run(foundation.readiness())

    assert tuple(field.name for field in fields(checks_type)) == (
        "database_connectivity",
        "database_schema_version",
    )
    assert tuple(field.name for field in fields(readiness_type)) == ("ready", "reason", "public")
    assert isinstance(readiness.public, checks_type)
    assert readiness.affects_liveness is False

    with pytest.raises(FrozenInstanceError):
        readiness.public.database_connectivity = True
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        readiness.public.arbitrary_check = True
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        readiness.affects_liveness = True
    with pytest.raises(TypeError):
        checks_type(
            database_connectivity=False,
            database_schema_version=False,
            arbitrary_check=True,
        )
    with pytest.raises(TypeError):
        checks_type(database_connectivity="false", database_schema_version=False)
    with pytest.raises(TypeError):
        readiness_type(
            ready=False,
            reason="NOT_STARTED",
            public=readiness.public,
            affects_liveness=True,
        )
    with pytest.raises(ValueError):
        readiness_type(
            ready=False,
            reason="arbitrary-secret-bearing-reason",
            public=readiness.public,
        )


def test_database_readiness_is_final_at_static_and_runtime_boundaries() -> None:
    purpose = "structurally fixed non-liveness-affecting readiness"
    readiness_type = _require_symbol(_FOUNDATION_MODULE, "DatabaseReadiness", purpose)
    foundation, _, _ = _foundation(purpose=purpose)
    readiness = _run(foundation.readiness())

    assert readiness_type.__final__ is True
    assert readiness.affects_liveness is False
    assert not hasattr(readiness, "__dict__")

    with pytest.raises(TypeError, match="DatabaseReadiness cannot be subclassed"):
        type("InvalidReadiness", (readiness_type,), {"affects_liveness": True})


def test_probe_protocols_define_exact_adapter_facing_signatures() -> None:
    purpose = "exact adapter-facing pool factory and probe protocols"
    module = _require_module(_FOUNDATION_MODULE, purpose)
    database_pool_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePool", purpose)
    database_settings_type = _require_symbol(_CONFIG_MODULE, "DatabaseSettings", purpose)

    factory_type = _require_symbol(_FOUNDATION_MODULE, "PoolFactory", purpose)
    assert getattr(factory_type, "_is_protocol", False) is True
    factory_signature = inspect.signature(factory_type.__call__)
    assert tuple(factory_signature.parameters) == ("self", "settings")
    factory_hints = get_type_hints(factory_type.__call__, vars(module), vars(module))
    assert factory_hints["settings"] is database_settings_type
    assert factory_hints["return"] is database_pool_type

    for symbol, result_type in (
        ("ConnectivityProbe", None),
        ("SchemaVersionProbe", str),
    ):
        probe_type = _require_symbol(_FOUNDATION_MODULE, symbol, purpose)
        assert getattr(probe_type, "_is_protocol", False) is True
        signature = inspect.signature(probe_type.__call__)
        assert tuple(signature.parameters) == ("self", "pool", "timeout_seconds")
        assert signature.parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
        hints = get_type_hints(probe_type.__call__, vars(module), vars(module))
        assert hints["pool"] is database_pool_type
        assert hints["timeout_seconds"] is float
        assert get_origin(hints["return"]) is Awaitable
        assert get_args(hints["return"]) == (result_type,)


def test_database_pool_protocol_defines_exact_async_lifecycle_signatures() -> None:
    purpose = "exact adapter-facing database pool lifecycle protocol"
    module = _require_module(_FOUNDATION_MODULE, purpose)
    pool_type = _require_symbol(_FOUNDATION_MODULE, "DatabasePool", purpose)

    assert getattr(pool_type, "_is_protocol", False) is True
    public_methods = {
        name
        for name, value in vars(pool_type).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {"open", "close"}
    for method_name in ("open", "close"):
        method = getattr(pool_type, method_name)
        assert inspect.iscoroutinefunction(method)
        signature = inspect.signature(method)
        assert tuple(signature.parameters) == ("self", "timeout_seconds")
        assert signature.parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
        hints = get_type_hints(method, vars(module), vars(module))
        assert hints["timeout_seconds"] is float
        assert hints["return"] is type(None)


def test_migration_backend_protocol_exposes_only_approved_sync_operations() -> None:
    purpose = "exact adapter-facing migration backend protocol"
    module = _require_module(_FOUNDATION_MODULE, purpose)
    backend_type = _require_symbol(_FOUNDATION_MODULE, "MigrationBackend", purpose)

    assert getattr(backend_type, "_is_protocol", False) is True
    public_methods = {
        name
        for name, value in vars(backend_type).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {"apply_pending", "rollback_last", "current_version"}
    for method_name in ("apply_pending", "rollback_last", "current_version"):
        method = getattr(backend_type, method_name)
        assert not inspect.iscoroutinefunction(method)
        assert tuple(inspect.signature(method).parameters) == ("self",)
        hints = get_type_hints(method, vars(module), vars(module))
        expected_return = str | None if method_name == "current_version" else type(None)
        assert hints["return"] == expected_return


@pytest.mark.parametrize(
    "source",
    ("factory", "open", "connectivity", "schema", "close", "migration"),
)
def test_internal_failure_graph_preserves_identity_without_public_secret_logging(
    source: str,
) -> None:
    purpose = f"exception graph and safe logging for {source} failure"
    marker = f"{source}-exception-graph-secret-marker"
    failure = _PrimaryFailure(marker)
    logger, stream = _capturing_logger()

    if source == "migration":
        runner_type = _require_symbol(_FOUNDATION_MODULE, "MigrationRunner", purpose)
        backend = _MigrationBackend()
        backend.apply_error = failure
        with pytest.raises(_PrimaryFailure) as captured:
            runner_type(backend, logger=logger).apply_pending()
    else:
        settings = _load_settings(_local_environment(), purpose)
        foundation_type = _require_symbol(_FOUNDATION_MODULE, "PostgresFoundation", purpose)
        pool = _FakePool([])
        factory: object = _PoolFactory(pool)
        if source == "factory":

            def failing_factory(_: object) -> _FakePool:
                raise failure

            factory = failing_factory
        elif source == "open":
            pool.open_error = failure
        elif source == "connectivity":
            pool.connectivity_error = failure
        elif source == "schema":
            pool.schema_error = failure

        foundation = foundation_type(
            settings,
            pool_factory=factory,
            connectivity_probe=_connectivity_probe,
            schema_version_probe=_schema_version_probe,
            logger=logger,
        )
        if source == "close":
            _run(foundation.start())
            pool.close_error = failure
            operation = foundation.close()
        else:
            operation = foundation.start()
        with pytest.raises(_PrimaryFailure) as captured:
            _run(operation)

    assert captured.value is failure
    assert _walk_exception_graph(captured.value) == (failure,)
    assert marker in _render_exception_graph(captured.value)
    assert marker not in stream.getvalue()


@pytest.mark.parametrize(
    "cleanup",
    (
        asyncio.CancelledError("cleanup-cancellation-marker"),
        _FatalProbeFailure("cleanup-baseexception-marker"),
    ),
    ids=("cancellation", "base-exception"),
)
def test_cleanup_baseexception_propagates_and_retains_pool(cleanup: BaseException) -> None:
    purpose = f"{type(cleanup).__name__} cleanup precedence and retained ownership"
    primary_marker = "ordinary-startup-primary-marker"
    primary = _PrimaryFailure(primary_marker)
    pool = _FakePool([])
    pool.open_error = primary
    pool.close_error = cleanup
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)

    with pytest.raises(type(cleanup)) as captured:
        _run(foundation.start())

    assert captured.value is cleanup
    graph = _walk_exception_graph(captured.value)
    assert cleanup in graph
    assert primary in graph
    assert primary_marker in _render_exception_graph(captured.value)
    assert foundation._pool is pool
    assert factory.calls == 1
    assert _run(foundation.readiness()).reason == "CLEANUP_PENDING"

    pool.close_error = None
    _run(foundation.close())
    assert foundation._pool is None


@pytest.mark.parametrize(
    "primary",
    (
        asyncio.CancelledError("primary-cancellation-marker"),
        _FatalProbeFailure("primary-baseexception-marker"),
    ),
    ids=("cancellation", "base-exception"),
)
def test_primary_baseexception_survives_ordinary_cleanup_failure(
    primary: BaseException,
) -> None:
    purpose = f"preserved {type(primary).__name__} with failed ordinary cleanup"
    cleanup_marker = "ordinary-cleanup-marker"
    pool = _FakePool([])
    pool.connectivity_error = primary
    pool.close_error = _CleanupFailure(cleanup_marker)
    foundation, _, factory = _foundation(pool=pool, purpose=purpose)

    with pytest.raises(type(primary)) as captured:
        _run(foundation.start())

    assert captured.value is primary
    assert cleanup_marker not in _render_exception_graph(captured.value)
    assert foundation._pool is pool
    assert factory.calls == 1
    assert _run(foundation.readiness()).reason == "CLEANUP_PENDING"

    pool.close_error = None
    _run(foundation.close())
    assert foundation._pool is None


def test_current_runtime_remains_unactivated() -> None:
    from main import app, create_app

    assert not hasattr(app.state, "postgres_foundation")
    assert "database_settings" not in inspect.signature(create_app).parameters
    assert "postgres_foundation" not in inspect.signature(create_app).parameters
    assert "/analyze" in app.openapi()["paths"]
