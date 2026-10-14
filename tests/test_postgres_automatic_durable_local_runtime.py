"""Opt-in real endpoint, automatic worker, and automatic callback experiment."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from test_postgres_analysis_callback_delivery_experiment import (
    _delivery_grant,
    _delivery_privileges,
    _local_transport,
    _Receiver,
)
from test_postgres_analysis_job_coordinator_experiment import _MODEL, _VIDEO
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _run_database,
)
from test_postgres_endpoint_callback_experiment import (
    _STATE_QUERY,
    _VIDEO_SHA256,
    _ExactLoopbackCallbackService,
)

from core.config import Settings
from core.database_config import DatabaseSettings
from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, RecoveryBatchSize
from main import create_app
from services.analysis_job_coordinator import CoordinatorTiming
from services.analysis_job_worker import ClaimLost, LeaseRenewed
from services.durable_local_runtime import DurableLocalRuntime

_OPT_IN = "SUPER7_AUTOMATIC_DURABLE_LOCAL_VIDEO"


def _settings(tmp_path: Path) -> Settings:
    from config.debug import DebugSettings

    return Settings(
        video_storage_root=str(_VIDEO.parent),
        model_path=str(_MODEL),
        ball_model_path=str(_MODEL),
        debug_output_dir=str(tmp_path / "artifacts"),
        debug=DebugSettings(enabled=False, retained_sessions=0),
        analysis_version="1.0.0",
    )


def _app(
    settings: Settings, database: DatabaseSettings, receiver: _Receiver
) -> tuple[FastAPI, DurableLocalRuntime]:
    raw_model_pair = f"{_MODEL}+{_MODEL}+bytetrack"
    runtime = DurableLocalRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret="ephemeral-local-test-credential",
        idempotency_hmac_secret=b"test-local-hmac-secret-never-deployed".ljust(32, b"x"),
        attempt_limit=1,
        timing=CoordinatorTiming(
            LeaseDuration(timedelta(seconds=180)),
            heartbeat_seconds=20,
            operation_watchdog_seconds=25,
            jitter_seconds=10,
            finalization_budget_seconds=90,
            retry_delays_seconds=(2, 5),
        ),
        recovery_batch_size=RecoveryBatchSize(2),
        worker_idle_seconds=0.1,
        model_catalog={raw_model_pair: "experiment_yolo11n_pair_v1"},
        callback_transport=_local_transport,
        logger=logging.getLogger("football_analysis.tests.automatic_durable"),
        operation_timeout_ms=5_000,
        lock_timeout_ms=1_000,
    )
    return (
        create_app(
            settings,
            callback_service=_ExactLoopbackCallbackService(receiver.url),
            durable_runtime=runtime,
        ),
        runtime,
    )


async def _post(app: FastAPI, callback_url: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/analyze",
            headers={
                "Authorization": "Bearer ephemeral-local-test-credential",
                "Idempotency-Key": "apex-automatic-local-once",
            },
            json={
                "videoId": "automatic_video_003",
                "playerId": "automatic_player",
                "videoUrl": _VIDEO.name,
                "callbackUrl": callback_url,
            },
        )


@pytest.mark.integration
def test_automatic_endpoint_worker_callback_survive_graceful_reconstruction(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if os.environ.get(_OPT_IN) != str(_VIDEO):
        pytest.skip("Automatic local runtime requires the exact owner-selected video opt-in.")
    assert _VIDEO.is_file() and _MODEL.is_file()
    assert hashlib.sha256(_VIDEO.read_bytes()).hexdigest() == _VIDEO_SHA256
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
            settings = _settings(tmp_path)

            async def experiment() -> None:
                # Stop only discovery in the first app, then admit through its
                # real handler. Its pool closes with a committed QUEUED row.
                first_app, first_runtime = _app(settings, database, receiver)
                async with first_app.router.lifespan_context(first_app):
                    await first_runtime.worker.stop()
                    response = await _post(first_app, receiver.url)
                    assert response.status_code == 202
                    job_id = UUID(response.json()["analysisId"])
                    queued = await _fetch_all(
                        first_runtime._pool,
                        "SELECT state, attempt_count FROM analysis_jobs WHERE job_id = %s",
                        (job_id,),
                    )
                    assert queued == [("QUEUED", 0)]

                second_app, second_runtime = _app(settings, database, receiver)
                original_renew = second_runtime.coordinator._worker_repository.renew_lease
                periodic_renewals = 0

                async def observed_renew(
                    *, authority: AttemptAuthority, lease_duration: LeaseDuration
                ) -> LeaseRenewed | ClaimLost:
                    nonlocal periodic_renewals
                    during_child = bool(second_runtime._executor.outstanding_submissions)
                    result = await original_renew(
                        authority=authority, lease_duration=lease_duration
                    )
                    if during_child and isinstance(result, LeaseRenewed):
                        periodic_renewals += 1
                    return result

                with patch.object(
                    second_runtime.coordinator._worker_repository,
                    "renew_lease",
                    side_effect=observed_renew,
                ):
                    async with second_app.router.lifespan_context(second_app):

                        async def delivered() -> tuple[object, ...]:
                            while True:
                                rows = await _fetch_all(
                                    second_runtime._pool, _STATE_QUERY, (job_id,)
                                )
                                if rows and rows[0][22] == "DELIVERED":
                                    return rows[0]
                                if second_runtime.worker.failure is not None:
                                    raise second_runtime.worker.failure
                                if second_runtime.dispatcher.failure is not None:
                                    raise second_runtime.dispatcher.failure
                                await asyncio.sleep(0.25)

                        row = await asyncio.wait_for(delivered(), 600)
                        assert row[:4] == (job_id, "COMPLETED", 1, "1.0.0")
                        assert row[11] == "SUCCEEDED"
                        assert row[14] == "COMPLETED"
                        assert row[22] == "DELIVERED"
                        assert periodic_renewals >= 1
                        event_id = cast(UUID, row[19])
                        assert len(receiver.requests) == 1
                        assert str(event_id) in receiver.applied
                        print(
                            "automatic_lifecycle J=COMPLETED A=SUCCEEDED R=COMPLETED "
                            "O=DELIVERED "
                            f"periodic_renewals={periodic_renewals} "
                            f"job_id={job_id} event_id={event_id} "
                            f"processing_duration_ms={row[17]} "
                            f"receiver_requests={len(receiver.requests)}"
                        )

                third_app, third_runtime = _app(settings, database, receiver)
                async with third_app.router.lifespan_context(third_app):
                    after = await _fetch_all(third_runtime._pool, _STATE_QUERY, (job_id,))
                    assert after == [row]
                    await asyncio.sleep(1.1)
                    assert len(receiver.requests) == 1
                    print(f"automatic_reconstruction job_id={job_id} event_id={event_id}")

            _run_database(experiment())
        assert factory.active_count == 0
    finally:
        primary = sys.exception()
        try:
            receiver.close()
        except BaseException as cleanup:
            if primary is not None and isinstance(cleanup, Exception):
                if cleanup.__context__ is primary:
                    cleanup.__context__ = None
                raise primary from cleanup
            raise
