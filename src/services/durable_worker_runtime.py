"""Single-process durable worker ownership, independent of HTTP and callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import get_logger
from domain.analysis_job_attempt import RecoveryBatchSize
from services.analysis_job_coordinator import CoordinatorTiming
from services.durable_worker_composition import WorkerConfiguration, build_worker_graph
from services.postgres_foundation import DatabaseReadiness, PostgresFoundation
from services.process_analysis_pool import ProcessAnalysisPool
from services.r2_video_input import configured_r2_video


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
        self._executor = ProcessAnalysisPool(settings)
        graph = build_worker_graph(
            pool=pool,
            database_settings=database_settings,
            child_executor=self._executor,
            configuration=WorkerConfiguration(
                timing,
                recovery_batch_size,
                worker_idle_seconds,
                model_catalog,
                operation_timeout_ms,
                lock_timeout_ms,
            ),
            logger=logger,
            r2_video=configured_r2_video(settings),
        )
        self.coordinator = graph.coordinator
        self.worker = graph.worker
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
