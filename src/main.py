"""Application entry point."""

from pathlib import Path

from fastapi import FastAPI

from adapters.yolo_ball_detector import YOLOBallDetector
from adapters.yolo_player_detector import YOLOPlayerDetector
from api.request_lifecycle import RequestLifecycle
from api.routes import create_router
from concurrency.admission import AdmissionController
from concurrency.executor import AnalysisExecutor
from core.config import Settings
from core.logging import get_logger
from diagnostics.artifacts import ArtifactManager
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


def create_app(
    settings: Settings | None = None,
    tracker: AutomaticPlayerTracker | None = None,
    selector: TargetPlayerSelector | None = None,
    validator: VideoValidator | None = None,
    lifecycle: RequestLifecycle | None = None,
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
    resolved_lifecycle = lifecycle or RequestLifecycle(
        AdmissionController(max_active_analyses=1),
        AnalysisExecutor(),
        ArtifactManager(
            Path(resolved_settings.debug_output_dir), resolved_settings.max_upload_bytes
        ),
    )
    app.state.admission_controller = resolved_lifecycle.admission
    app.state.analysis_executor = resolved_lifecycle.executor
    app.state.artifact_manager = resolved_lifecycle.artifacts
    app.state.request_lifecycle = resolved_lifecycle
    app.include_router(
        create_router(
            resolved_settings,
            validator or VideoValidator(resolved_settings),
            resolved_tracker,
            selector or WeightedTargetPlayerSelector(resolved_settings),
            FeatureExtractor(),
            NormalizedBallProximityAnalyzer(resolved_settings),
            BottomCenterMovementAnalyzer(resolved_settings),
            BallInteractionAnalyzer(resolved_settings),
            TechnicalEventAnalyzer(resolved_settings),
            PassDetector(resolved_settings),
            ShotDetector(),
            RuleBasedPhysicalActivityScorer(resolved_settings),
            get_logger("football_analysis.api"),
            resolved_lifecycle,
        )
    )
    return app


app = create_app()
