"""API-only durable ownership: one database foundation and admission repository."""

from __future__ import annotations

from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector
from adapters.psycopg_analysis_job_repository import PsycopgAnalysisJobAdmissionRepository
from adapters.psycopg_database import (
    PostgresConnectivityProbe,
    PostgresSchemaVersionProbe,
    PsycopgDatabasePool,
)
from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import get_logger
from services.durable_local_admission import DurableLocalAdmission
from services.postgres_foundation import DatabaseReadiness, PostgresFoundation


class DurableApiRuntime:
    """Own only the API database pool and durable admission path."""

    def __init__(
        self,
        *,
        settings: Settings,
        database_settings: DatabaseSettings,
        bearer_secret: str,
        idempotency_hmac_secret: bytes,
        attempt_limit: int,
        operation_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> None:
        pool = PsycopgDatabasePool(database_settings)
        self._foundation = PostgresFoundation(
            database_settings,
            pool_factory=lambda _: pool,
            connectivity_probe=PostgresConnectivityProbe(),
            schema_version_probe=PostgresSchemaVersionProbe(),
            logger=get_logger("football_analysis.durable_api"),
        )
        repository = PsycopgAnalysisJobAdmissionRepository(
            pool,
            connection_acquire_timeout_seconds=database_settings.pool.acquire_timeout_seconds,
            operation_timeout_ms=operation_timeout_ms,
            admission_lock_timeout_ms=lock_timeout_ms,
        )
        self.admission = DurableLocalAdmission(
            repository=repository,
            protector=HmacIdempotencyLookupProtector(idempotency_hmac_secret),
            bearer_secret=bearer_secret,
            analysis_version=settings.analysis_version,
            attempt_limit=attempt_limit,
            max_queue_size=settings.max_queued_analyses,
        )
        self.started = False

    async def start(self) -> None:
        await self._foundation.start()
        self.started = True

    async def stop(self) -> None:
        self.started = False
        await self._foundation.close()

    async def database_readiness(self) -> DatabaseReadiness:
        return await self._foundation.readiness()
