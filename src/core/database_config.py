"""Typed, secret-safe PostgreSQL configuration for the durability foundation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite


class DatabaseConfigurationError(ValueError):
    """Raised when required PostgreSQL configuration is missing or invalid."""


def _required_text(
    values: Mapping[str, str],
    name: str,
    *,
    preserve_whitespace: bool = False,
) -> str:
    try:
        value = values[name]
    except KeyError:
        raise DatabaseConfigurationError(f"{name} is required.") from None

    if not isinstance(value, str) or not value.strip():
        raise DatabaseConfigurationError(f"{name} must be a non-empty string.")
    return value if preserve_whitespace else value.strip()


def _required_int(values: Mapping[str, str], name: str) -> int:
    value = _required_text(values, name)
    try:
        parsed = int(value)
    except ValueError:
        pass
    else:
        return parsed
    raise DatabaseConfigurationError(f"{name} must be an integer.")


def _required_float(values: Mapping[str, str], name: str) -> float:
    value = _required_text(values, name)
    try:
        parsed = float(value)
    except ValueError:
        pass
    else:
        return parsed
    raise DatabaseConfigurationError(f"{name} must be a number.")


def _require_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseConfigurationError(f"{name} must be an integer.")


def _require_finite_positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatabaseConfigurationError(f"{name} must be a number.")
    if not isfinite(value) or value <= 0:
        raise DatabaseConfigurationError(f"{name} must be finite and strictly positive.")


@dataclass(frozen=True, slots=True)
class DatabasePoolSettings:
    """Validated bounds for the future parent-owned PostgreSQL pool."""

    min_size: int
    max_size: int
    acquire_timeout_seconds: float
    max_waiters: int
    open_timeout_seconds: float
    close_timeout_seconds: float

    def __post_init__(self) -> None:
        _require_int(self.min_size, "POSTGRES_POOL_MIN_SIZE")
        _require_int(self.max_size, "POSTGRES_POOL_MAX_SIZE")
        _require_int(self.max_waiters, "POSTGRES_POOL_MAX_WAITERS")

        if self.min_size < 0:
            raise DatabaseConfigurationError("POSTGRES_POOL_MIN_SIZE must be non-negative.")
        if self.max_size <= 0:
            raise DatabaseConfigurationError("POSTGRES_POOL_MAX_SIZE must be strictly positive.")
        if self.min_size > self.max_size:
            raise DatabaseConfigurationError(
                "POSTGRES_POOL_MIN_SIZE must not exceed POSTGRES_POOL_MAX_SIZE."
            )
        if self.max_waiters <= 0:
            raise DatabaseConfigurationError("POSTGRES_POOL_MAX_WAITERS must be strictly positive.")

        _require_finite_positive(
            self.acquire_timeout_seconds,
            "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS",
        )
        _require_finite_positive(
            self.open_timeout_seconds,
            "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS",
        )
        _require_finite_positive(
            self.close_timeout_seconds,
            "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS",
        )


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Required PostgreSQL settings without public credential serialization."""

    _host: str = field(repr=False)
    _port: int = field(repr=False)
    _database: str = field(repr=False)
    _user: str = field(repr=False)
    _password: str = field(repr=False)
    connect_timeout_seconds: int
    pool: DatabasePoolSettings
    required_schema_version: str

    def __post_init__(self) -> None:
        for value, name in (
            (self._host, "POSTGRES_HOST"),
            (self._database, "POSTGRES_DATABASE"),
            (self._user, "POSTGRES_USER"),
            (self._password, "POSTGRES_PASSWORD"),
            (self.required_schema_version, "POSTGRES_REQUIRED_SCHEMA_VERSION"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise DatabaseConfigurationError(f"{name} must be a non-empty string.")

        _require_int(self._port, "POSTGRES_PORT")
        if not 1 <= self._port <= 65535:
            raise DatabaseConfigurationError("POSTGRES_PORT must be between 1 and 65535.")
        _require_int(self.connect_timeout_seconds, "POSTGRES_CONNECT_TIMEOUT_SECONDS")
        if self.connect_timeout_seconds < 2:
            raise DatabaseConfigurationError(
                "POSTGRES_CONNECT_TIMEOUT_SECONDS must be an integer of at least 2 seconds."
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DatabaseSettings:
        """Parse all required values without reading or mutating process environment."""
        pool = DatabasePoolSettings(
            min_size=_required_int(values, "POSTGRES_POOL_MIN_SIZE"),
            max_size=_required_int(values, "POSTGRES_POOL_MAX_SIZE"),
            acquire_timeout_seconds=_required_float(
                values,
                "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS",
            ),
            max_waiters=_required_int(values, "POSTGRES_POOL_MAX_WAITERS"),
            open_timeout_seconds=_required_float(
                values,
                "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS",
            ),
            close_timeout_seconds=_required_float(
                values,
                "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS",
            ),
        )
        return cls(
            _host=_required_text(values, "POSTGRES_HOST"),
            _port=_required_int(values, "POSTGRES_PORT"),
            _database=_required_text(values, "POSTGRES_DATABASE"),
            _user=_required_text(values, "POSTGRES_USER"),
            _password=_required_text(
                values,
                "POSTGRES_PASSWORD",
                preserve_whitespace=True,
            ),
            connect_timeout_seconds=_required_int(
                values,
                "POSTGRES_CONNECT_TIMEOUT_SECONDS",
            ),
            pool=pool,
            required_schema_version=_required_text(
                values,
                "POSTGRES_REQUIRED_SCHEMA_VERSION",
            ),
        )

    def safe_diagnostics(self) -> dict[str, object]:
        """Return bounded operational metadata without connection identity or secrets."""
        return {
            "configured": True,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "pool": {
                "min_size": self.pool.min_size,
                "max_size": self.pool.max_size,
                "acquire_timeout_seconds": self.pool.acquire_timeout_seconds,
                "max_waiters": self.pool.max_waiters,
                "open_timeout_seconds": self.pool.open_timeout_seconds,
                "close_timeout_seconds": self.pool.close_timeout_seconds,
            },
            "required_schema_version": self.required_schema_version,
        }
