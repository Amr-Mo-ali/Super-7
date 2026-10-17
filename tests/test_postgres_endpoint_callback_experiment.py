"""Opt-in one-request HTTP-to-durable-callback experiment; no runtime cutover."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import HttpUrl
from test_postgres_analysis_callback_delivery_experiment import (
    _delivery_grant,
    _delivery_privileges,
    _local_transport,
    _Receiver,
)
from test_postgres_analysis_job_coordinator_experiment import (
    _MODEL,
    _VIDEO,
    _cleanup_experiment,
    _ObservedChild,
    _RecordingFinalizer,
)
from test_postgres_analysis_job_finalization_integration import _finalizer
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _repositories,
    _run_database,
)

from adapters.analysis_result_projection import convert_child_success
from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector
from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from config.debug import DebugSettings
from core.config import Settings
from domain.analysis_job import AnalysisJob as DurableAnalysisJob
from domain.analysis_job import AnalysisJobAccepted, AnalysisJobState, NewAnalysisJob
from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, WorkerSlotId
from main import create_app
from services.analysis_callback_delivery import deliver_one
from services.analysis_job_admission import AnalysisJobAdmissionRepository, admit_analysis_job
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.analysis_job_finalization import AnalysisJobCompleted
from services.analysis_job_worker import ClaimLost, LeaseRenewed
from services.analysis_queue import AnalysisJob, AnalysisQueue
from services.callback_service import CallbackService
from services.process_analysis_pool import ProcessAnalysisPool

_IMAGE_OPT_IN = "SUPER7_ENDPOINT_CALLBACK_EXPERIMENT_VIDEO"
_VIDEO_SHA256 = "86b9448e30105ad8f421c94f027b19c73cb96849aaa6420d4ba16f9386aea9ea"
_CALLER_SCOPE = "isolated-endpoint-callback-experiment"
_STATE_QUERY = """SELECT j.job_id, j.state, j.attempt_count, j.analysis_version,
                         j.video_id, j.player_id, j.video_reference, j.callback_url,
                         a.attempt_number, a.worker_slot_id, a.lease_id, a.status,
                         a.lease_expires_at, a.finished_at,
                         r.terminal_kind, r.result_schema_version, r.result_json,
                         r.processing_duration_ms, r.finalized_at,
                         o.callback_event_id, o.payload_schema_version, o.payload_json,
                         o.delivery_state, o.created_at, o.next_attempt_at
                  FROM analysis_jobs AS j
                  JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                  JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                  JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                  WHERE j.job_id = %s"""


class _DurableAdmissionQueue(AnalysisQueue):
    """Test-only bridge through the existing route's injected submit boundary."""

    def __init__(self, repository: AnalysisJobAdmissionRepository) -> None:
        super().__init__(capacity=1)
        self.repository = repository
        self.accepted_ids: list[UUID] = []
        self.protector = HmacIdempotencyLookupProtector(os.urandom(32))

    async def submit(self, job: AnalysisJob) -> bool:
        # The public route has no caller scope/key yet. This unique test-only
        # mapping proves durable persistence, not production idempotency policy.
        outcome = await admit_analysis_job(
            repository=self.repository,
            protector=self.protector,
            job_id=UUID(job.analysis_id),
            caller_scope=_CALLER_SCOPE,
            raw_idempotency_key=job.analysis_id,
            video_id=job.video_id,
            player_id=job.player_id,
            video_reference=job.video_reference,
            callback_url=job.callback_url,
            analysis_version="1.0.0",
            attempt_limit=1,
            max_queue_size=1,
        )
        assert isinstance(outcome, AnalysisJobAccepted)
        self.accepted_ids.append(outcome.job.job_id)
        return True


class _ExactLoopbackCallbackService(CallbackService):
    """Allow only this task's receiver at the route's existing validation seam."""

    def __init__(self, url: str) -> None:
        super().__init__(2, logging.getLogger("football_analysis.tests.endpoint_callback"))
        self.url = url

    def validate_callback_url(self, callback_url: HttpUrl) -> None:
        if str(callback_url) != self.url:
            raise ValueError("Callback is not the task-owned local receiver.")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        video_storage_root=str(_VIDEO.parent),
        model_path=str(_MODEL),
        ball_model_path=str(_MODEL),
        debug_output_dir=str(tmp_path / "child-artifacts"),
        debug=DebugSettings(enabled=False, retained_sessions=0),
        analysis_version="1.0.0",
    )


def _app(settings: Settings, queue: _DurableAdmissionQueue, callback_url: str) -> FastAPI:
    return create_app(
        settings,
        callback_service=_ExactLoopbackCallbackService(callback_url),
        analysis_queue=queue,
    )


async def _http_post(app: FastAPI, callback_url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/analyze",
            json={
                "videoId": "experiment_video_003",
                "playerId": "experiment_player",
                "videoUrl": _VIDEO.name,
                "callbackUrl": callback_url,
            },
        )


async def _close_app(app: FastAPI) -> None:
    # No legacy lifespan/queue consumer is entered in this isolated composition.
    # The ASGI transport closes with the client; only this app-owned lifecycle
    # has a shutdown action. Its database pool is closed by _repositories.
    await app.state.request_lifecycle.shutdown()


class _RecordingAdmission:
    def __init__(self) -> None:
        self.candidates: list[NewAnalysisJob] = []

    async def accept_or_get(
        self, candidate: NewAnalysisJob, *, max_queue_size: int
    ) -> AnalysisJobAccepted:
        assert max_queue_size == 1
        self.candidates.append(candidate)
        return AnalysisJobAccepted(
            DurableAnalysisJob(
                candidate.job_id,
                candidate.video_id,
                candidate.player_id,
                candidate.video_reference,
                candidate.callback_url,
                candidate.analysis_version,
                AnalysisJobState.QUEUED,
                candidate.attempt_limit,
                datetime.now(UTC),
            )
        )


def test_existing_analyze_route_uses_injected_durable_admission(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _RecordingAdmission()
        queue = _DurableAdmissionQueue(repository)
        callback_url = "http://127.0.0.1:8765/callback"
        app = _app(_settings(tmp_path), queue, callback_url)
        try:
            response = await _http_post(app, callback_url)
            assert response.status_code == 202
            body = response.json()
            assert body == {
                "analysisId": str(repository.candidates[0].job_id),
                "videoId": "experiment_video_003",
                "playerId": "experiment_player",
                "status": "queued",
            }
            assert queue.accepted_ids == [repository.candidates[0].job_id]
            assert queue.state_of(body["analysisId"]) is None
            assert queue.metrics().queued == 0
        finally:
            await _close_app(app)

    asyncio.run(scenario())


@pytest.mark.integration
def test_one_endpoint_request_survives_reconstruction_and_delivers_callback(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if os.environ.get(_IMAGE_OPT_IN) != str(_VIDEO):
        pytest.skip("Endpoint experiment requires the exact owner-selected local video opt-in.")
    assert _VIDEO.is_file() and _MODEL.is_file()
    assert hashlib.sha256(_VIDEO.read_bytes()).hexdigest() == _VIDEO_SHA256
    factory = _disposable_factory(request)
    receiver = _Receiver()
    primary: BaseException | None = None
    try:
        with factory() as database:
            _prepare_database(database)
            _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
            _delivery_grant(database.migration_settings, database.runtime_settings, True)
            _delivery_privileges(database.runtime_settings, update=True)
            database_settings = _current_settings(database.runtime_settings)
            process_settings = _settings(tmp_path)

            async def experiment() -> None:
                async with _repositories(database_settings) as (admission, _, pool):
                    first_pool = pool
                    first_queue = _DurableAdmissionQueue(admission)
                    first_app = _app(process_settings, first_queue, receiver.url)
                    try:
                        response = await _http_post(first_app, receiver.url)
                        assert response.status_code == 202
                        body = response.json()
                        job_id = UUID(body["analysisId"])
                        assert body == {
                            "analysisId": str(job_id),
                            "videoId": "experiment_video_003",
                            "playerId": "experiment_player",
                            "status": "queued",
                        }
                        assert first_queue.accepted_ids == [job_id]
                        assert first_queue.state_of(str(job_id)) is None
                        queued = await _fetch_all(
                            pool,
                            """SELECT job_id, state, attempt_count, video_reference, callback_url
                               FROM analysis_jobs WHERE job_id = %s""",
                            (job_id,),
                        )
                        assert queued == [(job_id, "QUEUED", 0, _VIDEO.name, receiver.url)]
                        print(f"endpoint_http status=202 body={body} queued={queued[0][1:3]}")
                    finally:
                        await _close_app(first_app)

                # The first app and its pool are closed. A new app/pool must see
                # the same committed QUEUED row before explicit processing.
                async with _repositories(database_settings) as (admission, worker, pool):
                    second_pool = pool
                    second_app = _app(
                        process_settings, _DurableAdmissionQueue(admission), receiver.url
                    )
                    try:
                        assert second_app is not first_app
                        assert pool is not first_pool
                        assert (
                            await _fetch_all(
                                pool,
                                """SELECT job_id, state, attempt_count, video_reference, callback_url
                               FROM analysis_jobs WHERE job_id = %s""",
                                (job_id,),
                            )
                            == queued
                        )
                        print(f"endpoint_reconstruction_before job_id={job_id} state=QUEUED")
                        executor = ProcessAnalysisPool(process_settings)
                        executor.start()
                        observed_child = _ObservedChild(executor)
                        original_renew = worker.renew_lease
                        periodic_renewals = 0

                        async def observed_renew(
                            *, authority: AttemptAuthority, lease_duration: LeaseDuration
                        ) -> LeaseRenewed | ClaimLost:
                            nonlocal periodic_renewals
                            during_child = observed_child.running
                            outcome = await original_renew(
                                authority=authority, lease_duration=lease_duration
                            )
                            if during_child and isinstance(outcome, LeaseRenewed):
                                periodic_renewals += 1
                            return outcome

                        finalizer = _RecordingFinalizer(_finalizer(pool))
                        raw_model_pair = f"{_MODEL}+{_MODEL}+bytetrack"
                        coordinator = AnalysisJobCoordinator(
                            worker_repository=worker,
                            finalization_repository=finalizer,
                            child_executor=observed_child,
                            result_projector=convert_child_success,
                            worker_slot_id=WorkerSlotId(uuid4()),
                            model_catalog={raw_model_pair: "experiment_yolo11n_pair_v1"},
                            timing=CoordinatorTiming(
                                LeaseDuration(timedelta(seconds=180)),
                                heartbeat_seconds=20,
                                operation_watchdog_seconds=25,
                                jitter_seconds=10,
                                finalization_budget_seconds=90,
                                retry_delays_seconds=(2, 5),
                            ),
                        )
                        try:
                            with patch.object(worker, "renew_lease", side_effect=observed_renew):
                                completed = await asyncio.wait_for(coordinator.run_one(), 600)
                            assert isinstance(completed, AnalysisJobCompleted)
                            assert completed.authority.job_id == job_id
                            assert periodic_renewals >= 1
                            assert finalizer.input is not None
                            assert finalizer.input[0] == completed.authority
                            rows = await _fetch_all(pool, _STATE_QUERY, (job_id,))
                            assert len(rows) == 1
                            pending = rows[0]
                            authority = completed.authority
                            assert pending[:4] == (job_id, "COMPLETED", 1, "1.0.0")
                            assert pending[8:13] == (
                                authority.attempt_number,
                                authority.worker_slot_id.value,
                                authority.lease_id.value,
                                "SUCCEEDED",
                                None,
                            )
                            assert pending[14:16] == ("COMPLETED", "analysis_result_v1")
                            assert pending[19:21] == (
                                completed.callback_event_id,
                                "analysis_callback_snapshot_v1",
                            )
                            assert pending[22] == "PENDING"
                            assert (
                                pending[13] == pending[18] == pending[23] == completed.finished_at
                            )
                            assert pending[17] == finalizer.input[1].processing_duration_ms
                            assert isinstance(pending[16], dict)
                            assert isinstance(pending[21], dict)
                            assert pending[16]["analysis_id"] == str(job_id)
                            assert pending[21]["request_id"] == str(job_id)
                            assert pending[21]["video_id"] == "experiment_video_003"
                            assert pending[21]["player_id"] == "experiment_player"

                            delivery = PsycopgAnalysisCallbackDeliveryRepository(
                                pool, connection_acquire_timeout_seconds=5
                            )

                            async def local_transport(
                                url: str,
                                data: bytes,
                                headers: Mapping[str, str],
                                timeout: float,
                            ) -> int:
                                assert url == receiver.url
                                return await _local_transport(url, data, headers, timeout)

                            assert (
                                await deliver_one(
                                    repository=delivery,
                                    transport=local_transport,
                                    event_id=completed.callback_event_id,
                                    timeout_seconds=2,
                                    retry_delay_ms=500,
                                )
                                == "DELIVERED"
                            )
                            assert len(receiver.requests) == 1
                            path, wire_body, headers = receiver.requests[0]
                            assert path == "/callback"
                            assert headers["X-Super7-Callback-Event-Id"] == str(
                                completed.callback_event_id
                            )
                            assert json.loads(wire_body)["callbackEventId"] == str(
                                completed.callback_event_id
                            )
                            assert receiver.applied[str(completed.callback_event_id)] == wire_body
                            delivered_rows = await _fetch_all(pool, _STATE_QUERY, (job_id,))
                            assert len(delivered_rows) == 1
                            delivered = delivered_rows[0]
                            assert delivered[:22] == pending[:22]
                            assert delivered[22] == "DELIVERED"
                            assert delivered[23:] == pending[23:]
                            result_payload = delivered[16]
                            assert isinstance(result_payload, dict)
                            print(
                                "endpoint_lifecycle J=COMPLETED A=SUCCEEDED R=COMPLETED "
                                "O=PENDING/DELIVERED "
                                f"periodic_renewals={periodic_renewals} "
                                f"job_id={job_id} event_id={completed.callback_event_id} "
                                f"processing_duration_ms={delivered[17]} "
                                f"processing_time_ms={result_payload.get('processing_time_ms')} "
                                f"receiver_requests={len(receiver.requests)}"
                            )
                        finally:
                            await _cleanup_experiment(coordinator, executor)
                    finally:
                        await _close_app(second_app)

                # A third, fresh app and pool observe the full immutable result,
                # event identity, timing measurements, and delivery state.
                async with _repositories(database_settings) as (admission, _, pool):
                    third_app = _app(
                        process_settings, _DurableAdmissionQueue(admission), receiver.url
                    )
                    try:
                        assert third_app is not second_app
                        assert pool is not second_pool
                        assert await _fetch_all(pool, _STATE_QUERY, (job_id,)) == [delivered]
                        print(
                            "endpoint_reconstruction_after state=COMPLETED "
                            "attempt=SUCCEEDED terminal=COMPLETED delivery=DELIVERED "
                            f"job_id={job_id} event_id={completed.callback_event_id}"
                        )
                    finally:
                        await _close_app(third_app)

            _run_database(experiment())
        assert factory.active_count == 0
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            receiver.close()
        except BaseException as cleanup:
            if primary is not None and isinstance(cleanup, Exception):
                if cleanup is primary:
                    raise
                pending_errors: list[BaseException] = [cleanup]
                seen: set[int] = set()
                while pending_errors:
                    observed = pending_errors.pop()
                    if observed is primary or id(observed) in seen:
                        continue
                    seen.add(id(observed))
                    for link_name in ("__cause__", "__context__"):
                        linked = getattr(observed, link_name)
                        if linked is primary:
                            setattr(observed, link_name, None)
                        elif linked is not None:
                            pending_errors.append(linked)
                    if isinstance(observed, BaseExceptionGroup):
                        pending_errors.extend(observed.exceptions)
                raise primary from cleanup
            raise
    assert not receiver.thread.is_alive()
