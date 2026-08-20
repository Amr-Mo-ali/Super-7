"""Application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from services.analysis_composition import create_analysis_components
from services.analysis_queue import AnalysisQueue, AnalysisWorker
from services.callback_service import CallbackService
from services.player_tracker import AutomaticPlayerTracker
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
) -> FastAPI:
    """Compose services; production CV runs in child-owned components via the process pool.

    Parent component overrides remain only for router/legacy compatibility; production
    analysis tests must inject ``process_analysis_pool`` rather than rely on them.
    """
    configure_logging()
    resolved_settings = settings or Settings.from_environment()

    # Router composition still needs these lazy components; child processes own analysis execution.
    components = create_analysis_components(
        resolved_settings,
        player_detector_logger=get_logger("football_analysis.detector"),
        ball_detector_logger=get_logger("football_analysis.ball_detector"),
        tracker_override=tracker,
        selector_override=selector,
        validator_override=validator,
    )
    get_logger("football_analysis.startup").info(
        "detector=%s model=%s device=%s enabled=true",
        components.tracker.__class__.__name__,
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
        resolved_analysis_queue, processor, get_logger("football_analysis.worker")
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            resolved_path_resolver.validate_storage_root()
            resolved_process_pool.start()
            await worker.start()
            yield
        finally:
            worker.begin_shutdown()
            try:
                await resolved_lifecycle.shutdown()
            finally:
                try:
                    await worker.shutdown()
                finally:
                    await resolved_process_pool.shutdown()

    app = FastAPI(
        title="Football Analysis MVP",
        version=resolved_settings.analysis_version,
        lifespan=lifespan,
    )
    app.state.admission_controller = resolved_lifecycle.admission
    app.state.analysis_executor = resolved_lifecycle.executor
    app.state.artifact_manager = resolved_lifecycle.artifacts
    app.state.request_lifecycle = resolved_lifecycle
    app.state.analysis_queue = resolved_analysis_queue
    app.state.analysis_worker = worker
    app.state.process_analysis_pool = resolved_process_pool
    app.state.startup_completed = True
    app.state.detectors_initialized = True
    app.state.configuration_loaded = True
    app.state.models_initialized = True
    app.include_router(
        create_health_router(resolved_lifecycle, resolved_path_resolver, resolved_analysis_queue)
    )
    app.include_router(
        create_router(
            resolved_settings,
            components.validator,
            components.tracker,
            components.selector,
            components.extractor,
            components.ball_proximity_analyzer,
            components.movement_analyzer,
            components.interaction_analyzer,
            components.technical_event_analyzer,
            components.pass_detector,
            components.shot_detector,
            components.physical_scorer,
            get_logger("football_analysis.api"),
            resolved_lifecycle,
            downloader or VideoDownloader(resolved_settings),
            resolved_path_resolver,
            resolved_callback_service,
            resolved_analysis_queue,
        )
    )

    return app


app = create_app()
