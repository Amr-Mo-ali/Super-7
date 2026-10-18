"""Explicit API-only ASGI factory; no worker, dispatcher, or model composition."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from os import environ

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from api.durable_admission_router import create_durable_admission_router
from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import configure_logging, get_logger
from services.callback_service import CallbackService
from services.cleanup_exception_links import remove_primary_backlinks
from services.durable_api_runtime import DurableApiRuntime
from services.video_path_resolver import VideoPathResolver


def _required(name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for durable API mode.")
    return value


def create_app(
    *,
    settings: Settings | None = None,
    runtime: DurableApiRuntime | None = None,
    path_resolver: VideoPathResolver | None = None,
    callback_service: CallbackService | None = None,
) -> FastAPI:
    """Construct the API role only after explicit opt-in."""
    if environ.get("SUPER7_DURABLE_API_ENABLED") != "1":
        raise RuntimeError("Durable API role is not enabled.")
    configure_logging()
    resolved_settings = settings or Settings(
        callback_timeout_seconds=float(environ.get("CALLBACK_TIMEOUT_SECONDS", 10.0)),
        max_queued_analyses=int(environ.get("MAX_QUEUED_ANALYSES", 10)),
        video_storage_root=environ.get("VIDEO_STORAGE_ROOT", "/videos"),
    )
    if runtime is None:
        runtime = DurableApiRuntime(
            settings=resolved_settings,
            database_settings=DatabaseSettings.from_mapping(environ),
            bearer_secret=_required("SUPER7_APEX_BEARER_SECRET"),
            idempotency_hmac_secret=bytes.fromhex(_required("SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX")),
            attempt_limit=int(_required("SUPER7_DURABLE_ATTEMPT_LIMIT")),
            operation_timeout_ms=int(_required("SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS")),
            lock_timeout_ms=int(_required("SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS")),
        )
    resolved_path = path_resolver or VideoPathResolver(resolved_settings.video_storage_root)
    resolved_callback = callback_service or CallbackService(
        resolved_settings.callback_timeout_seconds, get_logger("football_analysis.callback")
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            primary = sys.exception()
            cleanup_error: BaseException | None = None
            try:
                await runtime.stop()
            except BaseException as error:
                cleanup_error = error
            if cleanup_error is not None:
                if primary is None:
                    raise cleanup_error
                if isinstance(cleanup_error, Exception):
                    cleanup_error = remove_primary_backlinks(cleanup_error, primary)
                if cleanup_error is not primary:
                    if isinstance(cleanup_error, Exception):
                        previous_cause = primary.__cause__
                        if previous_cause is not None and previous_cause is not cleanup_error:
                            cleanup_error = BaseExceptionGroup(
                                "Primary and cleanup causes.", [previous_cause, cleanup_error]
                            )
                        raise primary from cleanup_error
                    previous_cause = cleanup_error.__cause__
                    if previous_cause is not None and previous_cause is not primary:
                        raise cleanup_error from BaseExceptionGroup(
                            "Fatal cleanup and primary causes.", [previous_cause, primary]
                        )
                    raise cleanup_error from primary

    app = FastAPI(
        title="Football Analysis MVP",
        version=resolved_settings.analysis_version,
        lifespan=lifespan,
    )
    app.state.durable_runtime = runtime
    app.include_router(
        create_durable_admission_router(runtime.admission, resolved_path, resolved_callback)
    )
    health = APIRouter()

    @health.get("/health/live", response_model=None)
    async def live() -> JSONResponse:
        return _health("live", {"startup_completed": runtime.started})

    @health.get("/health/ready", response_model=None)
    async def ready() -> JSONResponse:
        checks = {
            "api_started": runtime.started,
            "database": (await runtime.database_readiness()).ready,
        }
        return _health("ready", checks)

    @health.get("/health", response_model=None)
    async def combined() -> JSONResponse:
        live_checks = {"startup_completed": runtime.started}
        ready_checks = {
            "api_started": runtime.started,
            "database": (await runtime.database_readiness()).ready,
        }
        healthy = all(live_checks.values()) and all(ready_checks.values())
        return _health("health", {"live": live_checks, "ready": ready_checks}, healthy)

    app.include_router(health)
    return app


def _health(name: str, checks: Mapping[str, object], healthy: bool | None = None) -> JSONResponse:
    is_healthy = all(checks.values()) if healthy is None else healthy
    return JSONResponse(
        status_code=200 if is_healthy else 503,
        content={
            "status": "ok" if is_healthy else "unavailable",
            "component": name,
            "checks": checks,
        },
    )
