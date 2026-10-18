"""Offline equivalence controls for the two durable worker compositions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import ModuleType
from typing import cast

import pytest

import durable_local_main
import durable_worker_main
from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository
from core.config import Settings
from core.database_config import DatabasePoolSettings, DatabaseSettings
from domain.analysis_job_attempt import RecoveryBatchSize
from services.analysis_callback_delivery import CallbackTransport
from services.analysis_job_coordinator import CoordinatorTiming
from services.durable_local_runtime import DurableLocalRuntime
from services.durable_worker_composition import (
    WorkerConfiguration,
    parse_coordinator_timing,
    parse_worker_database_timeouts,
    parse_worker_loop_configuration,
)
from services.durable_worker_runtime import DurableWorkerRuntime

_WORKER_VALUES = {
    "SUPER7_DURABLE_LEASE_SECONDS": "180",
    "SUPER7_DURABLE_HEARTBEAT_SECONDS": "20",
    "SUPER7_DURABLE_WATCHDOG_SECONDS": "25",
    "SUPER7_DURABLE_JITTER_SECONDS": "10",
    "SUPER7_DURABLE_FINALIZE_SECONDS": "90",
    "SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS": "2",
    "SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS": "5",
    "SUPER7_DURABLE_RECOVERY_BATCH": "3",
    "SUPER7_DURABLE_WORKER_IDLE_SECONDS": "0.25",
    "SUPER7_DURABLE_MODEL_ID": "model-pair-v1",
    "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS": "5000",
    "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS": "1000",
}


def _settings() -> Settings:
    return Settings(model_path="models/player.pt", ball_model_path="models/ball.pt")


def _database() -> DatabaseSettings:
    return DatabaseSettings(
        _host="localhost",
        _port=5432,
        _database="isolated",
        _user="test",
        _password="test-only",
        connect_timeout_seconds=2,
        pool=DatabasePoolSettings(0, 1, 1, 1, 1, 1),
        required_schema_version="sprint2",
    )


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _WORKER_VALUES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SUPER7_DURABLE_LOCAL_ENABLED", "1")
    monkeypatch.setenv("SUPER7_DURABLE_WORKER_ENABLED", "1")
    monkeypatch.setenv("SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX", "00" * 32)
    monkeypatch.setenv("SUPER7_APEX_BEARER_SECRET", "test-only-bearer")
    monkeypatch.setenv("SUPER7_DURABLE_ATTEMPT_LIMIT", "1")


def _parsed_config(settings: Settings, required: Callable[[str], str]) -> WorkerConfiguration:
    timing = parse_coordinator_timing(required)
    recovery, idle, catalog = parse_worker_loop_configuration(settings, required)
    operation, lock = parse_worker_database_timeouts(required)
    return WorkerConfiguration(timing, recovery, idle, catalog, operation, lock)


def _local_runtime(
    settings: Settings,
    database: DatabaseSettings,
    config: WorkerConfiguration,
    *,
    idle: float | None = None,
) -> DurableLocalRuntime:
    return DurableLocalRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret="test-only-bearer",
        idempotency_hmac_secret=b"x" * 32,
        attempt_limit=1,
        timing=config.timing,
        recovery_batch_size=config.recovery_batch_size,
        worker_idle_seconds=config.idle_seconds if idle is None else idle,
        model_catalog=config.model_catalog,
        callback_transport=cast(CallbackTransport, object()),
        logger=logging.getLogger("football_analysis.tests.worker_composition"),
        operation_timeout_ms=config.operation_timeout_ms,
        lock_timeout_ms=config.lock_timeout_ms,
    )


def _worker_runtime(
    settings: Settings,
    database: DatabaseSettings,
    config: WorkerConfiguration,
    *,
    idle: float | None = None,
) -> DurableWorkerRuntime:
    return DurableWorkerRuntime(
        settings=settings,
        database_settings=database,
        timing=config.timing,
        recovery_batch_size=config.recovery_batch_size,
        worker_idle_seconds=config.idle_seconds if idle is None else idle,
        model_catalog=config.model_catalog,
        operation_timeout_ms=config.operation_timeout_ms,
        lock_timeout_ms=config.lock_timeout_ms,
    )


def test_both_entry_points_pass_the_same_worker_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    settings = _settings()
    database = _database()
    monkeypatch.setattr(Settings, "from_environment", classmethod(lambda cls: settings))
    monkeypatch.setattr(DatabaseSettings, "from_mapping", classmethod(lambda cls, values: database))
    captured: dict[str, dict[str, object]] = {}

    def capture(name: str) -> Callable[..., object]:
        def construct(**values: object) -> object:
            captured[name] = values
            return object()

        return construct

    monkeypatch.setattr(durable_local_main, "DurableLocalRuntime", capture("local"))
    monkeypatch.setattr(durable_worker_main, "DurableWorkerRuntime", capture("separated"))
    monkeypatch.setattr(durable_local_main, "_transport", lambda callback: object())
    monkeypatch.setattr(durable_local_main, "compose_app", lambda *args, **kwargs: object())
    durable_local_main.create_app()
    durable_worker_main.create_runtime()

    names = (
        "timing",
        "recovery_batch_size",
        "worker_idle_seconds",
        "model_catalog",
        "operation_timeout_ms",
        "lock_timeout_ms",
    )
    assert tuple(captured["local"][name] for name in names) == tuple(
        captured["separated"][name] for name in names
    )
    timing = cast(CoordinatorTiming, captured["local"]["timing"])
    assert timing.lease_duration.value.total_seconds() == 180
    assert timing.retry_delays_seconds == (2, 5)
    assert cast(RecoveryBatchSize, captured["local"]["recovery_batch_size"]).value == 3
    assert captured["local"]["model_catalog"] == {
        "models/player.pt+models/ball.pt+bytetrack": "model-pair-v1"
    }
    assert captured["local"]["operation_timeout_ms"] == 5000
    assert captured["local"]["lock_timeout_ms"] == 1000
    assert captured["local"]["settings"] is captured["separated"]["settings"] is settings
    assert captured["local"]["database_settings"] is database
    assert captured["separated"]["database_settings"] is database


def test_factory_validation_order_remains_role_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    settings = _settings()
    database = _database()
    monkeypatch.setattr(Settings, "from_environment", classmethod(lambda cls: settings))
    monkeypatch.setattr(DatabaseSettings, "from_mapping", classmethod(lambda cls, values: database))

    monkeypatch.delenv("SUPER7_DURABLE_LEASE_SECONDS")
    monkeypatch.delenv("SUPER7_APEX_BEARER_SECRET")
    with pytest.raises(ValueError, match="SUPER7_DURABLE_LEASE_SECONDS is required"):
        durable_local_main.create_app()
    monkeypatch.setenv("SUPER7_DURABLE_LEASE_SECONDS", "180")
    monkeypatch.delenv("SUPER7_DURABLE_RECOVERY_BATCH")
    with pytest.raises(ValueError, match="SUPER7_APEX_BEARER_SECRET is required"):
        durable_local_main.create_app()

    def database_failure(cls: object, values: object) -> DatabaseSettings:
        raise LookupError("Database configuration was read first.")

    monkeypatch.setattr(DatabaseSettings, "from_mapping", classmethod(database_failure))
    with pytest.raises(LookupError, match="Database configuration was read first"):
        durable_worker_main.create_runtime()

    monkeypatch.setenv("SUPER7_APEX_BEARER_SECRET", "test-only-bearer")
    monkeypatch.setenv("SUPER7_DURABLE_RECOVERY_BATCH", "3")
    monkeypatch.delenv("SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS")

    def transport_failure(callback: object) -> object:
        raise RuntimeError("Callback transport was built first.")

    monkeypatch.setattr(durable_local_main, "_transport", transport_failure)
    monkeypatch.setattr(DatabaseSettings, "from_mapping", classmethod(lambda cls, values: database))
    with pytest.raises(RuntimeError, match="Callback transport was built first"):
        durable_local_main.create_app()


@pytest.mark.parametrize(
    "module", [durable_local_main, durable_worker_main], ids=["local", "separated"]
)
@pytest.mark.parametrize("name", tuple(_WORKER_VALUES))
def test_missing_worker_value_keeps_role_specific_error(
    module: ModuleType, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv(name)
    role = "local durable mode" if module is durable_local_main else "durable worker mode"
    with pytest.raises(ValueError, match=rf"^{name} is required for {role}\.$"):
        _parsed_config(_settings(), module._required)


@pytest.mark.parametrize(
    "module", [durable_local_main, durable_worker_main], ids=["local", "separated"]
)
@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SUPER7_DURABLE_LEASE_SECONDS", "0", "Lease duration must be positive"),
        ("SUPER7_DURABLE_HEARTBEAT_SECONDS", "nan", "timing values must be finite"),
        ("SUPER7_DURABLE_RECOVERY_BATCH", "0", "Policy value must be positive"),
        ("SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS", "not-int", "invalid literal"),
    ],
)
def test_invalid_worker_value_keeps_validation(
    module: ModuleType, name: str, value: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        _parsed_config(_settings(), module._required)


def test_each_runtime_wires_the_same_graph_with_independent_owned_resources() -> None:
    settings = _settings()
    database = _database()
    values = _WORKER_VALUES
    config = _parsed_config(settings, values.__getitem__)
    local = _local_runtime(settings, database, config)
    separated = _worker_runtime(settings, database, config)
    second_separated = _worker_runtime(settings, database, config)

    slots = [
        runtime.coordinator._worker_slot_id.value
        for runtime in (local, separated, second_separated)
    ]
    assert len(set(slots)) == 3
    for runtime in (local, separated, second_separated):
        coordinator = runtime.coordinator
        repository = cast(PsycopgAnalysisJobWorkerRepository, coordinator._worker_repository)
        finalizer = cast(
            PsycopgAnalysisJobFinalizationRepository, coordinator._finalization_repository
        )
        assert runtime.worker._coordinator is coordinator
        assert runtime.worker._recovery_repository is repository
        assert coordinator._child_executor is runtime._executor
        assert coordinator._result_projector is convert_child_success
        assert repository._failure_snapshot_builder is failure_callback_snapshot_v1
        assert coordinator._timing is config.timing
        assert coordinator._model_catalog is config.model_catalog
        assert runtime.worker._batch_size is config.recovery_batch_size
        assert runtime.worker._idle_wait_seconds == config.idle_seconds
        assert repository._operation_timeout_ms == finalizer._operation_timeout_ms == 5000
        assert repository._lock_timeout_ms == finalizer._lock_timeout_ms == 1000
        assert runtime._executor._executor is None
        assert not runtime.worker.running
    pools = [
        cast(PsycopgAnalysisJobWorkerRepository, runtime.coordinator._worker_repository)._pool
        for runtime in (local, separated, second_separated)
    ]
    assert pools[0] is local._pool
    assert (
        cast(
            PsycopgAnalysisJobFinalizationRepository, local.coordinator._finalization_repository
        )._pool
        is local._pool
    )
    assert pools[1] is not pools[0] and pools[2] is not pools[1]
    assert local._executor is not separated._executor is not second_separated._executor


@pytest.mark.parametrize("runtime_type", [DurableLocalRuntime, DurableWorkerRuntime])
def test_invalid_idle_wait_is_rejected_by_existing_worker_loop(
    runtime_type: type[DurableLocalRuntime] | type[DurableWorkerRuntime],
) -> None:
    config = _parsed_config(_settings(), _WORKER_VALUES.__getitem__)
    settings = _settings()
    database = _database()
    with pytest.raises(ValueError, match="Idle wait must be finite, positive"):
        if runtime_type is DurableLocalRuntime:
            _local_runtime(settings, database, config, idle=0)
        else:
            _worker_runtime(settings, database, config, idle=0)
