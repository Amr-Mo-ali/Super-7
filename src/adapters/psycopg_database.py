"""Concrete Psycopg adapters for the inactive PostgreSQL foundation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast

from psycopg import InterfaceError, OperationalError
from psycopg_pool import AsyncConnectionPool

from adapters.yoyo_migration_backend import (
    _yoyo_metadata_compatibility,
    _YoyoMetadataCompatibility,
)
from core.database_config import DatabaseSettings
from services.postgres_foundation import (
    DatabaseConnectivityUnavailable,
    DatabasePool,
    DatabaseSchemaVersionUnavailable,
)

__all__ = [
    "PostgresConnectivityProbe",
    "PostgresSchemaVersionProbe",
    "PsycopgDatabasePool",
    "PsycopgPoolFactory",
]

_CONNECTIVITY_QUERY = "SELECT 1"
_CONNECTIVITY_MESSAGE = "PostgreSQL connectivity is unavailable."
_SCHEMA_VERSION_MESSAGE = "PostgreSQL migration metadata or version is unavailable."


class _QueryResult(Protocol):
    async def fetchall(self) -> list[tuple[object, ...]]: ...

    async def close(self) -> None: ...


class _Connection(Protocol):
    async def execute(self, query: str) -> _QueryResult: ...


class _DriverPool(Protocol):
    async def open(self, wait: bool = False, timeout: float = 30.0) -> None: ...

    async def close(self, timeout: float = 5.0) -> None: ...

    def connection(
        self,
        timeout: float | None = None,
    ) -> AbstractAsyncContextManager[_Connection]: ...


PoolBuilder = Callable[..., _DriverPool]
_DEFAULT_POOL_BUILDER = cast(PoolBuilder, AsyncConnectionPool)


class PsycopgDatabasePool:
    """Adapt one lazy ``AsyncConnectionPool`` to the foundation pool port."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        pool_builder: PoolBuilder = _DEFAULT_POOL_BUILDER,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("football_analysis.postgres_adapter")
        self._pool = pool_builder(
            "",
            open=False,
            min_size=settings.pool.min_size,
            max_size=settings.pool.max_size,
            timeout=settings.pool.acquire_timeout_seconds,
            max_waiting=settings.pool.max_waiters,
            kwargs={
                "host": settings._host,
                "port": settings._port,
                "dbname": settings._database,
                "user": settings._user,
                "password": settings._password,
                "connect_timeout": settings.connect_timeout_seconds,
            },
        )

    async def open(self, *, timeout_seconds: float) -> None:
        error_type: str | None = None
        try:
            await self._pool.open(wait=True, timeout=timeout_seconds)
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            _log_failure(self._logger, "postgres_pool_open_failed", error_type)
            raise DatabaseConnectivityUnavailable(_CONNECTIVITY_MESSAGE)

    async def close(self, *, timeout_seconds: float) -> None:
        error_type: str | None = None
        try:
            await self._pool.close(timeout=timeout_seconds)
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            _log_failure(self._logger, "postgres_pool_close_failed", error_type)
            raise DatabaseConnectivityUnavailable(_CONNECTIVITY_MESSAGE)

    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_Connection]:
        return self._pool.connection(timeout=timeout_seconds)


class PsycopgPoolFactory:
    """Create distinct lazy concrete pools from validated settings."""

    def __init__(
        self,
        *,
        pool_builder: PoolBuilder = _DEFAULT_POOL_BUILDER,
        logger: logging.Logger | None = None,
    ) -> None:
        self._pool_builder = pool_builder
        self._logger = logger

    def __call__(self, settings: DatabaseSettings) -> DatabasePool:
        return PsycopgDatabasePool(
            settings,
            pool_builder=self._pool_builder,
            logger=self._logger,
        )


class PostgresConnectivityProbe:
    """Execute one bounded read-only connectivity operation."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("football_analysis.postgres_adapter")

    async def __call__(
        self,
        pool: DatabasePool,
        *,
        timeout_seconds: float,
    ) -> None:
        concrete_pool = cast(PsycopgDatabasePool, pool)
        error_type: str | None = None
        try:
            async with concrete_pool.connection(timeout_seconds=timeout_seconds) as connection:
                result = await connection.execute(_CONNECTIVITY_QUERY)
                await result.close()
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            _log_failure(self._logger, "postgres_connectivity_probe_failed", error_type)
            raise DatabaseConnectivityUnavailable(_CONNECTIVITY_MESSAGE)


class PostgresSchemaVersionProbe:
    """Read the latest application migration revision through a bounded pool."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        metadata_compatibility: _YoyoMetadataCompatibility | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("football_analysis.postgres_adapter")
        self._metadata_compatibility = metadata_compatibility or _yoyo_metadata_compatibility()

    async def __call__(
        self,
        pool: DatabasePool,
        *,
        timeout_seconds: float,
    ) -> str:
        concrete_pool = cast(PsycopgDatabasePool, pool)
        version: str | None = None
        error_type: str | None = None
        connectivity_failure = False
        phase = "acquire"
        try:
            async with concrete_pool.connection(timeout_seconds=timeout_seconds) as connection:
                phase = "query"
                result = await connection.execute(
                    self._metadata_compatibility.select_applied_revisions_query
                )
                rows = await _fetchall_and_close(result)
                phase = "release"
            phase = "metadata"
            version = self._metadata_compatibility.current_version(rows)
        except Exception as error:
            error_type = type(error).__name__
            connectivity_failure = phase in {"acquire", "release"} or _is_connectivity_error(error)

        if error_type is not None:
            if connectivity_failure:
                _log_failure(self._logger, "postgres_schema_connectivity_failed", error_type)
                raise DatabaseConnectivityUnavailable(_CONNECTIVITY_MESSAGE)
            _log_failure(self._logger, "postgres_schema_version_probe_failed", error_type)
            raise DatabaseSchemaVersionUnavailable(_SCHEMA_VERSION_MESSAGE)

        if version is None:
            raise DatabaseSchemaVersionUnavailable(_SCHEMA_VERSION_MESSAGE)
        return version


async def _fetchall_and_close(result: _QueryResult) -> list[tuple[object, ...]]:
    try:
        rows = await result.fetchall()
    except BaseException:
        try:
            await result.close()
        except Exception:
            pass
        raise
    await result.close()
    return rows


def _is_connectivity_error(error: Exception) -> bool:
    if isinstance(error, (InterfaceError, OperationalError)):
        return True
    sqlstate = getattr(error, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate.startswith("08")


def _log_failure(logger: logging.Logger, event: str, error_type: str) -> None:
    try:
        logger.error("%s error_type=%s", event, error_type)
    except Exception:
        pass
