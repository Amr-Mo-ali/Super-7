"""Unit coverage for technical scoring from detected candidates only."""

from services.scoring.technical import TechnicalScorer
from services.technical_events.models import (
    BallLossCandidate,
    ControlledMovementCandidate,
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
)


def _controlled() -> ControlledMovementCandidate:
    return ControlledMovementCandidate("c", 1, 0, 10, 0, 1, 1, 100, 0.7, 10, 1, 0.8, 0.8)


def _events(
    controlled: tuple[ControlledMovementCandidate, ...] = (),
    losses: tuple[BallLossCandidate, ...] = (),
) -> TechnicalEventAnalysisResult:
    return TechnicalEventAnalysisResult(
        controlled,
        (),
        losses,
        TechnicalEventDiagnostics(technical_event_analysis_quality=0.8),
        (),
        None,
    )


def test_controlled_movement_only_generates_provisional_score() -> None:
    result = TechnicalScorer().score(_events((_controlled(),)))
    assert result.value is not None and 0 < result.value < 100
    assert result.status == "provisional_event_based"


def test_no_events_is_unavailable() -> None:
    assert TechnicalScorer().score(_events()).value is None


def test_ball_loss_reduces_existing_evidence_score() -> None:
    loss = BallLossCandidate("l", 1, 5, 0.5, 1, 2, 3, False, 0.8)
    scorer = TechnicalScorer()
    with_loss = scorer.score(_events((_controlled(),), (loss,))).value
    without_loss = scorer.score(_events((_controlled(),))).value
    assert with_loss is not None and without_loss is not None
    assert with_loss < without_loss
