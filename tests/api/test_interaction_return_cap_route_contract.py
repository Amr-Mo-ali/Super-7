"""Red route contract for preserving capped interaction evidence (audit finding F10)."""

import logging
from time import perf_counter
from typing import cast

import pytest

from api.routes import _completed
from core.config import Settings
from schemas.analysis import CompletedResponse, TechnicalScoreResponse
from services.ball_proximity import NormalizedBallProximityAnalyzer
from services.ball_tracker import BallTrackPoint
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzer
from services.interactions.models import InteractionAnalysisResult
from services.movement.analyzer import BottomCenterMovementAnalyzer
from services.pass_detection import PassDetectionResult, PassDetector
from services.player_detector import BoundingBox
from services.player_rating.models import PlayerRatingValue
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.scoring.protocols import PhysicalActivityScorerProtocol
from services.selection import PlayerTrack, Selection
from services.shot_detection import ShotDetectionResult, ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.technical_events.models import (
    ControlledMovementCandidate,
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
)
from services.video_validator import VideoMetadata

FPS = 30.0
FRAMES_PER_SEGMENT = 6


class _TechnicalAnalyzer:
    def __init__(self) -> None:
        self.interactions: list[InteractionAnalysisResult] = []

    def analyze(self, *args: object) -> TechnicalEventAnalysisResult:
        interactions = cast(InteractionAnalysisResult, args[2])
        self.interactions.append(interactions)
        segment = interactions.segments[0]
        candidate = ControlledMovementCandidate(
            "controlled-1",
            segment.segment_id,
            segment.start_frame,
            segment.end_frame,
            segment.start_time_seconds,
            segment.end_time_seconds,
            segment.duration_seconds,
            5.0,
            0.5,
            5.0,
            1.0,
            1.0,
            0.9,
        )
        return TechnicalEventAnalysisResult(
            (candidate,),
            (),
            (),
            TechnicalEventDiagnostics(
                controlled_movement_raw_candidates=1,
                controlled_movement_accepted_candidates=1,
                technical_event_analysis_quality=0.9,
            ),
            (),
        )


class _PassDetector:
    def analyze(self, *args: object) -> PassDetectionResult:
        del args
        return PassDetectionResult((), 0, 0, 0, {}, 0)


class _ShotDetector:
    def analyze(self, *args: object) -> ShotDetectionResult:
        del args
        return ShotDetectionResult((), 0, 0, 0, {}, 0)


class _NoPhysicalScore:
    def score(self, *args: object) -> None:
        del args
        return None


def _tracking_run(segment_count: int) -> tuple[TrackingRun, Selection, VideoMetadata]:
    frame_count = segment_count * FRAMES_PER_SEGMENT
    boxes: dict[int, BoundingBox] = {}
    balls: dict[int, BallTrackPoint] = {}
    for frame in range(frame_count):
        x = frame / 10
        box = BoundingBox(x, 0, x + 10, 10)
        boxes[frame] = box
        ball_x = x + 5 if frame % FRAMES_PER_SEGMENT < 5 else x + 50
        balls[frame] = BallTrackPoint(
            frame,
            frame / FPS,
            (ball_x, 10.0),
            0.9,
            True,
            frame // FRAMES_PER_SEGMENT + 1,
        )
    track = PlayerTrack(7, frame_count, frame_count, frame_count, 0, 0.9, 0, True)
    diagnostics = TrackingDiagnostics(
        frame_count,
        frame_count,
        frame_count,
        1,
        frame_count,
        raw_ball_detections=frame_count,
        filtered_ball_detections=frame_count,
        accepted_ball_track_observations=frame_count,
        unique_track_ids=1,
    )
    run = TrackingRun(
        (track,),
        diagnostics,
        {track.track_id: boxes},
        {track.track_id: {frame: 0.9 for frame in boxes}},
        balls,
        (0.9,) * frame_count,
        segment_count,
    )
    return (
        run,
        Selection(track, "visibility_and_track_continuity", 1.0, 1.0, 0.0),
        VideoMetadata("mp4", 1, frame_count / FPS, 1000, 1000, FPS, frame_count),
    )


def _ball_rating(response: CompletedResponse) -> PlayerRatingValue:
    assert response.player_rating_summary is not None
    return next(
        item
        for item in response.player_rating_summary.categories
        if item.category == "ball_involvement"
    )


@pytest.mark.parametrize("segment_count", (100, 101))
def test_return_cap_does_not_erase_route_interaction_technical_or_ball_evidence(
    segment_count: int,
) -> None:
    settings = Settings()
    run, selection, metadata = _tracking_run(segment_count)
    technical = _TechnicalAnalyzer()

    response = _completed(
        settings,
        "test-model",
        metadata,
        selection,
        FeatureExtractor(),
        run,
        1,
        NormalizedBallProximityAnalyzer(settings),
        BottomCenterMovementAnalyzer(settings),
        BallInteractionAnalyzer(settings),
        cast(TechnicalEventAnalyzer, technical),
        cast(PassDetector, _PassDetector()),
        cast(ShotDetector, _ShotDetector()),
        logging.getLogger("test.interaction-return-cap-route"),
        cast(PhysicalActivityScorerProtocol, _NoPhysicalScore()),
        "analysis-1",
        perf_counter(),
        selection_diagnostics=run.diagnostics,
    )

    expected_returned = min(segment_count, settings.interaction_max_returned_segments)
    assert (
        response.interaction_analysis.possible_ball_interaction_count.value,
        len(response.interaction_analysis.segments),
        len(technical.interactions),
        isinstance(response.scores.technical, TechnicalScoreResponse) if response.scores else False,
        _ball_rating(response).status,
    ) == (float(expected_returned), expected_returned, 1, True, "available")
