"""Shared worker policy and graph construction for opt-in durable roles."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository
from adapters.psycopg_database import PsycopgDatabasePool
from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import LeaseDuration, RecoveryBatchSize, WorkerSlotId
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.durable_worker_loop import DurableWorkerLoop
from services.process_analysis_pool import ProcessAnalysisPool


@dataclass(frozen=True, slots=True)
class WorkerConfiguration:
    timing: CoordinatorTiming
    recovery_batch_size: RecoveryBatchSize
    idle_seconds: float
    model_catalog: Mapping[str, str]
    operation_timeout_ms: int
    lock_timeout_ms: int


def parse_coordinator_timing(required: Callable[[str], str]) -> CoordinatorTiming:
    return CoordinatorTiming(
        LeaseDuration(timedelta(seconds=float(required("SUPER7_DURABLE_LEASE_SECONDS")))),
        heartbeat_seconds=float(required("SUPER7_DURABLE_HEARTBEAT_SECONDS")),
        operation_watchdog_seconds=float(required("SUPER7_DURABLE_WATCHDOG_SECONDS")),
        jitter_seconds=float(required("SUPER7_DURABLE_JITTER_SECONDS")),
        finalization_budget_seconds=float(required("SUPER7_DURABLE_FINALIZE_SECONDS")),
        retry_delays_seconds=(
            float(required("SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS")),
            float(required("SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS")),
        ),
    )


def parse_worker_loop_configuration(
    settings: Settings, required: Callable[[str], str]
) -> tuple[RecoveryBatchSize, float, Mapping[str, str]]:
    raw_model = f"{settings.model_path}+{settings.ball_model_path}+bytetrack"
    return (
        RecoveryBatchSize(int(required("SUPER7_DURABLE_RECOVERY_BATCH"))),
        float(required("SUPER7_DURABLE_WORKER_IDLE_SECONDS")),
        {raw_model: required("SUPER7_DURABLE_MODEL_ID")},
    )


def parse_worker_database_timeouts(required: Callable[[str], str]) -> tuple[int, int]:
    return (
        int(required("SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS")),
        int(required("SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS")),
    )


@dataclass(frozen=True, slots=True)
class WorkerGraph:
    coordinator: AnalysisJobCoordinator
    worker: DurableWorkerLoop


def build_worker_graph(
    *,
    pool: PsycopgDatabasePool,
    database_settings: DatabaseSettings,
    child_executor: ProcessAnalysisPool,
    configuration: WorkerConfiguration,
    logger: logging.Logger,
) -> WorkerGraph:
    """Wire a fresh slot against caller-owned pool and executor."""
    acquire = database_settings.pool.acquire_timeout_seconds
    worker_repository = PsycopgAnalysisJobWorkerRepository(
        pool=pool,
        connection_acquire_timeout_seconds=acquire,
        operation_timeout_ms=configuration.operation_timeout_ms,
        lock_timeout_ms=configuration.lock_timeout_ms,
        lease_id_factory=uuid4,
        failure_snapshot_builder=failure_callback_snapshot_v1,
    )
    finalizer = PsycopgAnalysisJobFinalizationRepository(
        pool=pool,
        connection_acquire_timeout_seconds=acquire,
        operation_timeout_ms=configuration.operation_timeout_ms,
        lock_timeout_ms=configuration.lock_timeout_ms,
    )
    coordinator = AnalysisJobCoordinator(
        worker_repository=worker_repository,
        finalization_repository=finalizer,
        child_executor=child_executor,
        result_projector=convert_child_success,
        worker_slot_id=WorkerSlotId(uuid4()),
        model_catalog=configuration.model_catalog,
        timing=configuration.timing,
    )
    worker = DurableWorkerLoop(
        coordinator=coordinator,
        recovery_repository=worker_repository,
        recovery_batch_size=configuration.recovery_batch_size,
        idle_wait_seconds=configuration.idle_seconds,
        logger=logger,
    )
    return WorkerGraph(coordinator, worker)
