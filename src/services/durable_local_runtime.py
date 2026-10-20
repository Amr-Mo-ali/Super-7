"""Explicit opt-in, single-process local durable composition; never the default app."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector
from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from adapters.psycopg_analysis_job_repository import PsycopgAnalysisJobAdmissionRepository
from adapters.psycopg_callback_dispatch_ownership import PsycopgCallbackDispatchOwnership
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import RecoveryBatchSize
from services.analysis_callback_delivery import CallbackTransport
from services.analysis_job_coordinator import CoordinatorTiming
from services.cleanup_exception_links import raise_after_cleanup
from services.durable_callback_dispatcher import DurableCallbackDispatcher
from services.durable_local_admission import DurableLocalAdmission
from services.durable_worker_composition import WorkerConfiguration, build_worker_graph
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
        self.admission = DurableLocalAdmission(
            repository=admission,
            protector=HmacIdempotencyLookupProtector(idempotency_hmac_secret),
            bearer_secret=bearer_secret,
            analysis_version=settings.analysis_version,
            attempt_limit=attempt_limit,
            max_queue_size=settings.max_queued_analyses,
        )
        self._executor = ProcessAnalysisPool(settings)
        graph = build_worker_graph(
            pool=self._pool,
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
        )
        self.coordinator = graph.coordinator
        self.worker = graph.worker
        delivery = PsycopgAnalysisCallbackDeliveryRepository(
            self._pool, connection_acquire_timeout_seconds=acquire
        )
        self._dispatch_ownership = PsycopgCallbackDispatchOwnership(database_settings)
        self.dispatcher = DurableCallbackDispatcher(
            repository=delivery,
            transport=callback_transport,
            ownership=self._dispatch_ownership,
            logger=logger,
        )
        self._started = False

    async def start(self) -> None:
        await self._foundation.start()
        try:
            await self._dispatch_ownership.acquire()
            self._executor.start()
            self.worker.start()
            self.dispatcher.start()
        except BaseException as primary:
            try:
                await self.stop()
            except BaseException as cleanup:
                raise_after_cleanup(primary, cleanup)
            raise
        self._started = True

    async def stop(self) -> None:
        """Drain submitted work before closing its executor or the database pool.

        Native executor shutdown(wait=True) can remain unbounded. This does not
        confer a production hard-stop or supervisor termination guarantee.
        """
        errors: list[BaseException] = []
        for operation in (
            self.dispatcher.stop,
            self._dispatch_ownership.close,
            self.worker.stop,
            self._executor.shutdown,
        ):
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
