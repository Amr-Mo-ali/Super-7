"""API contract tests for public-URL automatic target analysis."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import cv2
import httpx
import numpy as np
from pydantic import HttpUrl

from concurrency.cancellation import CancellationManager
from core.config import Settings
from main import create_app
from services.player_tracker import TrackingDiagnostics, TrackingRun
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
            TrackingDiagnostics(10, 10 if self._tracks else 0, len(self._tracks) * 8, len(self._tracks), 0),
        )


class FakeDownloader:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def download(self, video_url: HttpUrl, cancellation: CancellationManager) -> Iterator[Path]:
        del video_url, cancellation
        yield self._path


def test_analyze_accepts_a_public_video_url_and_returns_v2_completed(tmp_path: Path) -> None:
    response = asyncio.run(_post(_video(tmp_path), {"video_url": "https://cdn.example.com/video.avi"}))
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == body["analysis"]["id"]
    assert body["analysis"]["status"] == "completed"


def test_analyze_propagates_nested_metadata_unchanged(tmp_path: Path) -> None:
    metadata = {"video_id": "video_001", "context": {"players": ["player_123"]}}
    response = asyncio.run(
        _post(_video(tmp_path), {"video_url": "https://cdn.example.com/video.avi", "metadata": metadata})
    )
    assert response.status_code == 200
    assert response.json()["metadata"] == metadata
    assert list(response.json()["metadata"]) == list(metadata)


def test_analyze_rejects_invalid_or_unsupported_urls(tmp_path: Path) -> None:
    response = asyncio.run(_post(_video(tmp_path), {"video_url": "ftp://cdn.example.com/video.avi"}))
    assert response.status_code == 422


def test_analyze_rejects_localhost_url(tmp_path: Path) -> None:
    response = asyncio.run(
        _post(_video(tmp_path), {"video_url": "http://127.0.0.1/video.avi"}, use_fake_downloader=False)
    )
    assert response.status_code == 422
    assert "public IP" in response.json()["detail"]["error"]


async def _post(
    path: Path, payload: dict[str, object], use_fake_downloader: bool = True
) -> httpx.Response:
    tracker = FakeTracker((PlayerTrack(4, 8, 10, 8, 1, 0.9, 2, True),))
    app = create_app(
        Settings(selection_margin=0.01),
        tracker,
        downloader=FakeDownloader(path) if use_fake_downloader else None,  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/analyze", json=payload)


def _video(directory: Path) -> Path:
    path = directory / "video.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 64))  # type: ignore[attr-defined]
    for value in (0, 1, 2):
        writer.write(np.full((64, 64, 3), value, np.uint8))
    writer.release()
    return path
