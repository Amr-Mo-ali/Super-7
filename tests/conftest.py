"""Explicit test-task opt-in for disposable PostgreSQL verification."""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg
import pytest
from psycopg import sql

from core.database_config import DatabaseSettings

_GREEN_C2_OPTION = "--slice1-disposable-postgres"
_POSTGRES_IMAGE_OPTION = "--slice1-postgres-image"
_AUTHORIZED_IMAGE: Final = (
    "postgres:16.15-bookworm@"
    "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)
_AUTHORIZED_IMAGE_ID: Final = (
    "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)
_AUTHORIZED_REPO_DIGEST: Final = (
    "postgres@sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)
_OWNERSHIP_LABEL: Final = "com.super7.slice1.green-c2.task"
_CONTAINER_PREFIX: Final = "super7-slice1-green-c2-"
_PASSWORD_TARGET: Final = "/run/secrets/super7_slice1_postgres_password"
_POSTGRES_PORT: Final = "5432/tcp"
_EXPECTED_REVISION: Final = "sprint2_slice1_foundation"
_DOCKER_TIMEOUT_SECONDS: Final = 20.0
_STARTUP_TIMEOUT_SECONDS: Final = 45.0
_CONNECT_TIMEOUT_SECONDS: Final = 2
_POLL_INTERVAL_SECONDS: Final = 0.25
_SKIP_REASON: Final = (
    "Slice 1 integration requires the explicitly authorized task-scoped "
    "slice1_disposable_postgres_factory fixture; no fallback database is permitted."
)
_LOGGER = logging.getLogger("football_analysis.tests.slice1_disposable_postgres")


class _DisposablePostgresError(RuntimeError):
    """Secret-free failure at the disposable PostgreSQL boundary."""


@dataclass(slots=True)
class _OwnedResource:
    task_id: str
    name: str
    password_file: Path
    container_id: str | None = None


@dataclass(slots=True, repr=False)
class _DisposablePostgresSession:
    migration_settings: DatabaseSettings
    runtime_settings: DatabaseSettings
    _inspection_settings: DatabaseSettings
    _migration_bookkeeping: frozenset[str] | None = None

    def __repr__(self) -> str:
        return "_DisposablePostgresSession(configured=True)"

    def application_relations(self) -> frozenset[str]:
        relations = _non_system_relations(self._inspection_settings)
        if self._migration_bookkeeping is None:
            # The first call follows Yoyo's public current_version() operation
            # against the pristine task database. Its non-system relations are
            # therefore migration-tool bookkeeping without naming private
            # Yoyo tables or columns in this fixture.
            self._migration_bookkeeping = relations
            return frozenset()
        return relations - self._migration_bookkeeping


class _DisposablePostgresContext(AbstractContextManager[_DisposablePostgresSession]):
    def __init__(self, factory: _DisposablePostgresFactory) -> None:
        self._factory = factory
        self._resource: _OwnedResource | None = None

    def __enter__(self) -> _DisposablePostgresSession:
        resource = _new_owned_resource()
        self._resource = resource
        self._factory._register(resource)

        setup_failure = "Disposable PostgreSQL setup failed."
        try:
            session = _start_disposable_postgres(resource, self._factory.image)
        except _DisposablePostgresError as error:
            setup_failure = str(error)
        except Exception:
            pass
        except BaseException:
            try:
                self._factory._cleanup(resource)
            except Exception:
                pass
            raise
        else:
            return session

        primary_error = _DisposablePostgresError(setup_failure)
        try:
            self._factory._cleanup(resource)
        except Exception:
            pass
        except BaseException as cleanup_error:
            raise cleanup_error from primary_error
        raise primary_error

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        resource = self._resource
        if resource is not None:
            self._factory._cleanup(resource)


class _DisposablePostgresFactory:
    def __init__(self, image: str) -> None:
        self.image = image
        self._owned: dict[str, _OwnedResource] = {}

    @property
    def active_count(self) -> int:
        return len(self._owned)

    def __call__(self) -> AbstractContextManager[_DisposablePostgresSession]:
        return _DisposablePostgresContext(self)

    def _register(self, resource: _OwnedResource) -> None:
        self._owned[resource.task_id] = resource

    def _cleanup(self, resource: _OwnedResource) -> None:
        container_removed = False
        file_removed = False
        cleanup_failed = False
        container_fatal: BaseException | None = None
        file_fatal: BaseException | None = None

        try:
            _remove_exact_owned_container(resource)
            container_removed = True
        except Exception:
            cleanup_failed = True
        except BaseException as error:
            container_fatal = error

        try:
            resource.password_file.unlink(missing_ok=True)
            file_removed = not resource.password_file.exists()
        except OSError:
            cleanup_failed = True
        except BaseException as error:
            file_fatal = error

        if container_removed and file_removed:
            self._owned.pop(resource.task_id, None)
        if file_fatal is not None:
            if container_fatal is not None:
                raise file_fatal from container_fatal
            raise file_fatal
        if container_fatal is not None:
            raise container_fatal
        if cleanup_failed or not container_removed or not file_removed:
            raise _DisposablePostgresError("Disposable PostgreSQL cleanup failed.")

    def cleanup_all(self) -> None:
        cleanup_failed = False
        for resource in tuple(self._owned.values()):
            try:
                self._cleanup(resource)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise _DisposablePostgresError("Disposable PostgreSQL final cleanup failed.")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register explicit GREEN-C2 controls without enabling Docker by default."""
    group = parser.getgroup("slice1-postgres")
    group.addoption(
        _GREEN_C2_OPTION,
        action="store_true",
        default=False,
        help="run the authorized Slice 1 disposable PostgreSQL verification",
    )
    group.addoption(
        _POSTGRES_IMAGE_OPTION,
        action="store",
        default=None,
        help="exact local digest-pinned PostgreSQL image for GREEN-C2",
    )


@pytest.fixture
def slice1_disposable_postgres_factory(
    request: pytest.FixtureRequest,
) -> Iterator[_DisposablePostgresFactory]:
    """Yield the explicit task-owned factory, otherwise retain the approved skip."""
    if not request.config.getoption(_GREEN_C2_OPTION):
        pytest.skip(_SKIP_REASON)
    image = request.config.getoption(_POSTGRES_IMAGE_OPTION)
    if image != _AUTHORIZED_IMAGE:
        pytest.fail(
            "GREEN-C2 requires the exact authorized local PostgreSQL image digest.",
            pytrace=False,
        )

    _verify_authorized_local_image()
    factory = _DisposablePostgresFactory(image)
    try:
        yield factory
    finally:
        factory.cleanup_all()


def _new_owned_resource() -> _OwnedResource:
    task_id = secrets.token_hex(12)
    name = f"{_CONTAINER_PREFIX}{task_id}"
    password = secrets.token_urlsafe(36)
    descriptor: int | None = None
    raw_path: str | None = None
    creation_failed = False
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f"{name}-", suffix=".secret")
    except OSError:
        creation_failed = True
    if creation_failed or descriptor is None or raw_path is None:
        raise _DisposablePostgresError("Disposable PostgreSQL credential-file creation failed.")

    password_file = Path(raw_path)
    write_failed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(password)
        try:
            os.chmod(password_file, 0o600)
        except OSError:
            pass
    except OSError:
        write_failed = True
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            password_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if write_failed:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            password_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise _DisposablePostgresError("Disposable PostgreSQL credential-file write failed.")

    containment_check_failed = False
    inside_repository = False
    try:
        repository = Path(__file__).resolve().parents[1]
        inside_repository = password_file.resolve().is_relative_to(repository)
    except OSError:
        containment_check_failed = True
    if containment_check_failed or inside_repository:
        try:
            password_file.unlink(missing_ok=True)
        except OSError:
            pass
    if containment_check_failed:
        raise _DisposablePostgresError(
            "Disposable PostgreSQL credential-file containment check failed."
        )
    if inside_repository:
        raise _DisposablePostgresError(
            "Disposable PostgreSQL credential file must be outside the repository."
        )
    return _OwnedResource(task_id=task_id, name=name, password_file=password_file)


def _start_disposable_postgres(
    resource: _OwnedResource,
    image: str,
) -> _DisposablePostgresSession:
    if _container_ids_for_exact_name(resource.name):
        raise _DisposablePostgresError("Disposable PostgreSQL container name already exists.")

    principal_id = secrets.token_hex(12)
    bootstrap_user = f"s7a_{principal_id}"
    bootstrap_password = resource.password_file.read_text(encoding="utf-8")
    migration_user = f"s7m_{principal_id}"
    migration_password = secrets.token_urlsafe(36)
    runtime_user = f"s7r_{principal_id}"
    runtime_password = secrets.token_urlsafe(36)
    database_name = f"s7db_{principal_id}"

    container_id = _docker_checked(
        "run",
        "--detach",
        "--rm",
        "--pull=never",
        "--restart=no",
        "--name",
        resource.name,
        "--label",
        f"{_OWNERSHIP_LABEL}={resource.task_id}",
        "--tmpfs",
        "/var/lib/postgresql/data:rw,noexec,nosuid",
        "--publish",
        f"127.0.0.1::{_POSTGRES_PORT}",
        "--env",
        f"POSTGRES_USER={bootstrap_user}",
        "--env",
        "POSTGRES_DB=postgres",
        "--env",
        f"POSTGRES_PASSWORD_FILE={_PASSWORD_TARGET}",
        "--mount",
        f"type=bind,source={resource.password_file},target={_PASSWORD_TARGET},readonly",
        image,
    )
    if not _is_full_container_id(container_id):
        raise _DisposablePostgresError("Disposable PostgreSQL returned an invalid container ID.")
    resource.container_id = container_id

    port = _verify_started_container(resource, image)
    _wait_for_postgres(
        port=port,
        database="postgres",
        user=bootstrap_user,
        password=bootstrap_password,
    )
    _bootstrap_database(
        port=port,
        bootstrap_user=bootstrap_user,
        bootstrap_password=bootstrap_password,
        database_name=database_name,
        migration_user=migration_user,
        migration_password=migration_password,
        runtime_user=runtime_user,
        runtime_password=runtime_password,
    )

    migration_settings = _database_settings(
        port=port,
        database=database_name,
        user=migration_user,
        password=migration_password,
    )
    runtime_settings = _database_settings(
        port=port,
        database=database_name,
        user=runtime_user,
        password=runtime_password,
    )
    inspection_settings = _database_settings(
        port=port,
        database=database_name,
        user=bootstrap_user,
        password=bootstrap_password,
    )
    if _non_system_relations(inspection_settings):
        raise _DisposablePostgresError("Disposable PostgreSQL database was not initially empty.")
    _LOGGER.info(
        "slice1_disposable_postgres_started container_id=%s container_name=%s",
        container_id,
        resource.name,
    )
    return _DisposablePostgresSession(
        migration_settings=migration_settings,
        runtime_settings=runtime_settings,
        _inspection_settings=inspection_settings,
    )


def _verify_authorized_local_image() -> None:
    details = _docker_checked(
        "image",
        "inspect",
        _AUTHORIZED_IMAGE,
        "--format",
        "{{json .}}",
    )
    try:
        image = json.loads(details)
        valid = (
            image.get("Id") == _AUTHORIZED_IMAGE_ID
            and image.get("Os") == "linux"
            and image.get("Architecture") == "amd64"
            and _AUTHORIZED_REPO_DIGEST in image.get("RepoDigests", [])
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise _DisposablePostgresError("Authorized local PostgreSQL image verification failed.")


def _verify_started_container(resource: _OwnedResource, image: str) -> int:
    container_id = resource.container_id
    if container_id is None:
        raise _DisposablePostgresError("Disposable PostgreSQL container identity is unavailable.")

    actual_id = _docker_checked("inspect", "--format", "{{.Id}}", container_id)
    actual_name = _docker_checked("inspect", "--format", "{{.Name}}", container_id)
    actual_label = _docker_checked(
        "inspect",
        "--format",
        f'{{{{index .Config.Labels "{_OWNERSHIP_LABEL}"}}}}',
        container_id,
    )
    actual_image = _docker_checked("inspect", "--format", "{{.Image}}", container_id)
    configured_image = _docker_checked("inspect", "--format", "{{.Config.Image}}", container_id)
    restart_policy = _docker_checked(
        "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", container_id
    )
    auto_remove = _docker_checked("inspect", "--format", "{{.HostConfig.AutoRemove}}", container_id)
    tmpfs = _docker_checked("inspect", "--format", "{{json .HostConfig.Tmpfs}}", container_id)
    mounts = _docker_checked("inspect", "--format", "{{json .Mounts}}", container_id)
    bindings = _docker_checked(
        "inspect",
        "--format",
        f'{{{{json (index .NetworkSettings.Ports "{_POSTGRES_PORT}")}}}}',
        container_id,
    )

    try:
        parsed_tmpfs = json.loads(tmpfs)
        parsed_mounts = json.loads(mounts)
        parsed_bindings = json.loads(bindings)
        binding_valid = (
            isinstance(parsed_bindings, list)
            and len(parsed_bindings) == 1
            and parsed_bindings[0].get("HostIp") == "127.0.0.1"
            and str(parsed_bindings[0].get("HostPort", "")).isdigit()
        )
        port = int(parsed_bindings[0]["HostPort"]) if binding_valid else 0
        storage_valid = (
            isinstance(parsed_tmpfs, dict)
            and "/var/lib/postgresql/data" in parsed_tmpfs
            and isinstance(parsed_mounts, list)
            and not any(mount.get("Type") == "volume" for mount in parsed_mounts)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        binding_valid = False
        storage_valid = False
        port = 0

    invariants = {
        "id": actual_id == container_id,
        "name": actual_name == f"/{resource.name}",
        "ownership-label": actual_label == resource.task_id,
        "image-id": actual_image == _AUTHORIZED_IMAGE_ID,
        "configured-image": configured_image == image,
        "restart-policy": restart_policy == "no",
        "auto-remove": auto_remove.lower() == "true",
        "loopback-port": binding_valid and 1 <= port <= 65535,
        "tmpfs-storage": storage_valid,
    }
    failed_invariants = tuple(name for name, satisfied in invariants.items() if not satisfied)
    if failed_invariants:
        raise _DisposablePostgresError(
            "Disposable PostgreSQL container verification failed: "
            + ", ".join(failed_invariants)
            + "."
        )
    return port


def _wait_for_postgres(*, port: int, database: str, user: str, password: str) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    ready = False
    while time.monotonic() < deadline:
        try:
            connection = psycopg.connect(
                host="127.0.0.1",
                port=port,
                dbname=database,
                user=user,
                password=password,
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                autocommit=True,
            )
        except psycopg.Error:
            time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            connection.close()
            ready = True
            break
    if not ready:
        raise _DisposablePostgresError("Disposable PostgreSQL readiness timed out.")


def _bootstrap_database(
    *,
    port: int,
    bootstrap_user: str,
    bootstrap_password: str,
    database_name: str,
    migration_user: str,
    migration_password: str,
    runtime_user: str,
    runtime_password: str,
) -> None:
    bootstrap_settings = _database_settings(
        port=port,
        database="postgres",
        user=bootstrap_user,
        password=bootstrap_password,
    )
    with _connect(bootstrap_settings) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(migration_user),
                sql.Literal(migration_password),
            )
        )
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(runtime_user),
                sql.Literal(runtime_password),
            )
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                sql.Identifier(database_name),
                sql.Identifier(migration_user),
            )
        )
        connection.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database_name)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(database_name),
                sql.Identifier(migration_user),
                sql.Identifier(runtime_user),
            )
        )

    database_settings = _database_settings(
        port=port,
        database=database_name,
        user=bootstrap_user,
        password=bootstrap_password,
    )
    with _connect(database_settings) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime_user))
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(
                sql.Identifier(migration_user),
                sql.Identifier(runtime_user),
            )
        )
        privileges = connection.execute(
            """
            SELECT
                has_database_privilege(%s, %s, 'CONNECT'),
                has_schema_privilege(%s, 'public', 'USAGE'),
                has_schema_privilege(%s, 'public', 'CREATE')
            """,
            (runtime_user, database_name, runtime_user, runtime_user),
        ).fetchone()
        if privileges != (True, True, False):
            raise _DisposablePostgresError(
                "Disposable PostgreSQL runtime-role boundary verification failed."
            )


def _database_settings(
    *,
    port: int,
    database: str,
    user: str,
    password: str,
) -> DatabaseSettings:
    return DatabaseSettings.from_mapping(
        {
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(port),
            "POSTGRES_DATABASE": database,
            "POSTGRES_USER": user,
            "POSTGRES_PASSWORD": password,
            "POSTGRES_POOL_MIN_SIZE": "0",
            "POSTGRES_POOL_MAX_SIZE": "2",
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": str(_CONNECT_TIMEOUT_SECONDS),
            "POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS": "5",
            "POSTGRES_POOL_MAX_WAITERS": "4",
            "POSTGRES_POOL_OPEN_TIMEOUT_SECONDS": "10",
            "POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS": "10",
            "POSTGRES_REQUIRED_SCHEMA_VERSION": _EXPECTED_REVISION,
        }
    )


def _connect(settings: DatabaseSettings) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=settings._host,
        port=settings._port,
        dbname=settings._database,
        user=settings._user,
        password=settings._password,
        connect_timeout=settings.connect_timeout_seconds,
        autocommit=True,
    )


def _non_system_relations(settings: DatabaseSettings) -> frozenset[str]:
    rows: list[tuple[object, ...]] | None = None
    query_failed = False
    try:
        with _connect(settings) as connection:
            rows = connection.execute(
                """
                SELECT namespace.nspname, relation.relname
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname NOT LIKE 'pg_%'
                  AND namespace.nspname <> 'information_schema'
                  AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                ORDER BY namespace.nspname, relation.relname
                """
            ).fetchall()
    except psycopg.Error:
        query_failed = True
    if query_failed or rows is None:
        raise _DisposablePostgresError("Disposable PostgreSQL relation inspection failed.")
    return frozenset(
        f"{schema}.{relation}"
        for schema, relation in rows
        if isinstance(schema, str) and isinstance(relation, str)
    )


def _container_ids_for_exact_name(name: str) -> tuple[str, ...]:
    output = _docker_checked(
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"name=^/{name}$",
        "--format",
        "{{.ID}}",
        preserve_raw_output=True,
    )
    if not output:
        return ()

    if output.endswith("\r\n"):
        logical_line = output[:-2]
    elif output.endswith("\n"):
        logical_line = output[:-1]
    else:
        logical_line = output
    lines = logical_line.splitlines()
    if len(lines) != 1 or lines[0] != logical_line or not _is_full_container_id(logical_line):
        raise _DisposablePostgresError("Disposable PostgreSQL container identity is ambiguous.")
    return (logical_line,)


def _remove_exact_owned_container(resource: _OwnedResource) -> None:
    container_id = resource.container_id
    if container_id is None:
        recovered_ids = _container_ids_for_exact_name(resource.name)
        if not recovered_ids:
            return
        if len(recovered_ids) != 1 or not _is_full_container_id(recovered_ids[0]):
            raise _DisposablePostgresError("Disposable PostgreSQL container identity is ambiguous.")
        container_id = recovered_ids[0]
        actual_id = _docker_checked("inspect", "--format", "{{.Id}}", container_id)
        name = _docker_checked("inspect", "--format", "{{.Name}}", container_id)
        label = _docker_checked(
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{_OWNERSHIP_LABEL}"}}}}',
            container_id,
        )
        if actual_id != container_id or name != f"/{resource.name}" or label != resource.task_id:
            raise _DisposablePostgresError("Disposable PostgreSQL ownership verification failed.")
        resource.container_id = container_id
    elif not _is_full_container_id(container_id):
        raise _DisposablePostgresError("Disposable PostgreSQL container identity is ambiguous.")

    matching_ids = _container_ids_for_exact_id(container_id)
    if not matching_ids:
        return
    if matching_ids != (container_id,):
        raise _DisposablePostgresError("Disposable PostgreSQL container identity is ambiguous.")

    label = _docker_checked(
        "inspect",
        "--format",
        f'{{{{index .Config.Labels "{_OWNERSHIP_LABEL}"}}}}',
        container_id,
    )
    name = _docker_checked("inspect", "--format", "{{.Name}}", container_id)
    if label != resource.task_id or name != f"/{resource.name}":
        raise _DisposablePostgresError("Disposable PostgreSQL ownership verification failed.")

    _docker_checked("rm", "--force", container_id)
    if _container_ids_for_exact_id(container_id):
        raise _DisposablePostgresError("Disposable PostgreSQL container removal was incomplete.")
    _LOGGER.info(
        "slice1_disposable_postgres_removed container_id=%s container_name=%s",
        container_id,
        resource.name,
    )


def _container_ids_for_exact_id(container_id: str) -> tuple[str, ...]:
    output = _docker_checked(
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"id={container_id}",
        "--format",
        "{{.ID}}",
    )
    identifiers = tuple(item for item in output.splitlines() if item)
    if any(not _is_full_container_id(item) for item in identifiers):
        raise _DisposablePostgresError("Disposable PostgreSQL container identity is ambiguous.")
    return identifiers


def _is_full_container_id(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _docker_checked(*arguments: str, preserve_raw_output: bool = False) -> str:
    invocation_failed = False
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            ("docker", *arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        invocation_failed = True
    if invocation_failed or result is None or result.returncode != 0:
        raise _DisposablePostgresError("Disposable PostgreSQL Docker operation failed.")
    if preserve_raw_output:
        return result.stdout
    return result.stdout.strip()
