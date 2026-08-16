"""Thin HTTP orchestration for automatic target-player analysis."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path
from shutil import copyfile
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import HttpUrl

from api.public_rating_mapper import game_intelligence_result, public_rating_v2
from api.request_lifecycle import RequestLifecycle
from concurrency.cancellation import CancellationChecker, CancellationManager
from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.exceptions import AnalysisError, InternalDiagnosticsError
from core.reproducibility import metadata as reproducibility_metadata
from diagnostics.artifacts import ArtifactSession
from diagnostics.performance import current_collector
from schemas.analysis import (
    AmbiguousResponse,
    AnalyzeQueuedResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    BallLossCandidateResponse,
    CompletedResponse,
    ControlledMovementCandidateResponse,
    Diagnostics,
    DribbleCandidateResponse,
    FeatureMetric,
    InteractionAnalysisResponse,
    InteractionSegmentResponse,
    NonCompletedResponse,
    PassCandidateResponse,
    PassDetectionResponse,
    PipelineTiming,
    SelectedPlayer,
    ShotCandidateResponse,
    ShotDetectionResponse,
    StageGate,
    TechnicalEventAnalysisResponse,
    TrackingResponse,
    VideoResponse,
)
from services.analysis_queue import AnalysisJob, AnalysisJobState, AnalysisQueue
from services.ball_proximity import BallProximityAnalyzer, BallProximityResult
from services.callback_service import (
    CallbackPayload,
    CallbackService,
    DetailedRatings,
    FailedCallbackPayload,
)
from services.camera_motion import CameraMotionEstimator
from services.camera_motion import diagnostics as camera_motion_diagnostics
from services.debug_renderer import render_debug_video
from services.detailed_rating.engine import DetailedRatingEngine
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzerProtocol
from services.interactions.models import (
    BallObservation,
    InteractionAnalysisResult,
    PlayerObservation,
)
from services.movement.analyzer import MovementAnalyzer
from services.movement.schemas import MovementResult
from services.pass_detection import PASS_DETECTION_VERSION, PassDetectionResult, PassDetector
from services.player_rating.engine import PlayerRatingEngine
from services.player_rating.game_intelligence import (
    GameIntelligenceEvidenceDiagnostics,
    GameIntelligenceResult,
)
from services.player_tracker import AutomaticPlayerTracker, TrackingDiagnostics
from services.scoring.models import PhysicalEvidenceDiagnostics, PhysicalScoreResult
from services.scoring.protocols import PhysicalActivityScorerProtocol
from services.scoring.technical import TechnicalScorer, TechnicalScoreResult
from services.segment_ball import QUALITY_VERSION as SEGMENT_BALL_QUALITY_VERSION
from services.segment_ball import RECONSTRUCTION_VERSION, reconstruct
from services.segment_selection import build_segments, rejection_diagnostics, select_segment
from services.selection import Selection, TargetPlayerSelector
from services.shot_detection import SHOT_DETECTION_VERSION, ShotDetectionResult, ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.technical_events.models import (
    TechnicalEventAnalysisResult,
    TechnicalEvidenceDiagnostics,
)
from services.video_downloader import VideoDownloader
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata, VideoValidator


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
) -> APIRouter:
    """Create the only public route with injected analysis dependencies."""
    router = APIRouter()

    @router.post(
        "/analyze",
        response_model=AnalyzeQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze(
        payload: AnalyzeRequest,
    ) -> AnalyzeQueuedResponse:
        """Validate a lightweight job and enqueue it for the single background worker."""
        analysis_id = str(uuid4())
        try:
            path_resolver.validate_reference(payload.video_url)
            callback_service.validate_callback_url(payload.callback_url)
        except (OSError, ValueError, AnalysisError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"error": str(error)}
            ) from error
        job = AnalysisJob.create(
            analysis_id,
            payload.video_id,
            payload.player_id,
            payload.video_url,
            payload.callback_url,
        )
        if not await analysis_queue.submit(job):
            logger.warning(
                "analysis_queue_full analysis_id=%s video_id=%s player_id=%s queue_depth=%s",
                analysis_id,
                payload.video_id,
                payload.player_id,
                analysis_queue.metrics().queued,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "Analysis queue is full."},
            )
        logger.info(
            "analysis_job_queued analysis_id=%s video_id=%s player_id=%s queue_depth=%s",
            analysis_id,
            payload.video_id,
            payload.player_id,
            analysis_queue.metrics().queued,
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
    public_result = public_rating_v2(result)
    serialized = public_result.model_dump(mode="json")
    detailed = (
        DetailedRatingEngine().evaluate(
            result.scores.physical,
            result.technical_event_analysis,
            result.diagnostics.technical_event_analysis_quality,
        )
        if isinstance(result, CompletedResponse)
        else DetailedRatings()
    )
    return CallbackPayload(
        request_id=result.analysis_id,
        video_id=request.video_id,
        player_id=request.player_id,
        status=result.status,
        summary=serialized.get("summary", {}),
        ratings=serialized.get("ratings", {}),
        overall=serialized.get("overall"),
        detailed=detailed,
        events=serialized.get("events", {}),
    )


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
            return AnalysisJobState.CANCELLED
        except Exception as error:
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
        try:
            delivered = await callback_service.send_result(job.callback_url, callback_payload)
        except Exception:
            logger.exception("analysis_callback_error analysis_id=%s", job.analysis_id)
        else:
            if not delivered:
                logger.warning("analysis_callback_failed analysis_id=%s", job.analysis_id)
        return AnalysisJobState.COMPLETED

    return process


def _analyze_downloaded(
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
    analysis_id: str,
    started: float,
    detection_started: float,
    video_url: HttpUrl,
    downloader: VideoDownloader,
    cancellation: CancellationManager,
    artifacts: ArtifactSession,
    request_metadata: dict[str, Any],
) -> AnalyzeResponse:
    """Download one public URL before invoking the unchanged local-file pipeline."""
    with downloader.download(video_url, cancellation) as video_path:
        return _analyze_uploaded(
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
            analysis_id,
            started,
            detection_started,
            video_path,
            cancellation,
            artifacts,
            request_metadata,
        )


def _analyze_uploaded(
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
    analysis_id: str,
    started: float,
    detection_started: float,
    video_path: Path,
    cancellation: CancellationManager,
    artifacts: ArtifactSession,
    request_metadata: dict[str, Any],
) -> AnalyzeResponse:
    """Existing synchronous analysis path, executed by the lifecycle worker boundary."""
    checker = CancellationChecker(cancellation)
    profiler = current_collector()
    checker.check("upload validation")
    if profiler is None:
        metadata = validator.validate(video_path)
    else:
        with profiler.stage("video_validation"):
            metadata = validator.validate(video_path)
    checker.check("detection")
    if profiler is None:
        run = tracker.analyze(video_path, metadata)
    else:
        with profiler.stage("tracking_total"):
            run = tracker.analyze(video_path, metadata)
        profiler.set_video(
            metadata.duration_seconds, run.diagnostics.frames_processed, metadata.file_size_bytes
        )
    checker.check("tracking")
    detection_tracking_time_ms = round((perf_counter() - detection_started) * 1000)
    reproducibility = reproducibility_metadata(video_path, tracker.model_version)
    debug_source: Path | None = None
    if settings.debug.enabled and (settings.debug.save_video or settings.debug.save_frames):
        source = artifacts.reserve(f"source_video{video_path.suffix}", metadata.file_size_bytes)
        debug_source = artifacts.create(source)
        copyfile(video_path, debug_source)
        debug_source = artifacts.finalize(source)
        artifacts.retain()
    if run.diagnostics.total_person_detections == 0:
        return _noncompleted(
            analysis_id,
            "no_players_detected",
            "No player detections were produced.",
            run.diagnostics,
            0,
            request_metadata,
        )
    if not run.tracks:
        return _noncompleted(
            analysis_id,
            "player_detection_completed_tracking_not_available",
            "Player detection completed; multi-object tracking is not available.",
            run.diagnostics,
            0,
            request_metadata,
        )
    selection_diagnostics = run.diagnostics
    ranked: tuple[Selection, ...]
    selection_started = perf_counter()
    checker.check("player selection")
    if (
        settings.target_selection_mode == "segment"
        and run.player_boxes is not None
        and run.player_confidences is not None
    ):
        if profiler is None:
            segments = build_segments(
                run.player_boxes,
                run.player_confidences,
                run.ball_points or {},
                metadata.fps,
                settings,
            )
        else:
            with profiler.stage("player_selection"):
                segments = build_segments(
                    run.player_boxes,
                    run.player_confidences,
                    run.ball_points or {},
                    metadata.fps,
                    settings,
                )
        rejected, breakdown = rejection_diagnostics(run.tracks, segments, metadata.fps)
        selection_diagnostics = replace(
            run.diagnostics,
            rejected_tracks=tuple(rejected),
            rejected_track_reason_breakdown=breakdown,
        )
        selected = select_segment(segments)
        ranked = (selected,) if selected is not None else ()
    else:
        if profiler is None:
            ranked = selector.rank(run.tracks)
        else:
            with profiler.stage("player_selection"):
                ranked = selector.rank(run.tracks)
    selection_time_ms = round((perf_counter() - selection_started) * 1000)
    if not ranked:
        return _noncompleted(
            analysis_id,
            "no_valid_tracks",
            "No track passed the configured quality thresholds.",
            selection_diagnostics,
            0,
            request_metadata,
        )
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < settings.selection_margin:
        return AmbiguousResponse(
            analysis_id=analysis_id,
            status="ambiguous_target",
            candidate_count=len(ranked),
            warnings=[
                "The system could not identify one target player with sufficient confidence."
            ],
            metadata=request_metadata,
        )
    return _completed(
        settings,
        tracker.model_version,
        metadata,
        ranked[0],
        extractor,
        run,
        len(ranked),
        ball_proximity_analyzer,
        movement_analyzer,
        interaction_analyzer,
        technical_event_analyzer,
        pass_detector,
        shot_detector,
        logger,
        physical_scorer,
        analysis_id,
        started,
        selection_diagnostics,
        PipelineTiming(
            player_detection_time_ms=detection_tracking_time_ms,
            tracking_time_ms=detection_tracking_time_ms,
            segment_selection_time_ms=selection_time_ms,
        ),
        reproducibility,
        debug_source,
        checker,
        request_metadata,
    )


def _noncompleted(
    analysis_id: str,
    response_status: Literal[
        "invalid_video",
        "no_players_detected",
        "no_valid_tracks",
        "failed",
        "player_detection_completed_tracking_not_available",
    ],
    warning: str,
    diagnostics: TrackingDiagnostics,
    candidates: int,
    metadata: dict[str, Any] | None = None,
) -> NonCompletedResponse:
    """Return a truthful non-completed status with detector/tracker diagnostics."""
    return NonCompletedResponse(
        analysis_id=analysis_id,
        status=response_status,
        candidate_count=candidates,
        warnings=[warning],
        diagnostics=Diagnostics(**asdict(diagnostics), valid_candidate_tracks=candidates),
        pipeline_state="DETECTION"
        if response_status == "no_players_detected"
        else "PLAYER_SELECTION",
        quality_gates={
            "pipeline": StageGate(quality=0.0, status="rejected", failure_reasons=[warning])
        },
        metadata=metadata or {},
    )


def _log_rating_evidence(
    logger: logging.Logger,
    analysis_id: str,
    physical: PhysicalScoreResult | None,
    technical_events: TechnicalEventAnalysisResult | None,
    technical_score: TechnicalScoreResult,
    game_intelligence: GameIntelligenceResult,
    interaction: InteractionAnalysisResult | None,
    pass_detection: PassDetectionResult | None,
    shot_detection: ShotDetectionResult | None,
) -> None:
    """Emit the completed analysis' gate inputs, decisions, and event evidence once."""
    try:
        technical_gate = technical_events.diagnostics.evidence_gate if technical_events else None
        physical_gate = physical.evidence_gate if physical else None
        game_gate = game_intelligence.evidence_gate
        logger.info(
            "rating_evidence analysis_id=%s technical_gate=%s technical_failed_reasons=%s "
            "physical_gate=%s physical_failed_reasons=%s game_gate=%s game_failed_reasons=%s "
            "technical_status=%s technical_reason=%s physical_status=%s physical_reason=%s "
            "game_status=%s game_reason=%s controlled_movements=%s dribbles=%s ball_losses=%s "
            "possible_ball_interactions=%s interaction_segments=%s accepted_interaction_segments=%s "
            "pass_detection_available=%s pass_candidates=%s accepted_passes=%s "
            "shot_detection_available=%s shot_candidates=%s accepted_shots=%s",
            analysis_id,
            _gate_log_value(technical_gate),
            technical_gate.failed_reasons
            if technical_gate
            else ("technical_event_analysis_unavailable",),
            _gate_log_value(physical_gate),
            physical_gate.failed_reasons if physical_gate else ("physical_score_unavailable",),
            _gate_log_value(game_gate),
            game_gate.failed_reasons if game_gate else ("game_intelligence_unavailable",),
            technical_score.status,
            technical_score.reason,
            physical.status if physical is not None else None,
            physical.reason if physical is not None else None,
            game_intelligence.status,
            game_intelligence.reason,
            len(technical_events.controlled_movement_candidates)
            if technical_events is not None
            else 0,
            len(technical_events.dribble_candidates) if technical_events is not None else 0,
            len(technical_events.ball_loss_candidates) if technical_events is not None else 0,
            interaction.possible_ball_interaction_count if interaction else 0,
            len(interaction.segments) if interaction else 0,
            interaction.diagnostics.accepted_interaction_segments if interaction else 0,
            pass_detection is not None,
            pass_detection.raw_pass_candidates if pass_detection is not None else None,
            pass_detection.accepted_pass_candidates if pass_detection is not None else None,
            shot_detection is not None,
            shot_detection.raw_shot_candidates if shot_detection is not None else None,
            shot_detection.accepted_shot_candidates if shot_detection is not None else None,
        )
    except Exception:
        logger.exception(
            "rating_evidence_logging_failed analysis_id=%s",
            analysis_id,
        )


def _log_interaction_evidence(
    logger: logging.Logger,
    analysis_id: str,
    selection: Selection,
    metadata: VideoMetadata,
    player_observation_count: int,
    ball_observation_count: int,
    player_tracking_quality: float,
    ball_analysis_quality: float,
    interaction: InteractionAnalysisResult,
) -> None:
    """Emit existing interaction evidence counters without affecting analysis results."""
    try:
        diagnostics = interaction.diagnostics
        aligned = diagnostics.interaction_aligned_frames
        rejection_counts = {
            "short": diagnostics.rejected_short_interaction_segments,
            "low_confidence": diagnostics.rejected_low_confidence_interaction_segments,
            "low_global_quality": diagnostics.rejected_low_global_quality_interaction_segments,
            "invalid": diagnostics.rejected_invalid_interaction_segments,
        }
        logger.warning(
            "interaction_evidence analysis_id=%s track_id=%s player_observation_count=%s "
            "player_visible_duration_seconds=%s player_tracking_quality=%s "
            "ball_observation_count=%s ball_analysis_quality=%s "
            "aligned_player_ball_evidence_frames=%s player_ball_evidence_overlap_ratio=%s "
            "proximity_qualified_frames=%s proximity_ratio=%s raw_interaction_segments=%s "
            "accepted_interaction_segments=%s rejection_counts=%s",
            analysis_id,
            selection.track.track_id,
            player_observation_count,
            selection.segment_duration_seconds
            if selection.segment_duration_seconds is not None
            else selection.track.visible_frames / metadata.fps,
            player_tracking_quality,
            ball_observation_count,
            ball_analysis_quality,
            aligned,
            diagnostics.interaction_evidence_coverage_ratio,
            diagnostics.interaction_candidate_frames,
            diagnostics.interaction_candidate_frames / aligned if aligned else None,
            diagnostics.raw_interaction_segments,
            diagnostics.accepted_interaction_segments,
            rejection_counts,
        )
    except Exception:
        logger.exception("interaction_evidence_logging_failed analysis_id=%s", analysis_id)


def _gate_log_value(
    gate: TechnicalEvidenceDiagnostics
    | PhysicalEvidenceDiagnostics
    | GameIntelligenceEvidenceDiagnostics
    | None,
) -> dict[str, object] | None:
    """Keep the structured log compact while preserving every gate input and threshold."""
    if gate is None:
        return None
    raw = asdict(gate)
    thresholds = raw.get("thresholds")
    values: dict[str, object] = {}
    if isinstance(thresholds, dict):
        for name, threshold in thresholds.items():
            value = raw[name]
            values[name] = {
                "value": value,
                "threshold": threshold,
                "passed": value is not None and value >= threshold,
            }
    else:
        values = {
            name: value
            for name, value in raw.items()
            if name not in {"failed_reasons", "component_failed_reasons"}
        }
        if "visible_duration_seconds" in values:
            values["visible_duration_passed"] = (
                raw["visible_duration_seconds"] is not None
                and raw["visible_duration_seconds"] >= raw["minimum_visible_duration_seconds"]
            )
        values["available_component_count_passed"] = (
            raw["available_component_count"] >= raw["minimum_available_component_count"]
        )
    for name in ("minimum_visible_duration_seconds", "minimum_available_component_count"):
        if name in raw:
            values[name] = raw[name]
    if "component_failed_reasons" in raw:
        values["component_failed_reasons"] = raw["component_failed_reasons"]
    return values


def _completed(
    settings: Settings,
    model_version: str,
    metadata: VideoMetadata,
    selection: Selection,
    extractor: FeatureExtractor,
    run: object,
    valid_candidate_tracks: int,
    analyzer: BallProximityAnalyzer,
    movement_analyzer: MovementAnalyzer,
    interaction_analyzer: BallInteractionAnalyzerProtocol,
    technical_event_analyzer: TechnicalEventAnalyzer,
    pass_detector: PassDetector,
    shot_detector: ShotDetector,
    logger: logging.Logger,
    physical_scorer: PhysicalActivityScorerProtocol,
    analysis_id: str,
    started: float,
    selection_diagnostics: TrackingDiagnostics | None = None,
    timing: PipelineTiming | None = None,
    analysis_metadata: dict[str, str | None] | None = None,
    debug_source: Path | None = None,
    cancellation: CancellationChecker | None = None,
    request_metadata: dict[str, Any] | None = None,
) -> CompletedResponse:
    """Map a pure selection result to the successful public contract."""
    track = selection.track
    if cancellation is not None:
        cancellation.check("ball reconstruction")
    from services.player_tracker import TrackingRun

    typed_run = run if isinstance(run, TrackingRun) else None
    if typed_run is not None and selection.segment_start_frame is not None:
        start, end = selection.segment_start_frame, selection.segment_end_frame
        assert end is not None
        typed_run = TrackingRun(
            typed_run.tracks,
            typed_run.diagnostics,
            {
                track_id: {f: box for f, box in boxes.items() if start <= f <= end}
                for track_id, boxes in (typed_run.player_boxes or {}).items()
            },
            {
                track_id: {f: value for f, value in values.items() if start <= f <= end}
                for track_id, values in (typed_run.player_confidences or {}).items()
            },
            {f: point for f, point in (typed_run.ball_points or {}).items() if start <= f <= end},
            typed_run.ball_detection_confidences,
            typed_run.ball_track_segments,
            typed_run.ball_warning,
            {
                f: candidates
                for f, candidates in (typed_run.ball_candidates or {}).items()
                if start <= f <= end
            },
        )
    if cancellation is not None:
        cancellation.check("movement analysis")
    camera_motion = None
    if debug_source is not None:
        if cancellation is not None:
            cancellation.check("camera-motion estimation")
        try:
            camera_motion = CameraMotionEstimator().estimate(
                debug_source, selection.segment_start_frame or 0, selection.segment_end_frame
            )
        except Exception:
            logger.exception("camera_motion_estimation_failed analysis_id=%s", analysis_id)
    camera_diagnostics = camera_motion_diagnostics(camera_motion) if camera_motion else None
    global_quality = _ball_quality(
        typed_run.diagnostics.accepted_ball_track_observations if typed_run else 0,
        typed_run.diagnostics.frames_processed if typed_run else 0,
        tuple(
            point.confidence
            for point in (typed_run.ball_points or {}).values()
            if point.visible and point.confidence is not None
        )
        if typed_run
        else (),
        typed_run.ball_track_segments if typed_run else 0,
        typed_run.diagnostics.frames_with_multiple_ball_candidates if typed_run else 0,
        typed_run.diagnostics.rejected_ball_candidates if typed_run else 0,
        len(typed_run.ball_detection_confidences) if typed_run else 0,
    )
    stage_timing = timing or PipelineTiming()
    ball_started = perf_counter()
    segment_ball = None
    if typed_run is not None and selection.segment_start_frame is not None:
        segment_ball = reconstruct(
            typed_run.ball_candidates or {},
            selection.segment_start_frame,
            selection.segment_end_frame or selection.segment_start_frame,
            metadata.fps,
            settings,
        )
        typed_run = TrackingRun(
            typed_run.tracks,
            typed_run.diagnostics,
            typed_run.player_boxes,
            typed_run.player_confidences,
            segment_ball.points,
            typed_run.ball_detection_confidences,
            typed_run.ball_track_segments,
            typed_run.ball_warning,
            typed_run.ball_candidates,
        )
    proximity: BallProximityResult | None = None
    ball_reason: str | None = "Ball detection was unavailable for this video."
    if (
        typed_run is not None
        and typed_run.ball_warning is None
        and typed_run.ball_points is not None
        and typed_run.player_boxes is not None
    ):
        try:
            proximity = analyzer.analyze(
                typed_run.player_boxes.get(track.track_id, {}), typed_run.ball_points, metadata.fps
            )
            if proximity.ball_visible_frames < settings.ball_minimum_visible_frames:
                ball_reason = (
                    "The ball was visible in too few frames for reliable proximity analysis."
                )
                proximity = None
            else:
                ball_reason = None
        except Exception:
            ball_reason = "Ball detection was unavailable for this video."
    proximity_visible = proximity.ball_visible_frames if proximity is not None else 0
    ball_frames = proximity.ball_proximity_frames if proximity is not None else 0
    confidences = typed_run.ball_detection_confidences if typed_run is not None else ()
    accepted_confidences = (
        tuple(
            point.confidence
            for point in (typed_run.ball_points or {}).values()
            if point.visible and point.confidence is not None
        )
        if typed_run is not None
        else ()
    )
    quality = _ball_quality(
        proximity_visible,
        track.total_frames,
        accepted_confidences,
        typed_run.ball_track_segments if typed_run is not None else 0,
        typed_run.diagnostics.frames_with_multiple_ball_candidates if typed_run else 0,
        typed_run.diagnostics.rejected_ball_candidates if typed_run else 0,
        len(confidences),
    )
    if segment_ball is not None:
        quality = segment_ball.quality or 0.0
    if proximity is not None and segment_ball is None and quality < settings.ball_minimum_quality:
        proximity = None
        ball_reason = "Ball tracking quality was insufficient for reliable proximity analysis."
    pass_detection: PassDetectionResult | None = None
    shot_detection: ShotDetectionResult | None = None
    if (
        typed_run is not None
        and typed_run.player_boxes is not None
        and typed_run.ball_points is not None
    ):
        pass_detection = pass_detector.analyze(
            typed_run.player_boxes,
            typed_run.player_confidences or {},
            typed_run.ball_points,
            metadata.fps,
        )
        shot_detection = shot_detector.analyze(
            typed_run.player_boxes,
            typed_run.player_confidences or {},
            typed_run.ball_points,
            metadata.fps,
        )
        if cancellation is not None:
            cancellation.check("pass and shot detection")
    stage_timing = stage_timing.model_copy(
        update={"ball_processing_time_ms": round((perf_counter() - ball_started) * 1000)}
    )
    movement: MovementResult | None = None
    movement_reason: str | None = None
    if typed_run is not None and typed_run.player_boxes is not None:
        if cancellation is not None:
            cancellation.check("movement analysis")
        try:
            movement = movement_analyzer.analyze(
                typed_run.player_boxes.get(track.track_id, {}), metadata.fps
            )
        except AnalysisCancelled:
            raise
        except Exception:
            movement_reason = "Movement analysis failed."
    interaction: InteractionAnalysisResult | None = None
    interaction_reason: str | None = "Interaction analysis was unavailable."
    if (
        typed_run is not None
        and typed_run.player_boxes is not None
        and typed_run.ball_points is not None
    ):
        if cancellation is not None:
            cancellation.check("interaction analysis")
        try:
            logger.warning(
                "interaction_analysis_started analysis_id=%s track_id=%s",
                analysis_id,
                track.track_id,
            )
            players = tuple(
                PlayerObservation(frame, frame / metadata.fps, box, track.average_confidence)
                for frame, box in typed_run.player_boxes.get(track.track_id, {}).items()
            )
            balls = tuple(
                BallObservation(
                    point.frame_index, point.timestamp_seconds, point.center_point, point.confidence
                )
                for point in typed_run.ball_points.values()
                if point.visible and point.center_point is not None and point.confidence is not None
            )
            interaction = interaction_analyzer.analyze(
                players,
                balls,
                metadata.fps,
                (metadata.width, metadata.height),
                quality,
                min(1.0, track.visibility_ratio * track.average_confidence),
            )
            if cancellation is not None:
                cancellation.check("interaction analysis")
            interaction_reason = interaction.reason
            logger.warning(
                "interaction_analysis_finished analysis_id=%s track_id=%s processing_time_ms=%s",
                analysis_id,
                track.track_id,
                interaction.diagnostics.processing_time_ms,
            )
            logger.warning(
                "interaction_segment_count analysis_id=%s track_id=%s count=%s",
                analysis_id,
                track.track_id,
                interaction.possible_ball_interaction_count,
            )
            _log_interaction_evidence(
                logger,
                analysis_id,
                selection,
                metadata,
                len(players),
                len(balls),
                min(1.0, track.visibility_ratio * track.average_confidence),
                quality,
                interaction,
            )
        except AnalysisCancelled:
            raise
        except Exception:
            logger.exception(
                "interaction_analysis_failed analysis_id=%s track_id=%s stage=analyze",
                analysis_id,
                track.track_id,
            )
            interaction_reason = "Interaction analysis was unavailable."
    else:
        logger.warning(
            "interaction_analysis_skipped analysis_id=%s track_id=%s reason=%s",
            analysis_id,
            track.track_id,
            "selected player observations or accepted ball observations were unavailable",
        )
    technical_events: TechnicalEventAnalysisResult | None = None
    technical_reason: str | None = "Technical-event analysis was unavailable."
    event_started = perf_counter()
    if interaction is not None and movement is not None:
        if cancellation is not None:
            cancellation.check("event analysis")
        try:
            technical_events = technical_event_analyzer.analyze(
                players,
                balls,
                interaction,
                movement,
                metadata.fps,
                (metadata.width, metadata.height),
                min(1.0, track.visibility_ratio * track.average_confidence),
                quality,
                interaction.diagnostics.interaction_analysis_quality,
            )
            if cancellation is not None:
                cancellation.check("event analysis")
            technical_reason = technical_events.reason
        except AnalysisCancelled:
            raise
        except Exception:
            logger.exception("technical_event_analysis_failed analysis_id=%s", analysis_id)
    event_time_ms = round((perf_counter() - event_started) * 1000)
    warnings = (
        ["Ball proximity is an approximation and does not prove possession."]
        if proximity is not None
        else [ball_reason or "Ball detection confidence was insufficient."]
    )
    if selection.method == "best_continuous_track_segment":
        warnings.append(
            "Target player was selected from the best continuous track segment rather than the full video."
        )
        warnings.append("Ball analysis was scoped to the selected player segment.")
    if segment_ball is not None and segment_ball.interpolated_frames:
        warnings.append("Short ball-track gaps were interpolated for segment-level analysis.")
    if movement is not None:
        warnings.extend(
            [
                "Movement metrics are image-space estimates and may include camera motion.",
                "Movement intensity is not an official physical-performance score.",
            ]
        )
    if interaction is not None:
        warnings.extend(interaction.warnings)
    if technical_events is not None:
        warnings.extend(technical_events.warnings)
    physical_started = perf_counter()
    if cancellation is not None:
        cancellation.check("scoring")
    physical = None
    try:
        physical = physical_scorer.score(
            movement,
            track.visibility_ratio,
            track.visible_frames,
            track.longest_segment,
            track.average_confidence,
            min(
                settings.movement_raw_image_space_quality_cap,
                len(movement.trajectory) / max(track.visible_frames, 1),
            )
            if movement
            else None,
            "raw_image_space",
        )
    except Exception:
        logger.exception("physical_activity_score_failed analysis_id=%s", analysis_id)
    physical_time_ms = round((perf_counter() - physical_started) * 1000)
    technical_score_started = perf_counter()
    if cancellation is not None:
        cancellation.check("technical scoring")
    technical_score = TechnicalScorer().score(technical_events)
    technical_score_time_ms = round((perf_counter() - technical_score_started) * 1000)
    player_rating_summary = PlayerRatingEngine(settings=settings).summarize(
        technical=technical_score,
        physical=physical,
        interactions=interaction,
        events=technical_events,
    )
    total_time_ms = round((perf_counter() - started) * 1000)
    stage_timing = stage_timing.model_copy(
        update={
            "interaction_processing_time_ms": interaction.diagnostics.processing_time_ms
            if interaction
            else 0,
            "controlled_movement_time_ms": event_time_ms,
            "dribble_processing_time_ms": event_time_ms,
            "technical_scoring_time_ms": technical_score_time_ms,
            "physical_scoring_time_ms": physical_time_ms,
            "total_processing_time_ms": total_time_ms,
        }
    )
    response = CompletedResponse(
        analysis_id=analysis_id,
        status="completed",
        video=VideoResponse(**asdict(metadata)),
        selected_player=SelectedPlayer(
            track_id=track.track_id,
            selection_method=selection.method,
            selection_score=selection.score,
            confidence=track.average_confidence,
            visible_frames=track.visible_frames,
            visibility_ratio=track.visibility_ratio,
            ball_proximity_frames=ball_frames,
            ball_proximity_ratio=proximity.ball_proximity_ratio if proximity is not None else 0.0,
            visibility_contribution=selection.visibility_contribution,
            ball_proximity_contribution=selection.ball_contribution,
            segment_id=selection.segment_id,
            segment_start_frame=selection.segment_start_frame,
            segment_end_frame=selection.segment_end_frame,
            segment_duration_seconds=selection.segment_duration_seconds,
        ),
        tracking=TrackingResponse(
            frames_processed=(
                selection_diagnostics.frames_processed
                if selection_diagnostics
                else track.total_frames
            ),
            lost_track_count=track.lost_track_count,
            longest_continuous_visible_segment=track.longest_segment,
        ),
        features=extractor.features(proximity, ball_reason, movement, movement_reason),
        interaction_analysis=_interaction_response(interaction, interaction_reason),
        technical_event_analysis=_technical_event_response(technical_events, technical_reason),
        pass_detection=_pass_detection_response(pass_detection),
        shot_detection=_shot_detection_response(shot_detection),
        scores=extractor.scores(physical, technical_score),
        player_rating_summary=player_rating_summary,
        diagnostics=Diagnostics(
            frames_processed=(
                typed_run.diagnostics.frames_processed
                if typed_run is not None
                else track.total_frames
            ),
            frames_with_player_detections=(
                typed_run.diagnostics.frames_with_player_detections if typed_run is not None else 0
            ),
            total_person_detections=(
                typed_run.diagnostics.total_person_detections if typed_run is not None else 0
            ),
            tracks_created=typed_run.diagnostics.tracks_created if typed_run is not None else 0,
            valid_candidate_tracks=valid_candidate_tracks,
            ball_detections=len(confidences),
            ball_visible_frames=(
                selection_diagnostics.accepted_ball_track_observations
                if selection_diagnostics is not None
                else (typed_run.diagnostics.accepted_ball_track_observations if typed_run else 0)
            ),
            ball_track_segments=typed_run.ball_track_segments if typed_run is not None else 0,
            ball_detection_confidence_mean=sum(confidences) / len(confidences)
            if confidences
            else None,
            raw_ball_detections=typed_run.diagnostics.raw_ball_detections if typed_run else 0,
            filtered_ball_detections=(
                typed_run.diagnostics.filtered_ball_detections if typed_run else 0
            ),
            accepted_ball_track_observations=(
                typed_run.diagnostics.accepted_ball_track_observations if typed_run else 0
            ),
            frames_with_multiple_ball_candidates=(
                typed_run.diagnostics.frames_with_multiple_ball_candidates if typed_run else 0
            ),
            rejected_ball_candidates=(
                typed_run.diagnostics.rejected_ball_candidates if typed_run else 0
            ),
            unique_track_ids=typed_run.diagnostics.unique_track_ids if typed_run else 0,
            selected_track_visible_frames=track.visible_frames,
            ball_analysis_quality=quality if typed_run is not None else None,
            ball_analysis_scope="selected_player_segment"
            if segment_ball is not None
            else "full_video",
            ball_quality_scope="selected_player_segment"
            if segment_ball is not None
            else "full_video",
            global_ball_analysis_quality=global_quality if typed_run is not None else None,
            selected_segment_ball_analysis_quality=segment_ball.quality if segment_ball else None,
            ball_quality_failure_reasons=list(segment_ball.failure_reasons) if segment_ball else [],
            segment_ball_total_frames=(
                selection.segment_end_frame - selection.segment_start_frame + 1
            )
            if selection.segment_start_frame is not None and selection.segment_end_frame is not None
            else 0,
            segment_ball_detected_frames=segment_ball.detected_frames if segment_ball else 0,
            segment_ball_interpolated_frames=segment_ball.interpolated_frames
            if segment_ball
            else 0,
            segment_ball_reconstructed_frames=segment_ball.reconstructed_frames
            if segment_ball
            else 0,
            segment_ball_visibility_ratio=segment_ball.visibility_ratio if segment_ball else 0.0,
            segment_ball_track_segments_before_reconstruction=segment_ball.segments_before
            if segment_ball
            else 0,
            segment_ball_track_segments_after_reconstruction=segment_ball.segments_after
            if segment_ball
            else 0,
            segment_ball_longest_run_frames=segment_ball.longest_run if segment_ball else 0,
            segment_ball_longest_gap_frames=segment_ball.longest_gap if segment_ball else 0,
            segment_ball_mean_confidence=segment_ball.mean_confidence if segment_ball else None,
            segment_ball_multiple_candidate_ratio=segment_ball.multiple_candidate_ratio
            if segment_ball
            else 0.0,
            segment_ball_quality_components=segment_ball.quality_components
            if segment_ball
            else None,
            segment_ball_reconstruction_version=RECONSTRUCTION_VERSION if segment_ball else None,
            segment_ball_quality_version=SEGMENT_BALL_QUALITY_VERSION if segment_ball else None,
            segment_ball_processing_time_ms=segment_ball.processing_time_ms if segment_ball else 0,
            movement_frames=len(movement.trajectory) if movement else 0,
            movement_segments=movement.movement_segments if movement else 0,
            rejected_position_jumps=movement.rejected_position_jumps if movement else 0,
            smoothed_positions=movement.smoothed_positions if movement else 0,
            average_speed=movement.metrics.average_speed if movement else None,
            maximum_speed=movement.metrics.maximum_speed if movement else None,
            movement_observations=len(movement.trajectory) if movement else 0,
            movement_duration_seconds=(
                movement.trajectory[-1].timestamp_seconds - movement.trajectory[0].timestamp_seconds
                if movement and len(movement.trajectory) > 1
                else None
            ),
            movement_scoring_version="movement_rule_v0.2" if movement else None,
            stationary_frames=movement.metrics.stationary_frames if movement else 0,
            raw_stationary_segments=movement.metrics.raw_stationary_segments if movement else 0,
            accepted_stationary_segments=movement.metrics.stationary_period_count
            if movement
            else 0,
            rejected_short_stationary_segments=(
                movement.metrics.rejected_short_stationary_segments if movement else 0
            ),
            distance_component=movement.metrics.distance_component if movement else None,
            speed_component=movement.metrics.speed_component if movement else None,
            activity_component=movement.metrics.activity_component if movement else None,
            raw_movement_intensity=movement.metrics.raw_movement_intensity if movement else None,
            clamped_movement_intensity=movement.metrics.movement_intensity if movement else None,
            movement_intensity_saturated=(
                movement.metrics.movement_intensity >= 1.0 if movement else False
            ),
            movement_analysis_quality=(
                min(
                    settings.movement_raw_image_space_quality_cap,
                    len(movement.trajectory) / max(track.visible_frames, 1),
                )
                if movement
                else None
            ),
            camera_motion_enabled=bool(camera_motion and camera_motion.intervals),
            camera_motion_evaluated_intervals=len(camera_motion.intervals) if camera_motion else 0,
            camera_motion_accepted_intervals=camera_motion.accepted_intervals
            if camera_motion
            else 0,
            camera_motion_rejected_intervals=camera_motion.rejected_intervals
            if camera_motion
            else 0,
            camera_motion_coverage_ratio=camera_motion.coverage_ratio if camera_motion else 0.0,
            camera_motion_mean_confidence=(
                sum(item.confidence for item in camera_motion.intervals if item.accepted)
                / camera_motion.accepted_intervals
                if camera_motion and camera_motion.accepted_intervals
                else None
            ),
            camera_motion=camera_diagnostics,
            movement_metrics_source="raw_image_space",
            interaction_aligned_frames=interaction.diagnostics.interaction_aligned_frames
            if interaction
            else 0,
            interaction_candidate_frames=interaction.diagnostics.interaction_candidate_frames
            if interaction
            else 0,
            interaction_non_candidate_frames=interaction.diagnostics.interaction_non_candidate_frames
            if interaction
            else 0,
            interaction_missing_evidence_frames=interaction.diagnostics.interaction_missing_evidence_frames
            if interaction
            else 0,
            raw_interaction_segments=interaction.diagnostics.raw_interaction_segments
            if interaction
            else 0,
            accepted_interaction_segments=interaction.diagnostics.accepted_interaction_segments
            if interaction
            else 0,
            rejected_short_interaction_segments=interaction.diagnostics.rejected_short_interaction_segments
            if interaction
            else 0,
            rejected_low_confidence_interaction_segments=interaction.diagnostics.rejected_low_confidence_interaction_segments
            if interaction
            else 0,
            rejected_low_global_quality_interaction_segments=interaction.diagnostics.rejected_low_global_quality_interaction_segments
            if interaction
            else 0,
            rejected_invalid_interaction_segments=interaction.diagnostics.rejected_invalid_interaction_segments
            if interaction
            else 0,
            bridged_interaction_gaps=interaction.diagnostics.bridged_interaction_gaps
            if interaction
            else 0,
            maximum_bridged_gap_frames=interaction.diagnostics.maximum_bridged_gap_frames
            if interaction
            else 0,
            interaction_evidence_coverage_ratio=interaction.diagnostics.interaction_evidence_coverage_ratio
            if interaction
            else 0.0,
            interaction_confidence_version=interaction.diagnostics.interaction_confidence_version
            if interaction
            else None,
            interaction_analysis_quality=interaction.diagnostics.interaction_analysis_quality
            if interaction
            else None,
            interaction_processing_time_ms=interaction.diagnostics.processing_time_ms
            if interaction
            else 0,
            controlled_movement_raw_candidates=technical_events.diagnostics.controlled_movement_raw_candidates
            if technical_events
            else 0,
            controlled_movement_accepted_candidates=technical_events.diagnostics.controlled_movement_accepted_candidates
            if technical_events
            else 0,
            controlled_movement_rejected_short=technical_events.diagnostics.controlled_movement_rejected_short
            if technical_events
            else 0,
            controlled_movement_rejected_low_confidence=technical_events.diagnostics.controlled_movement_rejected_low_confidence
            if technical_events
            else 0,
            dribble_raw_candidates=technical_events.diagnostics.dribble_raw_candidates
            if technical_events
            else 0,
            dribble_accepted_candidates=technical_events.diagnostics.dribble_accepted_candidates
            if technical_events
            else 0,
            dribble_rejected_low_movement=technical_events.diagnostics.dribble_rejected_low_movement
            if technical_events
            else 0,
            dribble_rejected_low_confidence=technical_events.diagnostics.dribble_rejected_low_confidence
            if technical_events
            else 0,
            ball_loss_raw_candidates=technical_events.diagnostics.ball_loss_raw_candidates
            if technical_events
            else 0,
            ball_loss_accepted_candidates=technical_events.diagnostics.ball_loss_accepted_candidates
            if technical_events
            else 0,
            ball_loss_rejected_missing_post_evidence=technical_events.diagnostics.ball_loss_rejected_missing_post_evidence
            if technical_events
            else 0,
            ball_loss_rejected_recovery=technical_events.diagnostics.ball_loss_rejected_recovery
            if technical_events
            else 0,
            technical_event_analysis_quality=technical_events.diagnostics.technical_event_analysis_quality
            if technical_events
            else None,
            technical_event_processing_time_ms=technical_events.diagnostics.processing_time_ms
            if technical_events
            else 0,
            technical_evidence_score=(
                sum(technical_score.evidence.values()) if technical_score.evidence else 0.0
            ),
            technical_event_count=int(
                technical_score.evidence.get("controlled_movement_events", 0)
                + technical_score.evidence.get("dribble_events", 0)
                + technical_score.evidence.get("ball_loss_events", 0)
            ),
            technical_controlled_component=technical_score.controlled_component,
            technical_dribble_component=technical_score.dribble_component,
            technical_ball_loss_penalty=technical_score.ball_loss_penalty,
            technical_score_quality=technical_score.quality,
            controlled_movement_rejection_breakdown=technical_events.diagnostics.controlled_movement_rejection_breakdown
            if technical_events
            else None,
            controlled_movement_thresholds=technical_events.diagnostics.controlled_movement_thresholds
            if technical_events
            else None,
            controlled_movement_segment_statistics=list(
                technical_events.diagnostics.controlled_movement_segment_statistics
            )
            if technical_events
            else [],
            displacement_summary=technical_events.diagnostics.displacement_summary
            if technical_events
            else None,
            displacement_histogram=technical_events.diagnostics.displacement_histogram
            if technical_events
            else None,
            dribble_candidate_statistics=list(
                technical_events.diagnostics.dribble_candidate_statistics
            )
            if technical_events
            else [],
            dribble_rejection_breakdown=technical_events.diagnostics.dribble_rejection_breakdown
            if technical_events
            else None,
            dribble_thresholds=technical_events.diagnostics.dribble_thresholds
            if technical_events
            else None,
            physical_score_version=physical.version if physical else None,
            physical_confidence_version="physical_activity_confidence_v0.1" if physical else None,
            physical_score_raw=physical.raw_score if physical else None,
            physical_score_final=physical.value if physical else None,
            physical_score_confidence=physical.confidence if physical else None,
            physical_score_level=physical.level if physical else None,
            physical_score_quality_gate_passed=physical.status == "provisional_video_based"
            if physical
            else False,
            physical_score_confidence_capped=physical.confidence_capped if physical else False,
            physical_score_components=(
                {
                    "activity": physical.evidence.movement_intensity,
                    "active_time": physical.evidence.active_time_ratio,
                    "visibility": physical.evidence.visibility_ratio,
                    "continuity": physical.evidence.continuity_ratio,
                    "direction": physical.evidence.direction_component,
                }
                if physical and physical.evidence
                else None
            ),
            physical_score_processing_time_ms=physical.processing_time_ms if physical else 0,
            rejected_tracks=list(selection_diagnostics.rejected_tracks)
            if selection_diagnostics
            else [],
            rejected_track_reason_breakdown=(
                selection_diagnostics.rejected_track_reason_breakdown or {}
            )
            if selection_diagnostics
            else {},
        ),
        warnings=list(dict.fromkeys(warnings)),
        analysis_version=settings.analysis_version,
        model_version=model_version,
        processing_time_ms=total_time_ms,
        algorithm_versions={
            "player_detection": model_version,
            "tracking": "bytetrack_v0.1",
            "player_selection": "segment_selection_v0.1",
            "ball_reconstruction": RECONSTRUCTION_VERSION,
            "interaction_analysis": "interaction_analysis_v0.1",
            "controlled_movement": "controlled_movement_confidence_v0.1",
            "dribble": "dribble_candidate_confidence_v0.2",
            "technical_scoring": "technical_scoring_v0.1",
            "physical_scoring": physical.version if physical else "physical_activity_v0.1",
            "pass_detection": PASS_DETECTION_VERSION,
            "shot_detection": SHOT_DETECTION_VERSION,
        },
        timing=stage_timing,
        quality_gates=_quality_gates(
            track.visibility_ratio, quality, interaction, technical_events, physical, ball_reason
        ),
        metadata=request_metadata or {},
        analysis_metadata=analysis_metadata or {},
    )
    if typed_run is not None and debug_source is not None:
        if cancellation is not None:
            cancellation.check("debug rendering")
        try:
            response.debug_artifacts = render_debug_video(
                debug_source,
                debug_source.parent,
                selection,
                typed_run.player_boxes,
                typed_run.ball_points,
                interaction,
                technical_events,
                pass_detection,
                shot_detection,
                save_video=settings.debug.save_video,
                save_frames=settings.debug.save_frames,
            )
            response.debug_artifacts = _public_debug_artifact_references(response.debug_artifacts)
        except Exception:
            logger.exception("debug_render_failed analysis_id=%s", analysis_id)
            response.warnings.append(
                "Debug-video rendering failed; analysis results are unaffected."
            )
    try:
        game_intelligence = game_intelligence_result(response)
        _log_rating_evidence(
            logger,
            analysis_id,
            physical,
            technical_events,
            technical_score,
            game_intelligence,
            interaction,
            pass_detection,
            shot_detection,
        )
    except Exception:
        logger.exception("rating_evidence_logging_failed analysis_id=%s", analysis_id)
    _validate_completed_diagnostics(response)
    return response


def _public_debug_artifact_references(artifacts: dict[str, str]) -> dict[str, str]:
    """Expose opaque artifact names while retaining request-local paths internally."""
    return {name: path.replace("\\", "/").rsplit("/", 1)[-1] for name, path in artifacts.items()}


def _interaction_response(
    result: InteractionAnalysisResult | None, reason: str | None
) -> InteractionAnalysisResponse:
    def metric(value: float | None) -> FeatureMetric:
        return FeatureMetric(value=value, reason=None if result else reason)

    return InteractionAnalysisResponse(
        possible_ball_interaction_count=metric(
            float(result.possible_ball_interaction_count) if result else None
        ),
        possible_ball_interaction_time_seconds=metric(
            result.possible_ball_interaction_time_seconds if result else None
        ),
        longest_possible_ball_interaction_seconds=metric(
            result.longest_possible_ball_interaction_seconds if result else None
        ),
        mean_possible_ball_interaction_confidence=metric(
            result.mean_possible_ball_interaction_confidence if result else None
        ),
        interaction_candidate_frames=metric(
            float(result.interaction_candidate_frames) if result else None
        ),
        interaction_observed_frames=metric(
            float(result.interaction_observed_frames) if result else None
        ),
        interaction_evidence_coverage_ratio=metric(
            result.interaction_evidence_coverage_ratio if result else None
        ),
        segments=[InteractionSegmentResponse(**asdict(segment)) for segment in result.segments]
        if result
        else [],
        confidence_version=result.confidence_version if result else "interaction_confidence_v0.1",
    )


def _technical_event_response(
    result: TechnicalEventAnalysisResult | None, reason: str | None
) -> TechnicalEventAnalysisResponse:
    def metric(value: float | None) -> FeatureMetric:
        return FeatureMetric(value=value, reason=None if result else reason)

    controlled = result.controlled_movement_candidates if result else ()
    dribbles = result.dribble_candidates if result else ()
    losses = result.ball_loss_candidates if result else ()
    return TechnicalEventAnalysisResponse(
        controlled_movement_candidate_count=metric(float(len(controlled)) if result else None),
        controlled_movement_candidate_time_seconds=metric(
            sum(x.duration_seconds for x in controlled) if result else None
        ),
        mean_controlled_movement_confidence=metric(
            sum(x.confidence for x in controlled) / len(controlled) if controlled else None
        ),
        dribble_candidate_count=metric(float(len(dribbles)) if result else None),
        dribble_candidate_time_seconds=metric(
            sum(x.duration_seconds for x in dribbles) if result else None
        ),
        mean_dribble_candidate_confidence=metric(
            sum(x.confidence for x in dribbles) / len(dribbles) if dribbles else None
        ),
        ball_loss_candidate_count=metric(float(len(losses)) if result else None),
        mean_ball_loss_candidate_confidence=metric(
            sum(x.confidence for x in losses) / len(losses) if losses else None
        ),
        controlled_movement_candidates=[
            ControlledMovementCandidateResponse(**asdict(x)) for x in controlled
        ],
        dribble_candidates=[DribbleCandidateResponse(**asdict(x)) for x in dribbles],
        ball_loss_candidates=[BallLossCandidateResponse(**asdict(x)) for x in losses],
    )


def _pass_detection_response(result: PassDetectionResult | None) -> PassDetectionResponse:
    if result is None:
        return PassDetectionResponse()
    return PassDetectionResponse(
        pass_candidates=[PassCandidateResponse(**asdict(item)) for item in result.candidates],
        raw_pass_candidates=result.raw_pass_candidates,
        accepted_pass_candidates=result.accepted_pass_candidates,
        rejected_pass_candidates=result.rejected_pass_candidates,
        rejection_breakdown=result.rejection_breakdown,
        pass_detection_version=result.version,
        processing_time_ms=result.processing_time_ms,
    )


def _shot_detection_response(result: ShotDetectionResult | None) -> ShotDetectionResponse:
    if result is None:
        return ShotDetectionResponse()
    return ShotDetectionResponse(
        shot_candidates=[ShotCandidateResponse(**asdict(item)) for item in result.candidates],
        raw_shot_candidates=result.raw_shot_candidates,
        accepted_shot_candidates=result.accepted_shot_candidates,
        rejected_shot_candidates=result.rejected_shot_candidates,
        rejection_breakdown=result.rejection_breakdown,
        shot_detection_version=result.version,
        processing_time_ms=result.processing_time_ms,
    )


def _ball_quality(
    visible_frames: int,
    frames_processed: int,
    accepted_confidences: tuple[float, ...],
    segments: int,
    multiple_frames: int,
    rejected: int,
    raw_detections: int,
) -> float:
    """Conservative, explainable quality score for gating proximity output."""
    visibility = visible_frames / frames_processed if frames_processed else 0.0
    confidence = (
        sum(accepted_confidences) / len(accepted_confidences) if accepted_confidences else 0.0
    )
    continuity = visible_frames / (visible_frames + max(segments - 1, 0)) if visible_frames else 0.0
    multiple_rate = multiple_frames / frames_processed if frames_processed else 1.0
    rejected_rate = rejected / raw_detections if raw_detections else 1.0
    return max(
        0.0,
        min(
            1.0,
            (visibility + confidence + continuity + (1 - multiple_rate) + (1 - rejected_rate)) / 5,
        ),
    )


def _validate_completed_diagnostics(response: CompletedResponse) -> None:
    """Prevent successful responses from asserting impossible stage counters."""
    diagnostics = response.diagnostics
    player = response.selected_player
    failures: list[str] = []
    if diagnostics.tracks_created <= 0:
        failures.append("selected player requires tracks_created > 0")
    if player.visible_frames > 0 and (
        diagnostics.frames_with_player_detections <= 0 or diagnostics.total_person_detections <= 0
    ):
        failures.append("visible selected player requires non-zero player detection counters")
    if diagnostics.valid_candidate_tracks < 1:
        failures.append("selected player requires at least one valid candidate")
    if player.visible_frames > diagnostics.frames_processed:
        failures.append("selected player visibility exceeds processed frames")
    if diagnostics.ball_visible_frames > diagnostics.frames_processed:
        failures.append("ball visibility exceeds processed frames")
    if diagnostics.ball_visible_frames != diagnostics.accepted_ball_track_observations:
        failures.append("ball visibility must equal accepted primary-ball observations")
    if failures:
        raise InternalDiagnosticsError("; ".join(failures))


def _quality_gates(
    player_quality: float,
    ball_quality: float,
    interaction: InteractionAnalysisResult | None,
    events: TechnicalEventAnalysisResult | None,
    physical: object | None,
    ball_reason: str | None,
) -> dict[str, StageGate]:
    """Give each pipeline stage an explicit, machine-readable outcome."""

    def gate(quality: float, reason: str | None = None) -> StageGate:
        return StageGate(
            quality=max(0.0, min(1.0, quality)),
            status="accepted" if reason is None else "rejected",
            failure_reasons=[] if reason is None else [reason],
        )

    return {
        "player_selection": gate(player_quality),
        "ball_analysis": gate(ball_quality, ball_reason),
        "interaction_analysis": gate(
            interaction.diagnostics.interaction_analysis_quality if interaction else 0.0,
            interaction.reason if interaction else "Interaction analysis was unavailable.",
        ),
        "event_analysis": gate(
            events.diagnostics.technical_event_analysis_quality if events else 0.0,
            events.reason if events else "Technical-event analysis was unavailable.",
        ),
        "technical_scoring": gate(1.0),
        "physical_scoring": gate(
            float(getattr(physical, "confidence", 0.0) or 0.0),
            getattr(physical, "reason", None) if physical else "Physical scoring was unavailable.",
        ),
    }
