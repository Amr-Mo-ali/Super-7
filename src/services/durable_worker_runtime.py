"""Single-process durable worker ownership, independent of HTTP and callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import get_logger
from domain.analysis_job_attempt import RecoveryBatchSize, WorkerSlotId
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.durable_worker_loop import DurableWorkerLoop
from services.postgres_foundation import DatabaseReadiness, PostgresFoundation
from services.process_analysis_pool import ProcessAnalysisPool


class DurableWorkerRuntime:
    """Own one DB pool, one fresh slot incarnation, and one spawned child pool."""

    def __init__(
        self,
        *,
        settings: Settings,
        database_settings: DatabaseSettings,
        timing: CoordinatorTiming,
        recovery_batch_size: RecoveryBatchSize,
        worker_idle_seconds: float,
        model_catalog: Mapping[str, str],
        operation_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> None:
        logger = get_logger("football_analysis.durable_worker")
        pool = PsycopgDatabasePool(database_settings)
        self._foundation = PostgresFoundation(
            database_settings,
            pool_factory=lambda _: pool,
            connectivity_probe=PostgresConnectivityProbe(),
            schema_version_probe=PostgresSchemaVersionProbe(),
            logger=logger,
        )
        acquire = database_settings.pool.acquire_timeout_seconds
        worker_repository = PsycopgAnalysisJobWorkerRepository(
            pool=pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=operation_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            lease_id_factory=uuid4,
            failure_snapshot_builder=failure_callback_snapshot_v1,
        )
        finalizer = PsycopgAnalysisJobFinalizationRepository(
            pool=pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=operation_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        self._executor = ProcessAnalysisPool(settings)
        self.coordinator = AnalysisJobCoordinator(
            worker_repository=worker_repository,
            finalization_repository=finalizer,
            child_executor=self._executor,
            result_projector=convert_child_success,
            worker_slot_id=WorkerSlotId(uuid4()),
            model_catalog=model_catalog,
            timing=timing,
        )
        self.worker = DurableWorkerLoop(
            coordinator=self.coordinator,
            recovery_repository=worker_repository,
            recovery_batch_size=recovery_batch_size,
            idle_wait_seconds=worker_idle_seconds,
            logger=logger,
        )
        self.started = False

    async def start(self) -> None:
        await self._foundation.start()
        try:
            self._executor.start()
            self.worker.start()
        except BaseException as primary:
            try:
                await self.stop()
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        self.started = True

    async def stop(self) -> None:
        """Settle active work before closing DB; native shutdown may remain unbounded."""
        self.started = False
        errors: list[BaseException] = []
        for operation in (self.worker.stop, self._executor.shutdown):
            try:
                await operation()
            except BaseException as error:
                errors.append(error)
        outstanding = self.coordinator.outstanding_tasks
        if outstanding:
            _, pending = await asyncio.wait(outstanding, timeout=60)
            if pending:
                errors.append(TimeoutError("Durable persistence observers remain active."))
        if self._executor.outstanding_submissions:
            errors.append(TimeoutError("Submitted child work remains active."))
        if not self.coordinator.outstanding_tasks and not self._executor.outstanding_submissions:
            try:
                await self._foundation.close()
            except BaseException as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Durable worker cleanup failed.", errors)

    @property
    def ready(self) -> bool:
        return self.started and self.worker.running and self.worker.failure is None

    async def database_readiness(self) -> DatabaseReadiness:
        return await self._foundation.readiness()
