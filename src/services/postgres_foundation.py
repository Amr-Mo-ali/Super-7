"""Inactive PostgreSQL lifecycle and migration boundary for Sprint 2 Slice 1."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, Protocol, final

from core.database_config import DatabaseSettings


class DatabaseStartupError(RuntimeError):
    """Raised when PostgreSQL opens but cannot satisfy startup invariants."""


class DatabaseConnectivityUnavailable(RuntimeError):
    """Signal expected operational connectivity unavailability."""


class DatabaseSchemaVersionUnavailable(RuntimeError):
    """Signal expected operational schema-version probe unavailability."""


class DatabasePool(Protocol):
    """Small asynchronous pool surface owned by the parent process."""

    async def open(self, *, timeout_seconds: float) -> None: ...

    async def close(self, *, timeout_seconds: float) -> None: ...


class PoolFactory(Protocol):
    """Create one parent-owned pool from validated settings."""

    def __call__(self, settings: DatabaseSettings) -> DatabasePool: ...


class ConnectivityProbe(Protocol):
    """Adapter-facing bounded connectivity probe."""

    def __call__(
        self,
        pool: DatabasePool,
        *,
        timeout_seconds: float,
    ) -> Awaitable[None]: ...


class SchemaVersionProbe(Protocol):
    """Adapter-facing bounded schema-version probe."""

    def __call__(
        self,
        pool: DatabasePool,
        *,
        timeout_seconds: float,
    ) -> Awaitable[str]: ...


DatabaseReadinessReason = Literal[
    "NOT_STARTED",
    "CLEANUP_PENDING",
    "CLOSED",
    "CONNECTIVITY_UNAVAILABLE",
    "SCHEMA_VERSION_UNAVAILABLE",
    "SCHEMA_VERSION_MISMATCH",
    "READY",
]
_READINESS_MATRIX: Final[Mapping[DatabaseReadinessReason, tuple[bool, bool, bool]]] = (
    MappingProxyType(
        {
            "NOT_STARTED": (False, False, False),
            "CLEANUP_PENDING": (False, False, False),
            "CLOSED": (False, False, False),
            "CONNECTIVITY_UNAVAILABLE": (False, False, False),
            "SCHEMA_VERSION_UNAVAILABLE": (False, True, False),
            "SCHEMA_VERSION_MISMATCH": (False, True, False),
            "READY": (True, True, True),
        }
    )
)
_READINESS_REASONS = frozenset(_READINESS_MATRIX)


@dataclass(frozen=True, slots=True)
class DatabasePublicChecks:
    """Fixed public database checks with no diagnostic extension point."""

    database_connectivity: bool
    database_schema_version: bool

    def __post_init__(self) -> None:
        if type(self.database_connectivity) is not bool:
            raise TypeError("database_connectivity must be a Boolean.")
        if type(self.database_schema_version) is not bool:
            raise TypeError("database_schema_version must be a Boolean.")


@final
@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    """Bounded public database readiness without connection identity or errors."""

    ready: bool
    reason: DatabaseReadinessReason
    public: DatabasePublicChecks

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("DatabaseReadiness cannot be subclassed.")

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise TypeError("ready must be a Boolean.")
        if not isinstance(self.reason, str) or self.reason not in _READINESS_REASONS:
            raise ValueError("reason must be a supported database readiness reason.")
        if not isinstance(self.public, DatabasePublicChecks):
            raise TypeError("public must contain fixed database readiness checks.")
        expected = _READINESS_MATRIX[self.reason]
        actual = (
            self.ready,
            self.public.database_connectivity,
            self.public.database_schema_version,
        )
        if actual != expected:
            raise ValueError("database readiness fields must match the reason.")

    @property
    def affects_liveness(self) -> Literal[False]:
        """Database readiness never changes process liveness."""
        return False


@dataclass(frozen=True, slots=True)
class FoundationMigrationPolicy:
    """Slice 1 permits migration metadata but no Super-7 domain relations."""

    allows_migration_tool_metadata: bool
    allows_domain_relations: bool


FOUNDATION_MIGRATION_POLICY = FoundationMigrationPolicy(
    allows_migration_tool_metadata=True,
    allows_domain_relations=False,
)


class MigrationBackend(Protocol):
    """Operational migration operations supplied by the selected tool adapter."""

    def apply_pending(self) -> None: ...

    def rollback_last(self) -> None: ...

    def current_version(self) -> str | None: ...


class MigrationRunner:
    """Expose only the reviewed operational migration boundary."""

    def __init__(
        self,
        backend: MigrationBackend,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self._logger = logger or logging.getLogger("football_analysis.postgres_migrations")

    def apply_pending(self) -> None:
        try:
            self._backend.apply_pending()
        except Exception as error:
            _log_failure(self._logger, "postgres_migration_apply_failed", error)
            raise
        _log_event(self._logger, "postgres_migration_apply_finished")

    def rollback_last(self) -> None:
        try:
            self._backend.rollback_last()
        except Exception as error:
            _log_failure(self._logger, "postgres_migration_rollback_failed", error)
            raise
        _log_event(self._logger, "postgres_migration_rollback_finished")

    def current_version(self) -> str | None:
        try:
            version = self._backend.current_version()
        except Exception as error:
            _log_failure(self._logger, "postgres_migration_version_failed", error)
            raise
        _log_event(self._logger, "postgres_migration_version_finished")
        return version


class _LifecycleState(Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    STARTED = "STARTED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLOSED = "CLOSED"


class PostgresFoundation:
    """Own one lazy pool; the parent must serialize lifecycle operations."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        pool_factory: PoolFactory,
        connectivity_probe: ConnectivityProbe,
        schema_version_probe: SchemaVersionProbe,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._pool_factory = pool_factory
        self._connectivity_probe = connectivity_probe
        self._schema_version_probe = schema_version_probe
        self._logger = logger or logging.getLogger("football_analysis.postgres_foundation")
        self._pool: DatabasePool | None = None
        self._state = _LifecycleState.NEW

    async def start(self) -> None:
        """Open and verify the parent-owned pool once."""
        if self._state is _LifecycleState.CLOSED:
            raise RuntimeError("PostgreSQL foundation has been closed.")
        if self._state is _LifecycleState.CLEANUP_PENDING:
            raise RuntimeError("PostgreSQL foundation cleanup is pending.")
        if self._state is _LifecycleState.STARTING:
            raise RuntimeError("PostgreSQL lifecycle operations must be serialized.")
        if self._state is _LifecycleState.STARTED:
            return

        self._state = _LifecycleState.STARTING
        try:
            pool = self._pool_factory(self._settings)
        except BaseException:
            self._state = _LifecycleState.NEW
            raise
        self._pool = pool
        try:
            await pool.open(timeout_seconds=self._settings.pool.open_timeout_seconds)
            await self._connectivity_probe(
                pool,
                timeout_seconds=self._settings.pool.acquire_timeout_seconds,
            )
            schema_version = await self._schema_version_probe(
                pool,
                timeout_seconds=self._settings.pool.acquire_timeout_seconds,
            )
            if schema_version != self._settings.required_schema_version:
                raise DatabaseStartupError(
                    "PostgreSQL schema version does not match the required version."
                )
        except BaseException as primary_error:
            try:
                await pool.close(timeout_seconds=self._settings.pool.close_timeout_seconds)
            except BaseException as cleanup_error:
                self._state = _LifecycleState.CLEANUP_PENDING
                if isinstance(cleanup_error, Exception):
                    if isinstance(primary_error, Exception):
                        _log_failure(
                            self._logger,
                            "postgres_foundation_start_failed",
                            primary_error,
                        )
                    _log_failure(
                        self._logger,
                        "postgres_foundation_start_cleanup_failed",
                        cleanup_error,
                    )
                else:
                    raise
            else:
                self._pool = None
                self._state = _LifecycleState.NEW
                if isinstance(primary_error, Exception):
                    _log_failure(
                        self._logger,
                        "postgres_foundation_start_failed",
                        primary_error,
                    )
            raise

        self._state = _LifecycleState.STARTED
        _log_event(self._logger, "postgres_foundation_started")

    async def close(self) -> None:
        """Close the owned pool, retaining it until close succeeds."""
        if self._state is _LifecycleState.CLOSED:
            return
        if self._state is _LifecycleState.STARTING:
            raise RuntimeError("PostgreSQL lifecycle operations must be serialized.")
        if self._state is _LifecycleState.NEW:
            self._state = _LifecycleState.CLOSED
            return

        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgreSQL foundation lost ownership of its pool.")

        try:
            await pool.close(timeout_seconds=self._settings.pool.close_timeout_seconds)
        except BaseException as error:
            self._state = _LifecycleState.CLEANUP_PENDING
            if isinstance(error, Exception):
                _log_failure(self._logger, "postgres_foundation_close_failed", error)
            raise
        self._pool = None
        self._state = _LifecycleState.CLOSED
        _log_event(self._logger, "postgres_foundation_closed")

    async def readiness(self) -> DatabaseReadiness:
        """Probe required database readiness without affecting process liveness."""
        pool = self._pool
        if self._state is _LifecycleState.CLEANUP_PENDING:
            return _readiness(
                False,
                "CLEANUP_PENDING",
                connectivity=False,
                schema=False,
            )
        if self._state is _LifecycleState.CLOSED:
            return _readiness(False, "CLOSED", connectivity=False, schema=False)
        if self._state is not _LifecycleState.STARTED or pool is None:
            return _readiness(False, "NOT_STARTED", connectivity=False, schema=False)

        timeout = self._settings.pool.acquire_timeout_seconds
        try:
            await self._connectivity_probe(pool, timeout_seconds=timeout)
        except DatabaseConnectivityUnavailable as error:
            _log_failure(self._logger, "postgres_readiness_connectivity_failed", error)
            return _readiness(
                False,
                "CONNECTIVITY_UNAVAILABLE",
                connectivity=False,
                schema=False,
            )

        try:
            schema_version = await self._schema_version_probe(pool, timeout_seconds=timeout)
        except DatabaseSchemaVersionUnavailable as error:
            _log_failure(self._logger, "postgres_readiness_schema_probe_failed", error)
            return _readiness(
                False,
                "SCHEMA_VERSION_UNAVAILABLE",
                connectivity=True,
                schema=False,
            )

        if schema_version != self._settings.required_schema_version:
            return _readiness(
                False,
                "SCHEMA_VERSION_MISMATCH",
                connectivity=True,
                schema=False,
            )
        return _readiness(True, "READY", connectivity=True, schema=True)


def _readiness(
    ready: bool,
    reason: DatabaseReadinessReason,
    *,
    connectivity: bool,
    schema: bool,
) -> DatabaseReadiness:
    return DatabaseReadiness(
        ready=ready,
        reason=reason,
        public=DatabasePublicChecks(
            database_connectivity=connectivity,
            database_schema_version=schema,
        ),
    )


def _log_event(logger: logging.Logger, event: str) -> None:
    try:
        logger.info(event)
    except Exception:
        pass


def _log_failure(logger: logging.Logger, event: str, error: Exception) -> None:
    try:
        logger.error("%s error_type=%s", event, type(error).__name__)
    except Exception:
        pass
