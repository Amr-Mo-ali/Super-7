"""Thin HTTP orchestration for automatic target-player analysis."""

import logging
from dataclasses import asdict
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status

from core.config import Settings
from core.exceptions import AnalysisError, InvalidRequestError, RealDetectorNotConfiguredError
from schemas.analysis import (
    AmbiguousResponse,
    AnalyzeResponse,
    CompletedResponse,
    Diagnostics,
    NonCompletedResponse,
    SelectedPlayer,
    TrackingResponse,
    VideoResponse,
)
from services.ball_proximity import BallProximityAnalyzer, BallProximityResult
from services.feature_extractor import FeatureExtractor
from services.player_tracker import AutomaticPlayerTracker, TrackingDiagnostics
from services.selection import Selection, TargetPlayerSelector
from services.video_validator import VideoMetadata, VideoValidator, temporary_upload


def create_router(
    settings: Settings,
    validator: VideoValidator,
    tracker: AutomaticPlayerTracker,
    selector: TargetPlayerSelector,
    extractor: FeatureExtractor,
    ball_proximity_analyzer: BallProximityAnalyzer,
    logger: logging.Logger,
) -> APIRouter:
    """Create the only public route with injected analysis dependencies."""
    router = APIRouter()

    @router.post("/analyze", response_model=AnalyzeResponse)
    async def analyze(request: Request, video: Annotated[UploadFile, File()]) -> AnalyzeResponse:
        """Validate one video and select exactly one non-ambiguous player track."""
        started = perf_counter()
        analysis_id = str(uuid4())
        try:
            unexpected_fields = set((await request.form()).keys()) - {"video"}
            if unexpected_fields:
                raise InvalidRequestError("Only the video multipart field is accepted.")
            async with temporary_upload(video, settings) as video_path:
                metadata = validator.validate(video_path)
                run = tracker.analyze(video_path, metadata)
            if run.diagnostics.total_person_detections == 0:
                return _noncompleted(
                    analysis_id,
                    "no_players_detected",
                    "No player detections were produced.",
                    run.diagnostics,
                    0,
                )
            ranked = selector.rank(run.tracks)
            if not run.tracks:
                return _noncompleted(
                    analysis_id,
                    "player_detection_completed_tracking_not_available",
                    "Player detection completed; multi-object tracking is not available.",
                    run.diagnostics,
                    0,
                )
            if not ranked:
                return _noncompleted(
                    analysis_id,
                    "no_valid_tracks",
                    "No track passed the configured quality thresholds.",
                    run.diagnostics,
                    0,
                )
            if len(ranked) > 1 and ranked[0].score - ranked[1].score < settings.selection_margin:
                return AmbiguousResponse(
                    analysis_id=analysis_id,
                    status="ambiguous_target",
                    candidate_count=len(ranked),
                    warnings=[
                        "The system could not identify one target player "
                        "with sufficient confidence."
                    ],
                )
            selection = ranked[0]
            return _completed(
                settings,
                tracker.model_version,
                metadata,
                selection,
                extractor,
                run,
                ball_proximity_analyzer,
                analysis_id,
                started,
            )
        except RealDetectorNotConfiguredError as error:
            diagnostics = TrackingDiagnostics(0, 0, 0, 0, 0)
            return _noncompleted(analysis_id, "failed", str(error), diagnostics, 0)
        except AnalysisError as error:
            logger.warning("analysis_validation_failed: %s", error)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"error": str(error)}
            ) from error
        except Exception as error:
            logger.exception("analysis_failed")
            raise HTTPException(status_code=500, detail={"error": "Analysis failed."}) from error

    return router


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
) -> NonCompletedResponse:
    """Return a truthful non-completed status with detector/tracker diagnostics."""
    return NonCompletedResponse(
        analysis_id=analysis_id,
        status=response_status,
        candidate_count=candidates,
        warnings=[warning],
        diagnostics=Diagnostics(**asdict(diagnostics), valid_candidate_tracks=candidates),
    )


def _completed(
    settings: Settings,
    model_version: str,
    metadata: VideoMetadata,
    selection: Selection,
    extractor: FeatureExtractor,
    run: object,
    analyzer: BallProximityAnalyzer,
    analysis_id: str,
    started: float,
) -> CompletedResponse:
    """Map a pure selection result to the successful public contract."""
    track = selection.track
    from services.player_tracker import TrackingRun

    typed_run = run if isinstance(run, TrackingRun) else None
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
    visible = proximity.ball_visible_frames if proximity is not None else 0
    ball_frames = proximity.ball_proximity_frames if proximity is not None else 0
    confidences = typed_run.ball_detection_confidences if typed_run is not None else ()
    warnings = (
        ["Ball proximity is an approximation and does not prove possession."]
        if proximity is not None
        else [ball_reason or "Ball detection confidence was insufficient."]
    )
    return CompletedResponse(
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
        ),
        tracking=TrackingResponse(
            frames_processed=track.total_frames,
            lost_track_count=track.lost_track_count,
            longest_continuous_visible_segment=track.longest_segment,
        ),
        features=extractor.features(proximity, ball_reason),
        scores=extractor.scores(),
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
            valid_candidate_tracks=0,
            ball_detections=len(confidences),
            ball_visible_frames=visible,
            ball_track_segments=typed_run.ball_track_segments if typed_run is not None else 0,
            ball_detection_confidence_mean=sum(confidences) / len(confidences)
            if confidences
            else None,
        ),
        warnings=warnings,
        analysis_version=settings.analysis_version,
        model_version=model_version,
        processing_time_ms=round((perf_counter() - started) * 1000),
    )
