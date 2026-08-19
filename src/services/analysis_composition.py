"""Reusable construction of the CPU-analysis dependency graph."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from adapters.yolo_ball_detector import YOLOBallDetector
from adapters.yolo_player_detector import YOLOPlayerDetector
from core.config import Settings
from core.logging import get_logger
from services.ball_proximity import NormalizedBallProximityAnalyzer
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzer
from services.movement.analyzer import BottomCenterMovementAnalyzer
from services.pass_detection import PassDetector
from services.player_tracker import AutomaticPlayerTracker, DetectionOnlyPlayerTracker
from services.scoring.physical_activity import RuleBasedPhysicalActivityScorer
from services.selection import TargetPlayerSelector, WeightedTargetPlayerSelector
from services.shot_detection import ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.tracker import ByteTrackTracker
from services.video_validator import VideoValidator


@dataclass(frozen=True, slots=True)
class AnalysisComponents:
    """Dependencies used by established CPU-heavy analysis calculation only."""

    validator: VideoValidator
    tracker: AutomaticPlayerTracker
    selector: TargetPlayerSelector
    extractor: FeatureExtractor
    ball_proximity_analyzer: NormalizedBallProximityAnalyzer
    movement_analyzer: BottomCenterMovementAnalyzer
    interaction_analyzer: BallInteractionAnalyzer
    technical_event_analyzer: TechnicalEventAnalyzer
    pass_detector: PassDetector
    shot_detector: ShotDetector
    physical_scorer: RuleBasedPhysicalActivityScorer


def create_analysis_components(
    settings: Settings,
    logger: logging.Logger,
    *,
    tracker_override: AutomaticPlayerTracker | None = None,
    selector_override: TargetPlayerSelector | None = None,
    validator_override: VideoValidator | None = None,
) -> AnalysisComponents:
    """Build one independent, lazy CPU-analysis graph without loading a model."""

    del logger

    def tracker_factory() -> ByteTrackTracker:
        return ByteTrackTracker(settings)

    tracker = tracker_override or DetectionOnlyPlayerTracker(
        YOLOPlayerDetector(settings, get_logger("football_analysis.detector")),
        tracker_factory,
        settings,
        YOLOBallDetector(settings, get_logger("football_analysis.ball_detector")),
    )
    return AnalysisComponents(
        validator=validator_override or VideoValidator(settings),
        tracker=tracker,
        selector=selector_override or WeightedTargetPlayerSelector(settings),
        extractor=FeatureExtractor(),
        ball_proximity_analyzer=NormalizedBallProximityAnalyzer(settings),
        movement_analyzer=BottomCenterMovementAnalyzer(settings),
        interaction_analyzer=BallInteractionAnalyzer(settings),
        technical_event_analyzer=TechnicalEventAnalyzer(settings),
        pass_detector=PassDetector(settings),
        shot_detector=ShotDetector(),
        physical_scorer=RuleBasedPhysicalActivityScorer(settings),
    )
