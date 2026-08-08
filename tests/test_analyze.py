"""Backend integration contract tests for the analyze endpoint."""

import asyncio
from pathlib import Path
from typing import cast

import httpx

from core.config import Settings
from main import create_app
from schemas.analysis import AnalyzeAcceptedResponse, AnalyzeRequest
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata, VideoValidator


class FakeResolver:
    def resolve(self, filename: str) -> Path:
        assert filename == "test-video.mp4"
        return Path(__file__)


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


def test_analyze_accepts_a_valid_backend_request() -> None:
    response = asyncio.run(_post(_payload()))
    assert response.status_code == 200
    assert response.json() == {
        "request_id": response.json()["request_id"],
        "video_id": "video-123",
        "player_id": "player-456",
        "status": "accepted",
    }


def test_analyze_rejects_missing_required_fields() -> None:
    response = asyncio.run(_post({"videoId": "video-123"}))
    assert response.status_code == 422


def test_analyze_rejects_an_invalid_callback_url() -> None:
    payload = _payload()
    payload["callbackUrl"] = "not-a-url"
    response = asyncio.run(_post(payload))
    assert response.status_code == 422


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


async def _post(payload: dict[str, str]) -> httpx.Response:
    app = create_app(
        Settings(),
        tracker=FakeTracker(),
        validator=cast(VideoValidator, FakeValidator()),
        path_resolver=cast(VideoPathResolver, FakeResolver()),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/analyze", json=payload)


def _payload() -> dict[str, str]:
    return {
        "videoId": "video-123",
        "playerId": "player-456",
        "videoUrl": "test-video.mp4",
        "callbackUrl": "http://72.62.28.146/api/video-analysis/webhook",
    }
