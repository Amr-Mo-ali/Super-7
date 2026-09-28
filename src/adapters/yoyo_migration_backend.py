"""Concrete Yoyo adapter for the inactive Slice 1 migration boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlunsplit

from yoyo import (  # type: ignore[import-untyped]
    __version__ as yoyo_version,
)
from yoyo import (
    default_migration_table,
    get_backend,
    read_migrations,
)

from core.database_config import DatabaseSettings

__all__ = ["DatabaseMigrationError", "YoyoMigrationBackend"]

_PACKAGED_MIGRATION_SOURCE = "package:adapters:migrations/postgres"
_MIGRATION_MESSAGE = "PostgreSQL migration operation failed."
_COMPATIBLE_YOYO_VERSION = "9.0.0"
_MIGRATION_ID_COLUMN = "migration_id"
_SELECT_APPLIED_REVISIONS = f'SELECT "{_MIGRATION_ID_COLUMN}" FROM "{default_migration_table}"'


class DatabaseMigrationError(RuntimeError):
    """Safe public classification for an ordinary migration-tool failure."""


@dataclass(frozen=True, slots=True)
class _YoyoMetadataCompatibility:
    """Isolate the exact Yoyo 9.0.0 metadata schema used by async readiness."""

    select_applied_revisions_query: str = field(repr=False)
    _catalog: tuple[str, ...] = field(repr=False)

    def current_version(self, rows: Sequence[tuple[object, ...]]) -> str | None:
        applied: set[str] = set()
        for row in rows:
            if len(row) != 1 or not isinstance(row[0], str) or not row[0]:
                raise DatabaseMigrationError(_MIGRATION_MESSAGE)
            applied.add(row[0])

        if not applied:
            return None
        if not applied.issubset(self._catalog):
            # A non-empty value keeps mismatch policy in PostgresFoundation
            # without exposing an unknown private metadata value.
            return "UNRECOGNIZED_APPLIED_REVISION"
        for revision in reversed(self._catalog):
            if revision in applied:
                return revision
        raise DatabaseMigrationError(_MIGRATION_MESSAGE)


def _yoyo_metadata_compatibility(
    migration_sources: Sequence[str | Path] = (_PACKAGED_MIGRATION_SOURCE,),
    *,
    migration_reader: Callable[..., Any] | None = None,
) -> _YoyoMetadataCompatibility:
    if yoyo_version != _COMPATIBLE_YOYO_VERSION:
        raise DatabaseMigrationError(_MIGRATION_MESSAGE)
    reader = migration_reader or read_migrations
    migrations = reader(*(str(source) for source in migration_sources))
    catalog = tuple(str(migration.id) for migration in migrations)
    if not catalog or len(catalog) != len(set(catalog)) or any(not item for item in catalog):
        raise DatabaseMigrationError(_MIGRATION_MESSAGE)
    return _YoyoMetadataCompatibility(
        select_applied_revisions_query=_SELECT_APPLIED_REVISIONS,
        _catalog=catalog,
    )


class YoyoMigrationBackend:
    """Delegate the approved migration operations to Yoyo public APIs."""

    def __init__(
        self,
        settings: DatabaseSettings,
        migration_sources: Sequence[str | Path] = (_PACKAGED_MIGRATION_SOURCE,),
        *,
        backend_factory: Callable[[str], Any] | None = None,
        migration_reader: Callable[..., Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._database_uri = _database_uri(settings)
        self._migration_sources = tuple(str(source) for source in migration_sources)
        self._backend_factory = backend_factory or get_backend
        self._migration_reader = migration_reader or read_migrations
        self._logger = logger or logging.getLogger("football_analysis.postgres_migrations")
        self._backend: Any | None = None
        self._migrations: Any | None = None

    def apply_pending(self) -> None:
        error_type: str | None = None
        try:
            backend, migrations = self._resources()
            backend.apply_migrations(backend.to_apply(migrations))
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            self._raise_migration_error("postgres_migration_apply_failed", error_type)

    def rollback_last(self) -> None:
        error_type: str | None = None
        try:
            backend, migrations = self._resources()
            to_rollback = backend.to_rollback(migrations)
            if to_rollback:
                backend.rollback_migrations(to_rollback[:1])
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            self._raise_migration_error("postgres_migration_rollback_failed", error_type)

    def current_version(self) -> str | None:
        error_type: str | None = None
        version: str | None = None
        try:
            backend, migrations = self._resources()
            for migration in reversed(list(migrations)):
                if backend.is_applied(migration):
                    version = str(migration.id)
                    break
        except Exception as error:
            error_type = type(error).__name__
        if error_type is not None:
            self._raise_migration_error("postgres_migration_version_failed", error_type)
        return version

    def _resources(self) -> tuple[Any, Any]:
        if self._backend is None:
            self._backend = self._backend_factory(self._database_uri)
        if self._migrations is None:
            self._migrations = self._migration_reader(*self._migration_sources)
        return self._backend, self._migrations

    def _raise_migration_error(self, event: str, error_type: str) -> None:
        _log_failure(self._logger, event, error_type)
        raise DatabaseMigrationError(_MIGRATION_MESSAGE)


def _database_uri(settings: DatabaseSettings) -> str:
    host = settings._host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = (
        f"{quote(settings._user, safe='')}:{quote(settings._password, safe='')}@"
        f"{host}:{settings._port}"
    )
    query = urlencode({"connect_timeout": settings.connect_timeout_seconds})
    return urlunsplit(
        (
            "postgresql+psycopg",
            netloc,
            f"/{settings._database}",
            query,
            "",
        )
    )


def _log_failure(logger: logging.Logger, event: str, error_type: str) -> None:
    try:
        logger.error("%s error_type=%s", event, error_type)
    except Exception:
        pass
