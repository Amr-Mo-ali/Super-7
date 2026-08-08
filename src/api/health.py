"""Small operational health endpoints independent of analysis payload contracts."""

from os import W_OK, access
from pathlib import Path
from tempfile import gettempdir

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from api.request_lifecycle import RequestLifecycle
from services.analysis_queue import AnalysisQueue
from services.video_path_resolver import VideoPathResolver

type HealthChecks = dict[str, bool]
type CombinedHealthChecks = dict[str, HealthChecks]


def create_health_router(
    lifecycle: RequestLifecycle,
    path_resolver: VideoPathResolver,
    analysis_queue: AnalysisQueue,
) -> APIRouter:
    """Create non-inference health endpoints for one application ownership graph."""
    router = APIRouter()

    @router.get("/health/live", response_model=None)
    async def live(request: Request) -> JSONResponse:
        checks = _live_checks(request)
        return _response("live", checks)

    @router.get("/health/ready", response_model=None)
    async def ready(request: Request) -> JSONResponse:
        checks = await _ready_checks(request, lifecycle, path_resolver, analysis_queue)
        return _response("ready", checks)

    @router.get("/health", response_model=None)
    async def health(request: Request) -> JSONResponse:
        live_checks = _live_checks(request)
        ready_checks = await _ready_checks(request, lifecycle, path_resolver, analysis_queue)
        checks: CombinedHealthChecks = {"live": live_checks, "ready": ready_checks}
        return _response("health", checks, all(live_checks.values()) and all(ready_checks.values()))

    return router


def _live_checks(request: Request) -> HealthChecks:
    return {
        "startup_completed": bool(getattr(request.app.state, "startup_completed", False)),
        "detectors_initialized": bool(getattr(request.app.state, "detectors_initialized", False)),
        "configuration_loaded": bool(getattr(request.app.state, "configuration_loaded", False)),
    }


async def _ready_checks(
    request: Request,
    lifecycle: RequestLifecycle,
    path_resolver: VideoPathResolver,
    analysis_queue: AnalysisQueue,
) -> HealthChecks:
    upload_directory = Path(gettempdir())
    storage = path_resolver.storage_root_checks()
    queue = analysis_queue.metrics()
    return {
        "admission_controller": lifecycle.admission.accepting,
        "analysis_queue_capacity": queue.accepting and queue.queued < queue.capacity,
        "analysis_worker": queue.worker_running,
        "cancellation_manager": not lifecycle.shutting_down,
        "artifact_manager": lifecycle.artifacts is not None,
        "models_available": bool(getattr(request.app.state, "models_initialized", False)),
        "upload_directory": upload_directory.is_dir() and access(upload_directory, W_OK),
        "video_storage_exists": storage["exists"],
        "video_storage_readable": storage["readable"],
        "video_storage_accessible": storage["accessible"],
        "video_storage_read_only": storage["read_only"],
    }


def _response(
    name: str,
    checks: HealthChecks | CombinedHealthChecks,
    healthy: bool | None = None,
) -> JSONResponse:
    is_healthy = all(checks.values()) if healthy is None else healthy
    return JSONResponse(
        status_code=status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if is_healthy else "unavailable",
            "component": name,
            "checks": checks,
        },
    )
