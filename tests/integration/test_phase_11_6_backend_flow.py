"""Phase 11.6 request-to-callback verification with an isolated backend simulation."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import Any, cast

import httpx
from fastapi import FastAPI
from process_pool_lifecycle_fake import FakeProcessPoolLifecycle

from core.config import Settings
from core.exceptions import VideoNotFoundError
from main import create_app
from schemas.analysis import Diagnostics, NonCompletedResponse
from services.analysis_queue import AnalysisJobState, AnalysisQueue
from services.callback_service import CallbackPayload, CallbackService, FailedCallbackPayload
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.process_contracts import ChildAnalysisRequest, ParentFailure
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata, VideoValidator

backend_mock_app = cast(
    FastAPI,
    run_path(str(Path(__file__).parents[2] / "integration" / "backend_mock" / "app.py"))["app"],
)


class _Resolver:
    def validate_reference(self, filename: str) -> None:
        if not filename.endswith(".mp4"):
            raise ValueError("unexpected test video reference")

    def resolve(self, filename: str) -> Path:
        self.validate_reference(filename)
        if filename == "missing-video.mp4":
            raise VideoNotFoundError("Requested video file does not exist.")
        return Path(__file__)

    def validate_storage_root(self) -> Path:
        return Path(__file__).parent

    def storage_root_checks(self) -> dict[str, bool]:
        return {"exists": True, "readable": True, "accessible": True, "read_only": True}


class _Validator:
    def validate(self, path: Path) -> VideoMetadata:
        del path
        return VideoMetadata("mp4", 1, 1, 64, 64, 10, 10)


class _NoPlayerTracker:
    model_version = "phase-11-6"

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        return TrackingRun(
            (), TrackingDiagnostics(1, 0, 0, 0, 0, rejected_track_reason_breakdown={})
        )


class _BackendDatabase:
    def __init__(self) -> None:
        self.callbacks: list[CallbackPayload | FailedCallbackPayload] = []
        self.video_analysis_updates: dict[str, CallbackPayload | FailedCallbackPayload] = {}

    def update_from_callback(self, payload: CallbackPayload | FailedCallbackPayload) -> None:
        self.callbacks.append(payload)
        self.video_analysis_updates[payload.video_id] = payload


def test_backend_mock_records_callbacks_and_database_updates() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=backend_mock_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://backend-mock") as client:
            assert (await client.delete("/callbacks")).status_code == 204
            assert (
                await client.post(
                    "/webhook",
                    json={"video_id": "video-123", "status": "completed"},
                )
            ).status_code == 204
            response = await client.get("/callbacks")
        assert response.json() == {
            "callbacks": [{"video_id": "video-123", "status": "completed"}],
            "database_updates": {"video-123": {"video_id": "video-123", "status": "completed"}},
        }

    asyncio.run(scenario())


def test_complete_request_lifecycle_delivers_callback_and_updates_backend(
    caplog: Any,
) -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        pool = FakeProcessPoolLifecycle([_process_response])
        app = _app(database, process_pool=pool)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://super-7") as client:
                assert (await client.get("/health/ready")).status_code == 200
                response = await client.post("/analyze", json=_payload())
            assert response.status_code == 202
            analysis_id = response.json()["analysisId"]
            await _wait_for(lambda: analysis_id in _completed_ids(database))
        callback = database.video_analysis_updates["video-123"]
        assert callback.request_id == analysis_id
        assert callback.status == "no_players_detected"
        assert app.state.analysis_queue.state_of(analysis_id) is AnalysisJobState.COMPLETED
        assert len(pool.requests) == 1
        assert pool.requests[0].analysis_id == analysis_id
        assert pool.requests[0].video_id == "video-123"
        assert pool.requests[0].player_id == "player-456"
        assert pool.requests[0].video_reference == "test-video.mp4"
        assert not hasattr(pool.requests[0], "callback_url")
        assert pool.start_calls == pool.shutdown_calls == 1

    caplog.set_level(logging.INFO)
    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert any("analysis_admission_accepted" in message for message in messages)
    assert any("analysis_job_started" in message for message in messages)
    assert any(
        "analysis_execution_finished" in message and "execution_outcome=completed" in message
        for message in messages
    )
    assert any(
        "analysis_job_terminal" in message and "final_state=COMPLETED" in message
        for message in messages
    )


def test_missing_file_produces_a_sanitized_failure_callback(caplog: Any) -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        pool = FakeProcessPoolLifecycle(
            [ParentFailure("VideoNotFoundError", "Analysis could not be completed.")]
        )
        app = _app(database, process_pool=pool)
        async with app.router.lifespan_context(app):
            response = await _post(app, _payload(video_url="missing-video.mp4"))
            assert response.status_code == 202
            analysis_id = response.json()["analysisId"]
            await _wait_for(
                lambda: app.state.analysis_queue.state_of(analysis_id) is AnalysisJobState.FAILED
            )
        callback = database.callbacks[0]
        assert callback.status == "failed"
        assert callback.error == {
            "code": "VideoNotFoundError",
            "message": "Analysis could not be completed.",
        }
        assert callback.request_id == analysis_id
        assert pool.requests[0].video_reference == "missing-video.mp4"

    caplog.set_level(logging.INFO)
    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert any("analysis_admission_accepted" in message for message in messages)
    assert any(
        "analysis_execution_finished" in message and "execution_outcome=failed" in message
        for message in messages
    )
    assert any(
        "analysis_job_terminal" in message and "final_state=FAILED" in message
        for message in messages
    )


def test_queue_saturation_returns_503_without_callback_or_execution() -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        app = _app(database, queue=AnalysisQueue(2))
        first = await _post(app, _payload("video-one"))
        second = await _post(app, _payload("video-two"))
        rejected = await _post(app, _payload("video-three"))
        assert first.status_code == second.status_code == 202
        assert rejected.status_code == 503
        assert database.callbacks == []

    asyncio.run(scenario())


def test_callback_failure_does_not_change_completed_analysis_state(caplog: Any) -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        app = _app(
            database,
            callback_delivered=False,
            process_pool=FakeProcessPoolLifecycle([_process_response]),
        )
        async with app.router.lifespan_context(app):
            response = await _post(app, _payload())
            analysis_id = response.json()["analysisId"]
            await _wait_for(
                lambda: app.state.analysis_queue.state_of(analysis_id) is AnalysisJobState.COMPLETED
            )
        assert database.callbacks == []

    caplog.set_level(logging.WARNING)
    asyncio.run(scenario())
    assert any("analysis_callback_failed" in record.getMessage() for record in caplog.records)


def test_restart_cancels_accepted_waiting_jobs() -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        queue = AnalysisQueue(2)
        app = _app(database, queue=queue)
        response = await _post(app, _payload())
        analysis_id = response.json()["analysisId"]
        queue.stop_accepting()
        assert queue.state_of(analysis_id) is AnalysisJobState.CANCELLED
        assert database.callbacks == []

    asyncio.run(scenario())


def test_multiple_requests_are_callback_delivered_fifo() -> None:
    async def scenario() -> None:
        database = _BackendDatabase()
        pool = FakeProcessPoolLifecycle([_process_response, _process_response])
        app = _app(database, queue=AnalysisQueue(3), process_pool=pool)
        first = await _post(app, _payload("video-one"))
        second = await _post(app, _payload("video-two"))
        assert first.status_code == second.status_code == 202
        async with app.router.lifespan_context(app):
            await _wait_for(lambda: len(database.callbacks) == 2)
        assert [payload.video_id for payload in database.callbacks] == ["video-one", "video-two"]
        assert [request.video_id for request in pool.requests] == ["video-one", "video-two"]

    asyncio.run(scenario())


async def _post(app: FastAPI, payload: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://super-7") as client:
        return await client.post("/analyze", json=payload)


def _app(
    database: _BackendDatabase,
    queue: AnalysisQueue | None = None,
    callback_delivered: bool = True,
    process_pool: FakeProcessPoolLifecycle | None = None,
) -> FastAPI:
    resolved_pool = (
        process_pool if process_pool is not None else FakeProcessPoolLifecycle([_process_response])
    )
    return create_app(
        Settings(max_queued_analyses=3),
        tracker=_NoPlayerTracker(),
        validator=cast(VideoValidator, _Validator()),
        path_resolver=cast(VideoPathResolver, _Resolver()),
        callback_service=_callback_service(database, callback_delivered),
        analysis_queue=queue,
        process_analysis_pool=resolved_pool,
    )


def _process_response(request: ChildAnalysisRequest) -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id=request.analysis_id,
        status="no_players_detected",
        warnings=[],
        diagnostics=Diagnostics(
            frames_processed=0,
            frames_with_player_detections=0,
            total_person_detections=0,
            tracks_created=0,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
    )


def _payload(video_id: str = "video-123", video_url: str = "test-video.mp4") -> dict[str, str]:
    return {
        "videoId": video_id,
        "playerId": "player-456",
        "videoUrl": video_url,
        "callbackUrl": "http://72.62.28.146/api/video-analysis/webhook",
    }


def _completed_ids(database: _BackendDatabase) -> set[str]:
    return {payload.request_id for payload in database.callbacks}


def _callback_service(database: _BackendDatabase, delivered: bool) -> CallbackService:
    def transport(url: str, body: bytes, timeout: float) -> int:
        del url, timeout
        if not delivered:
            return 500
        payload = json.loads(body)
        callback = (
            FailedCallbackPayload.model_validate(payload)
            if payload["status"] == "failed"
            else CallbackPayload.model_validate(payload)
        )
        database.update_from_callback(callback)
        return 204

    def resolver(*_: object, **__: object) -> list[tuple[Any, ...]]:
        return [(None, None, None, None, ("8.8.8.8", 443))]

    async def no_delay(_: float) -> None:
        return None

    return CallbackService(
        1,
        logging.getLogger("test.phase_11_6.callback"),
        transport=transport,
        resolver=resolver,
        sleep=no_delay,
    )


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background verification did not finish")
