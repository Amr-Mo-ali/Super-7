"""Deterministic synthetic checks for technical-event candidate heuristics."""

from core.config import Settings
from services.interactions.models import (
    BallObservation,
    InteractionAnalysisResult,
    InteractionDiagnostics,
    InteractionSegment,
    PlayerObservation,
)
from services.movement.schemas import MovementMetrics, MovementPoint, MovementResult
from services.player_detector import BoundingBox
from services.technical_events.analyzer import TechnicalEventAnalyzer


def _interaction() -> InteractionAnalysisResult:
    segment = InteractionSegment(
        1, 0, 9, 0.0, 0.9, 1.0, 10, 0, 10, 1.0, 2, 2, 0.2, 0.2, 0.9, 0.9, 0.9
    )
    diagnostics = InteractionDiagnostics(10, 10, 0, 0, 1, 1, 0, 0, 0, 2, 1.0, "v", 0.9, 0)
    return InteractionAnalysisResult(
        (segment,), 1, 1.0, 1.0, 0.9, 10, 10, 1.0, "v", diagnostics, (), None
    )


def _movement() -> MovementResult:
    trajectory = tuple(
        MovementPoint(frame, frame / 10, (frame * 2.0, 10.0), 10.0) for frame in range(10)
    )
    metrics = MovementMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    return MovementResult(metrics, trajectory, 1, 0, 10)


def test_sustained_movement_creates_controlled_candidate() -> None:
    players = tuple(
        PlayerObservation(i, i / 10, BoundingBox(i * 2, 0, i * 2 + 10, 10), 0.9) for i in range(10)
    )
    balls = tuple(BallObservation(i, i / 10, (i * 2 + 5, 10), 0.9) for i in range(10))
    result = TechnicalEventAnalyzer(Settings()).analyze(
        players, balls, _interaction(), _movement(), 10, (64, 64), 0.9, 0.9, 0.9
    )
    assert len(result.controlled_movement_candidates) == 1
    assert result.controlled_movement_candidates[0].confidence <= 1


def test_stationary_player_is_not_a_controlled_candidate() -> None:
    players = tuple(PlayerObservation(i, i / 10, BoundingBox(0, 0, 10, 10), 0.9) for i in range(10))
    balls = tuple(BallObservation(i, i / 10, (5, 10), 0.9) for i in range(10))
    result = TechnicalEventAnalyzer(Settings()).analyze(
        players, balls, _interaction(), _movement(), 10, (64, 64), 0.9, 0.9, 0.9
    )
    assert result.controlled_movement_candidates == ()
    assert result.diagnostics.controlled_movement_rejection_breakdown == {
        "duration": 0,
        "displacement": 1,
        "proximity": 0,
        "direction": 0,
        "coverage": 0,
        "confidence": 0,
    }
    assert result.diagnostics.controlled_movement_thresholds == {
        "min_duration": 0.25,
        "min_displacement": 0.15,
        "min_direction_similarity": 0.35,
    }


def test_low_quality_returns_reason_without_candidates() -> None:
    result = TechnicalEventAnalyzer(Settings()).analyze(
        (), (), _interaction(), None, 10, (64, 64), 0.9, 0.1, 0.9
    )
    assert result.reason is not None
    assert result.dribble_candidates == ()
