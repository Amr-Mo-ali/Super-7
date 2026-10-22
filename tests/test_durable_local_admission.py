"""Database-free controls for the approved local Apex header identity."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import httpx
import pytest
from pydantic import HttpUrl

from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector
from adapters.psycopg_analysis_job_repository import (
    AnalysisJobAdmissionLockTimeout,
    AnalysisJobPersistenceUnavailable,
)
from core.config import Settings
from domain.analysis_job import (
    AnalysisJob,
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
    AnalysisJobState,
    NewAnalysisJob,
)
from domain.video_integrity import VideoIntegrity
from main import create_app
from services.callback_service import CallbackService
from services.durable_local_admission import (
    DurableAdmissionConflict,
    DurableAdmissionFull,
    DurableAdmissionInvalidKey,
    DurableAdmissionUnauthorized,
    DurableLocalAdmission,
)
from services.durable_local_runtime import DurableLocalRuntime
from services.postgres_foundation import DatabasePublicChecks, DatabaseReadiness


class _Repository:
    def __init__(self) -> None:
        self.stored: NewAnalysisJob | None = None
        self.force_full = False
        self.failure: Exception | None = None
        self.calls = 0

    async def accept_or_get(
        self, candidate: NewAnalysisJob, *, max_queue_size: int
    ) -> (
        AnalysisJobAccepted
        | AnalysisJobExisting
        | AnalysisJobIdempotencyConflict
        | AnalysisJobCapacityRejected
    ):
        assert max_queue_size == 2
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if self.stored is None:
            if self.force_full:
                return AnalysisJobCapacityRejected()
            self.stored = candidate
            return AnalysisJobAccepted(_job(candidate))
        if candidate.idempotency_lookup != self.stored.idempotency_lookup:
            return AnalysisJobCapacityRejected()
        if (
            candidate.request_fingerprint != self.stored.request_fingerprint
            or candidate.video_integrity != self.stored.video_integrity
        ):
            return AnalysisJobIdempotencyConflict(self.stored.job_id)
        return AnalysisJobExisting(_job(self.stored))


def _job(candidate: NewAnalysisJob) -> AnalysisJob:
    return AnalysisJob(
        candidate.job_id,
        candidate.video_id,
        candidate.player_id,
        candidate.video_reference,
        candidate.callback_url,
        candidate.analysis_version,
        AnalysisJobState.QUEUED,
        candidate.attempt_limit,
        datetime.now(UTC),
        candidate.video_integrity,
    )


def _admission(repository: _Repository) -> DurableLocalAdmission:
    return DurableLocalAdmission(
        repository=repository,
        protector=HmacIdempotencyLookupProtector(b"x" * 32),
        bearer_secret="ephemeral-test-secret",
        analysis_version="1.0.0",
        attempt_limit=1,
        max_queue_size=2,
    )


def test_authentication_and_key_are_required_without_value_echo() -> None:
    admission = _admission(_Repository())
    for values in ([], ["Bearer wrong"], ["Bearer secret", "Bearer secret"]):
        with pytest.raises(DurableAdmissionUnauthorized) as captured:
            admission.authenticate(values)
        assert "secret" not in str(captured.value)
    admission.authenticate(["Bearer ephemeral-test-secret"])
    for values in ([], [""], [" "], ["two", "values"], ["x" * 256]):
        with pytest.raises(DurableAdmissionInvalidKey):
            admission.key(values)
    assert admission.key(["opaque-key"]) == "opaque-key"


def test_same_scoped_key_reuses_identity_and_conflict_is_rejected() -> None:
    async def scenario() -> None:
        repository = _Repository()
        admission = _admission(repository)

        async def admit(player: str) -> AnalysisJobAccepted | AnalysisJobExisting:
            return await admission.admit(
                key="opaque-key",
                video_id="video",
                player_id=player,
                video_reference="video.mp4",
                callback_url=HttpUrl("https://callback.example/result"),
            )

        first = await admit("player")
        duplicate = await admit("player")
        assert isinstance(first, AnalysisJobAccepted)
        assert isinstance(duplicate, AnalysisJobExisting)
        assert duplicate.job.job_id == first.job.job_id
        assert isinstance(first.job.job_id, UUID)
        assert repository.stored is not None
        assert repository.stored.idempotency_lookup != b"opaque-key"
        with pytest.raises(DurableAdmissionConflict):
            await admit("different")

    asyncio.run(scenario())


def test_capacity_rejection_is_visible() -> None:
    async def scenario() -> None:
        repository = _Repository()
        repository.force_full = True
        with pytest.raises(DurableAdmissionFull):
            await _admission(repository).admit(
                key="opaque",
                video_id="video",
                player_id="player",
                video_reference="video.mp4",
                callback_url=HttpUrl("https://callback.example/result"),
            )

    asyncio.run(scenario())


class _LocalCallbackValidator(CallbackService):
    def __init__(self) -> None:
        super().__init__(10, logging.getLogger("football_analysis.tests.durable_admission"))

    def validate_callback_url(self, callback_url: HttpUrl) -> None:
        assert str(callback_url) == "http://127.0.0.1:8765/callback"


def test_verified_route_validates_fields_preserves_signed_query_and_binds_idempotency() -> None:
    class SignedCallbackValidator(_LocalCallbackValidator):
        def validate_callback_url(self, callback_url: HttpUrl) -> None:
            assert str(callback_url) == "http://127.0.0.1:8765/callback?sig=opaque%2Fvalue"

    async def scenario() -> None:
        repository = _Repository()
        runtime = cast(DurableLocalRuntime, SimpleNamespace(admission=_admission(repository)))
        app = create_app(
            Settings(video_storage_root="E:/super7/dataset/raw/social_media"),
            callback_service=SignedCallbackValidator(),
            durable_runtime=runtime,
        )
        payload: dict[str, object] = {
            "videoId": "video",
            "playerId": "player",
            "videoUrl": "video.mp4",
            "callbackUrl": "http://127.0.0.1:8765/callback?sig=opaque%2Fvalue",
        }
        verified = {
            **payload,
            "videoKey": "videos/video.mp4",
            "videoSizeBytes": 5,
            "videoSha256": hashlib.sha256(b"video").hexdigest(),
            "hashAlgorithm": "sha256",
        }
        headers = {"Authorization": "Bearer ephemeral-test-secret", "Idempotency-Key": "key"}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.post("/analyze", json=verified)).status_code == 401
                for bad in (
                    {**verified, "videoKey": "videos/../video.mp4"},
                    {**verified, "videoSizeBytes": True},
                    {**verified, "videoSha256": "A" * 64},
                    {**verified, "hashAlgorithm": "SHA256"},
                    {**payload, "videoKey": "videos/video.mp4"},
                ):
                    assert (
                        await client.post("/analyze", json=bad, headers=headers)
                    ).status_code == 422
                assert repository.calls == 0
                first = await client.post("/analyze", json=verified, headers=headers)
                assert first.status_code == 202
                assert repository.stored is not None
                assert repository.stored.callback_url == verified["callbackUrl"]
                assert repository.stored.video_integrity == VideoIntegrity(
                    5, cast(str, verified["videoSha256"])
                )
                assert (
                    await client.post("/analyze", json=verified, headers=headers)
                ).json() == first.json()
                assert (
                    await client.post("/analyze", json=payload, headers=headers)
                ).status_code == 409
                changed = {**verified, "videoSha256": "0" * 64}
                assert (
                    await client.post("/analyze", json=changed, headers=headers)
                ).status_code == 409
        finally:
            await app.state.request_lifecycle.shutdown()

    asyncio.run(scenario())


def test_existing_route_uses_durable_headers_only_when_explicitly_enabled() -> None:
    async def scenario() -> None:
        repository = _Repository()
        runtime = cast(
            DurableLocalRuntime,
            SimpleNamespace(admission=_admission(repository)),
        )
        app = create_app(
            Settings(video_storage_root="E:/super7/dataset/raw/social_media"),
            callback_service=_LocalCallbackValidator(),
            durable_runtime=runtime,
        )
        payload = {
            "videoId": "video",
            "playerId": "player",
            "videoUrl": "video.mp4",
            "callbackUrl": "http://127.0.0.1:8765/callback",
        }
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.post("/analyze", json=payload)).status_code == 401
                auth = {"Authorization": "Bearer ephemeral-test-secret"}
                assert (
                    await client.post("/analyze", json=payload, headers=auth)
                ).status_code == 422
                headers = {**auth, "Idempotency-Key": "apex-once"}
                first = await client.post("/analyze", json=payload, headers=headers)
                assert first.status_code == 202
                duplicate = await client.post("/analyze", json=payload, headers=headers)
                assert duplicate.status_code == 202
                assert duplicate.json() == first.json()
                assert first.json() == {
                    "analysisId": str(repository.stored.job_id) if repository.stored else None,
                    "videoId": "video",
                    "playerId": "player",
                    "status": "queued",
                }
                conflicting = await client.post(
                    "/analyze", json={**payload, "playerId": "different"}, headers=headers
                )
                assert conflicting.status_code == 409
        finally:
            await app.state.request_lifecycle.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [None, b'{"videoId":"video"}', b'{"videoId":'])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
def test_durable_authentication_precedes_body_validation(
    body: bytes | None, authorization: str | None
) -> None:
    async def scenario() -> None:
        repository = _Repository()
        runtime = cast(DurableLocalRuntime, SimpleNamespace(admission=_admission(repository)))
        app = create_app(
            Settings(video_storage_root="E:/super7/dataset/raw/social_media"),
            callback_service=_LocalCallbackValidator(),
            durable_runtime=runtime,
        )
        headers = {"Content-Type": "application/json", "Idempotency-Key": "opaque-key"}
        if authorization is not None:
            headers["Authorization"] = authorization
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                rejected = await client.post("/analyze", content=body, headers=headers)
                assert rejected.status_code == 401
                assert rejected.json() == {"detail": {"error": "Apex authentication required."}}
                assert repository.calls == 0
                malformed = await client.post(
                    "/analyze",
                    content=body,
                    headers={**headers, "Authorization": "Bearer ephemeral-test-secret"},
                )
                assert malformed.status_code == 422
                assert repository.calls == 0
                assert repository.stored is None
        finally:
            await app.state.request_lifecycle.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure_type",
    [AnalysisJobPersistenceUnavailable, AnalysisJobAdmissionLockTimeout],
)
def test_durable_admission_unavailability_returns_fixed_503(
    failure_type: type[AnalysisJobPersistenceUnavailable],
) -> None:
    async def scenario() -> None:
        repository = _Repository()
        repository.failure = failure_type("private SQL, credentials and request details")
        runtime = cast(DurableLocalRuntime, SimpleNamespace(admission=_admission(repository)))
        app = create_app(
            Settings(video_storage_root="E:/super7/dataset/raw/social_media"),
            callback_service=_LocalCallbackValidator(),
            durable_runtime=runtime,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/analyze",
                    headers={
                        "Authorization": "Bearer ephemeral-test-secret",
                        "Idempotency-Key": "opaque-key",
                    },
                    json={
                        "videoId": "video",
                        "playerId": "player",
                        "videoUrl": "video.mp4",
                        "callbackUrl": "http://127.0.0.1:8765/callback",
                    },
                )
                assert response.status_code == 503
                assert response.json() == {
                    "detail": {"error": "Analysis admission is temporarily unavailable."}
                }
                assert repository.stored is None
        finally:
            await app.state.request_lifecycle.shutdown()

    asyncio.run(scenario())


def test_opt_in_lifespan_starts_only_durable_actors_and_stops_them() -> None:
    class Actor:
        running = True
        failure: BaseException | None = None

    class Runtime:
        admission = _admission(_Repository())
        worker = Actor()
        dispatcher = Actor()
        starts = 0
        stops = 0

        async def start(self) -> None:
            self.starts += 1

        async def stop(self) -> None:
            self.stops += 1

        async def database_readiness(self) -> DatabaseReadiness:
            return DatabaseReadiness(True, "READY", DatabasePublicChecks(True, True))

    async def scenario() -> None:
        runtime = Runtime()
        app = create_app(
            Settings(video_storage_root=str(Path.cwd())),
            durable_runtime=cast(DurableLocalRuntime, runtime),
        )
        async with app.router.lifespan_context(app):
            assert runtime.starts == 1
            assert runtime.stops == 0
            assert app.state.analysis_queue.metrics().worker_running is False
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                ready = await client.get("/health/ready")
                assert ready.json()["checks"]["durable_worker"] is True
                assert ready.json()["checks"]["durable_dispatcher"] is True
                assert ready.json()["checks"]["database"] is True
        assert runtime.stops == 1

    asyncio.run(scenario())


def test_opt_in_shutdown_preserves_active_primary_and_cleanup_failure() -> None:
    primary = AssertionError("primary")
    cleanup = RuntimeError("cleanup")

    class Runtime:
        admission = _admission(_Repository())

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            raise cleanup

    async def scenario() -> None:
        app = create_app(
            Settings(video_storage_root=str(Path.cwd())),
            durable_runtime=cast(DurableLocalRuntime, Runtime()),
        )
        with pytest.raises(AssertionError) as captured:
            async with app.router.lifespan_context(app):
                raise primary
        assert captured.value is primary
        assert primary.__cause__ is cleanup
        assert cleanup.__context__ is None

    asyncio.run(scenario())
