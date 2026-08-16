"""Focused coverage for the completed-analysis evidence diagnostic event."""

import logging

from _pytest.logging import LogCaptureFixture

from api.routes import _log_rating_evidence
from core.logging import configure_logging
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.pass_detection import PassDetectionResult
from services.player_rating.game_intelligence import (
    GameIntelligenceEngine,
    GameIntelligenceEvidence,
    GameIntelligenceResult,
)
from services.scoring.models import PhysicalEvidenceDiagnostics, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult
from services.shot_detection import ShotDetectionResult
from services.technical_events.models import (
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
    TechnicalEvidenceDiagnostics,
)


def _technical_events() -> TechnicalEventAnalysisResult:
    gate = TechnicalEvidenceDiagnostics(
        0.4,
        0.6,
        0.3,
        0.5,
        {
            "player_track_quality": 0.5,
            "ball_analysis_quality": 0.5,
            "interaction_analysis_quality": 0.5,
            "interaction_evidence_coverage_ratio": 0.6,
        },
        (
            "player_track_quality",
            "interaction_analysis_quality",
            "interaction_evidence_coverage_ratio",
        ),
    )
    return TechnicalEventAnalysisResult(
        (), (), (), TechnicalEventDiagnostics(evidence_gate=gate), (), "x"
    )


def _physical() -> PhysicalScoreResult:
    gate = PhysicalEvidenceDiagnostics(
        0.4,
        0.8,
        2.0,
        20,
        0.4,
        {
            "movement_quality": 0.55,
            "visibility_ratio": 0.2,
            "visible_duration_seconds": 3.0,
            "movement_observations": 30,
            "accepted_interval_ratio": 0.6,
        },
        (
            "movement_quality",
            "visible_duration_seconds",
            "movement_observations",
            "accepted_interval_ratio",
        ),
    )
    return PhysicalScoreResult(
        None,
        None,
        None,
        None,
        None,
        "insufficient_evidence",
        "v",
        "x",
        None,
        (),
        "",
        None,
        False,
        0,
        gate,
    )


def _game() -> GameIntelligenceResult:
    return GameIntelligenceEngine().evaluate(
        GameIntelligenceEvidence(
            3.0,
            0.8,
            0.8,
            0.8,
            1.0,
            1,
            1.0,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            1.0,
            0.8,
            0.8,
        )
    )


def _interaction() -> InteractionAnalysisResult:
    diagnostics = InteractionDiagnostics(3, 2, 0, 0, 2, 1, 0, 0, 0, 0, 0.8, "v", 0.8, 0)
    return InteractionAnalysisResult((), 3, 1.0, 1.0, 0.8, 3, 3, 0.8, "v", diagnostics, (), None)


def test_rating_evidence_log_identifies_all_failed_gates_and_analysis_id(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="football_analysis")
    _log_rating_evidence(
        logging.getLogger("football_analysis.api"),
        "analysis-1",
        _physical(),
        _technical_events(),
        TechnicalScoreResult(None, None, "unavailable", "x", {}, None, None, 0, 0),
        _game(),
        _interaction(),
        PassDetectionResult((), 3, 1, 2, {}, 0),
        ShotDetectionResult((), 2, 1, 1, {}, 0),
    )

    message = next(
        record.getMessage() for record in caplog.records if record.msg.startswith("rating_evidence")
    )
    assert "analysis_id=analysis-1" in message
    assert (
        "technical_failed_reasons=('player_track_quality', 'interaction_analysis_quality', 'interaction_evidence_coverage_ratio')"
        in message
    )
    assert (
        "physical_failed_reasons=('movement_quality', 'visible_duration_seconds', 'movement_observations', 'accepted_interval_ratio')"
        in message
    )
    assert "game_failed_reasons=('visible_duration_seconds',)" in message
    assert (
        "possible_ball_interactions=3 interaction_segments=0 accepted_interaction_segments=1"
        in message
    )
    assert (
        "pass_detection_available=True pass_candidates=3 accepted_passes=1 "
        "shot_detection_available=True shot_candidates=2 accepted_shots=1" in message
    )


def test_rating_evidence_logger_is_reachable_from_production_namespace() -> None:
    configure_logging()
    assert logging.getLogger("football_analysis.api").isEnabledFor(logging.INFO)
