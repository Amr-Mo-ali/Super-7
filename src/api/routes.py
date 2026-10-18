"""Thin HTTP orchestration for automatic target-player analysis."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import HttpUrl

from adapters.analysis_callback_presentation import callback_payload
from api.durable_admission_router import create_durable_admission_router
from api.request_lifecycle import RequestLifecycle
from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.exceptions import AnalysisError
from diagnostics.analysis_stage_timing import (
    _log_analysis_stage_timing,
)
from diagnostics.job_events import log_job_event
from schemas.analysis import (
    AnalyzeQueuedResponse,
    AnalyzeRequest,
    AnalyzeResponse,
)
from services.analysis_pipeline import _analyze_uploaded
from services.analysis_queue import AnalysisJob, AnalysisJobState, AnalysisQueue
from services.ball_proximity import BallProximityAnalyzer
from services.callback_service import (
    CallbackPayload,
    CallbackService,
    FailedCallbackPayload,
)
from services.durable_local_admission import DurableLocalAdmission
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzerProtocol
from services.movement.analyzer import MovementAnalyzer
from services.pass_detection import PassDetector
from services.player_tracker import AutomaticPlayerTracker
from services.process_contracts import (
    ChildAnalysisRequest,
    ParentCancelled,
    ParentChildResult,
    ParentFailure,
)
from services.scoring.protocols import PhysicalActivityScorerProtocol
from services.selection import TargetPlayerSelector
from services.shot_detection import ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.video_downloader import VideoDownloader
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoValidator


def create_router(
    settings: Settings,
    validator: VideoValidator,
    tracker: AutomaticPlayerTracker,
    selector: TargetPlayerSelector,
    extractor: FeatureExtractor,
    ball_proximity_analyzer: BallProximityAnalyzer,
    movement_analyzer: MovementAnalyzer,
    interaction_analyzer: BallInteractionAnalyzerProtocol,
    technical_event_analyzer: TechnicalEventAnalyzer,
    pass_detector: PassDetector,
    shot_detector: ShotDetector,
    physical_scorer: PhysicalActivityScorerProtocol,
    logger: logging.Logger,
    lifecycle: RequestLifecycle,
    downloader: VideoDownloader,
    path_resolver: VideoPathResolver,
    callback_service: CallbackService,
    analysis_queue: AnalysisQueue,
    durable_admission: DurableLocalAdmission | None = None,
) -> APIRouter:
    """Create the only public route with injected analysis dependencies."""
    if durable_admission is not None:
        return create_durable_admission_router(durable_admission, path_resolver, callback_service)
    router = APIRouter()

    @router.post(
        "/analyze",
        response_model=AnalyzeQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze(
        payload: AnalyzeRequest,
        request: Request,
    ) -> AnalyzeQueuedResponse:
        """Validate a lightweight job and enqueue it for the single background worker."""
        admission_started = perf_counter()
        try:
            path_resolver.validate_reference(payload.video_url)
            callback_service.validate_callback_url(payload.callback_url)
        except (OSError, ValueError, AnalysisError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"error": str(error)}
            ) from error
        analysis_id = str(uuid4())
        job = AnalysisJob.create(
            analysis_id,
            payload.video_id,
            payload.player_id,
            payload.video_url,
            payload.callback_url,
        )
        if not await analysis_queue.submit(job):
            metrics = analysis_queue.metrics()
            log_job_event(
                logger,
                "analysis_admission_rejected",
                analysis_id=analysis_id,
                video_id=payload.video_id,
                player_id=payload.player_id,
                queue_depth=metrics.queued,
                queue_capacity=metrics.capacity,
                active_analysis_count=metrics.active_analysis_count,
                max_active_analyses=metrics.max_active_analyses,
                accepting=metrics.accepting,
                fields={
                    "admission_duration_ms": _milliseconds(perf_counter() - admission_started),
                    "rejection_reason": "shutting_down" if not metrics.accepting else "queue_full",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "Analysis queue is full."},
            )
        metrics = analysis_queue.metrics()
        log_job_event(
            logger,
            "analysis_admission_accepted",
            analysis_id=analysis_id,
            video_id=payload.video_id,
            player_id=payload.player_id,
            queue_depth=metrics.queued,
            queue_capacity=metrics.capacity,
            active_analysis_count=metrics.active_analysis_count,
            max_active_analyses=metrics.max_active_analyses,
            accepting=metrics.accepting,
            fields={"admission_duration_ms": _milliseconds(perf_counter() - admission_started)},
        )
        return AnalyzeQueuedResponse(
            analysis_id=analysis_id,
            video_id=payload.video_id,
            player_id=payload.player_id,
        )

    return router


def _callback_payload(
    request: AnalyzeRequest,
    result: AnalyzeResponse,
) -> CallbackPayload:
    """Build the backend callback body from an already-finalized analysis result."""
    return callback_payload(request.video_id, request.player_id, result)


class ProcessAnalysisExecutor(Protocol):
    """The parent-side process adapter surface needed by one queue-job processor."""

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult: ...


class ProcessCallbackSender(Protocol):
    """The callback delivery surface retained by the parent-side processor."""

    async def send_result(
        self, callback_url: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool: ...


def create_process_analysis_job_processor(
    process_pool: ProcessAnalysisExecutor,
    callback_service: ProcessCallbackSender,
    logger: logging.Logger,
) -> Callable[[AnalysisJob], Awaitable[AnalysisJobState]]:
    """Create an unused parent-owned processor for serialized child analysis results."""

    async def process(job: AnalysisJob) -> AnalysisJobState:
        started = perf_counter()
        try:
            child_request = ChildAnalysisRequest(
                job.analysis_id,
                job.video_id,
                job.player_id,
                job.video_reference,
            )
            result = await process_pool.execute(child_request)
            if isinstance(result, ParentCancelled):
                _log_process_execution(logger, job.analysis_id, "cancelled", started)
                return AnalysisJobState.CANCELLED
            if isinstance(result, ParentFailure):
                _log_process_execution(logger, job.analysis_id, "failed", started)
                await _send_process_failure_callback(
                    callback_service, logger, job, result.error_code, result.public_message
                )
                return AnalysisJobState.FAILED
            mapping_started = perf_counter()
            try:
                request = AnalyzeRequest.model_validate(
                    {
                        "videoId": job.video_id,
                        "playerId": job.player_id,
                        "videoUrl": job.video_reference,
                        "callbackUrl": str(job.callback_url),
                    }
                )
                callback_payload = _callback_payload(request, result)
            except asyncio.CancelledError:
                _log_analysis_stage_timing(
                    logger,
                    analysis_id=job.analysis_id,
                    stage="parent_response_mapping",
                    role="parent",
                    outcome="cancelled",
                    duration_ms=_milliseconds(perf_counter() - mapping_started),
                )
                raise
            except Exception:
                _log_analysis_stage_timing(
                    logger,
                    analysis_id=job.analysis_id,
                    stage="parent_response_mapping",
                    role="parent",
                    outcome="failed",
                    duration_ms=_milliseconds(perf_counter() - mapping_started),
                )
                raise
            _log_analysis_stage_timing(
                logger,
                analysis_id=job.analysis_id,
                stage="parent_response_mapping",
                role="parent",
                outcome="success",
                duration_ms=_milliseconds(perf_counter() - mapping_started),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_process_execution(logger, job.analysis_id, "failed", started, error)
            await _send_process_failure_callback(
                callback_service,
                logger,
                job,
                "ProcessPoolError",
                "Analysis could not be completed.",
            )
            return AnalysisJobState.FAILED
        _log_process_execution(logger, job.analysis_id, "completed", started)
        await _send_process_callback(
            callback_service, logger, job, callback_payload, "analysis_callback_error"
        )
        return AnalysisJobState.COMPLETED

    return process


def _log_process_execution(
    logger: logging.Logger,
    analysis_id: str,
    outcome: str,
    started: float,
    error: Exception | None = None,
) -> None:
    fields = "" if error is None else f" error_type={type(error).__name__}"
    logger.info(
        "analysis_execution_finished analysis_id=%s execution_outcome=%s analysis_duration_ms=%s%s",
        analysis_id,
        outcome,
        _milliseconds(perf_counter() - started),
        fields,
    )


async def _send_process_failure_callback(
    callback_service: ProcessCallbackSender,
    logger: logging.Logger,
    job: AnalysisJob,
    error_code: str,
    public_message: str,
) -> None:
    payload = FailedCallbackPayload(
        request_id=job.analysis_id,
        video_id=job.video_id,
        player_id=job.player_id,
        status="failed",
        summary={},
        ratings={},
        events={},
        error={"code": error_code, "message": public_message},
    )
    await _send_process_callback(
        callback_service, logger, job, payload, "analysis_failure_callback_error"
    )


async def _send_process_callback(
    callback_service: ProcessCallbackSender,
    logger: logging.Logger,
    job: AnalysisJob,
    payload: CallbackPayload | FailedCallbackPayload,
    error_event: str,
) -> None:
    try:
        delivered = await callback_service.send_result(job.callback_url, payload)
    except Exception as error:
        logger.warning(
            "%s analysis_id=%s error_type=%s", error_event, job.analysis_id, type(error).__name__
        )
    else:
        if not delivered:
            logger.warning("analysis_callback_failed analysis_id=%s", job.analysis_id)


def create_analysis_job_processor(
    settings: Settings,
    validator: VideoValidator,
    tracker: AutomaticPlayerTracker,
    selector: TargetPlayerSelector,
    extractor: FeatureExtractor,
    ball_proximity_analyzer: BallProximityAnalyzer,
    movement_analyzer: MovementAnalyzer,
    interaction_analyzer: BallInteractionAnalyzerProtocol,
    technical_event_analyzer: TechnicalEventAnalyzer,
    pass_detector: PassDetector,
    shot_detector: ShotDetector,
    logger: logging.Logger,
    physical_scorer: PhysicalActivityScorerProtocol,
    lifecycle: RequestLifecycle,
    path_resolver: VideoPathResolver,
    callback_service: CallbackService,
) -> Callable[[AnalysisJob], Awaitable[AnalysisJobState]]:
    """Create the one reusable job executor around the established analysis pipeline."""

    async def process(job: AnalysisJob) -> AnalysisJobState:
        started = perf_counter()
        request = AnalyzeRequest.model_validate(
            {
                "videoId": job.video_id,
                "playerId": job.player_id,
                "videoUrl": job.video_reference,
                "callbackUrl": str(job.callback_url),
            }
        )
        try:
            video_path = path_resolver.resolve(job.video_reference)
            result = await lifecycle.execute_with_artifacts(
                job.analysis_id,
                lambda cancellation, artifacts: _analyze_uploaded(
                    settings,
                    validator,
                    tracker,
                    selector,
                    extractor,
                    ball_proximity_analyzer,
                    movement_analyzer,
                    interaction_analyzer,
                    technical_event_analyzer,
                    pass_detector,
                    shot_detector,
                    logger,
                    physical_scorer,
                    job.analysis_id,
                    started,
                    started,
                    video_path,
                    cancellation,
                    artifacts,
                    {},
                ),
            )
            callback_payload = _callback_payload(request, result)
        except AnalysisCancelled:
            logger.info(
                "analysis_execution_finished analysis_id=%s execution_outcome=cancelled "
                "analysis_duration_ms=%s",
                job.analysis_id,
                _milliseconds(perf_counter() - started),
            )
            return AnalysisJobState.CANCELLED
        except Exception as error:
            logger.info(
                "analysis_execution_finished analysis_id=%s execution_outcome=failed "
                "analysis_duration_ms=%s",
                job.analysis_id,
                _milliseconds(perf_counter() - started),
            )
            failure_payload = FailedCallbackPayload(
                request_id=job.analysis_id,
                video_id=job.video_id,
                player_id=job.player_id,
                status="failed",
                summary={},
                ratings={},
                events={},
                error={
                    "code": type(error).__name__,
                    "message": "Analysis could not be completed.",
                },
            )
            try:
                delivered = await callback_service.send_result(job.callback_url, failure_payload)
            except Exception:
                logger.exception("analysis_failure_callback_error analysis_id=%s", job.analysis_id)
            else:
                if not delivered:
                    logger.warning("analysis_callback_failed analysis_id=%s", job.analysis_id)
            logger.exception("analysis_job_pipeline_failed analysis_id=%s", job.analysis_id)
            return AnalysisJobState.FAILED
        logger.info(
            "analysis_execution_finished analysis_id=%s execution_outcome=completed "
            "analysis_duration_ms=%s",
            job.analysis_id,
            _milliseconds(perf_counter() - started),
        )
        try:
            delivered = await callback_service.send_result(job.callback_url, callback_payload)
        except Exception:
            logger.exception("analysis_callback_error analysis_id=%s", job.analysis_id)
        else:
            if not delivered:
                logger.warning("analysis_callback_failed analysis_id=%s", job.analysis_id)
        return AnalysisJobState.COMPLETED

    return process


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))
