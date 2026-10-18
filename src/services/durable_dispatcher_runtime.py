"""Single-process durable callback dispatcher ownership, without HTTP listener."""

from __future__ import annotations

from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.database_config import DatabaseSettings
from core.logging import get_logger
from services.analysis_callback_delivery import CallbackTransport
from services.durable_callback_dispatcher import DurableCallbackDispatcher
from services.postgres_foundation import DatabaseReadiness, PostgresFoundation


class DurableDispatcherRuntime:
    """Own one DB pool and exactly one local delivery actor."""

    def __init__(
        self, *, database_settings: DatabaseSettings, callback_transport: CallbackTransport
    ) -> None:
        logger = get_logger("football_analysis.durable_dispatcher")
        pool = PsycopgDatabasePool(database_settings)
        self._foundation = PostgresFoundation(
            database_settings,
            pool_factory=lambda _: pool,
            connectivity_probe=PostgresConnectivityProbe(),
            schema_version_probe=PostgresSchemaVersionProbe(),
            logger=logger,
        )
        delivery = PsycopgAnalysisCallbackDeliveryRepository(
            pool,
            connection_acquire_timeout_seconds=database_settings.pool.acquire_timeout_seconds,
        )
        self.dispatcher = DurableCallbackDispatcher(
            repository=delivery, transport=callback_transport, logger=logger
        )
        self.started = False

    async def start(self) -> None:
        await self._foundation.start()
        try:
            self.dispatcher.start()
        except BaseException as primary:
            try:
                await self.stop()
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        self.started = True

    async def stop(self) -> None:
        self.started = False
        error: BaseException | None = None
        try:
            await self.dispatcher.stop()
        except BaseException as caught:
            error = caught
        try:
            await self._foundation.close()
        except BaseException as cleanup:
            if error is not None:
                raise BaseExceptionGroup(
                    "Durable dispatcher cleanup failed.", [error, cleanup]
                ) from cleanup
            raise
        if error is not None:
            raise error

    @property
    def ready(self) -> bool:
        return self.started and self.dispatcher.running and self.dispatcher.failure is None

    async def database_readiness(self) -> DatabaseReadiness:
        return await self._foundation.readiness()
