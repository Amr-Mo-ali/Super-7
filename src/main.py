"""Application entry point."""

from fastapi import FastAPI

from adapters.yolo_ball_detector import YOLOBallDetector
from adapters.yolo_player_detector import YOLOPlayerDetector
from api.routes import create_router
from core.config import Settings
from core.logging import get_logger
from services.ball_proximity import NormalizedBallProximityAnalyzer
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzer
from services.movement.analyzer import BottomCenterMovementAnalyzer
from services.player_tracker import AutomaticPlayerTracker, DetectionOnlyPlayerTracker
from services.scoring.physical_activity import RuleBasedPhysicalActivityScorer
from services.selection import TargetPlayerSelector, WeightedTargetPlayerSelector
from services.tracker import ByteTrackTracker
from services.video_validator import VideoValidator


def create_app(
    settings: Settings | None = None,
    tracker: AutomaticPlayerTracker | None = None,
    selector: TargetPlayerSelector | None = None,
) -> FastAPI:
    """Compose immutable settings and small injected MVP services."""
    resolved_settings = settings or Settings.from_environment()
    resolved_tracker = tracker or DetectionOnlyPlayerTracker(
        YOLOPlayerDetector(resolved_settings, get_logger("football_analysis.detector")),
        ByteTrackTracker(resolved_settings),
        resolved_settings,
        YOLOBallDetector(resolved_settings, get_logger("football_analysis.ball_detector")),
    )
    get_logger("football_analysis.startup").info(
        "detector=%s model=%s device=%s enabled=true",
        resolved_tracker.__class__.__name__,
        resolved_settings.model_path,
        resolved_settings.model_device,
    )
    app = FastAPI(title="Football Analysis MVP", version=resolved_settings.analysis_version)
    app.include_router(
        create_router(
            resolved_settings,
            VideoValidator(resolved_settings),
            resolved_tracker,
            selector or WeightedTargetPlayerSelector(resolved_settings),
            FeatureExtractor(),
            NormalizedBallProximityAnalyzer(resolved_settings),
            BottomCenterMovementAnalyzer(resolved_settings),
            BallInteractionAnalyzer(resolved_settings),
            RuleBasedPhysicalActivityScorer(resolved_settings),
            get_logger("football_analysis.api"),
        )
    )
    return app


app = create_app()
