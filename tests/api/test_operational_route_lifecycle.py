"""Route-level characterization for operational isolation without model execution."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

import httpx

from api.request_lifecycle import RequestLifecycle
from concurrency.admission import AdmissionController
from concurrency.executor import AnalysisExecutor
from core.config import Settings
from diagnostics.artifacts import ArtifactManager
from main import create_app
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.video_validator import VideoMetadata, VideoValidator


class FakeValidator:
    def validate(self, path: Path) -> VideoMetadata:
        return VideoMetadata("avi", path.stat().st_size, 1, 64, 64, 10, 10)


class BlockingTracker:
    model_version = "fake"

    def __init__(self, started: Event, release: Event) -> None:
        self.started, self.release, self.calls = started, release, 0

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        self.calls += 1
        self.started.set()
        self.release.wait()
        return TrackingRun((), TrackingDiagnostics(1, 0, 0, 0, 0))


def test_backend_acceptance_does_not_start_route_analysis() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            tracker = BlockingTracker(Event(), Event())
            lifecycle = RequestLifecycle(
                AdmissionController(1), AnalysisExecutor(), ArtifactManager(Path(directory), 1024)
            )
            app = create_app(
                Settings(debug_output_dir=directory),
                tracker,
                validator=cast(VideoValidator, FakeValidator()),
                lifecycle=lifecycle,
            )
            transport = httpx.ASGITransport(app=app)
            held = await lifecycle.admission.admit()
            assert held is not None
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                second = await client.post(
                    "/analyze",
                    json={
                        "videoId": "video-123",
                        "playerId": "player-456",
                        "videoUrl": "y.avi",
                        "callbackUrl": "https://backend.example.com/webhook",
                    },
                )
            await held.release()
            assert second.status_code == 200
            assert second.json()["status"] == "accepted"
            assert tracker.calls == 0
            metrics = await lifecycle.admission.metrics()
            assert (metrics.active_permits, metrics.rejected_analyses) == (0, 0)

    asyncio.run(scenario())


def test_each_application_owns_one_independent_operational_graph() -> None:
    with TemporaryDirectory() as directory:
        first = create_app(
            Settings(debug_output_dir=directory),
            tracker=BlockingTracker(Event(), Event()),
            validator=cast(VideoValidator, FakeValidator()),
        )
        second = create_app(
            Settings(debug_output_dir=directory + "2"),
            tracker=BlockingTracker(Event(), Event()),
            validator=cast(VideoValidator, FakeValidator()),
        )
        assert first.state.request_lifecycle is first.state.request_lifecycle
        assert first.state.admission_controller is not second.state.admission_controller
        assert first.state.analysis_executor is not second.state.analysis_executor
