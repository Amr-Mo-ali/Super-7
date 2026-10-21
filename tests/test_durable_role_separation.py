"""Offline Slice 1 controls for the three isolated role entry points."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import HttpUrl

import durable_api_main
import durable_dispatcher_main
import durable_worker_main
from adapters.psycopg_analysis_job_repository import AnalysisJobPersistenceUnavailable
from adapters.psycopg_callback_dispatch_ownership import PsycopgCallbackDispatchOwnership
from core.config import Settings
from services.analysis_job_coordinator import AnalysisJobCoordinator
from services.callback_service import CallbackService
from services.durable_api_runtime import DurableApiRuntime
from services.durable_callback_dispatcher import DurableCallbackDispatcher
from services.durable_dispatcher_runtime import DurableDispatcherRuntime
from services.durable_local_admission import (
    DurableAdmissionConflict,
    DurableAdmissionFull,
    DurableAdmissionInvalidKey,
    DurableAdmissionUnauthorized,
)
from services.durable_local_runtime import DurableLocalRuntime
from services.durable_worker_loop import DurableWorkerLoop
from services.durable_worker_runtime import DurableWorkerRuntime
from services.postgres_foundation import PostgresFoundation
from services.process_analysis_pool import ProcessAnalysisPool
from services.video_path_resolver import VideoPathResolver


@dataclass
class _Readiness:
    ready: bool


class _Admission:
    outcome = "accepted"

    def authenticate(self, values: list[str]) -> None:
        if values != ["Bearer secret"]:
            raise DurableAdmissionUnauthorized("Apex authentication required.")

    def key(self, values: list[str]) -> str:
        if values != ["key-1"]:
            raise DurableAdmissionInvalidKey("Idempotency-Key is required.")
        return values[0]

    async def admit(
        self,
        *,
        key: str,
        video_id: str,
        player_id: str,
        video_reference: str,
        callback_url: HttpUrl,
    ) -> SimpleNamespace:
        assert (key, video_id, player_id, video_reference, str(callback_url)) == (
            "key-1",
            "video-1",
            "player-1",
            "sample.mp4",
            "https://callback.example/result",
        )
        if self.outcome == "full":
            raise DurableAdmissionFull("Analysis queue is full.")
        if self.outcome == "conflict":
            raise DurableAdmissionConflict("Idempotency-Key conflicts with an existing request.")
        if self.outcome == "unavailable":
            raise AnalysisJobPersistenceUnavailable("unavailable")
        return SimpleNamespace(
            job=SimpleNamespace(job_id=uuid4(), video_id=video_id, player_id=player_id)
        )


class _ApiRuntime:
    def __init__(self) -> None:
        self.admission = _Admission()
        self.started = False
        self.database_ready = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def database_readiness(self) -> _Readiness:
        return _Readiness(self.database_ready)


class _Path:
    def validate_reference(self, filename: str) -> None:
        assert filename == "sample.mp4"


class _Callback:
    def validate_callback_url(self, url: HttpUrl) -> None:
        assert str(url) == "https://callback.example/result"


async def _request(client: httpx.AsyncClient, *, bearer: bool = True) -> httpx.Response:
    return await client.post(
        "/analyze",
        headers={
            "Authorization": "Bearer secret" if bearer else "Bearer wrong",
            "Idempotency-Key": "key-1",
        },
        json={
            "videoId": "video-1",
            "playerId": "player-1",
            "videoUrl": "sample.mp4",
            "callbackUrl": "https://callback.example/result",
        },
    )


def test_api_only_admission_and_local_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPER7_LEGACY_IN_MEMORY_ENABLED", raising=False)
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    runtime = _ApiRuntime()
    app = durable_api_main.create_app(
        settings=Settings(video_storage_root="Z:/absent-slice1-video-root"),
        runtime=cast(DurableApiRuntime, runtime),
        path_resolver=VideoPathResolver("Z:/absent-slice1-video-root"),
        callback_service=cast("CallbackService", _Callback()),
    )

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                unauthorized = await client.post(
                    "/analyze", headers={"Authorization": "Bearer wrong"}, content=b"{"
                )
                assert unauthorized.status_code == 401
                accepted = await _request(client)
                assert accepted.status_code == 202
                assert set(accepted.json()) == {"analysisId", "videoId", "playerId", "status"}
                assert (await client.get("/health/ready")).status_code == 200
                runtime.admission.outcome = "full"
                assert (await _request(client)).status_code == 503
                runtime.admission.outcome = "conflict"
                assert (await _request(client)).status_code == 409
                runtime.admission.outcome = "unavailable"
                unavailable = await _request(client)
                assert unavailable.status_code == 503
                assert (
                    unavailable.json()["detail"]["error"]
                    == "Analysis admission is temporarily unavailable."
                )
                invalid = await client.post(
                    "/analyze",
                    headers={"Authorization": "Bearer secret", "Idempotency-Key": "key-1"},
                    json={
                        "videoId": "video-1",
                        "playerId": "player-1",
                        "videoUrl": "../escape.mp4",
                        "callbackUrl": "https://callback.example/result",
                    },
                )
                assert invalid.status_code == 422
                missing_key = await client.post(
                    "/analyze",
                    headers={"Authorization": "Bearer secret"},
                    json={
                        "videoId": "video-1",
                        "playerId": "player-1",
                        "videoUrl": "sample.mp4",
                        "callbackUrl": "https://callback.example/result",
                    },
                )
                assert missing_key.status_code == 422
                runtime.database_ready = False
                assert (await client.get("/health/ready")).status_code == 503
                assert (await client.get("/health/live")).status_code == 200
        assert not runtime.started

    asyncio.run(run())


@pytest.mark.parametrize(
    ("module", "flag"),
    [
        (durable_worker_main, "SUPER7_DURABLE_WORKER_ENABLED"),
        (durable_dispatcher_main, "SUPER7_DURABLE_DISPATCHER_ENABLED"),
    ],
)
def test_process_roles_fail_closed_before_configuration(
    module: object, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(flag, raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        module.create_runtime()  # type: ignore[attr-defined]


class _Actor:
    def __init__(self, failure: bool) -> None:
        self.failure = failure
        self.waiting = asyncio.Event()

    async def wait(self) -> None:
        self.waiting.set()
        if self.failure:
            raise RuntimeError("actor failed")
        await asyncio.Event().wait()


class _ProcessRuntime:
    def __init__(self, failure: bool) -> None:
        self.worker = self.dispatcher = _Actor(failure)
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


@pytest.mark.parametrize("worker", [True, False])
def test_actor_failure_is_observed_and_stopped(worker: bool) -> None:
    runtime = _ProcessRuntime(True)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="actor failed"):
            if worker:
                await durable_worker_main.serve(
                    cast(DurableWorkerRuntime, runtime), asyncio.Event()
                )
            else:
                await durable_dispatcher_main.serve(
                    cast(DurableDispatcherRuntime, runtime), asyncio.Event()
                )
        assert runtime.events == ["start", "stop"]

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_stop_signal_settles_role_without_actor_cancellation(worker: bool) -> None:
    runtime = _ProcessRuntime(False)

    async def run() -> None:
        stop = asyncio.Event()
        stop.set()
        if worker:
            await durable_worker_main.serve(cast(DurableWorkerRuntime, runtime), stop)
        else:
            await durable_dispatcher_main.serve(cast(DurableDispatcherRuntime, runtime), stop)
        assert runtime.events == ["start", "stop"]

    asyncio.run(run())


class _Foundation:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("db_start")

    async def close(self) -> None:
        self.events.append("db_close")

    async def readiness(self) -> _Readiness:
        return _Readiness(True)


class _DispatchOwnership:
    def __init__(self, events: list[str], *, fail_acquire: bool = False) -> None:
        self.events = events
        self.fail_acquire = fail_acquire

    async def acquire(self) -> None:
        self.events.append("ownership_acquire")
        if self.fail_acquire:
            raise RuntimeError("dispatcher already owned")

    async def close(self) -> None:
        self.events.append("ownership_close")


class _OwnedActor:
    def __init__(
        self, events: list[str], *, fail_start: bool = False, fail_stop: bool = False
    ) -> None:
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.running = False
        self.failure: BaseException | None = None

    def start(self) -> None:
        self.events.append("actor_start")
        if self.fail_start:
            raise ValueError("actor start failed")
        self.running = True

    async def stop(self) -> None:
        self.events.append("actor_stop")
        self.running = False
        if self.fail_stop:
            raise RuntimeError("actor stop failed")


class _Executor:
    def __init__(self, events: list[str], *, fail_start: bool = False) -> None:
        self.events = events
        self.fail_start = fail_start
        self.outstanding_submissions: set[object] = set()

    def start(self) -> None:
        self.events.append("pool_start")
        if self.fail_start:
            raise ValueError("pool start failed")

    async def shutdown(self) -> None:
        self.events.append("pool_stop")


def test_api_runtime_owns_only_database_lifecycle() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableApiRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime.started = False

    async def run() -> None:
        await runtime.start()
        assert runtime.started
        await runtime.stop()
        assert not runtime.started
        assert events == ["db_start", "db_close"]

    asyncio.run(run())


def test_worker_runtime_settles_pool_and_database_after_start_failure() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableWorkerRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._executor = cast("ProcessAnalysisPool", _Executor(events, fail_start=True))
    runtime.worker = cast("DurableWorkerLoop", _OwnedActor(events))
    runtime.coordinator = cast("AnalysisJobCoordinator", SimpleNamespace(outstanding_tasks=set()))
    runtime.started = False

    async def run() -> None:
        with pytest.raises(ValueError, match="pool start failed"):
            await runtime.start()
        assert events == ["db_start", "pool_start", "actor_stop", "pool_stop", "db_close"]
        assert not runtime.ready

    asyncio.run(run())


def test_worker_runtime_preserves_primary_when_cleanup_fails() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableWorkerRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._executor = cast("ProcessAnalysisPool", _Executor(events, fail_start=True))
    runtime.worker = cast("DurableWorkerLoop", _OwnedActor(events, fail_stop=True))
    runtime.coordinator = cast("AnalysisJobCoordinator", SimpleNamespace(outstanding_tasks=set()))
    runtime.started = False

    async def run() -> None:
        with pytest.raises(ValueError, match="pool start failed") as failure:
            await runtime.start()
        assert isinstance(failure.value.__cause__, RuntimeError)
        assert events[-2:] == ["pool_stop", "db_close"]
        assert not runtime.ready

    asyncio.run(run())


def test_dispatcher_runtime_settles_database_after_actor_start_failure() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableDispatcherRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._ownership = cast("PsycopgCallbackDispatchOwnership", _DispatchOwnership(events))
    runtime.dispatcher = cast("DurableCallbackDispatcher", _OwnedActor(events, fail_start=True))
    runtime.started = False

    async def run() -> None:
        with pytest.raises(ValueError, match="actor start failed"):
            await runtime.start()
        assert events == [
            "db_start",
            "ownership_acquire",
            "actor_start",
            "actor_stop",
            "ownership_close",
            "db_close",
        ]
        assert not runtime.ready

    asyncio.run(run())


def test_dispatcher_runtime_releases_ownership_before_pool_on_graceful_stop() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableDispatcherRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._ownership = cast("PsycopgCallbackDispatchOwnership", _DispatchOwnership(events))
    runtime.dispatcher = cast("DurableCallbackDispatcher", _OwnedActor(events))
    runtime.started = False

    async def run() -> None:
        await runtime.start()
        await runtime.stop()
        assert events == [
            "db_start",
            "ownership_acquire",
            "actor_start",
            "actor_stop",
            "ownership_close",
            "db_close",
        ]

    asyncio.run(run())


def test_dispatcher_runtime_competing_start_cleans_up_without_starting_actor() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableDispatcherRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._ownership = cast(
        "PsycopgCallbackDispatchOwnership", _DispatchOwnership(events, fail_acquire=True)
    )
    runtime.dispatcher = cast("DurableCallbackDispatcher", _OwnedActor(events))
    runtime.started = False

    async def run() -> None:
        with pytest.raises(RuntimeError, match="dispatcher already owned"):
            await runtime.start()
        assert events == [
            "db_start",
            "ownership_acquire",
            "actor_stop",
            "ownership_close",
            "db_close",
        ]
        assert not runtime.ready

    asyncio.run(run())


@pytest.mark.parametrize("failed_stage", ["ownership", "executor", "worker", "dispatcher"])
def test_combined_runtime_acquires_before_actors_and_cleans_failed_start(
    failed_stage: str,
) -> None:
    events: list[str] = []
    failure = RuntimeError(f"{failed_stage} start failed")

    class StageOwnership(_DispatchOwnership):
        async def acquire(self) -> None:
            events.append("ownership_acquire")
            if failed_stage == "ownership":
                raise failure

    class StageExecutor(_Executor):
        def start(self) -> None:
            events.append("pool_start")
            if failed_stage == "executor":
                raise failure

    class StageActor:
        def __init__(self, name: str) -> None:
            self.name = name
            self.running = False
            self.failure: BaseException | None = None

        def start(self) -> None:
            events.append(f"{self.name}_start")
            if failed_stage == self.name:
                raise failure
            self.running = True

        async def stop(self) -> None:
            events.append(f"{self.name}_stop")
            self.running = False

    runtime = object.__new__(DurableLocalRuntime)
    runtime._foundation = cast("PostgresFoundation", _Foundation(events))
    runtime._dispatch_ownership = cast("PsycopgCallbackDispatchOwnership", StageOwnership(events))
    runtime._executor = cast("ProcessAnalysisPool", StageExecutor(events))
    runtime.worker = cast("DurableWorkerLoop", StageActor("worker"))
    runtime.dispatcher = cast("DurableCallbackDispatcher", StageActor("dispatcher"))
    runtime.coordinator = cast("AnalysisJobCoordinator", SimpleNamespace(outstanding_tasks=set()))
    runtime._started = False

    async def run() -> None:
        with pytest.raises(RuntimeError) as observed:
            await runtime.start()
        assert observed.value is failure
        starts = ["db_start", "ownership_acquire"]
        if failed_stage != "ownership":
            starts.append("pool_start")
        if failed_stage in {"worker", "dispatcher"}:
            starts.append("worker_start")
        if failed_stage == "dispatcher":
            starts.append("dispatcher_start")
        assert events == [
            *starts,
            "dispatcher_stop",
            "ownership_close",
            "worker_stop",
            "pool_stop",
            "db_close",
        ]
        assert not runtime.ready

    asyncio.run(run())


def test_api_import_does_not_load_legacy_or_actor_composition() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import durable_api_main, sys; forbidden = "
            "{'main', 'services.process_analysis_pool', 'services.durable_worker_loop', "
            "'services.durable_callback_dispatcher'}; assert not (forbidden & sys.modules.keys())",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", [durable_worker_main, durable_dispatcher_main])
def test_process_main_exposes_failure_without_starting_services(
    module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "create_runtime", object)

    async def fail(_runtime: object, _stop: asyncio.Event) -> None:
        raise RuntimeError("actor failed")

    monkeypatch.setattr(module, "serve", fail)
    with pytest.raises(RuntimeError, match="actor failed"):
        module.main()  # type: ignore[attr-defined]


@pytest.mark.parametrize("module", [durable_worker_main, durable_dispatcher_main])
def test_process_main_returns_after_stop_without_starting_services(
    module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "create_runtime", object)

    async def stopped(_runtime: object, stop: asyncio.Event) -> None:
        assert asyncio.get_running_loop().is_running()
        stop.set()

    monkeypatch.setattr(module, "serve", stopped)
    assert module.main() == 0  # type: ignore[attr-defined]


def test_api_factory_ignores_model_environment_and_video_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    monkeypatch.setenv("MODEL_CONFIDENCE", "not-a-number")
    runtime = _ApiRuntime()
    app = durable_api_main.create_app(
        runtime=cast(DurableApiRuntime, runtime),
        path_resolver=VideoPathResolver("Z:/absent-slice1-video-root"),
        callback_service=cast(CallbackService, _Callback()),
    )
    assert app.title == "Football Analysis MVP"
    assert not runtime.started
