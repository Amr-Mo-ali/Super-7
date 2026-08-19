"""Lazy-engine tests that never contact PostgreSQL."""

from core.config import Settings
from persistence.engine import (
    create_persistence_engine,
    create_session_factory,
    dispose_persistence_engine,
    safe_database_url,
)


def _settings() -> Settings:
    return Settings(
        persistence_enabled=True,
        database_url="postgresql+psycopg://super7:secret@localhost:5432/super7",
    )


def test_engine_construction_is_lazy_and_url_logging_is_secret_safe() -> None:
    engine = create_persistence_engine(_settings())
    try:
        assert engine.pool.size() == 2
        assert "secret" not in safe_database_url(str(engine.url))
        session_factory = create_session_factory(engine)
        assert session_factory.kw["expire_on_commit"] is False
    finally:
        dispose_persistence_engine(engine)


def test_engine_requires_enabled_persistence() -> None:
    try:
        create_persistence_engine(Settings())
    except ValueError as error:
        assert "Persistence is disabled" in str(error)
    else:
        raise AssertionError("disabled persistence unexpectedly constructed an engine")
