"""Default-off disposable proof of durable roles in distinct OS processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import httpx
import psutil  # type: ignore[import-untyped]
import pytest
from test_postgres_analysis_callback_delivery_experiment import (
    _delivery_grant,
    _delivery_privileges,
    _Receiver,
)
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _run_database,
)
from test_postgres_endpoint_callback_experiment import _STATE_QUERY
from test_postgres_separated_roles_integration import (
    _CREDENTIAL,
    _api,
    _assert_authority_continuity,
    _CheckedLoopback,
    _DeterministicChild,
    _pool,
    _report_cleanup,
    _settings,
    _worker,
)

import durable_dispatcher_main
import durable_worker_main
from adapters.psycopg_database import PsycopgDatabasePool
from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, WorkerClaim, WorkerSlotId
from durable_api_main import create_app
from services.analysis_job_worker import NoJobAvailable, WorkerSlotUnavailable
from services.durable_callback_transport import create_callback_transport
from services.durable_dispatcher_runtime import DurableDispatcherRuntime

_OPT_IN = "SUPER7_SEPARATED_PROCESSES_POSTGRES_EXPERIMENT"
_START_TIMEOUT = 20.0
_RESULT_TIMEOUT = 30.0
_STOP_TIMEOUT = 12.0
_JOIN_TIMEOUT = 8.0
_FALLBACK_TIMEOUT = 3.0
_CHILD_COMMAND_TIMEOUT = 90.0


@dataclass(frozen=True)
class _Message:
    kind: str
    pid: int
    data: tuple[object, ...] = ()


class _ProcessPort(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def exitcode(self) -> int | None: ...

    def is_alive(self) -> bool: ...
    def join(self, timeout: float | None = None) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class _ConnectionPort(Protocol):
    def poll(self, timeout: float = 0.0) -> bool: ...
    def recv(self) -> object: ...
    def send(self, obj: object) -> None: ...
    def close(self) -> None: ...


@dataclass
class _Role:
    name: str
    process: _ProcessPort
    connection: _ConnectionPort
    backlog: list[_Message] = field(default_factory=list)
    stop_sent: bool = False
    stopped: _Message | None = None
    settled: bool = False
    forced: bool = False


def _send(connection: _ConnectionPort, kind: str, *data: object) -> None:
    connection.send(_Message(kind, os.getpid(), data))


def _digest(value: object) -> str:
    frozen = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(frozen).hexdigest()


async def _command(connection: Connection, actor: asyncio.Task[None] | None = None) -> _Message:
    deadline = asyncio.get_running_loop().time() + _CHILD_COMMAND_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if actor is not None and actor.done():
            await actor
            raise RuntimeError("Role actor exited before its stop command.")
        if connection.poll(0):
            message = connection.recv()
            if not isinstance(message, _Message) or message.kind not in {
                "admit",
                "inspect",
                "stop",
            }:
                raise AssertionError("Unexpected role IPC command.")
            return message
        await asyncio.sleep(0.02)
    raise TimeoutError("Role stop/result command timed out.")


async def _api_role(
    connection: Connection, database: DatabaseSettings, root: str, url: str
) -> None:
    os.environ["SUPER7_DURABLE_API_ENABLED"] = "1"
    settings = Settings(
        video_storage_root=str(Path(root) / "unmounted-videos"),
        analysis_version="1.0.0",
        max_queued_analyses=2,
    )
    runtime = _api(settings, database)
    app = create_app(settings=settings, runtime=runtime, callback_service=_CheckedLoopback(url))
    async with app.router.lifespan_context(app):
        pool = _pool(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert not Path(settings.video_storage_root).exists()
            _send(connection, "ready", ready.status_code, runtime.started)
            while True:
                command = await _command(connection)
                if command.kind == "stop":
                    break
                if command.kind == "admit":
                    payload = {
                        "videoId": "isolated-process-video",
                        "playerId": "isolated-process-player",
                        "videoUrl": "synthetic-fixture.mp4",
                        "callbackUrl": url,
                    }
                    headers = {
                        "Authorization": f"Bearer {_CREDENTIAL}",
                        "Idempotency-Key": "isolated-process-once",
                    }
                    first = await client.post("/analyze", json=payload, headers=headers)
                    assert first.status_code == 202
                    job_id = UUID(first.json()["analysisId"])
                    queued = await _fetch_all(
                        pool,
                        "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                        (job_id,),
                    )
                    duplicate = await client.post("/analyze", json=payload, headers=headers)
                    assert duplicate.status_code == 202
                    count = await _fetch_all(pool, "SELECT count(*) FROM analysis_jobs")
                    _send(
                        connection,
                        "admitted",
                        str(job_id),
                        duplicate.json()["analysisId"],
                        queued,
                        count,
                    )
                elif command.kind == "inspect":
                    rows = await _fetch_all(
                        pool,
                        _STATE_QUERY,
                        (UUID(cast(str, command.data[0])),),
                    )
                    assert len(rows) == 1
                    row = rows[0]
                    _send(
                        connection,
                        "observed",
                        str(row[0]),
                        row[1],
                        row[22],
                        str(row[19]),
                        _digest(row[16]),
                        _digest(row[21]),
                    )
    _send(connection, "stopped", runtime._foundation._pool is None)


async def _actor_role(
    connection: Connection,
    serve: Callable[[asyncio.Event], Coroutine[object, object, None]],
    started: Callable[[], bool],
    ready_data: Callable[[], tuple[object, ...]],
    stopped_data: Callable[[], tuple[object, ...]],
) -> None:
    stop = asyncio.Event()
    actor = asyncio.create_task(serve(stop))
    try:
        deadline = asyncio.get_running_loop().time() + _START_TIMEOUT
        while not started():
            if actor.done():
                await actor
                raise RuntimeError("Role exited during startup.")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Role startup timed out.")
            await asyncio.sleep(0.02)
        _send(connection, "ready", *ready_data())
        command = await _command(connection, actor)
        assert command.kind == "stop"
    finally:
        primary = sys.exception()
        stop.set()
        failures: list[BaseException] = []
        try:
            await actor  # Parent owns the bounded join and forced-failure fallback.
        except BaseException as error:
            failures.append(error)
        _report_cleanup(primary, failures)
    _send(connection, "stopped", *stopped_data())


def _api_entry(connection: Connection, database: DatabaseSettings, root: str, url: str) -> None:
    _child_entry(connection, _api_role(connection, database, root, url))


def _worker_entry(connection: Connection, database: DatabaseSettings, root: str) -> None:
    _child_entry(connection, _worker_role(connection, database, root))


def _dispatcher_entry(connection: Connection, database: DatabaseSettings, url: str) -> None:
    _child_entry(connection, _dispatcher_role(connection, database, url))


def _child_entry(connection: Connection, operation: Coroutine[object, object, None]) -> None:
    try:
        try:
            _run_database(operation)
        except BaseException as error:
            send_failures: list[BaseException] = []
            try:
                _send(connection, "failure", type(error).__name__)
            except BaseException as send_error:
                send_failures.append(send_error)
            _report_cleanup(error, send_failures)
            if isinstance(error, Exception):
                raise SystemExit(1) from None
            raise
    finally:
        primary = sys.exception()
        close_failures: list[BaseException] = []
        try:
            connection.close()
        except BaseException as error:
            close_failures.append(error)
        _report_cleanup(primary, close_failures)


async def _worker_role(connection: Connection, database: DatabaseSettings, root: str) -> None:
    child = _DeterministicChild()
    runtime = _worker(_settings(Path(root)), database, child)
    claims: list[WorkerClaim] = []
    repository = runtime.coordinator._worker_repository
    real_claim_next = repository.claim_next

    async def observed_claim_next(
        *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
    ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable:
        claim = await real_claim_next(worker_slot_id=worker_slot_id, lease_duration=lease_duration)
        if isinstance(claim, WorkerClaim):
            claims.append(claim)
            _send(connection, "claim", claim.authority)
        return claim

    from unittest.mock import patch

    with patch.object(repository, "claim_next", observed_claim_next):
        await _actor_role(
            connection,
            lambda stop: durable_worker_main.serve(runtime, stop),
            lambda: runtime.started,
            lambda: (runtime.coordinator._worker_slot_id.value,),
            lambda: (
                runtime._foundation._pool is None,
                len(child.requests),
                len(claims),
                not runtime.coordinator.outstanding_tasks and not child.outstanding_submissions,
            ),
        )


async def _dispatcher_role(connection: Connection, database: DatabaseSettings, url: str) -> None:
    validator = _CheckedLoopback(url)
    runtime = DurableDispatcherRuntime(
        database_settings=database, callback_transport=create_callback_transport(validator)
    )
    await _actor_role(
        connection,
        lambda stop: durable_dispatcher_main.serve(runtime, stop),
        lambda: runtime.started,
        lambda: (),
        lambda: (runtime._foundation._pool is None, validator.resolutions),
    )


class _RoleFailure(RuntimeError):
    """A task-owned role failed or exited before its expected IPC event."""


def _start_role(
    context: BaseContext,
    name: str,
    target: Callable[..., None],
    args: tuple[object, ...],
) -> _Role:
    parent, child = context.Pipe(duplex=True)
    process: BaseProcess = context.Process(  # type: ignore[attr-defined]
        target=target, args=(child, *args), name=f"super7-experiment-{name}"
    )
    try:
        process.start()
    except BaseException:
        parent.close()
        child.close()
        raise
    child.close()
    return _Role(name, process, parent)


def _expect(role: _Role, kind: str, timeout: float) -> _Message:
    deadline = time.monotonic() + timeout
    while True:
        for index, queued in enumerate(role.backlog):
            if queued.kind == kind:
                return role.backlog.pop(index)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{role.name} IPC {kind} timed out.")
        if role.connection.poll(min(remaining, 0.05)):
            try:
                message = role.connection.recv()
            except EOFError as error:
                raise _RoleFailure(f"{role.name} IPC closed early.") from error
            if not isinstance(message, _Message):
                raise _RoleFailure(f"{role.name} sent an invalid IPC event.")
            if role.process.pid is not None and message.pid != role.process.pid:
                raise _RoleFailure(f"{role.name} IPC PID did not match its process.")
            if message.kind == "failure":
                failure_type = message.data[0] if message.data else "unknown"
                raise _RoleFailure(f"{role.name} reported {failure_type}.")
            if message.kind == kind:
                return message
            role.backlog.append(message)
        elif not role.process.is_alive():
            raise _RoleFailure(f"{role.name} exited before IPC {kind}.")


def _command_role(role: _Role, kind: str, *data: object) -> None:
    role.connection.send(_Message(kind, os.getpid(), data))


def _inspect_alive(role: _Role, failures: list[BaseException]) -> bool | None:
    try:
        return role.process.is_alive()
    except BaseException as error:
        failures.append(error)
        return None


def _descendants(roles: list[_Role]) -> tuple[list[tuple[int, float]], list[BaseException]]:
    owned: list[tuple[int, float]] = []
    failures: list[BaseException] = []
    for role in roles:
        try:
            pid = role.process.pid
            if pid is None or _inspect_alive(role, failures) is not True:
                continue
            owned.extend(
                (child.pid, child.create_time())
                for child in psutil.Process(pid).children(recursive=True)
            )
        except psutil.NoSuchProcess:
            pass
        except BaseException as error:
            failures.append(error)
    return owned, failures


def _settle_roles(
    roles: list[_Role],
    *,
    stop_timeout: float = _STOP_TIMEOUT,
    join_timeout: float = _JOIN_TIMEOUT,
    fallback_timeout: float = _FALLBACK_TIMEOUT,
) -> list[BaseException]:
    """Settle each owned process independently; forced fallback always fails the test."""
    active = [role for role in roles if not role.settled]
    descendants, failures = _descendants(active)
    for role in reversed(active):
        if _inspect_alive(role, failures) is not False and not role.stop_sent:
            try:
                _command_role(role, "stop")
                role.stop_sent = True
            except BaseException as error:
                failures.append(error)
    for role in reversed(active):
        try:
            if role.stopped is None:
                try:
                    role.stopped = _expect(role, "stopped", stop_timeout)
                except BaseException as error:
                    failures.append(error)
            try:
                role.process.join(join_timeout)
            except BaseException as error:
                failures.append(error)
            if _inspect_alive(role, failures) is not False:
                role.forced = True
                failures.append(TimeoutError(f"{role.name} required forced termination."))
                try:
                    role.process.terminate()
                except BaseException as error:
                    failures.append(error)
                try:
                    role.process.join(fallback_timeout)
                except BaseException as error:
                    failures.append(error)
                if _inspect_alive(role, failures) is not False:
                    try:
                        role.process.kill()
                    except BaseException as error:
                        failures.append(error)
                    try:
                        role.process.join(fallback_timeout)
                    except BaseException as error:
                        failures.append(error)
            alive = _inspect_alive(role, failures)
            if alive is not False:
                failures.append(AssertionError(f"{role.name} process remains alive or unverified."))
            else:
                try:
                    exitcode = role.process.exitcode
                except BaseException as error:
                    failures.append(error)
                else:
                    if exitcode != 0:
                        failures.append(AssertionError(f"{role.name} exited nonzero."))
            if role.stopped is None:
                failures.append(
                    AssertionError(f"{role.name} had no graceful stop acknowledgement.")
                )
            role.settled = alive is False
        finally:
            try:
                role.connection.close()
            except BaseException as error:
                failures.append(error)
    for pid, created_at in descendants:
        try:
            child = psutil.Process(pid)
            if child.create_time() != created_at or not child.is_running():
                continue
            failures.append(AssertionError("Task-owned role descendant required termination."))
            try:
                child.terminate()
                child.wait(fallback_timeout)
            except psutil.TimeoutExpired:
                child.kill()
                child.wait(fallback_timeout)
        except psutil.NoSuchProcess:
            pass
        except BaseException as error:
            failures.append(error)
    return failures


async def _wait_state(
    pool: PsycopgDatabasePool, job_id: UUID, state: str, roles: list[_Role]
) -> tuple[object, ...]:
    async def observe() -> tuple[object, ...]:
        while True:
            rows = await _fetch_all(pool, _STATE_QUERY, (job_id,))
            if rows and rows[0][22] == state:
                return rows[0]
            for role in roles:
                if not role.process.is_alive():
                    raise _RoleFailure(f"{role.name} exited before {state} persisted.")
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(observe(), _RESULT_TIMEOUT)


def _require_ready(role: _Role) -> _Message:
    message = _expect(role, "ready", _START_TIMEOUT)
    assert message.pid == role.process.pid
    return message


def _require_graceful(roles: list[_Role]) -> None:
    failures = _settle_roles(roles)
    _report_cleanup(None, failures)
    assert all(role.stopped is not None and role.process.exitcode == 0 for role in roles)
    assert all(not role.process.is_alive() and not role.forced for role in roles)


async def _scenario(database: DatabaseSettings, root: Path, receiver: _Receiver) -> None:
    context = mp.get_context("spawn")
    inspector = PsycopgDatabasePool(database)
    roles: list[_Role] = []
    try:
        await inspector.open(timeout_seconds=database.pool.open_timeout_seconds)
        api = _start_role(context, "api-one", _api_entry, (database, str(root), receiver.url))
        roles.append(api)
        assert _require_ready(api).data == (200, True)
        assert api.process.pid != os.getpid()
        _command_role(api, "admit")
        admitted = _expect(api, "admitted", _RESULT_TIMEOUT)
        job_id = UUID(cast(str, admitted.data[0]))
        assert admitted.data[1] == str(job_id)
        assert admitted.data[2] == [("QUEUED", 0)]
        assert admitted.data[3] == [(1,)]
        assert await _fetch_all(
            inspector,
            "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
            (job_id,),
        ) == [("QUEUED", 0)]

        worker = _start_role(context, "worker-one", _worker_entry, (database, str(root)))
        roles.append(worker)
        worker_ready = _require_ready(worker)
        assert worker.process.pid != os.getpid()
        claimed = _expect(worker, "claim", _RESULT_TIMEOUT)
        authority = cast(AttemptAuthority, claimed.data[0])
        assert isinstance(authority, AttemptAuthority)
        assert authority.job_id == job_id
        assert authority.worker_slot_id.value == worker_ready.data[0]
        pending = await _wait_state(inspector, job_id, "PENDING", [api, worker])
        assert pending[:4] == (job_id, "COMPLETED", 1, "1.0.0")
        assert pending[11] == "SUCCEEDED" and pending[14] == "COMPLETED"
        attempt_rows = await _fetch_all(
            inspector,
            "SELECT job_id, attempt_number, worker_slot_id, lease_id "
            "FROM analysis_job_attempts WHERE job_id = %s",
            (job_id,),
        )
        terminal_rows = await _fetch_all(
            inspector,
            "SELECT job_id, attempt_number FROM analysis_job_terminals WHERE job_id = %s",
            (job_id,),
        )
        assert len(attempt_rows) == len(terminal_rows) == 1
        _assert_authority_continuity(authority, attempt_rows[0], terminal_rows[0])
        assert await _fetch_all(
            inspector,
            """SELECT (SELECT count(*) FROM analysis_job_attempts WHERE job_id = %s),
                      (SELECT count(*) FROM analysis_job_terminals WHERE job_id = %s),
                      (SELECT count(*) FROM analysis_callback_outbox WHERE job_id = %s)""",
            (job_id, job_id, job_id),
        ) == [(1, 1, 1)]

        dispatcher = _start_role(
            context, "dispatcher-one", _dispatcher_entry, (database, receiver.url)
        )
        roles.append(dispatcher)
        _require_ready(dispatcher)
        first_roles = [api, worker, dispatcher]
        first_pids = [role.process.pid for role in first_roles]
        assert None not in first_pids and len(set(first_pids + [os.getpid()])) == 4
        delivered = await _wait_state(inspector, job_id, "DELIVERED", first_roles)
        assert (delivered[1], delivered[11], delivered[14], delivered[22]) == (
            "COMPLETED",
            "SUCCEEDED",
            "COMPLETED",
            "DELIVERED",
        )
        result = delivered[16]
        event_id = cast(UUID, delivered[19])
        snapshot = cast(dict[str, object], delivered[21])
        assert len(receiver.requests) == 1
        path, body, headers = receiver.requests[0]
        assert path == "/callback"
        assert json.loads(body) == {**snapshot, "callbackEventId": str(event_id)}
        assert headers["X-Super7-Callback-Event-Id"] == str(event_id)
        _require_graceful(first_roles)
        assert api.stopped is not None and api.stopped.data == (True,)
        assert worker.stopped is not None and worker.stopped.data == (True, 1, 1, True)
        assert dispatcher.stopped is not None and dispatcher.stopped.data == (True, 1)

        fresh_api = _start_role(context, "api-two", _api_entry, (database, str(root), receiver.url))
        roles.append(fresh_api)
        assert _require_ready(fresh_api).data == (200, True)
        _command_role(fresh_api, "inspect", str(job_id))
        observed = _expect(fresh_api, "observed", _RESULT_TIMEOUT)
        assert observed.data == (
            str(job_id),
            "COMPLETED",
            "DELIVERED",
            str(event_id),
            _digest(result),
            _digest(snapshot),
        )
        fresh_worker = _start_role(context, "worker-two", _worker_entry, (database, str(root)))
        roles.append(fresh_worker)
        fresh_worker_ready = _require_ready(fresh_worker)
        assert fresh_worker_ready.data[0] != worker_ready.data[0]
        fresh_dispatcher = _start_role(
            context, "dispatcher-two", _dispatcher_entry, (database, receiver.url)
        )
        roles.append(fresh_dispatcher)
        _require_ready(fresh_dispatcher)
        fresh_roles = [fresh_api, fresh_worker, fresh_dispatcher]
        all_pids = [role.process.pid for role in roles]
        assert None not in all_pids and len(set(all_pids + [os.getpid()])) == 7
        await asyncio.sleep(1.2)  # More than one idle dispatcher poll.
        assert len(receiver.requests) == 1
        persisted = await _fetch_all(inspector, _STATE_QUERY, (job_id,))
        assert len(persisted) == 1
        assert (persisted[0][16], persisted[0][19], persisted[0][21], persisted[0][22]) == (
            result,
            event_id,
            snapshot,
            "DELIVERED",
        )
        _require_graceful(fresh_roles)
        assert fresh_api.stopped is not None and fresh_api.stopped.data == (True,)
        assert fresh_worker.stopped is not None and fresh_worker.stopped.data == (True, 0, 0, True)
        assert fresh_dispatcher.stopped is not None and fresh_dispatcher.stopped.data == (True, 0)
        assert len(receiver.requests) == 1
        assert all(role.process.exitcode == 0 and not role.forced for role in roles)
    finally:
        primary = sys.exception()
        failures = _settle_roles(roles)
        try:
            await inspector.close(timeout_seconds=database.pool.close_timeout_seconds)
        except BaseException as error:
            failures.append(error)
        if roles:
            print(
                "separated_processes_outcomes "
                + " ".join(
                    f"{role.name}_pid={role.process.pid}_exit={role.process.exitcode}"
                    for role in roles
                )
                + f" forced={sum(role.forced for role in roles)}"
            )
        _report_cleanup(primary, failures)


def _echo_entry(connection: Connection) -> None:
    """Database-free spawn control for the same startup/stop IPC boundary."""
    try:
        _send(connection, "ready", True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if connection.poll(0.05):
                command = connection.recv()
                assert isinstance(command, _Message) and command.kind == "stop"
                _send(connection, "stopped", True)
                return
        raise TimeoutError("Offline echo stop timed out.")
    finally:
        connection.close()


class _SilentConnection:
    def poll(self, timeout: float = 0.0) -> bool:
        del timeout
        return False

    def recv(self) -> object:
        raise AssertionError("Silent IPC connection cannot receive.")

    def send(self, obj: object) -> None:
        del obj

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, *, alive: bool, exitcode: int | None = None) -> None:
        self._alive = alive
        self._exitcode = exitcode
        self.terminated = False
        self.joined = False

    @property
    def pid(self) -> int | None:
        return None

    @property
    def exitcode(self) -> int | None:
        return self._exitcode

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None and timeout <= 1
        self.joined = True

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self._exitcode = -15

    def kill(self) -> None:
        raise AssertionError("Termination should have settled this fake process.")


class _StopConnection(_SilentConnection):
    def __init__(self) -> None:
        self.sent: list[_Message] = []
        self.closed = False

    def send(self, obj: object) -> None:
        assert isinstance(obj, _Message)
        self.sent.append(obj)

    def poll(self, timeout: float = 0.0) -> bool:
        del timeout
        return bool(self.sent)

    def recv(self) -> object:
        assert self.sent
        self.sent.clear()
        return _Message("stopped", os.getpid(), (True,))

    def close(self) -> None:
        self.closed = True
        raise ValueError("Independent IPC-close failure.")


def test_spawn_ipc_round_trip_and_graceful_exit() -> None:
    role = _start_role(mp.get_context("spawn"), "offline-echo", _echo_entry, ())
    try:
        ready = _require_ready(role)
        assert ready.pid == role.process.pid != os.getpid()
        assert ready.data == (True,)
        _require_graceful([role])
        assert role.stopped is not None and role.stopped.data == (True,)
    finally:
        primary = sys.exception()
        failures = _settle_roles([role], stop_timeout=1, join_timeout=1, fallback_timeout=1)
        _report_cleanup(primary, failures)


def test_process_ipc_timeout_is_bounded() -> None:
    role = _Role("silent", _FakeProcess(alive=True), _SilentConnection())
    with pytest.raises(TimeoutError, match="IPC ready timed out"):
        _expect(role, "ready", 0.02)


def test_process_early_failure_is_reported_before_timeout() -> None:
    role = _Role("early", _FakeProcess(alive=False, exitcode=7), _SilentConnection())
    with pytest.raises(_RoleFailure, match="exited before IPC ready"):
        _expect(role, "ready", 1)


def test_forced_cleanup_and_independent_failure_links_preserve_primary() -> None:
    process = _FakeProcess(alive=True)
    connection = _StopConnection()
    role = _Role("owned", process, connection)
    failures = _settle_roles([role], stop_timeout=0.02, join_timeout=0.02, fallback_timeout=0.02)
    assert process.joined and process.terminated and connection.closed and role.forced
    assert len(failures) >= 3
    primary = AssertionError("Original scenario failure.")
    try:
        raise primary
    except AssertionError:
        with pytest.raises(AssertionError) as caught:
            _report_cleanup(sys.exception(), failures)
    assert caught.value is primary
    reachable: set[int] = set()
    active: set[int] = set()

    def visit(error: BaseException) -> None:
        identity = id(error)
        assert identity not in active, "Cleanup exception graph has a cycle."
        if identity in reachable:
            return
        active.add(identity)
        for linked in (error.__cause__, error.__context__):
            if linked is not None:
                visit(linked)
        if isinstance(error, BaseExceptionGroup):
            for member in error.exceptions:
                visit(member)
        active.remove(identity)
        reachable.add(identity)

    visit(primary)
    assert all(id(error) in reachable for error in failures)


def test_liveness_inspection_failure_does_not_skip_owned_roles() -> None:
    inspection = RuntimeError("Owned process liveness inspection failed.")

    class _BrokenLiveness(_FakeProcess):
        @property
        def pid(self) -> int:
            return os.getpid()

        def is_alive(self) -> bool:
            raise inspection

    broken_connection = _StopConnection()
    later_connection = _StopConnection()
    later_process = _FakeProcess(alive=True)
    broken = _Role("broken-inspection", _BrokenLiveness(alive=True), broken_connection)
    later = _Role("later-owned", later_process, later_connection)

    failures = _settle_roles(
        [later, broken], stop_timeout=0.02, join_timeout=0.02, fallback_timeout=0.02
    )

    assert any(error is inspection for error in failures)
    assert broken_connection.closed and later_connection.closed
    assert later_process.joined and later_process.terminated
    assert later.stopped is not None and later.stopped.kind == "stopped"
    assert not broken.settled


def test_fatal_cleanup_group_retains_primary_and_ordinary_failure() -> None:
    primary = AssertionError("Primary process assertion.")
    ordinary = ValueError("Independent ordinary cleanup failure.")
    fatal = KeyboardInterrupt()
    try:
        raise primary
    except AssertionError:
        with pytest.raises(BaseExceptionGroup) as caught:
            _report_cleanup(sys.exception(), [ordinary, fatal])
    assert caught.value.exceptions == (ordinary, fatal)
    assert caught.value.__cause__ is primary


@pytest.mark.integration
def test_separated_os_processes_with_disposable_postgres(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip("Separated-process PostgreSQL experiment requires explicit opt-in.")
    factory = _disposable_factory(request)
    try:
        with factory() as disposable:
            _prepare_database(disposable)
            _grant_runtime_worker_privileges(
                disposable.migration_settings, disposable.runtime_settings
            )
            _delivery_grant(disposable.migration_settings, disposable.runtime_settings, True)
            _delivery_privileges(disposable.runtime_settings, update=True)
            receiver = _Receiver()
            try:
                database = _current_settings(disposable.runtime_settings)
                _run_database(_scenario(database, tmp_path, receiver))
            finally:
                primary = sys.exception()
                failures: list[BaseException] = []
                try:
                    receiver.close()
                except BaseException as error:
                    failures.append(error)
                try:
                    if receiver.thread.is_alive():
                        failures.append(AssertionError("Task-owned receiver thread remains alive."))
                except BaseException as error:
                    failures.append(error)
                _report_cleanup(primary, failures)
    finally:
        primary = sys.exception()
        failures = []
        if factory.active_count != 0:
            failures.append(AssertionError("Disposable PostgreSQL resource remains owned."))
        _report_cleanup(primary, failures)
    print("separated_processes_cleanup receiver_thread_alive=0 disposable_resources=0")
