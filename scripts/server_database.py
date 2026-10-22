"""Explicit, one-shot PostgreSQL provisioning and migration for separated roles.

Run only from the server Compose maintenance profile. Never runs on role startup.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from yoyo import default_migration_table, read_migrations  # type: ignore[import-untyped]

from adapters.yoyo_migration_backend import DatabaseMigrationError, YoyoMigrationBackend
from core.database_config import DatabaseSettings

# Administrative DDL cannot bind identifiers. Keep SQL templates constant and
# compose every variable identifier/value with psycopg Identifier/Literal.

MIGRATIONS = "package:adapters:migrations/postgres"
REVISION = "sprint2_slice5_video_integrity"
CATALOG = (
    "sprint2_slice1_foundation",
    "sprint2_slice2_analysis_job",
    "sprint2_slice3_worker_attempt",
    "sprint2_slice4_result_outbox",
    REVISION,
)
TABLES = (
    "analysis_jobs",
    "analysis_job_attempts",
    "analysis_job_terminals",
    "analysis_callback_outbox",
)
METADATA_TABLES = ("_yoyo_migration", "_yoyo_log", "_yoyo_version", "yoyo_lock")
PASSWORD_FILE = Path("/run/secrets/postgres_password")


class ServerDatabaseError(RuntimeError):
    """Safe refusal; never include SQL, credentials, or connection strings."""


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ServerDatabaseError(f"{name} is required.")
    return value


def config() -> dict[str, str]:
    values = {
        name: required(name)
        for name in (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DATABASE",
            "POSTGRES_USER",
            "SUPER7_MIGRATION_DB_USER",
            "SUPER7_MIGRATION_DB_PASSWORD",
            "SUPER7_API_DB_USER",
            "SUPER7_API_DB_PASSWORD",
            "SUPER7_WORKER_DB_USER",
            "SUPER7_WORKER_DB_PASSWORD",
            "SUPER7_DISPATCHER_DB_USER",
            "SUPER7_DISPATCHER_DB_PASSWORD",
        )
    }
    if values["POSTGRES_DATABASE"] in ("postgres", "template0", "template1"):
        raise ServerDatabaseError("A dedicated application database is required.")
    names = [
        values[f"SUPER7_{role}_DB_USER"] for role in ("MIGRATION", "API", "WORKER", "DISPATCHER")
    ]
    if len(set(names + [values["POSTGRES_USER"]])) != 5:
        raise ServerDatabaseError("Administrative and runtime role names must be distinct.")
    if tuple(str(item.id) for item in read_migrations(MIGRATIONS)) != CATALOG:
        raise ServerDatabaseError("Migration catalog differs from the reviewed Slice 1-5 sequence.")
    return values


def connect(
    values: dict[str, str], *, role: str, control: bool = False
) -> psycopg.Connection[tuple[object, ...]]:
    if role == "admin":
        user = values["POSTGRES_USER"]
        password = PASSWORD_FILE.read_text(encoding="utf-8").rstrip("\r\n")
    else:
        user = values[f"SUPER7_{role}_DB_USER"]
        password = values[f"SUPER7_{role}_DB_PASSWORD"]
    return psycopg.connect(
        host=values["POSTGRES_HOST"],
        port=int(values["POSTGRES_PORT"]),
        dbname="postgres" if control else values["POSTGRES_DATABASE"],
        user=user,
        password=password,
        connect_timeout=5,
        autocommit=True,
    )


def provision_fresh(values: dict[str, str]) -> None:
    """Create a new database only when its name and all four role names are unused."""
    roles = ("MIGRATION", "API", "WORKER", "DISPATCHER")
    with connect(values, role="admin", control=True) as connection:
        occupied = connection.execute(
            "SELECT 1 FROM pg_database "
            "WHERE datname NOT IN ('postgres', 'template0', 'template1') LIMIT 1"
        ).fetchone()
        if occupied:
            raise ServerDatabaseError(
                "Cluster already contains an application database; fresh provisioning refused."
            )
        if connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (values["POSTGRES_DATABASE"],)
        ).fetchone():
            raise ServerDatabaseError(
                "Application database already exists; fresh provisioning refused."
            )
        names = [values[f"SUPER7_{role}_DB_USER"] for role in roles]
        if connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)", (names,)
        ).fetchone():
            raise ServerDatabaseError(
                "An application role already exists; fresh provisioning refused."
            )
        for role in roles:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(values[f"SUPER7_{role}_DB_USER"]),
                    sql.Literal(values[f"SUPER7_{role}_DB_PASSWORD"]),
                )
            ).close()
        database = sql.Identifier(values["POSTGRES_DATABASE"])
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                database, sql.Identifier(values["SUPER7_MIGRATION_DB_USER"])
            )
        ).close()
        connection.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database)
        ).close()
        for role in roles:
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    database, sql.Identifier(values[f"SUPER7_{role}_DB_USER"])
                )
            ).close()
    with connect(values, role="admin") as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC").close()
    print("Fresh database and four distinct roles provisioned; schema is not migrated.")


def check_migration_preconditions(values: dict[str, str]) -> None:
    if required("SUPER7_WRITERS_STOPPED") != "1" or required("SUPER7_BACKUP_CONFIRMED") != "1":
        raise ServerDatabaseError("Writer shutdown and database backup must be confirmed.")
    with connect(values, role="admin") as connection:
        roles = tuple(
            values[f"SUPER7_{name}_DB_USER"]
            for name in ("MIGRATION", "API", "WORKER", "DISPATCHER")
        )
        attributes = connection.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
            (list(roles),),
        ).fetchall()
        if len(attributes) != 4 or any(
            row[1:] != (True, False, False, False, False, False) for row in attributes
        ):
            raise ServerDatabaseError("Application roles have unsafe attributes.")
        memberships = connection.execute(
            "SELECT 1 FROM pg_auth_members AS m JOIN pg_roles AS r ON r.oid = m.member "
            "WHERE r.rolname = ANY(%s) LIMIT 1",
            (list(roles),),
        ).fetchone()
        if memberships:
            raise ServerDatabaseError("Application role membership requires separate review.")
        owner = connection.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        if owner != (values["SUPER7_MIGRATION_DB_USER"],):
            raise ServerDatabaseError("Migration role must own the application database.")
        others = connection.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()
        if others != (0,):
            raise ServerDatabaseError("Other database sessions remain; stop all writers first.")
        relations = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT c.relname, pg_get_userbyid(c.relowner) "
                "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
            ).fetchall()
        }
        if set(relations) - set(TABLES) - set(METADATA_TABLES):
            raise ServerDatabaseError("Unknown public relations; migration refused.")
        if any(owner != values["SUPER7_MIGRATION_DB_USER"] for owner in relations.values()):
            raise ServerDatabaseError("Migration role must own the existing public relations.")
        if default_migration_table not in relations:
            if relations:
                raise ServerDatabaseError(
                    "Nonempty schema without migration history; refusing bootstrap."
                )
            return
        applied = {
            row[0]
            for row in connection.execute(
                sql.SQL("SELECT migration_id FROM {}").format(
                    sql.Identifier(default_migration_table)
                )
            ).fetchall()
        }
        if not applied or applied != set(CATALOG[: len(applied)]):
            raise ServerDatabaseError("Migration history is unknown or noncontiguous.")


def migration_settings(values: dict[str, str]) -> DatabaseSettings:
    return DatabaseSettings.from_mapping(
        {
            "POSTGRES_HOST": values["POSTGRES_HOST"],
            "POSTGRES_PORT": values["POSTGRES_PORT"],
            "POSTGRES_DATABASE": values["POSTGRES_DATABASE"],
            "POSTGRES_USER": values["SUPER7_MIGRATION_DB_USER"],
            "POSTGRES_PASSWORD": values["SUPER7_MIGRATION_DB_PASSWORD"],
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": "5",
            "POSTGRES_POOL_MIN_SIZE": "0",
            "POSTGRES_POOL_MAX_SIZE": "1",
            "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "5",
            "POSTGRES_POOL_MAX_WAITERS": "1",
            "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "10",
            "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "10",
            "POSTGRES_REQUIRED_SCHEMA_VERSION": REVISION,
        }
    )


def grant_runtime(values: dict[str, str]) -> None:
    api = sql.Identifier(values["SUPER7_API_DB_USER"])
    worker = sql.Identifier(values["SUPER7_WORKER_DB_USER"])
    dispatcher = sql.Identifier(values["SUPER7_DISPATCHER_DB_USER"])
    with connect(values, role="admin") as connection:
        with connection.transaction():
            # An adopted database may carry earlier grants. Remove both direct
            # and PUBLIC rights before applying the reviewed runtime matrix.
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC").close()
            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC"
            ).close()
            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC"
            ).close()
            for role in (api, worker, dispatcher):
                connection.execute(
                    sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(role)
                ).close()
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
                        role
                    )
                ).close()
                connection.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
                    ).format(role)
                ).close()
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role)
                ).close()
                connection.execute(
                    sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                        sql.Identifier(default_migration_table), role
                    )
                ).close()
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT ON analysis_jobs TO {}").format(api)
            ).close()
            for table in ("analysis_jobs", "analysis_job_attempts"):
                connection.execute(
                    sql.SQL("GRANT SELECT, UPDATE ON {} TO {}").format(
                        sql.Identifier(table), worker
                    )
                ).close()
            connection.execute(
                sql.SQL("GRANT INSERT ON analysis_job_attempts TO {}").format(worker)
            ).close()
            for table in ("analysis_job_terminals", "analysis_callback_outbox"):
                connection.execute(
                    sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(
                        sql.Identifier(table), worker
                    )
                ).close()
            for table in TABLES:
                connection.execute(
                    sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), dispatcher)
                ).close()
            connection.execute(
                sql.SQL(
                    "GRANT UPDATE (delivery_state, next_attempt_at) ON analysis_callback_outbox TO {}"
                ).format(dispatcher)
            ).close()


def migrate(values: dict[str, str]) -> None:
    check_migration_preconditions(values)
    runner = YoyoMigrationBackend(migration_settings(values), (MIGRATIONS,))
    runner.apply_pending()
    if runner.current_version() != REVISION:
        raise ServerDatabaseError("Schema did not reach the required Slice 5 revision.")
    grant_runtime(values)
    verify_grants(values)
    print("Migration revision and distinct runtime grants verified.")


def inspect_version(values: dict[str, str]) -> None:
    """Read server and Yoyo version without applying or creating schema."""
    with connect(values, role="admin") as connection:
        version = connection.execute("SHOW server_version_num").fetchone()
        metadata = connection.execute("SELECT to_regclass('public._yoyo_migration')").fetchone()
        if version is None or not isinstance(version[0], str):
            raise ServerDatabaseError("PostgreSQL version is unavailable.")
        if metadata is None or metadata[0] is None:
            applied: list[str] = []
        else:
            applied = [
                str(row[0])
                for row in connection.execute(
                    sql.SQL("SELECT migration_id FROM {} ORDER BY migration_id").format(
                        sql.Identifier(default_migration_table)
                    )
                ).fetchall()
            ]
    print(
        f"PostgreSQL server_version_num={version[0]}; applied_revisions={','.join(applied) or 'none'}"
    )


def verify_grants(values: dict[str, str]) -> None:
    expected = {
        "API": {"analysis_jobs": "ri"},
        "WORKER": {
            "analysis_jobs": "ru",
            "analysis_job_attempts": "riu",
            "analysis_job_terminals": "ri",
            "analysis_callback_outbox": "ri",
        },
        "DISPATCHER": {table: "r" for table in TABLES},
    }
    for role, role_permissions in expected.items():
        with connect(values, role=role) as connection:
            relations = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT c.relname, c.relkind FROM pg_class AS c "
                    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
                ).fetchall()
            }
            if set(relations) - set(TABLES) - set(METADATA_TABLES):
                raise ServerDatabaseError("Unknown public relation; grants cannot be verified.")
            if not set(TABLES).union({default_migration_table}).issubset(relations):
                raise ServerDatabaseError("Required public relations are missing.")
            schema = connection.execute(
                "SELECT has_schema_privilege(current_user, 'public', 'USAGE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE')"
            ).fetchone()
            if schema != (True, False):
                raise ServerDatabaseError(f"{role} schema grants differ from policy.")
            for table in (name for name, kind in relations.items() if kind != "S"):
                result = connection.execute(
                    "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
                    "has_table_privilege(current_user, %s, 'INSERT'), "
                    "has_table_privilege(current_user, %s, 'UPDATE'), "
                    "has_table_privilege(current_user, %s, 'DELETE'), "
                    "has_table_privilege(current_user, %s, 'TRUNCATE'), "
                    "has_table_privilege(current_user, %s, 'REFERENCES'), "
                    "has_table_privilege(current_user, %s, 'TRIGGER')",
                    (table,) * 7,
                ).fetchone()
                permitted = role_permissions.get(
                    table, "r" if table == default_migration_table else ""
                )
                wanted = (*tuple(letter in permitted for letter in "riud"), False, False, False)
                if result != wanted:
                    raise ServerDatabaseError(f"{role} {table} grants differ from policy.")
                if "r" in permitted:
                    connection.execute(
                        sql.SQL("SELECT 1 FROM {} LIMIT 0").format(sql.Identifier(table))
                    ).close()
            for sequence, kind in relations.items():
                if kind != "S":
                    continue
                rights = connection.execute(
                    "SELECT has_sequence_privilege(current_user, %s, 'USAGE'), "
                    "has_sequence_privilege(current_user, %s, 'SELECT'), "
                    "has_sequence_privilege(current_user, %s, 'UPDATE')",
                    (sequence, sequence, sequence),
                ).fetchone()
                if rights != (False, False, False):
                    raise ServerDatabaseError(f"{role} {sequence} grants differ from policy.")
            columns = connection.execute(
                "SELECT has_column_privilege(current_user, 'analysis_callback_outbox', "
                "'delivery_state', 'UPDATE'), "
                "has_column_privilege(current_user, 'analysis_callback_outbox', "
                "'next_attempt_at', 'UPDATE'), "
                "has_column_privilege(current_user, 'analysis_callback_outbox', "
                "'payload_json', 'UPDATE')"
            ).fetchone()
            if columns != ((True, True, False) if role == "DISPATCHER" else (False, False, False)):
                raise ServerDatabaseError(f"{role} outbox column grants differ from policy.")
            # EXPLAIN validates actual statement permissions without mutating rows.
            statements = (
                ("EXPLAIN INSERT INTO analysis_jobs DEFAULT VALUES", role == "API"),
                ("EXPLAIN UPDATE analysis_jobs SET state = state WHERE FALSE", role == "WORKER"),
                ("EXPLAIN DELETE FROM analysis_jobs WHERE FALSE", False),
                (
                    "EXPLAIN UPDATE analysis_callback_outbox "
                    "SET delivery_state = delivery_state WHERE FALSE",
                    role == "DISPATCHER",
                ),
                (
                    "EXPLAIN UPDATE analysis_callback_outbox "
                    "SET payload_json = payload_json WHERE FALSE",
                    False,
                ),
            )
            for statement, allowed in statements:
                try:
                    connection.execute(statement).close()
                except psycopg.errors.InsufficientPrivilege:
                    if allowed:
                        raise ServerDatabaseError(
                            f"{role} required SQL statement was denied."
                        ) from None
                else:
                    if not allowed:
                        raise ServerDatabaseError(f"{role} forbidden SQL statement was allowed.")
    print("Role table, column and schema permission matrix verified.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("provision-fresh", "inspect-version", "migrate", "verify-grants")
    )
    args = parser.parse_args()
    try:
        values = config()
        if args.command == "provision-fresh":
            provision_fresh(values)
        elif args.command == "inspect-version":
            inspect_version(values)
        elif args.command == "migrate":
            migrate(values)
        else:
            verify_grants(values)
    except (
        ServerDatabaseError,
        DatabaseMigrationError,
        OSError,
        ValueError,
        psycopg.Error,
    ) as error:
        # Avoid driver exception text: connection strings and SQL may contain secrets.
        print(f"Server database operation refused ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
