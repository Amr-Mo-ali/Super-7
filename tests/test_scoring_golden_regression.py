"""Golden regression coverage for the current provisional scoring formulas."""

import pytest

from core.config import Settings
from services.movement.schemas import MovementMetrics, MovementPoint, MovementResult
from services.scoring.physical_activity import RuleBasedPhysicalActivityScorer
from services.scoring.technical import TechnicalScorer
from services.technical_events.models import (
    BallLossCandidate,
    ControlledMovementCandidate,
    DribbleCandidate,
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
)


def _events() -> TechnicalEventAnalysisResult:
    controlled = ControlledMovementCandidate(
        "controlled-1", 1, 0, 10, 0, 1, 1, 100, 0.7, 10, 1, 0.8, 0.8
    )
    dribble = DribbleCandidate(
        "dribble-1",
        "controlled-1",
        0,
        10,
        1,
        2,
        0.7,
        0.8,
        0.8,
        "directional_dribble_candidate",
        0.5,
        0.6,
        0.75,
        "dribble_candidate_confidence_v0.2",
    )
    loss = BallLossCandidate("loss-1", 1, 10, 1, 1, 2, 3, False, 0.9)
    return TechnicalEventAnalysisResult(
        (controlled,),
        (dribble,),
        (loss,),
        TechnicalEventDiagnostics(technical_event_analysis_quality=0.8),
        (),
    )


def _movement() -> MovementResult:
    metrics = MovementMetrics(100, 20, 30, 0, 0, 2, 0, 0, 1, 0, 0.7, 0, 0, 0, 0, 0, 0, 0)
    points = tuple(MovementPoint(index, index / 10, (0, 0), 10) for index in range(51))
    return MovementResult(metrics, points, 1, 0, 51)


def test_technical_score_golden_output() -> None:
    result = TechnicalScorer().score(_events())

    assert result.value == pytest.approx(63.83333333333333)
    assert result.confidence == pytest.approx(0.62)
    assert result.controlled_component == pytest.approx(0.73)
    assert result.dribble_component == pytest.approx(0.6816666666666666)
    assert result.ball_loss_penalty == pytest.approx(0.0675)
    assert result.evidence == {
        "controlled_movement_events": 1.0,
        "dribble_events": 1.0,
        "ball_loss_events": 1.0,
    }


def test_physical_score_golden_output() -> None:
    result = RuleBasedPhysicalActivityScorer(Settings()).score(
        _movement(), 0.8, 51, 51, 0.9, 0.8, "raw_image_space"
    )

    assert result.value == pytest.approx(75.94444444444444)
    assert result.raw_score == pytest.approx(0.7594444444444445)
    assert result.confidence == pytest.approx(0.75)
    assert result.confidence_capped is True
    assert (result.level, result.level_label, result.level_midpoint) == (4, "good", 75.0)
