"""Small operational health endpoints independent of analysis payload contracts."""

from os import W_OK, access
from pathlib import Path
from tempfile import gettempdir

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from api.request_lifecycle import RequestLifecycle

type HealthChecks = dict[str, bool]
type CombinedHealthChecks = dict[str, HealthChecks]


def create_health_router(lifecycle: RequestLifecycle) -> APIRouter:
    """Create non-inference health endpoints for one application ownership graph."""
    router = APIRouter()

    @router.get("/health/live", response_model=None)
    async def live(request: Request) -> JSONResponse:
        checks = _live_checks(request)
        return _response("live", checks)

    @router.get("/health/ready", response_model=None)
    async def ready(request: Request) -> JSONResponse:
        checks = await _ready_checks(request, lifecycle)
        return _response("ready", checks)

    @router.get("/health", response_model=None)
    async def health(request: Request) -> JSONResponse:
        live_checks = _live_checks(request)
        ready_checks = await _ready_checks(request, lifecycle)
        checks: CombinedHealthChecks = {"live": live_checks, "ready": ready_checks}
        return _response("health", checks, all(live_checks.values()) and all(ready_checks.values()))

    return router


def _live_checks(request: Request) -> HealthChecks:
    return {
        "startup_completed": bool(getattr(request.app.state, "startup_completed", False)),
        "detectors_initialized": bool(getattr(request.app.state, "detectors_initialized", False)),
        "configuration_loaded": bool(getattr(request.app.state, "configuration_loaded", False)),
    }


async def _ready_checks(request: Request, lifecycle: RequestLifecycle) -> HealthChecks:
    metrics = await lifecycle.admission.metrics()
    upload_directory = Path(gettempdir())
    return {
        "admission_controller": lifecycle.admission.accepting,
        "admission_capacity": metrics.active_permits < metrics.max_active_analyses,
        "cancellation_manager": not lifecycle.shutting_down,
        "artifact_manager": lifecycle.artifacts is not None,
        "models_available": bool(getattr(request.app.state, "models_initialized", False)),
        "upload_directory": upload_directory.is_dir() and access(upload_directory, W_OK),
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
