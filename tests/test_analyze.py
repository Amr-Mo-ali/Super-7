"""API contract tests for automatic target analysis."""

import asyncio
from pathlib import Path

import cv2
import httpx
import numpy as np

from core.config import Settings
from main import create_app
from services.player_tracker import AutomaticPlayerTracker, TrackingDiagnostics, TrackingRun
from services.selection import PlayerTrack
from services.video_validator import VideoMetadata


class FakeTracker:
    model_version = "fake-1"

    def __init__(self, tracks: tuple[PlayerTrack, ...]) -> None:
        self._tracks = tracks

    def analyze(self, video_path: Path, metadata: VideoMetadata) -> TrackingRun:
        del video_path, metadata
        return TrackingRun(
            self._tracks,
            TrackingDiagnostics(
                10, 10 if self._tracks else 0, len(self._tracks) * 8, len(self._tracks), 0
            ),
        )


def test_analyze_accepts_only_video_and_returns_completed(tmp_path: Path) -> None:
    response = asyncio.run(
        _post(_video(tmp_path), FakeTracker((PlayerTrack(4, 8, 10, 8, 1, 0.9, 2, True),)), {})
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["selected_player"]["track_id"] == 4
    assert "player_id" not in body


def test_analyze_rejects_manual_selection_fields(tmp_path: Path) -> None:
    response = asyncio.run(_post(_video(tmp_path), FakeTracker(()), {"player_id": "bad"}))
    assert response.status_code == 422


def test_analyze_returns_documented_ambiguity(tmp_path: Path) -> None:
    tracks = (
        PlayerTrack(1, 8, 10, 8, 0, 0.9, 0, False),
        PlayerTrack(2, 8, 10, 8, 0, 0.9, 0, False),
    )
    response = asyncio.run(_post(_video(tmp_path), FakeTracker(tracks), {}))
    assert response.status_code == 200
    assert response.json()["status"] == "ambiguous_target"


def test_invalid_video_returns_structured_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.avi"
    path.write_bytes(b"broken")
    response = asyncio.run(_post(path, FakeTracker(()), {}))
    assert response.status_code == 422
    assert "error" in response.json()["detail"]


async def _post(
    path: Path, tracker: AutomaticPlayerTracker, data: dict[str, str]
) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(Settings(selection_margin=0.01), tracker))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with path.open("rb") as file:
            return await client.post(
                "/analyze", data=data, files={"video": (path.name, file, "video/x-msvideo")}
            )


def _video(directory: Path) -> Path:
    path = directory / "video.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 64))  # type: ignore[attr-defined]
    for value in (0, 1, 2):
        writer.write(np.full((64, 64, 3), value, np.uint8))
    writer.release()
    return path
