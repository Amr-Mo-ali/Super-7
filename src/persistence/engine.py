"""Lazy SQLAlchemy engine construction for future persistence roles."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from core.config import Settings


def safe_database_url(database_url: str) -> str:
    """Return a log-safe URL that never includes a password."""
    return make_url(database_url).render_as_string(hide_password=True)


def create_persistence_engine(settings: Settings) -> Engine:
    """Construct, but do not connect, a bounded PostgreSQL engine."""
    if not settings.persistence_enabled or not settings.database_url:
        raise ValueError("Persistence is disabled or DATABASE_URL is not configured.")
    return create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=settings.database_pool_recycle_seconds,
        pool_pre_ping=True,
        echo=settings.database_sql_echo,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a future repository session factory without opening a connection."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def dispose_persistence_engine(engine: Engine) -> None:
    """Explicitly release pooled connections during future role shutdown."""
    engine.dispose()
