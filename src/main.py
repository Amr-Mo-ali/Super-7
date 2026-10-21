"""Application entry point."""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from os import environ
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI

from api.health import create_health_router
from api.request_lifecycle import RequestLifecycle
from api.routes import create_process_analysis_job_processor, create_router
from concurrency.admission import AdmissionController
from concurrency.executor import AnalysisExecutor
from config.analysis import DEFAULT_MAX_ACTIVE_ANALYSES
from core.config import Settings
from core.logging import configure_logging, get_logger
from diagnostics.artifacts import ArtifactManager
from services.analysis_queue import AnalysisQueue, AnalysisWorker
from services.callback_service import CallbackService
from services.durable_local_runtime import DurableLocalRuntime
from services.player_tracker import AutomaticPlayerTracker, DetectionOnlyPlayerTracker
from services.process_analysis_pool import ProcessAnalysisPool
from services.process_contracts import ChildAnalysisRequest, ParentChildResult
from services.selection import TargetPlayerSelector
from services.video_downloader import VideoDownloader
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoValidator


class ProcessPoolLifecycle(Protocol):
    def start(self) -> None: ...

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult: ...

    async def shutdown(self) -> None: ...


def create_app(
    settings: Settings | None = None,
    tracker: AutomaticPlayerTracker | None = None,
    selector: TargetPlayerSelector | None = None,
    validator: VideoValidator | None = None,
    lifecycle: RequestLifecycle | None = None,
    downloader: VideoDownloader | None = None,
    path_resolver: VideoPathResolver | None = None,
    callback_service: CallbackService | None = None,
    analysis_queue: AnalysisQueue | None = None,
    process_analysis_pool: ProcessPoolLifecycle | None = None,
    durable_runtime: DurableLocalRuntime | None = None,
) -> FastAPI:
    """Compose services; legacy CV runs in child-owned components via the process pool.

    Parent analysis overrides remain accepted for existing callers; legacy analysis
    runs through ``process_analysis_pool`` and builds its graph in the child.
    """
    if durable_runtime is None and environ.get("SUPER7_LEGACY_IN_MEMORY_ENABLED") != "1":
        raise RuntimeError(
            "Legacy in-memory admission is disabled. "
            "Set SUPER7_LEGACY_IN_MEMORY_ENABLED=1 only for development or tests."
        )
    configure_logging()
    resolved_settings = settings or Settings.from_environment()

    get_logger("football_analysis.startup").info(
        "detector=%s model=%s device=%s enabled=true",
        type(tracker).__name__ if tracker else DetectionOnlyPlayerTracker.__name__,
        resolved_settings.model_path,
        resolved_settings.model_device,
    )
    resolved_lifecycle = lifecycle or RequestLifecycle(
        AdmissionController(max_active_analyses=DEFAULT_MAX_ACTIVE_ANALYSES),
        AnalysisExecutor(),
        ArtifactManager(
            Path(resolved_settings.debug_output_dir),
            resolved_settings.max_upload_bytes,
            retained_sessions=resolved_settings.debug.retained_sessions,
        ),
        request_deadline_seconds=resolved_settings.request_deadline_seconds,
    )
    resolved_path_resolver = path_resolver or VideoPathResolver(
        resolved_settings.video_storage_root
    )
    resolved_callback_service = callback_service or CallbackService(
        resolved_settings.callback_timeout_seconds,
        get_logger("football_analysis.callback"),
    )
    resolved_analysis_queue = analysis_queue or AnalysisQueue(resolved_settings.max_queued_analyses)
    resolved_process_pool = process_analysis_pool or ProcessAnalysisPool(resolved_settings)
    processor = create_process_analysis_job_processor(
        resolved_process_pool, resolved_callback_service, get_logger("football_analysis.api")
    )
    worker = AnalysisWorker(
        resolved_analysis_queue,
        processor,
        get_logger("football_analysis.worker"),
        resolved_settings.analysis_shutdown_grace_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if durable_runtime is None or not getattr(durable_runtime, "remote_video", False):
                resolved_path_resolver.validate_storage_root()
            if durable_runtime is None:
                resolved_process_pool.start()
                await worker.start()
            else:
                await durable_runtime.start()
            yield
        finally:
            if durable_runtime is None:
                worker.begin_shutdown()
                try:
                    await resolved_lifecycle.shutdown()
                finally:
                    try:
                        await worker.shutdown()
                    finally:
                        await resolved_process_pool.shutdown()
            else:
                primary = sys.exception()
                cleanup_errors: list[BaseException] = []
                try:
                    await resolved_lifecycle.shutdown()
                except BaseException as error:
                    cleanup_errors.append(error)
                try:
                    await durable_runtime.stop()
                except BaseException as error:
                    cleanup_errors.append(error)
                if cleanup_errors:
                    cleanup = (
                        cleanup_errors[0]
                        if len(cleanup_errors) == 1
                        else BaseExceptionGroup("Durable app shutdown failed.", cleanup_errors)
                    )
                    if primary is not None and isinstance(cleanup, Exception):
                        for observed_error in cleanup_errors:
                            if observed_error.__context__ is primary:
                                observed_error.__context__ = None
                        raise primary from cleanup
                    if primary is not None:
                        raise cleanup from primary
                    raise cleanup

    app = FastAPI(
        title="Football Analysis MVP",
        version=resolved_settings.analysis_version,
        lifespan=lifespan,
        docs_url=None if durable_runtime is not None else "/docs",
        redoc_url=None if durable_runtime is not None else "/redoc",
        openapi_url=None if durable_runtime is not None else "/openapi.json",
    )
    app.state.admission_controller = resolved_lifecycle.admission
    app.state.analysis_executor = resolved_lifecycle.executor
    app.state.artifact_manager = resolved_lifecycle.artifacts
    app.state.request_lifecycle = resolved_lifecycle
    app.state.analysis_queue = resolved_analysis_queue
    app.state.analysis_worker = worker
    app.state.process_analysis_pool = resolved_process_pool
    app.state.durable_runtime = durable_runtime
    app.state.startup_completed = True
    app.state.detectors_initialized = True
    app.state.configuration_loaded = True
    app.state.models_initialized = True
    app.include_router(
        create_health_router(
            resolved_lifecycle,
            resolved_path_resolver,
            resolved_analysis_queue,
            durable_runtime,
        )
    )
    app.include_router(
        create_router(
            get_logger("football_analysis.api"),
            resolved_path_resolver,
            resolved_callback_service,
            resolved_analysis_queue,
            durable_runtime.admission if durable_runtime is not None else None,
        )
    )

    return app


@asynccontextmanager
async def _disabled_lifespan(_: FastAPI) -> AsyncIterator[None]:
    raise RuntimeError(
        "main:app is disabled. Select an explicit durable role, or opt into "
        "legacy development with SUPER7_LEGACY_IN_MEMORY_ENABLED=1."
    )
    yield


# Importing main must remain safe for the combined durable factory. Without an
# explicit legacy opt-in this ASGI object has no admission route and cannot start.
app = (
    create_app()
    if environ.get("SUPER7_LEGACY_IN_MEMORY_ENABLED") == "1"
    else FastAPI(lifespan=_disabled_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
)
