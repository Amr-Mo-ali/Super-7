"""Default-off, one-loop integration of independently owned durable roles."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import HttpUrl
from test_analysis_job_finalization_contract import _RAW_MODEL, _SAFE_MODEL, _response
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

from adapters.psycopg_database import PsycopgDatabasePool
from config.debug import DebugSettings
from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import (
    AttemptAuthority,
    LeaseDuration,
    LeaseId,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from durable_api_main import create_app
from schemas.analysis import CompletedResponse
from services.analysis_job_coordinator import CoordinatorTiming
from services.analysis_job_worker import NoJobAvailable, WorkerSlotUnavailable
from services.callback_destination import CallbackDestination
from services.callback_service import CallbackService
from services.cleanup_exception_links import remove_primary_backlinks
from services.durable_api_runtime import DurableApiRuntime
from services.durable_callback_transport import create_callback_transport
from services.durable_dispatcher_runtime import DurableDispatcherRuntime
from services.durable_worker_runtime import DurableWorkerRuntime
from services.process_analysis_pool import ValidatedChildSuccess
from services.process_contracts import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    validate_child_result,
)

_OPT_IN = "SUPER7_SEPARATED_ROLES_POSTGRES_EXPERIMENT"
_CREDENTIAL = "isolated-separate-role-test-only"
_HMAC = b"isolated-separate-role-hmac-test-only".ljust(32, b"x")


class _DeterministicChild:
    def __init__(self) -> None:
        self.requests: list[ChildAnalysisRequest] = []
        self.started = False

    @property
    def outstanding_submissions(self) -> tuple[()]:
        return ()

    def start(self) -> None:
        assert not self.started
        self.started = True

    async def execute_enveloped(self, request: ChildAnalysisRequest) -> ValidatedChildSuccess:
        assert self.started
        assert request.video_reference == "synthetic-fixture.mp4"
        assert request.analysis_version == "1.0.0"
        self.requests.append(request)
        response = _response("completed").model_copy(
            update={
                "analysis_id": request.analysis_id,
                "analysis_version": request.analysis_version,
            }
        )
        assert isinstance(response, CompletedResponse)
        raw = ChildAnalysisSuccess(
            request.analysis_id,
            response.model_dump_json(),
            request.analysis_version,
            _RAW_MODEL,
            17,
        )
        validated = validate_child_result(request.analysis_id, CHILD_ANALYSIS_SCHEMA_VERSION, raw)
        assert isinstance(validated, CompletedResponse)
        return ValidatedChildSuccess(raw, validated)

    async def shutdown(self) -> None:
        self.started = False


class _CheckedLoopback(CallbackService):
    """Permit only the task-owned receiver, retaining the pinned production transport."""

    def __init__(self, url: str) -> None:
        super().__init__(2, logging.getLogger("football_analysis.tests.separated_roles"))
        self.url = url
        parts = urlsplit(url)
        assert parts.scheme == "http" and parts.hostname == "127.0.0.1"
        assert parts.port is not None and parts.path == "/callback"
        self.port = parts.port
        self.resolutions = 0

    def validate_callback_url(self, callback_url: HttpUrl) -> None:
        if str(callback_url) != self.url:
            raise ValueError("Callback is not the task-owned receiver.")

    def resolve_callback_destination(self, callback_url: HttpUrl) -> CallbackDestination:
        self.validate_callback_url(callback_url)
        self.resolutions += 1
        return CallbackDestination(
            "127.0.0.1",
            self.port,
            IPv4Address("127.0.0.1"),
            "http",
            f"127.0.0.1:{self.port}",
            "/callback",
        )


def _settings(tmp_path: Path) -> Settings:
    missing_root = tmp_path / "unmounted-videos"
    assert not missing_root.exists()
    return Settings(
        video_storage_root=str(missing_root),
        model_path=str(tmp_path / "missing-player-model.pt"),
        ball_model_path=str(tmp_path / "missing-ball-model.pt"),
        debug_output_dir=str(tmp_path / "unused-artifacts"),
        debug=DebugSettings(enabled=False, retained_sessions=0),
        analysis_version="1.0.0",
        max_queued_analyses=2,
    )


def _api(settings: Settings, database: DatabaseSettings) -> DurableApiRuntime:
    return DurableApiRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret=_CREDENTIAL,
        idempotency_hmac_secret=_HMAC,
        attempt_limit=1,
        operation_timeout_ms=5_000,
        lock_timeout_ms=1_000,
    )


def _worker(
    settings: Settings, database: DatabaseSettings, child: _DeterministicChild
) -> DurableWorkerRuntime:
    with patch("services.durable_worker_runtime.ProcessAnalysisPool", return_value=child):
        runtime = DurableWorkerRuntime(
            settings=settings,
            database_settings=database,
            timing=CoordinatorTiming(LeaseDuration(timedelta(seconds=30)), 1, 5, 1, 10, (0, 0)),
            recovery_batch_size=RecoveryBatchSize(2),
            worker_idle_seconds=0.05,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            operation_timeout_ms=5_000,
            lock_timeout_ms=1_000,
        )
    assert cast(object, runtime._executor) is child
    return runtime


def _pool(
    runtime: DurableApiRuntime | DurableWorkerRuntime | DurableDispatcherRuntime,
) -> PsycopgDatabasePool:
    pool = runtime._foundation._pool
    assert isinstance(pool, PsycopgDatabasePool)
    return pool


def _track_pool_close(
    pool: PsycopgDatabasePool,
    label: str,
    close_order: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pool.close

    async def observed_close(*, timeout_seconds: float) -> None:
        close_order.append(label)
        await original(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(pool, "close", observed_close)


def _assert_authority_continuity(
    authority: AttemptAuthority,
    persisted_attempt: tuple[object, ...],
    persisted_terminal: tuple[object, ...],
) -> None:
    expected = (
        authority.job_id,
        authority.attempt_number,
        authority.worker_slot_id.value,
        authority.lease_id.value,
    )
    assert persisted_attempt == expected, "Persisted attempt differs from execution claim."
    assert persisted_terminal == expected[:2], "Terminal is not linked to execution attempt."


def _report_cleanup(primary: BaseException | None, failures: list[BaseException]) -> None:
    if not failures:
        return
    cleanup = (
        failures[0]
        if len(failures) == 1
        else BaseExceptionGroup("Role cleanup failures.", failures)
    )
    if primary is None:
        raise cleanup
    if isinstance(cleanup, Exception):
        cleanup = remove_primary_backlinks(cleanup, primary)
        if cleanup is primary:
            return
        previous_cause = primary.__cause__
        if previous_cause is not None and previous_cause is not cleanup:
            cleanup = BaseExceptionGroup("Primary and cleanup causes.", [previous_cause, cleanup])
        raise primary from cleanup
    previous_cause = cleanup.__cause__
    if previous_cause is not None and previous_cause is not primary:
        raise cleanup from BaseExceptionGroup(
            "Fatal cleanup and primary causes.", [previous_cause, primary]
        )
    raise cleanup from primary


async def _stop_roles(
    primary: BaseException | None,
    started: list[DurableWorkerRuntime | DurableDispatcherRuntime],
) -> None:
    failures: list[BaseException] = []
    for role in reversed(started):
        try:
            await role.stop()
        except BaseException as error:
            failures.append(error)
    _report_cleanup(primary, failures)


async def _wait_for_state(
    pool: PsycopgDatabasePool,
    job_id: UUID,
    state: str,
    worker: DurableWorkerRuntime,
    dispatcher: DurableDispatcherRuntime | None = None,
) -> tuple[object, ...]:
    async def observe() -> tuple[object, ...]:
        while True:
            rows = await _fetch_all(pool, _STATE_QUERY, (job_id,))
            if rows and rows[0][22] == state:
                return rows[0]
            if worker.worker.failure is not None:
                raise worker.worker.failure
            if dispatcher is not None and dispatcher.dispatcher.failure is not None:
                raise dispatcher.dispatcher.failure
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(observe(), 30)


async def _experiment(
    settings: Settings,
    database: DatabaseSettings,
    receiver: _Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    validator = _CheckedLoopback(receiver.url)
    api = _api(settings, database)
    app = create_app(settings=settings, runtime=api, callback_service=validator)
    assert not any(
        hasattr(api, name) for name in ("worker", "dispatcher", "_executor", "coordinator")
    )

    close_order: list[str] = []
    async with app.router.lifespan_context(app):
        api_pool = _pool(api)
        _track_pool_close(api_pool, "api", close_order, monkeypatch)
        assert (await api.database_readiness()).ready
        assert not Path(settings.video_storage_root).exists()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            payload = {
                "videoId": "isolated-synthetic-video",
                "playerId": "isolated-synthetic-player",
                "videoUrl": "synthetic-fixture.mp4",
                "callbackUrl": receiver.url,
            }
            headers = {
                "Authorization": f"Bearer {_CREDENTIAL}",
                "Idempotency-Key": "isolated-separate-role-once",
            }
            first = await client.post("/analyze", json=payload, headers=headers)
            assert first.status_code == 202
            job_id = UUID(first.json()["analysisId"])
            assert await _fetch_all(
                api_pool,
                "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                (job_id,),
            ) == [("QUEUED", 0)]  # New connection sees the committed admission.
            duplicate = await client.post("/analyze", json=payload, headers=headers)
            assert duplicate.status_code == 202
            assert duplicate.json()["analysisId"] == str(job_id)
            assert await _fetch_all(api_pool, "SELECT count(*) FROM analysis_jobs") == [(1,)]

        child = _DeterministicChild()
        worker = _worker(settings, database, child)
        claims: list[WorkerClaim] = []
        repository = worker.coordinator._worker_repository
        real_claim_next = repository.claim_next

        async def observed_claim_next(
            *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
        ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable:
            outcome = await real_claim_next(
                worker_slot_id=worker_slot_id, lease_duration=lease_duration
            )
            if isinstance(outcome, WorkerClaim):
                claims.append(outcome)
            return outcome

        monkeypatch.setattr(repository, "claim_next", observed_claim_next)
        dispatcher = DurableDispatcherRuntime(
            database_settings=database, callback_transport=create_callback_transport(validator)
        )
        started: list[DurableWorkerRuntime | DurableDispatcherRuntime] = []
        try:
            await worker.start()
            started.append(worker)
            worker_pool = _pool(worker)
            _track_pool_close(worker_pool, "worker", close_order, monkeypatch)
            assert len({id(api_pool), id(worker_pool)}) == 2
            pending = await _wait_for_state(worker_pool, job_id, "PENDING", worker)
            assert pending[:4] == (job_id, "COMPLETED", 1, "1.0.0")
            assert len(claims) == 1
            claim = claims[0]
            assert claim.job.job_id == job_id
            assert claim.authority.worker_slot_id == worker.coordinator._worker_slot_id
            attempt_rows = await _fetch_all(
                worker_pool,
                "SELECT job_id, attempt_number, worker_slot_id, lease_id "
                "FROM analysis_job_attempts WHERE job_id = %s",
                (job_id,),
            )
            terminal_rows = await _fetch_all(
                worker_pool,
                "SELECT job_id, attempt_number FROM analysis_job_terminals WHERE job_id = %s",
                (job_id,),
            )
            assert len(attempt_rows) == len(terminal_rows) == 1
            _assert_authority_continuity(claim.authority, attempt_rows[0], terminal_rows[0])
            assert pending[11] == "SUCCEEDED" and pending[14] == "COMPLETED"
            assert len(child.requests) == 1 and child.requests[0].analysis_id == str(job_id)
            assert await _fetch_all(
                worker_pool,
                """SELECT (SELECT count(*) FROM analysis_job_attempts WHERE job_id = %s),
                          (SELECT count(*) FROM analysis_job_terminals WHERE job_id = %s),
                          (SELECT count(*) FROM analysis_callback_outbox WHERE job_id = %s)""",
                (job_id, job_id, job_id),
            ) == [(1, 1, 1)]
            await dispatcher.start()
            started.append(dispatcher)
            dispatcher_pool = _pool(dispatcher)
            _track_pool_close(dispatcher_pool, "dispatcher", close_order, monkeypatch)
            assert len({id(api_pool), id(worker_pool), id(dispatcher_pool)}) == 3
            delivered = await _wait_for_state(
                dispatcher_pool, job_id, "DELIVERED", worker, dispatcher
            )
            assert delivered[1] == "COMPLETED" and delivered[11] == "SUCCEEDED"
            assert delivered[14] == "COMPLETED" and delivered[22] == "DELIVERED"
            event_id = cast(UUID, delivered[19])
            snapshot = cast(dict[str, object], delivered[21])
            result = delivered[16]
            assert len(receiver.requests) == 1
            path, body, received_headers = receiver.requests[0]
            assert path == "/callback"
            assert json.loads(body) == {**snapshot, "callbackEventId": str(event_id)}
            assert received_headers["X-Super7-Callback-Event-Id"] == str(event_id)
            assert validator.resolutions == 1
        finally:
            await _stop_roles(sys.exception(), started)
        assert not worker.coordinator.outstanding_tasks
        assert not child.outstanding_submissions
        assert (await api.database_readiness()).ready  # Neither actor closed the API pool.
        assert worker._foundation._pool is None and dispatcher._foundation._pool is None
        assert close_order == ["dispatcher", "worker"]
    assert api._foundation._pool is None
    assert close_order == ["dispatcher", "worker", "api"]

    # Fresh role objects and fresh pools read the same durable result/event.
    second_api = _api(settings, database)
    second_app = create_app(settings=settings, runtime=second_api, callback_service=validator)
    second_child = _DeterministicChild()
    second_worker = _worker(settings, database, second_child)
    second_dispatcher = DurableDispatcherRuntime(
        database_settings=database, callback_transport=create_callback_transport(validator)
    )
    second_close_order: list[str] = []
    async with second_app.router.lifespan_context(second_app):
        second_api_pool = _pool(second_api)
        _track_pool_close(second_api_pool, "api", second_close_order, monkeypatch)
        second_started: list[DurableWorkerRuntime | DurableDispatcherRuntime] = []
        try:
            await second_worker.start()
            second_started.append(second_worker)
            _track_pool_close(_pool(second_worker), "worker", second_close_order, monkeypatch)
            await second_dispatcher.start()
            second_started.append(second_dispatcher)
            _track_pool_close(
                _pool(second_dispatcher), "dispatcher", second_close_order, monkeypatch
            )
            assert (
                len({id(second_api_pool), id(_pool(second_worker)), id(_pool(second_dispatcher))})
                == 3
            )
            assert second_worker.coordinator._worker_slot_id != worker.coordinator._worker_slot_id
            assert all(
                old is not new
                for old, new in zip(
                    (api_pool, worker_pool, dispatcher_pool),
                    (second_api_pool, _pool(second_worker), _pool(second_dispatcher)),
                    strict=True,
                )
            )
            persisted = await _fetch_all(second_api_pool, _STATE_QUERY, (job_id,))
            assert len(persisted) == 1
            assert (persisted[0][16], persisted[0][19], persisted[0][21], persisted[0][22]) == (
                result,
                event_id,
                snapshot,
                "DELIVERED",
            )
            await asyncio.sleep(1.2)  # Covers more than one idle-dispatcher poll.
            assert len(receiver.requests) == 1
            assert second_child.requests == []
            assert (await second_api.database_readiness()).ready
        finally:
            await _stop_roles(sys.exception(), second_started)
        assert (await second_api.database_readiness()).ready
        assert second_close_order == ["dispatcher", "worker"]
    assert second_api._foundation._pool is None
    assert second_close_order == ["dispatcher", "worker", "api"]


def test_authority_continuity_accepts_matching_attempt_and_terminal() -> None:
    authority = AttemptAuthority(uuid4(), 1, WorkerSlotId(uuid4()), LeaseId(uuid4()))
    attempt = (
        authority.job_id,
        authority.attempt_number,
        authority.worker_slot_id.value,
        authority.lease_id.value,
    )
    _assert_authority_continuity(authority, attempt, attempt[:2])


@pytest.mark.parametrize("difference", ["attempt", "lease", "terminal_attempt"])
def test_authority_continuity_rejects_changed_persistence(difference: str) -> None:
    authority = AttemptAuthority(uuid4(), 1, WorkerSlotId(uuid4()), LeaseId(uuid4()))
    attempt = (
        authority.job_id,
        authority.attempt_number,
        authority.worker_slot_id.value,
        authority.lease_id.value,
    )
    terminal = attempt[:2]
    if difference == "attempt":
        attempt = (attempt[0], attempt[1] + 1, attempt[2], attempt[3])
    elif difference == "lease":
        attempt = (attempt[0], attempt[1], attempt[2], uuid4())
    else:
        terminal = (terminal[0], terminal[1] + 1)
    assert attempt[2] == authority.worker_slot_id.value
    with pytest.raises(AssertionError, match="Persisted attempt|Terminal is not linked"):
        _assert_authority_continuity(authority, attempt, terminal)


@pytest.mark.integration
def test_separated_roles_one_job_with_disposable_postgres(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip("Separated-role PostgreSQL experiment requires explicit opt-in.")
    factory = _disposable_factory(request)
    receiver = _Receiver()
    try:
        with factory() as disposable:
            _prepare_database(disposable)
            _grant_runtime_worker_privileges(
                disposable.migration_settings, disposable.runtime_settings
            )
            _delivery_grant(disposable.migration_settings, disposable.runtime_settings, True)
            _delivery_privileges(disposable.runtime_settings, update=True)
            database = _current_settings(disposable.runtime_settings)
            _run_database(_experiment(_settings(tmp_path), database, receiver, monkeypatch))
        assert factory.active_count == 0
    finally:
        primary = sys.exception()
        failures: list[BaseException] = []
        try:
            receiver.close()  # Independently shuts down, closes, joins and inspects the thread.
        except BaseException as error:
            failures.append(error)
        try:
            if receiver.thread.is_alive():
                failures.append(AssertionError("Task-owned receiver thread is still alive."))
        except BaseException as error:
            failures.append(error)
        _report_cleanup(primary, failures)
    assert not receiver.thread.is_alive()
