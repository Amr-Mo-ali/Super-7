"""One-time, collision-refusing setup for the separate durable Compose trial."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg import sql
from yoyo import read_migrations  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adapters.yoyo_migration_backend import YoyoMigrationBackend  # noqa: E402
from core.database_config import DatabaseSettings  # noqa: E402

PROJECT = "super7-durable-compose-trial-20261015"
VOLUME = "super7-durable-compose-pgdata-20261015"
APP_IMAGE = "football-analysis:durable-compose-trial-local"
API_PORT = 18080
POSTGRES_IMAGE = (
    "postgres:16.15-bookworm@"
    "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)
POSTGRES_DIGEST = "postgres@sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
SECRET_DIR = ROOT / ".local-durable-compose"
CONFIG = SECRET_DIR / "config.json"
POSTGRES_PASSWORD = SECRET_DIR / "postgres-password.secret"
COMPOSE_ENV = SECRET_DIR / "compose.env"
RUNTIME_ENV = SECRET_DIR / "runtime.env"
BOOTSTRAP_CONFIG = SECRET_DIR / "bootstrap-config.json"
INSPECT_ENV = SECRET_DIR / "inspect.env"
API_ENV = SECRET_DIR / "api.env"
WORKER_ENV = SECRET_DIR / "worker.env"
DISPATCHER_ENV = SECRET_DIR / "dispatcher.env"
CONTAINER_CONFIG = Path("/run/secrets/bootstrap_config")
CONTAINER_POSTGRES_PASSWORD = Path("/run/secrets/postgres_password")
MIGRATIONS = "package:adapters:migrations/postgres"
REVISION = "sprint2_slice4_result_outbox"
CATALOG = (
    "sprint2_slice1_foundation",
    "sprint2_slice2_analysis_job",
    "sprint2_slice3_worker_attempt",
    REVISION,
)
CONFIG_KEYS = (
    "project",
    "volume",
    "owner_id",
    "database",
    "bootstrap_user",
    "bootstrap_password",
    "migration_user",
    "migration_password",
    "runtime_user",
    "runtime_password",
    "apex_bearer_secret",
    "idempotency_hmac_secret_hex",
    "model_id",
)


class TrialSetupError(RuntimeError):
    """Secret-free setup refusal; existing resources are never repaired."""


def _docker(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ("docker", *arguments), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TrialSetupError("Docker is unavailable to this terminal.") from error
    if result.returncode:
        raise TrialSetupError("Docker inspection failed; no resource was changed.")
    return result.stdout.strip()


def _check_prepare_prerequisites() -> None:
    if SECRET_DIR.exists():
        raise TrialSetupError("Trial credential directory already exists; refusing to adopt it.")
    for path in (
        CONFIG,
        POSTGRES_PASSWORD,
        COMPOSE_ENV,
        RUNTIME_ENV,
        BOOTSTRAP_CONFIG,
        INSPECT_ENV,
        API_ENV,
        WORKER_ENV,
        DISPATCHER_ENV,
    ):
        ignored = subprocess.run(
            ("git", "check-ignore", "--quiet", "--", str(path)),
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if ignored.returncode:
            raise TrialSetupError("Trial credential paths must be Git-ignored.")
    try:
        image = json.loads(_docker("image", "inspect", POSTGRES_IMAGE, "--format", "{{json .}}"))
    except (TypeError, ValueError) as error:
        raise TrialSetupError("Pinned PostgreSQL image inspection failed.") from error
    if (
        image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
        or POSTGRES_DIGEST not in image.get("RepoDigests", [])
    ):
        raise TrialSetupError("The approved PostgreSQL image is not cached for linux/amd64.")
    if VOLUME in _docker("volume", "ls", "--format", "{{.Name}}").splitlines():
        raise TrialSetupError("The trial volume name is already in use.")
    if APP_IMAGE in _docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines():
        raise TrialSetupError("The trial application image tag is already in use.")
    if f"{PROJECT}_default" in _docker("network", "ls", "--format", "{{.Name}}").splitlines():
        raise TrialSetupError("The trial network name is already in use.")
    if any(
        name.startswith(f"{PROJECT}-")
        for name in _docker("ps", "--all", "--format", "{{.Names}}").splitlines()
    ):
        raise TrialSetupError("A trial container name is already in use.")
    if _docker(
        "ps",
        "--all",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--format",
        "{{.Names}}",
    ):
        raise TrialSetupError("The trial Compose project already has containers.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", API_PORT))
        except OSError as error:
            raise TrialSetupError("The trial loopback API port is occupied.") from error
    if not (ROOT / "models" / "yolo11n.pt").is_file():
        raise TrialSetupError("The trial model file is unavailable.")
    if not (ROOT / "dataset" / "raw" / "social_media").is_dir():
        raise TrialSetupError("The trial video directory is unavailable.")


def _private_file(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)


def _new_config() -> dict[str, str]:
    suffix = secrets.token_hex(5)
    return {
        "project": PROJECT,
        "volume": VOLUME,
        "owner_id": secrets.token_hex(16),
        "database": f"s7trial_db_{suffix}",
        "bootstrap_user": f"s7trial_boot_{suffix}",
        "bootstrap_password": secrets.token_urlsafe(36),
        "migration_user": f"s7trial_migrate_{suffix}",
        "migration_password": secrets.token_urlsafe(36),
        "runtime_user": f"s7trial_runtime_{suffix}",
        "runtime_password": secrets.token_urlsafe(36),
        "apex_bearer_secret": secrets.token_urlsafe(36),
        "idempotency_hmac_secret_hex": secrets.token_hex(32),
        "model_id": "trial_yolo11n_pair_bytetrack_v1",
    }


def _runtime_env(config: dict[str, str]) -> str:
    values = {
        "SUPER7_DURABLE_LOCAL_ENABLED": "1",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DATABASE": config["database"],
        "POSTGRES_USER": config["runtime_user"],
        "POSTGRES_PASSWORD": config["runtime_password"],
        "POSTGRES_CONNECT_TIMEOUT_SECONDS": "2",
        "POSTGRES_POOL_MIN_SIZE": "0",
        "POSTGRES_POOL_MAX_SIZE": "4",
        "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "5",
        "POSTGRES_POOL_MAX_WAITERS": "8",
        "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "10",
        "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "10",
        "POSTGRES_REQUIRED_SCHEMA_VERSION": REVISION,
        "VIDEO_STORAGE_ROOT": "/videos",
        "MODEL_PATH": "/models/yolo11n.pt",
        "BALL_MODEL_PATH": "/models/yolo11n.pt",
        "MODEL_DEVICE": "cpu",
        "CALLBACK_TIMEOUT_SECONDS": "10",
        "DEBUG_ARTIFACTS_ENABLED": "false",
        "SUPER7_APEX_BEARER_SECRET": config["apex_bearer_secret"],
        "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX": config["idempotency_hmac_secret_hex"],
        "SUPER7_DURABLE_ATTEMPT_LIMIT": "1",
        "SUPER7_DURABLE_MODEL_ID": config["model_id"],
        "SUPER7_DURABLE_RECOVERY_BATCH": "2",
        "SUPER7_DURABLE_WORKER_IDLE_SECONDS": "0.1",
        "SUPER7_DURABLE_LEASE_SECONDS": "180",
        "SUPER7_DURABLE_HEARTBEAT_SECONDS": "20",
        "SUPER7_DURABLE_WATCHDOG_SECONDS": "25",
        "SUPER7_DURABLE_JITTER_SECONDS": "10",
        "SUPER7_DURABLE_FINALIZE_SECONDS": "90",
        "SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS": "2",
        "SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS": "5",
        "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS": "5000",
        "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS": "1000",
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _role_env(config: dict[str, str], role: str) -> str:
    """Select only the existing runtime settings consumed by this role."""
    values = dict(line.split("=", 1) for line in _runtime_env(config).splitlines())
    database = {key for key in values if key.startswith("POSTGRES_")}
    role_keys = {
        "api": {
            "CALLBACK_TIMEOUT_SECONDS",
            "SUPER7_APEX_BEARER_SECRET",
            "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX",
            "SUPER7_DURABLE_ATTEMPT_LIMIT",
            "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS",
            "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS",
        },
        "worker": {
            "VIDEO_STORAGE_ROOT",
            "MODEL_PATH",
            "BALL_MODEL_PATH",
            "MODEL_DEVICE",
            "DEBUG_ARTIFACTS_ENABLED",
            "SUPER7_DURABLE_MODEL_ID",
            "SUPER7_DURABLE_RECOVERY_BATCH",
            "SUPER7_DURABLE_WORKER_IDLE_SECONDS",
            "SUPER7_DURABLE_LEASE_SECONDS",
            "SUPER7_DURABLE_HEARTBEAT_SECONDS",
            "SUPER7_DURABLE_WATCHDOG_SECONDS",
            "SUPER7_DURABLE_JITTER_SECONDS",
            "SUPER7_DURABLE_FINALIZE_SECONDS",
            "SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS",
            "SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS",
            "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS",
            "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS",
        },
        "dispatcher": set(),
    }[role]
    enabled = f"SUPER7_DURABLE_{role.upper()}_ENABLED"
    return f"{enabled}=1\n" + "".join(
        f"{key}={value}\n" for key, value in values.items() if key in database | role_keys
    )


def _bootstrap_config(config: dict[str, str]) -> str:
    keys = (
        "project",
        "volume",
        "database",
        "bootstrap_user",
        "migration_user",
        "migration_password",
        "runtime_user",
        "runtime_password",
    )
    return json.dumps({key: config[key] for key in keys}, separators=(",", ":")) + "\n"


def _inspect_env(config: dict[str, str]) -> str:
    return (
        f"POSTGRES_DATABASE={config['database']}\n"
        f"POSTGRES_USER={config['runtime_user']}\n"
        f"POSTGRES_PASSWORD={config['runtime_password']}\n"
    )


def _write_service_files(config: dict[str, str]) -> None:
    _private_file(BOOTSTRAP_CONFIG, _bootstrap_config(config))
    _private_file(INSPECT_ENV, _inspect_env(config))


def _write_role_files(config: dict[str, str]) -> None:
    for role, path in (("api", API_ENV), ("worker", WORKER_ENV), ("dispatcher", DISPATCHER_ENV)):
        _private_file(path, _role_env(config, role))


def prepare() -> None:
    _check_prepare_prerequisites()
    config = _new_config()
    SECRET_DIR.mkdir(mode=0o700)
    _private_file(CONFIG, json.dumps(config, separators=(",", ":")) + "\n")
    _private_file(POSTGRES_PASSWORD, config["bootstrap_password"] + "\n")
    _private_file(
        COMPOSE_ENV,
        f"TRIAL_BOOTSTRAP_USER={config['bootstrap_user']}\nTRIAL_OWNER_ID={config['owner_id']}\n",
    )
    _private_file(RUNTIME_ENV, _runtime_env(config))
    _write_service_files(config)
    _write_role_files(config)
    print("Trial credentials prepared in the ignored trial directory; no container was started.")


def derive_service_files() -> None:
    """Add scoped utility inputs to an existing, unchanged trial preparation."""
    if any(path.exists() or path.is_symlink() for path in (BOOTSTRAP_CONFIG, INSPECT_ENV)):
        raise TrialSetupError(
            "Service-specific credential output already exists; refusing overwrite."
        )
    try:
        raw: object = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        if not all(isinstance(raw.get(key), str) and raw[key] for key in CONFIG_KEYS):
            raise ValueError
        config = cast(dict[str, str], raw)
        if config["project"] != PROJECT or config["volume"] != VOLUME:
            raise ValueError
        expected_compose = (
            f"TRIAL_BOOTSTRAP_USER={config['bootstrap_user']}\n"
            f"TRIAL_OWNER_ID={config['owner_id']}\n"
        )
        if (
            POSTGRES_PASSWORD.read_text(encoding="utf-8") != config["bootstrap_password"] + "\n"
            or COMPOSE_ENV.read_text(encoding="utf-8") != expected_compose
            or RUNTIME_ENV.read_text(encoding="utf-8") != _runtime_env(config)
        ):
            raise ValueError
    except (OSError, TypeError, ValueError) as error:
        raise TrialSetupError(
            "Existing trial credential files are missing or inconsistent."
        ) from error
    for path in (BOOTSTRAP_CONFIG, INSPECT_ENV):
        ignored = subprocess.run(
            ("git", "check-ignore", "--quiet", "--", str(path)),
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if ignored.returncode:
            raise TrialSetupError("Service-specific credential paths must be Git-ignored.")
    _write_service_files(config)
    print("Service-specific trial credentials derived; existing files were unchanged.")


def derive_role_files() -> None:
    """Bounded transition from the existing combined trial preparation."""
    outputs = (API_ENV, WORKER_ENV, DISPATCHER_ENV)
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise TrialSetupError("Role credential output already exists; refusing overwrite.")
    try:
        raw: object = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        config = cast(dict[str, str], raw)
        if config.get("project") != PROJECT or config.get("volume") != VOLUME:
            raise ValueError
        if any(not isinstance(config.get(key), str) or not config[key] for key in CONFIG_KEYS):
            raise ValueError
        if (
            POSTGRES_PASSWORD.read_text(encoding="utf-8") != config["bootstrap_password"] + "\n"
            or COMPOSE_ENV.read_text(encoding="utf-8")
            != f"TRIAL_BOOTSTRAP_USER={config['bootstrap_user']}\nTRIAL_OWNER_ID={config['owner_id']}\n"
            or RUNTIME_ENV.read_text(encoding="utf-8") != _runtime_env(config)
            or BOOTSTRAP_CONFIG.read_text(encoding="utf-8") != _bootstrap_config(config)
            or INSPECT_ENV.read_text(encoding="utf-8") != _inspect_env(config)
        ):
            raise ValueError
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise TrialSetupError(
            "Existing trial credential files are missing or inconsistent."
        ) from error
    for path in outputs:
        ignored = subprocess.run(
            ("git", "check-ignore", "--quiet", "--", str(path)),
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if ignored.returncode:
            raise TrialSetupError("Role credential paths must be Git-ignored.")
    _write_role_files(config)
    print("Role credentials derived; existing trial credentials were unchanged.")


def verify_volume() -> None:
    """Refuse restart if the persistent volume no longer has this trial's identity."""
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        volume = json.loads(_docker("volume", "inspect", VOLUME, "--format", "{{json .}}"))
        if (
            config["project"] != PROJECT
            or config["volume"] != VOLUME
            or volume["Name"] != VOLUME
            or volume["Labels"]["com.super7.durable-compose-trial.owner"] != config["owner_id"]
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise TrialSetupError("Trial volume identity does not match; refusing restart.") from error
    print("Trial persistent volume identity verified; no data was read.")


def _container_config() -> dict[str, str]:
    try:
        raw = json.loads(CONTAINER_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        config: dict[str, str] = raw
        if config.get("project") != PROJECT or config.get("volume") != VOLUME:
            raise ValueError
        return config
    except (OSError, ValueError) as error:
        raise TrialSetupError(
            "Trial bootstrap configuration is unavailable or mismatched."
        ) from error


def _connect(config: dict[str, str], *, role: str, database: str) -> Any:
    password = (
        CONTAINER_POSTGRES_PASSWORD.read_text(encoding="utf-8").strip()
        if role == "bootstrap"
        else config[f"{role}_password"]
    )
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname=database,
        user=config[f"{role}_user"],
        password=password,
        connect_timeout=2,
        autocommit=True,
    )


def _migration_settings(config: dict[str, str]) -> DatabaseSettings:
    return DatabaseSettings.from_mapping(
        {
            "POSTGRES_HOST": "postgres",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DATABASE": config["database"],
            "POSTGRES_USER": config["migration_user"],
            "POSTGRES_PASSWORD": config["migration_password"],
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


def bootstrap() -> None:
    config = _container_config()
    catalog = tuple(str(item.id) for item in read_migrations(MIGRATIONS))
    if catalog != CATALOG:
        raise TrialSetupError("The trial migration catalog does not match Slice 1-4.")
    with _connect(config, role="bootstrap", database="postgres") as connection:
        role_names = (config["migration_user"], config["runtime_user"])
        if (
            connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)", (list(role_names),)
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (config["database"],)
            ).fetchone()
        ):
            raise TrialSetupError("Trial roles or database already exist; refusing to adopt them.")
        for role in ("migration", "runtime"):
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(config[f"{role}_user"]),
                    sql.Literal(config[f"{role}_password"]),
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
    with _connect(config, role="bootstrap", database=config["database"]) as connection:
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
                sql.Identifier(config["migration_user"]),
                sql.Identifier(config["runtime_user"]),
            )
        ).close()
    runner = YoyoMigrationBackend(_migration_settings(config), (MIGRATIONS,))
    runner.apply_pending()
    if runner.current_version() != REVISION:
        raise TrialSetupError("The trial schema did not reach Slice 4.")
    with _connect(config, role="bootstrap", database=config["database"]) as connection:
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
    with _connect(config, role="runtime", database=config["database"]) as connection:
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
    if privileges != (
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
    ):
        raise TrialSetupError("The trial runtime role does not have the approved grants.")
    print("Trial PostgreSQL reached Slice 4 with restricted runtime grants.")


def inspect(job_id: UUID) -> None:
    """Read state through the restricted runtime role; never expose payloads."""
    from os import environ

    config = {
        "runtime_user": environ["POSTGRES_USER"],
        "runtime_password": environ["POSTGRES_PASSWORD"],
        "database": environ["POSTGRES_DATABASE"],
    }
    with _connect(config, role="runtime", database=config["database"]) as connection:
        cursor = connection.execute(
            """SELECT j.job_id::text, j.state, j.attempt_count,
                      a.attempt_number, a.status,
                      r.terminal_kind, r.result_schema_version,
                      r.result_json IS NOT NULL AS result_persisted,
                      r.processing_duration_ms,
                      o.callback_event_id::text, o.delivery_state
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
            raise TrialSetupError("No trial job has that ID.")
        print(
            json.dumps(dict(zip((column.name for column in cursor.description), row, strict=True)))
        )
        cursor.close()


def inspect_backlog() -> None:
    """Report only cutover-relevant counts through the restricted runtime role."""
    from os import environ

    config = {
        "runtime_user": environ["POSTGRES_USER"],
        "runtime_password": environ["POSTGRES_PASSWORD"],
        "database": environ["POSTGRES_DATABASE"],
    }
    with _connect(config, role="runtime", database=config["database"]) as connection:
        queued, running, pending = connection.execute(
            """SELECT
                (SELECT count(*) FROM analysis_jobs WHERE state = 'QUEUED'),
                (SELECT count(*) FROM analysis_jobs WHERE state = 'RUNNING'),
                (SELECT count(*) FROM analysis_callback_outbox
                 WHERE delivery_state IN ('PENDING', 'RETRYING'))"""
        ).fetchone()
    print(json.dumps({"queued": queued, "running": running, "pending_callbacks": pending}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("derive-service-files")
    commands.add_parser("derive-role-files")
    commands.add_parser("verify-volume")
    commands.add_parser("inspect-backlog")
    commands.add_parser("bootstrap")
    query = commands.add_parser("inspect")
    query.add_argument("job_id", type=UUID)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        elif args.command == "derive-service-files":
            derive_service_files()
        elif args.command == "derive-role-files":
            derive_role_files()
        elif args.command == "verify-volume":
            verify_volume()
        elif args.command == "inspect-backlog":
            inspect_backlog()
        elif args.command == "bootstrap":
            bootstrap()
        else:
            inspect(args.job_id)
    except TrialSetupError as error:
        print(
            f"Trial {args.command} refused: {error} Existing resources were retained.",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(
            f"Trial {args.command} failed ({type(error).__name__}); existing resources were retained.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
