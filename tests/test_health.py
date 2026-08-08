"""Health and application-import smoke tests."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from api.request_lifecycle import RequestLifecycle
from concurrency.admission import AdmissionController
from concurrency.executor import AnalysisExecutor
from core.config import Settings
from diagnostics.artifacts import ArtifactManager
from main import app, create_app
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.video_validator import VideoMetadata


def test_fastapi_application_imports_and_exposes_docs() -> None:
    """The module-level FastAPI application is importable."""
    response = asyncio.run(_request("/openapi.json"))

    assert response.status_code == 200
    assert "/analyze" in response.json()["paths"]
    assert {"/health", "/health/live", "/health/ready"} <= set(response.json()["paths"])


def test_live_ready_and_combined_health_endpoints_report_initialized_application() -> None:
    async def scenario() -> None:
        for path in ("/health/live", "/health/ready", "/health"):
            response = await _request(path)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            checks = response.json()["checks"]
            if path == "/health":
                assert all(checks["live"].values())
                assert all(checks["ready"].values())
            else:
                assert all(checks.values())

    asyncio.run(scenario())


class StubTracker:
    model_version = "stub"

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        return TrackingRun((), TrackingDiagnostics(0, 0, 0, 0, 0))


def test_readiness_becomes_unavailable_after_lifecycle_shutdown() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            lifecycle = RequestLifecycle(
                AdmissionController(1), AnalysisExecutor(), ArtifactManager(Path(directory), 1024)
            )
            test_app = create_app(
                Settings(debug_output_dir=directory), tracker=StubTracker(), lifecycle=lifecycle
            )
            transport = httpx.ASGITransport(app=test_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 200
                await lifecycle.shutdown()
                response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["checks"]["admission_controller"] is False
            assert response.json()["checks"]["cancellation_manager"] is False

    asyncio.run(scenario())


async def _request(path: str) -> httpx.Response:
    """Send an in-process ASGI request without a running web server."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)
