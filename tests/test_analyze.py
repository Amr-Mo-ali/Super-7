"""Backend integration contract tests for the analyze endpoint."""

import asyncio
import logging
import time
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import HttpUrl

from core.config import Settings
from main import create_app
from schemas.analysis import AnalyzeAcceptedResponse, AnalyzeRequest
from services.analysis_queue import AnalysisQueue
from services.callback_service import CallbackPayload, CallbackService, FailedCallbackPayload
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata, VideoValidator


class FakeResolver:
    def resolve(self, filename: str) -> Path:
        assert filename == "test-video.mp4"
        return Path(__file__)

    def validate_reference(self, filename: str) -> None:
        assert filename == "test-video.mp4"

    def validate_storage_root(self) -> Path:
        return Path(__file__).parent

    def storage_root_checks(self) -> dict[str, bool]:
        return {"exists": True, "readable": True, "accessible": True, "read_only": True}


class FakeValidator:
    def validate(self, path: Path) -> VideoMetadata:
        del path
        return VideoMetadata("mp4", 1, 1, 64, 64, 10, 10)


class FakeTracker:
    model_version = "fake"

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        return TrackingRun(
            (), TrackingDiagnostics(1, 0, 0, 0, 0, rejected_track_reason_breakdown={})
        )


class FailingTracker:
    model_version = "fake"

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        raise RuntimeError("simulated analysis failure")


class FakeCallbackService:
    def __init__(self) -> None:
        self.payloads: list[CallbackPayload | FailedCallbackPayload] = []

    async def send_result(
        self, callback_url: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        del callback_url
        self.payloads.append(payload)
        return True

    def validate_callback_url(self, callback_url: HttpUrl) -> None:
        del callback_url


def test_analyze_accepts_a_valid_backend_request(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="football_analysis.api")
    callback = FakeCallbackService()
    response = asyncio.run(_post(_payload(), callback))
    assert response.status_code == 202
    assert response.json() == {
        "analysisId": response.json()["analysisId"],
        "videoId": "video-123",
        "playerId": "player-456",
        "status": "queued",
    }
    assert set(response.json()) == {"analysisId", "videoId", "playerId", "status"}
    assert "detailed" not in response.json()
    assert "scores" not in response.json()
    assert "schema_version" not in response.json()
    assert callback.payloads == []
    admission = next(
        record.getMessage()
        for record in caplog.records
        if "analysis_admission_accepted" in record.getMessage()
    )
    assert "admission_duration_ms=" in admission
    assert "active_analysis_count=0" in admission
    assert "max_active_analyses=1" in admission
    assert "test-video.mp4" not in admission
    assert "72.62.28.146" not in admission


def test_analyze_rejects_missing_required_fields() -> None:
    response = asyncio.run(_post({"videoId": "video-123"}))
    assert response.status_code == 422


def test_analyze_rejects_an_invalid_callback_url() -> None:
    payload = _payload()
    payload["callbackUrl"] = "not-a-url"
    response = asyncio.run(_post(payload))
    assert response.status_code == 422


def test_analyze_rejects_explicitly_when_the_queue_is_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="football_analysis.api")
        queue = AnalysisQueue(1)
        first = await _post(_payload(), analysis_queue=queue)
        second = await _post(_payload(), analysis_queue=queue)
        assert first.status_code == 202
        assert second.status_code == 503
        assert second.json()["detail"]["error"] == "Analysis queue is full."
        rejection = next(
            record.getMessage()
            for record in caplog.records
            if "analysis_admission_rejected" in record.getMessage()
        )
        assert "rejection_reason=queue_full" in rejection
        assert "admission_duration_ms=" in rejection

    asyncio.run(scenario())


def test_lifespan_worker_delivers_one_final_callback() -> None:
    async def scenario() -> None:
        callback = FakeCallbackService()
        app = _app(callback)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/analyze", json=_payload())
            assert response.status_code == 202
            await _wait_for(lambda: len(callback.payloads) == 1)
        assert callback.payloads[0].status == "no_players_detected"
        assert isinstance(callback.payloads[0], CallbackPayload)
        assert callback.payloads[0].detailed.model_dump(mode="json") == _detailed_nulls()

    asyncio.run(scenario())


def test_lifespan_worker_delivers_failure_callback() -> None:
    async def scenario() -> None:
        callback = FakeCallbackService()
        app = _app(callback, FailingTracker())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/analyze", json=_payload())
            assert response.status_code == 202
            await _wait_for(lambda: len(callback.payloads) == 1)
        assert callback.payloads[0].status == "failed"
        assert callback.payloads[0].error == {
            "code": "RuntimeError",
            "message": "Analysis could not be completed.",
        }
        assert isinstance(callback.payloads[0], FailedCallbackPayload)
        assert "detailed" not in callback.payloads[0].model_dump(mode="json")

    asyncio.run(scenario())


def test_execution_duration_excludes_inline_callback_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="football_analysis")

        def delayed_transport(*_: object) -> int:
            time.sleep(0.02)
            return 204

        callback = CallbackService(
            1,
            logging.getLogger("football_analysis.callback"),
            transport=delayed_transport,
            resolver=lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
        )
        app = _app(callback)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/analyze", json=_payload())
            assert response.status_code == 202
            await _wait_for(
                lambda: any(
                    "analysis_job_terminal" in record.getMessage() for record in caplog.records
                )
            )

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    execution = next(message for message in messages if "analysis_execution_finished" in message)
    callback = next(message for message in messages if "analysis_callback_finished" in message)
    terminal = next(message for message in messages if "analysis_job_terminal" in message)
    assert _milliseconds(callback, "callback_duration_ms") >= 10
    assert _milliseconds(execution, "analysis_duration_ms") < _milliseconds(
        callback, "callback_duration_ms"
    )
    assert _milliseconds(terminal, "end_to_end_duration_ms") >= _milliseconds(
        callback, "callback_duration_ms"
    )


def test_request_aliases_normalize_to_snake_case_fields() -> None:
    payload = _payload()
    request = AnalyzeRequest.model_validate(payload)
    assert request.model_dump(mode="json") == {
        "video_id": "video-123",
        "player_id": "player-456",
        "video_url": "test-video.mp4",
        "callback_url": "http://72.62.28.146/api/video-analysis/webhook",
    }
    assert request.model_dump(mode="json", by_alias=True) == payload


def test_accepted_response_serializes_the_public_contract() -> None:
    response = AnalyzeAcceptedResponse(
        request_id="request-789", video_id="video-123", player_id="player-456"
    )
    assert response.model_dump(mode="json") == {
        "request_id": "request-789",
        "video_id": "video-123",
        "player_id": "player-456",
        "status": "accepted",
    }


async def _post(
    payload: dict[str, str],
    callback: FakeCallbackService | None = None,
    analysis_queue: AnalysisQueue | None = None,
) -> httpx.Response:
    app = _app(callback or FakeCallbackService(), analysis_queue=analysis_queue)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/analyze", json=payload)


def _app(
    callback: CallbackService | FakeCallbackService,
    tracker: FakeTracker | FailingTracker | None = None,
    analysis_queue: AnalysisQueue | None = None,
) -> FastAPI:
    return create_app(
        Settings(),
        tracker=tracker or FakeTracker(),
        validator=cast(VideoValidator, FakeValidator()),
        path_resolver=cast(VideoPathResolver, FakeResolver()),
        callback_service=cast(CallbackService, callback),
        analysis_queue=analysis_queue,
    )


async def _wait_for(predicate: object) -> None:
    for _ in range(100):
        if callable(predicate) and predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background worker did not finish")


def _payload() -> dict[str, str]:
    return {
        "videoId": "video-123",
        "playerId": "player-456",
        "videoUrl": "test-video.mp4",
        "callbackUrl": "http://72.62.28.146/api/video-analysis/webhook",
    }


def _detailed_nulls() -> dict[str, None]:
    return {
        "speed_and_fitness": None,
        "ball_control_and_individual_skill": None,
        "passing_and_playmaking": None,
        "shooting_and_finishing": None,
        "defending_and_duels": None,
        "tactical_intelligence_and_teamwork": None,
        "positioning_and_off_ball_movement": None,
    }


def _milliseconds(message: str, field: str) -> int:
    value = next(part.split("=", 1)[1] for part in message.split() if part.startswith(f"{field}="))
    return int(value)
