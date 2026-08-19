"""Configuration coverage for the dormant persistence foundation."""

import pytest

from core.config import Settings


def test_persistence_is_disabled_without_a_database_url() -> None:
    settings = Settings()
    assert settings.persistence_enabled is False
    assert settings.database_url is None


def test_persistence_requires_a_database_url_when_enabled() -> None:
    with pytest.raises(ValueError, match="database_url is required"):
        Settings(persistence_enabled=True)


def test_persistence_configuration_is_valid_and_hides_credentials_from_repr() -> None:
    settings = Settings(
        persistence_enabled=True,
        database_url="postgresql+psycopg://super7:secret@db.example/super7",
        lease_duration_seconds=120,
        lease_renewal_interval_seconds=30,
    )
    assert settings.database_pool_size == 2
    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_pool_size", 0),
        ("database_max_overflow", -1),
        ("database_pool_timeout_seconds", 0),
        ("database_pool_recycle_seconds", 0),
        ("worker_poll_interval_seconds", 0),
        ("lease_duration_seconds", 0),
        ("default_max_attempts", 0),
        ("callback_poll_interval_seconds", 0),
    ],
)
def test_persistence_numeric_settings_are_validated(field: str, value: int | float) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_lease_renewal_must_be_less_than_lease_duration() -> None:
    with pytest.raises(ValueError, match="less than"):
        Settings(lease_duration_seconds=30, lease_renewal_interval_seconds=30)


def test_environment_parses_persistence_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSISTENCE_ENABLED", "yes")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://super7:secret@localhost/super7")
    settings = Settings.from_environment()
    assert settings.persistence_enabled is True
    assert settings.database_url is not None


def test_environment_rejects_invalid_persistence_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSISTENCE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="Expected a boolean"):
        Settings.from_environment()
