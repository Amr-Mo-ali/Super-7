"""One-time, owner-invoked persistent PostgreSQL setup for the opt-in local app.

This is deliberately separate from application startup and the disposable pytest
fixture. It never removes a container, volume, database, or credential file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from yoyo import read_migrations  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adapters.yoyo_migration_backend import YoyoMigrationBackend  # noqa: E402
from core.database_config import DatabaseSettings  # noqa: E402

IMAGE = (
    "postgres:16.15-bookworm@"
    "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)
IMAGE_ID = "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
REPO_DIGEST = "postgres@" + IMAGE_ID
CONTAINER = "super7-durable-local-pg-20261015"
VOLUME = "super7-durable-local-pgdata-20261015"
LABEL = "com.super7.durable-local.task"
PORT = 55432
SECRET_DIR = ROOT / ".local-durable"
CONFIG = SECRET_DIR / "config.json"
BOOTSTRAP_PASSWORD = SECRET_DIR / "postgres-bootstrap-password.secret"
PASSWORD_TARGET = "/run/secrets/super7_local_postgres_password"
MIGRATIONS = "package:adapters:migrations/postgres"
REVISION = "sprint2_slice4_result_outbox"
CATALOG = (
    "sprint2_slice1_foundation",
    "sprint2_slice2_analysis_job",
    "sprint2_slice3_worker_attempt",
    REVISION,
)


class SetupError(RuntimeError):
    """A secret-free local setup failure."""


def _docker(*args: str) -> str:
    try:
        result = subprocess.run(
            ("docker", *args), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("Docker is unavailable to this terminal.") from error
    if result.returncode:
        raise SetupError("Docker operation failed; inspect it in the owner terminal.")
    return result.stdout.strip()


def _verify_image() -> None:
    try:
        image = json.loads(_docker("image", "inspect", IMAGE, "--format", "{{json .}}"))
    except (ValueError, TypeError) as error:
        raise SetupError("Pinned local PostgreSQL image inspection failed.") from error
    if not (
        image.get("Id") == IMAGE_ID
        and image.get("Os") == "linux"
        and image.get("Architecture") == "amd64"
        and REPO_DIGEST in image.get("RepoDigests", [])
    ):
        raise SetupError("The local PostgreSQL image does not match the approved digest.")


def _assert_no_collisions() -> None:
    if SECRET_DIR.exists():
        raise SetupError("The local credential directory already exists; refusing to adopt it.")
    containers = _docker("ps", "--all", "--format", "{{.Names}}").splitlines()
    volumes = _docker("volume", "ls", "--format", "{{.Name}}").splitlines()
    if CONTAINER in containers or VOLUME in volumes:
        raise SetupError("The named local PostgreSQL container or volume already exists.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as error:
            raise SetupError("The intended loopback PostgreSQL port is occupied.") from error


def _assert_secret_paths_ignored() -> None:
    for path in (CONFIG, BOOTSTRAP_PASSWORD):
        result = subprocess.run(
            ("git", "check-ignore", "--quiet", "--", str(path)),
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SetupError("Local credential paths must be Git-ignored before creation.")


def _create_private_file(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)


def _new_config() -> dict[str, Any]:
    suffix = secrets.token_hex(5)
    return {
        "image": IMAGE,
        "container": CONTAINER,
        "volume": VOLUME,
        "ownership_label": secrets.token_hex(16),
        "port": PORT,
        "database": f"s7local_{suffix}",
        "bootstrap_user": f"s7local_admin_{suffix}",
        "bootstrap_password": secrets.token_urlsafe(36),
        "migration_user": f"s7local_migrate_{suffix}",
        "migration_password": secrets.token_urlsafe(36),
        "runtime_user": f"s7local_runtime_{suffix}",
        "runtime_password": secrets.token_urlsafe(36),
        "apex_bearer_secret": secrets.token_urlsafe(36),
        "idempotency_hmac_secret_hex": secrets.token_hex(32),
        "video_root": str(ROOT / "dataset" / "raw" / "social_media"),
        "model_path": str(ROOT / "models" / "yolo11n.pt"),
        "ball_model_path": str(ROOT / "models" / "yolo11n.pt"),
        "model_id": "local_yolo11n_pair_bytetrack_v1",
    }


def _load_config() -> dict[str, Any]:
    try:
        config: dict[str, Any] = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SetupError("Local durable configuration is unavailable.") from error
    if (
        config.get("image") != IMAGE
        or config.get("container") != CONTAINER
        or config.get("volume") != VOLUME
        or config.get("port") != PORT
        or not isinstance(config.get("ownership_label"), str)
    ):
        raise SetupError("Local durable configuration identity does not match this setup.")
    return config


def _container_state(config: dict[str, Any]) -> bool:
    try:
        details = json.loads(_docker("inspect", CONTAINER, "--format", "{{json .}}"))
        bindings = details["HostConfig"]["PortBindings"]["5432/tcp"]
        mounts = details["Mounts"]
        valid = (
            details["Name"] == "/" + CONTAINER
            and re.fullmatch(r"[0-9a-f]{64}", details["Id"]) is not None
            and details["Image"] == IMAGE_ID
            and details["Config"]["Image"] == IMAGE
            and details["Config"]["Labels"].get(LABEL) == config["ownership_label"]
            and details["HostConfig"]["AutoRemove"] is False
            and details["HostConfig"]["RestartPolicy"]["Name"] == "no"
            and bindings == [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}]
            and any(
                mount.get("Type") == "volume"
                and mount.get("Name") == VOLUME
                and mount.get("Destination") == "/var/lib/postgresql/data"
                for mount in mounts
            )
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SetupError("Local PostgreSQL container inspection failed.") from error
    if not valid:
        raise SetupError("Local PostgreSQL container ownership or settings changed.")
    return bool(details["State"]["Running"])


def _connect(config: dict[str, Any], *, user: str, password: str, database: str) -> Any:
    return psycopg.connect(
        host="127.0.0.1",
        port=PORT,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=2,
        autocommit=True,
    )


def _settings(config: dict[str, Any], *, migration: bool) -> DatabaseSettings:
    role = "migration" if migration else "runtime"
    return DatabaseSettings.from_mapping(
        {
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(PORT),
            "POSTGRES_DATABASE": config["database"],
            "POSTGRES_USER": config[f"{role}_user"],
            "POSTGRES_PASSWORD": config[f"{role}_password"],
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": "2",
            "POSTGRES_POOL_MIN_SIZE": "0",
            "POSTGRES_POOL_MAX_SIZE": "4",
            "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "5",
            "POSTGRES_POOL_MAX_WAITERS": "8",
            "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "10",
            "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "10",
            "POSTGRES_REQUIRED_SCHEMA_VERSION": REVISION,
        }
    )


def _wait_ready(config: dict[str, Any]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with _connect(
                config,
                user=config["bootstrap_user"],
                password=config["bootstrap_password"],
                database="postgres",
            ):
                return
        except psycopg.Error:
            time.sleep(0.5)
    raise SetupError("Local PostgreSQL readiness timed out.")


def _bootstrap(config: dict[str, Any]) -> None:
    with _connect(
        config,
        user=config["bootstrap_user"],
        password=config["bootstrap_password"],
        database="postgres",
    ) as connection:
        for role in ("migration", "runtime"):
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(config[f"{role}_user"]), sql.Literal(config[f"{role}_password"])
                )
            ).close()
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                sql.Identifier(config["database"]), sql.Identifier(config["migration_user"])
            )
        ).close()
        connection.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(config["database"])
            )
        ).close()
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(config["database"]),
                sql.Identifier(config["migration_user"]),
                sql.Identifier(config["runtime_user"]),
            )
        ).close()
    with _connect(
        config,
        user=config["bootstrap_user"],
        password=config["bootstrap_password"],
        database=config["database"],
    ) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC").close()
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(config["runtime_user"])
            )
        ).close()
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(
                sql.Identifier(config["migration_user"]), sql.Identifier(config["runtime_user"])
            )
        ).close()


def _migrate(config: dict[str, Any]) -> None:
    catalog = tuple(str(migration.id) for migration in read_migrations(MIGRATIONS))
    if catalog != CATALOG:
        raise SetupError("The local migration catalog does not match the reviewed Slice 1–4 set.")
    runner = YoyoMigrationBackend(_settings(config, migration=True), (MIGRATIONS,))
    runner.apply_pending()
    if runner.current_version() != REVISION:
        raise SetupError("Local PostgreSQL did not reach the required Slice 4 revision.")


def _grant_runtime(config: dict[str, Any]) -> None:
    with _connect(
        config,
        user=config["bootstrap_user"],
        password=config["bootstrap_password"],
        database=config["database"],
    ) as connection:
        runtime = sql.Identifier(config["runtime_user"])
        for table in ("analysis_jobs", "analysis_job_attempts"):
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE {} TO {}").format(
                    sql.Identifier(table), runtime
                )
            ).close()
        for table in ("analysis_job_terminals", "analysis_callback_outbox"):
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT ON TABLE {} TO {}").format(
                    sql.Identifier(table), runtime
                )
            ).close()
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (delivery_state, next_attempt_at) ON analysis_callback_outbox TO {}"
            ).format(runtime)
        ).close()


def _verify_runtime(config: dict[str, Any]) -> None:
    with _connect(
        config,
        user=config["runtime_user"],
        password=config["runtime_password"],
        database=config["database"],
    ) as connection:
        privileges = connection.execute(
            """SELECT
                has_database_privilege(current_user, current_database(), 'CONNECT'),
                has_schema_privilege(current_user, 'public', 'USAGE'),
                has_schema_privilege(current_user, 'public', 'CREATE'),
                has_table_privilege(current_user, 'analysis_jobs', 'SELECT'),
                has_table_privilege(current_user, 'analysis_jobs', 'INSERT'),
                has_table_privilege(current_user, 'analysis_jobs', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_jobs', 'DELETE'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'SELECT'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'INSERT'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_job_attempts', 'DELETE'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'INSERT'),
                has_table_privilege(current_user, 'analysis_job_terminals', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'INSERT'),
                has_column_privilege(current_user, 'analysis_callback_outbox',
                                     'delivery_state', 'UPDATE'),
                has_column_privilege(current_user, 'analysis_callback_outbox',
                                     'next_attempt_at', 'UPDATE'),
                has_column_privilege(current_user, 'analysis_callback_outbox',
                                     'payload_json', 'UPDATE'),
                has_table_privilege(current_user, 'analysis_callback_outbox', 'DELETE')
            """
        ).fetchone()
        expected = (
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
        )
        if privileges != expected:
            raise SetupError("The local runtime role does not have the intended narrow grants.")
    if (
        YoyoMigrationBackend(_settings(config, migration=False), (MIGRATIONS,)).current_version()
        != REVISION
    ):
        raise SetupError("The restricted runtime role cannot see the required schema revision.")


def init() -> None:
    _assert_secret_paths_ignored()
    _verify_image()
    _assert_no_collisions()
    config = _new_config()
    SECRET_DIR.mkdir(mode=0o700)
    _create_private_file(CONFIG, json.dumps(config, separators=(",", ":")) + "\n")
    _create_private_file(BOOTSTRAP_PASSWORD, config["bootstrap_password"] + "\n")
    created = _docker(
        "volume", "create", "--name", VOLUME, "--label", f"{LABEL}={config['ownership_label']}"
    )
    if created != VOLUME:
        raise SetupError("The local PostgreSQL volume identity is unexpected.")
    _docker(
        "run",
        "--detach",
        "--pull=never",
        "--restart=no",
        "--name",
        CONTAINER,
        "--label",
        f"{LABEL}={config['ownership_label']}",
        "--publish",
        f"127.0.0.1:{PORT}:5432",
        "--env",
        f"POSTGRES_USER={config['bootstrap_user']}",
        "--env",
        "POSTGRES_DB=postgres",
        "--env",
        f"POSTGRES_PASSWORD_FILE={PASSWORD_TARGET}",
        "--mount",
        f"type=volume,source={VOLUME},target=/var/lib/postgresql/data",
        "--mount",
        f"type=bind,source={BOOTSTRAP_PASSWORD},target={PASSWORD_TARGET},readonly",
        IMAGE,
    )
    if not _container_state(config):
        raise SetupError("The local PostgreSQL container did not start with its owned volume.")
    _wait_ready(config)
    _bootstrap(config)
    _migrate(config)
    _grant_runtime(config)
    _verify_runtime(config)
    print(
        "Persistent local PostgreSQL is ready at 127.0.0.1:55432; schema Slice 4; restricted runtime role verified."
    )
    print(f"Owned container: {CONTAINER}; persistent volume: {VOLUME}.")


def db_lifecycle(*, start: bool) -> None:
    config = _load_config()
    running = _container_state(config)
    if start and not running:
        _docker("start", CONTAINER)
        if not _container_state(config):
            raise SetupError("The owned local PostgreSQL container did not start.")
        _wait_ready(config)
    elif not start and running:
        _docker("stop", "--time", "30", CONTAINER)
        if _container_state(config):
            raise SetupError("The owned local PostgreSQL container remains running.")
    print(
        "Owned local PostgreSQL is running."
        if start
        else "Owned local PostgreSQL is stopped; volume retained."
    )


def inspect(job_id: UUID) -> None:
    config = _load_config()
    _container_state(config)
    with _connect(
        config,
        user=config["runtime_user"],
        password=config["runtime_password"],
        database=config["database"],
    ) as connection:
        cursor = connection.execute(
            """SELECT j.job_id::text, j.state, j.attempt_count,
                      a.attempt_number, a.worker_slot_id::text, a.lease_id::text, a.status,
                      r.terminal_kind, r.result_schema_version,
                      r.result_json IS NOT NULL AS result_persisted,
                      r.processing_duration_ms, r.finalized_at,
                      o.callback_event_id::text, o.delivery_state, o.next_attempt_at
               FROM analysis_jobs AS j
               LEFT JOIN analysis_job_attempts AS a
                   ON a.job_id = j.job_id AND a.attempt_number = j.attempt_count
               LEFT JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
               LEFT JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
               WHERE j.job_id = %s""",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SetupError("No persisted local job has that ID.")
        keys = [column.name for column in cursor.description or ()]
        print(json.dumps(dict(zip(keys, row, strict=True)), default=str, indent=2))
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create a new persistent task-owned database once")
    commands.add_parser("db-start", help="start only the verified owned database container")
    commands.add_parser("db-stop", help="stop only the verified owned database container")
    query = commands.add_parser("inspect", help="inspect persisted state by job ID")
    query.add_argument("job_id", type=UUID)
    args = parser.parse_args()
    try:
        if args.command == "init":
            init()
        elif args.command == "db-start":
            db_lifecycle(start=True)
        elif args.command == "db-stop":
            db_lifecycle(start=False)
        else:
            inspect(args.job_id)
    except Exception as error:
        print(
            f"Local durable {args.command} failed ({type(error).__name__}); no resource was deleted.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
