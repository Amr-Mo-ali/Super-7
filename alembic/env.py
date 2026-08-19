"""Alembic environment for Super-7-owned PostgreSQL objects only."""

from logging.config import fileConfig

import persistence.models  # noqa: F401
from alembic import context
from core.config import Settings
from persistence.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    settings = Settings.from_environment()
    if not settings.persistence_enabled or not settings.database_url:
        raise RuntimeError(
            "Set PERSISTENCE_ENABLED=true and DATABASE_URL before running Alembic migrations."
        )
    return settings.database_url


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    del parent_names
    return type_ != "table" or (name is not None and name.startswith("super7_"))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    from sqlalchemy import engine_from_config

    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=None)
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_name=include_name
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
