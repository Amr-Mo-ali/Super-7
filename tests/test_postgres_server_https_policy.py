"""Opt-in durable evidence for legacy HTTP outbox records under server policy."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from test_postgres_analysis_callback_delivery_experiment import _delivery_grant
from test_postgres_analysis_job_finalization_integration import _finalizer, _snapshot
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _repositories,
    _run_database,
)

from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from domain.analysis_job import AnalysisJobAccepted, NewAnalysisJob
from domain.analysis_job_attempt import LeaseDuration, WorkerClaim, WorkerSlotId
from services.analysis_callback_delivery import deliver_one
from services.analysis_job_finalization import AnalysisJobCompleted
from services.callback_service import CallbackService
from services.durable_callback_transport import create_callback_transport


@pytest.mark.integration
def test_server_policy_keeps_legacy_http_outbox_pending(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        _delivery_grant(database.migration_settings, database.runtime_settings, True)
        settings = _current_settings(database.runtime_settings)
        callback_url = "http://callback.example/hook?sig=synthetic%2Btoken%3D"

        async def experiment() -> None:
            async with _repositories(settings) as (admission, worker, pool):
                job_id = uuid4()
                candidate = NewAnalysisJob(
                    job_id=job_id,
                    idempotency_lookup=hashlib.sha256(job_id.bytes + b"lookup").digest(),
                    request_fingerprint=hashlib.sha256(job_id.bytes + b"fingerprint").digest(),
                    video_id="synthetic-video",
                    player_id="synthetic-player",
                    video_reference="synthetic.mp4",
                    callback_url=callback_url,
                    analysis_version="1.0.0",
                    attempt_limit=1,
                )
                assert isinstance(
                    await admission.accept_or_get(candidate, max_queue_size=1),
                    AnalysisJobAccepted,
                )
                claim = await worker.claim_next(
                    worker_slot_id=WorkerSlotId(uuid4()),
                    lease_duration=LeaseDuration(timedelta(seconds=30)),
                )
                assert isinstance(claim, WorkerClaim)
                completed = await _finalizer(pool).finalize_success(
                    authority=claim.authority, result=_snapshot(claim)
                )
                assert isinstance(completed, AnalysisJobCompleted)
                event_id = completed.callback_event_id
                assert isinstance(event_id, UUID)
                delivery = PsycopgAnalysisCallbackDeliveryRepository(
                    pool, connection_acquire_timeout_seconds=5
                )

                async def evidence() -> tuple[object, ...]:
                    rows = await _fetch_all(
                        pool,
                        "SELECT j.state, j.callback_url, o.callback_event_id, "
                        "o.delivery_state, o.next_attempt_at, o.payload_json "
                        "FROM analysis_jobs AS j JOIN analysis_callback_outbox AS o "
                        "ON o.job_id = j.job_id WHERE j.job_id = %s",
                        (job_id,),
                    )
                    assert len(rows) == 1
                    return rows[0]

                before = await evidence()
                assert before[:4] == ("COMPLETED", callback_url, event_id, "PENDING")
                assert isinstance(before[4], datetime)

                def forbidden_resolver(
                    *_args: object, **_kwargs: object
                ) -> list[tuple[object, ...]]:
                    raise AssertionError("HTTP callback resolved a destination")

                validator = CallbackService(
                    1, logging.getLogger("https.policy.db.test"), resolver=forbidden_resolver
                )
                transport = create_callback_transport(validator, https_only_callbacks=True)

                class ForbiddenClient:
                    def __init__(self, **_kwargs: object) -> None:
                        raise AssertionError("HTTP callback opened a network client")

                with patch(
                    "services.durable_callback_transport.httpx.AsyncClient", ForbiddenClient
                ):
                    outcome = await deliver_one(
                        repository=delivery,
                        transport=transport,
                        event_id=event_id,
                        timeout_seconds=2,
                        retry_delay_ms=30_000,
                    )
                assert outcome == "RETRYING"
                after = await evidence()
                assert after[:3] == before[:3]
                assert after[3] == "RETRYING"
                assert isinstance(after[4], datetime) and after[4] > before[4]
                assert after[5] == before[5]
                assert (
                    await deliver_one(
                        repository=delivery,
                        transport=transport,
                        event_id=event_id,
                        timeout_seconds=2,
                        retry_delay_ms=30_000,
                    )
                    == "NOT_DUE"
                )

        _run_database(experiment())
    assert factory.active_count == 0
