"""Explicit opt-in, single-process local durable composition; never the default app."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from uuid import uuid4

from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector
from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_repository import PsycopgAnalysisJobAdmissionRepository
from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import RecoveryBatchSize, WorkerSlotId
from services.analysis_callback_delivery import CallbackTransport
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.durable_callback_dispatcher import DurableCallbackDispatcher
from services.durable_local_admission import DurableLocalAdmission
from services.durable_worker_loop import DurableWorkerLoop
from services.postgres_foundation import DatabaseReadiness, PostgresFoundation
from services.process_analysis_pool import ProcessAnalysisPool


class DurableLocalRuntime:
    """Own one PostgreSQL pool, one child pool, and two exclusive local actors."""

    def __init__(
        self,
        *,
        settings: Settings,
        database_settings: DatabaseSettings,
        bearer_secret: str,
        idempotency_hmac_secret: bytes,
        attempt_limit: int,
        timing: CoordinatorTiming,
        recovery_batch_size: RecoveryBatchSize,
        worker_idle_seconds: float,
        model_catalog: Mapping[str, str],
        callback_transport: CallbackTransport,
        logger: logging.Logger,
        operation_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> None:
        self._pool = PsycopgDatabasePool(database_settings)
        self._foundation = PostgresFoundation(
            database_settings,
            pool_factory=lambda _: self._pool,
            connectivity_probe=PostgresConnectivityProbe(),
            schema_version_probe=PostgresSchemaVersionProbe(),
            logger=logger,
        )
        acquire = database_settings.pool.acquire_timeout_seconds
        admission = PsycopgAnalysisJobAdmissionRepository(
            self._pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=operation_timeout_ms,
            admission_lock_timeout_ms=lock_timeout_ms,
        )
        worker_repository = PsycopgAnalysisJobWorkerRepository(
            pool=self._pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=operation_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            lease_id_factory=uuid4,
        )
        finalizer = PsycopgAnalysisJobFinalizationRepository(
            pool=self._pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=operation_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        self.admission = DurableLocalAdmission(
            repository=admission,
            protector=HmacIdempotencyLookupProtector(idempotency_hmac_secret),
            bearer_secret=bearer_secret,
            analysis_version=settings.analysis_version,
            attempt_limit=attempt_limit,
            max_queue_size=settings.max_queued_analyses,
        )
        self._executor = ProcessAnalysisPool(settings)
        self.coordinator = AnalysisJobCoordinator(
            worker_repository=worker_repository,
            finalization_repository=finalizer,
            child_executor=self._executor,
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
        delivery = PsycopgAnalysisCallbackDeliveryRepository(
            self._pool, connection_acquire_timeout_seconds=acquire
        )
        self.dispatcher = DurableCallbackDispatcher(
            repository=delivery,
            transport=callback_transport,
            logger=logger,
        )
        self._started = False

    async def start(self) -> None:
        await self._foundation.start()
        try:
            self._executor.start()
            self.worker.start()
            self.dispatcher.start()
        except BaseException as primary:
            try:
                await self.stop()
            except BaseException as cleanup:
                if isinstance(cleanup, Exception):
                    if cleanup.__context__ is primary:
                        cleanup.__context__ = None
                    raise primary from cleanup
                raise cleanup from primary
            raise
        self._started = True

    async def stop(self) -> None:
        """Drain submitted work before closing its executor or the database pool.

        Native executor shutdown(wait=True) can remain unbounded. This does not
        confer a production hard-stop or supervisor termination guarantee.
        """
        errors: list[BaseException] = []
        for operation in (self.dispatcher.stop, self.worker.stop, self._executor.shutdown):
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
        self._started = False
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Durable runtime cleanup failed.", errors)

    @property
    def ready(self) -> bool:
        return (
            self._started
            and self.worker.running
            and self.worker.failure is None
            and self.dispatcher.running
            and self.dispatcher.failure is None
        )

    async def database_readiness(self) -> DatabaseReadiness:
        return await self._foundation.readiness()
