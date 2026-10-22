"""Opt-in PostgreSQL evidence for adopted server role grants."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from test_postgres_analysis_job_worker_integration import (
    _disposable_factory,
    _prepare_database,
)

from core.database_config import DatabaseSettings
from scripts import server_database


def _connect(settings: DatabaseSettings) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=settings._host,
        port=settings._port,
        dbname=settings._database,
        user=settings._user,
        password=settings._password,
        autocommit=True,
    )


def test_adopted_database_overgrants_are_removed(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        admin = database.inspection_settings
        migration = database.migration_settings
        worker = database.runtime_settings
        # Identifier and literal metacharacters must remain data throughout DDL.
        api_user = 'server_api"; DROP TABLE analysis_jobs;--'
        dispatcher_user = "server_dispatch_security_probe"
        api_password = "synthetic-api' OR '1'='1"
        dispatcher_password = "synthetic-dispatch-security-probe"
        password_file = tmp_path / "postgres-password"
        password_file.write_text(admin._password, encoding="utf-8")
        monkeypatch.setattr(server_database, "PASSWORD_FILE", password_file)
        values = {
            "POSTGRES_HOST": admin._host,
            "POSTGRES_PORT": str(admin._port),
            "POSTGRES_DATABASE": admin._database,
            "POSTGRES_USER": admin._user,
            "SUPER7_MIGRATION_DB_USER": migration._user,
            "SUPER7_MIGRATION_DB_PASSWORD": migration._password,
            "SUPER7_API_DB_USER": api_user,
            "SUPER7_API_DB_PASSWORD": api_password,
            "SUPER7_WORKER_DB_USER": worker._user,
            "SUPER7_WORKER_DB_PASSWORD": worker._password,
            "SUPER7_DISPATCHER_DB_USER": dispatcher_user,
            "SUPER7_DISPATCHER_DB_PASSWORD": dispatcher_password,
        }
        with _connect(admin) as connection:
            for user, password in (
                (api_user, api_password),
                (dispatcher_user, dispatcher_password),
            ):
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(user), sql.Literal(password)
                    )
                ).close()
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(admin._database), sql.Identifier(user)
                    )
                ).close()
            connection.execute(
                sql.SQL("GRANT TRUNCATE, REFERENCES, TRIGGER ON analysis_jobs TO {}").format(
                    sql.Identifier(api_user)
                )
            ).close()
            connection.execute("GRANT TRUNCATE ON analysis_jobs TO PUBLIC").close()
        with pytest.raises(server_database.ServerDatabaseError, match="grants differ"):
            server_database.verify_grants(values)
        server_database.grant_runtime(values)
        server_database.verify_grants(values)
        with _connect(admin) as connection:
            metadata = connection.execute(
                "SELECT relname FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname IN ('_yoyo_log', '_yoyo_version') "
                "AND c.relkind = 'r' LIMIT 1"
            ).fetchone()
            assert metadata is not None
            connection.execute(
                sql.SQL("GRANT SELECT ON {} TO PUBLIC").format(sql.Identifier(str(metadata[0])))
            ).close()
        with pytest.raises(server_database.ServerDatabaseError, match="grants differ"):
            server_database.verify_grants(values)
        server_database.grant_runtime(values)
        server_database.verify_grants(values)
        with _connect(admin) as connection:
            privileges = connection.execute(
                "SELECT has_table_privilege(%s, 'analysis_jobs', 'TRUNCATE'), "
                "has_table_privilege(%s, 'analysis_jobs', 'REFERENCES'), "
                "has_table_privilege(%s, 'analysis_jobs', 'TRIGGER')",
                (api_user, api_user, api_user),
            ).fetchone()
            relation = connection.execute("SELECT to_regclass('public.analysis_jobs')").fetchone()
        assert privileges == (False, False, False)
        assert relation == ("analysis_jobs",)
    assert factory.active_count == 0
