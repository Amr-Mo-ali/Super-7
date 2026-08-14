"""Focused contract tests for evidence-gated detailed callback ratings."""

import pytest

from schemas.analysis import (
    BallLossCandidateResponse,
    ControlledMovementCandidateResponse,
    DribbleCandidateResponse,
    PhysicalScoreEvidenceResponse,
    PhysicalScoreResponse,
    TechnicalEventAnalysisResponse,
    UnsupportedMetric,
)
from services.detailed_rating.engine import DetailedRatingEngine


def _physical(
    status: str = "provisional_video_based", intensity: float = 0.7
) -> PhysicalScoreResponse:
    return PhysicalScoreResponse(
        value=75,
        level=3,
        confidence=0.7,
        status=status,
        version="physical_activity_video_v0.1",
        evidence=PhysicalScoreEvidenceResponse(
            movement_intensity=intensity,
            active_time_ratio=0.8,
            visibility_ratio=0.8,
            continuity_ratio=0.9,
            direction_component=0.5,
            movement_analysis_quality=0.8,
            movement_duration_seconds=5,
            movement_observations=50,
            accepted_interval_ratio=0.9,
        ),
        explanation="visible movement activity",
    )


def _controlled() -> ControlledMovementCandidateResponse:
    return ControlledMovementCandidateResponse(
        event_id="controlled-1",
        source_interaction_segment_id=1,
        start_frame=0,
        end_frame=10,
        start_time_seconds=0,
        end_time_seconds=1,
        duration_seconds=1,
        player_displacement_pixels=100,
        normalized_player_displacement=0.7,
        ball_displacement_pixels=10,
        proximity_frame_ratio=1,
        direction_similarity=0.8,
        confidence=0.8,
        status="controlled_movement_candidate",
    )


def _dribble() -> DribbleCandidateResponse:
    return DribbleCandidateResponse(
        event_id="dribble-1",
        source_controlled_movement_id="controlled-1",
        start_frame=0,
        end_frame=10,
        duration_seconds=1,
        direction_changes=2,
        normalized_player_displacement=0.7,
        normalized_player_path_length=0.8,
        movement_evidence_component=0.7,
        candidate_subtype="directional_dribble_candidate",
        proximity_persistence=0.8,
        path_straightness=0.9,
        confidence=0.8,
        confidence_version="dribble_candidate_confidence_v0.2",
        status="dribble_candidate",
    )


def _events(
    controlled: list[ControlledMovementCandidateResponse] | None = None,
    dribbles: list[DribbleCandidateResponse] | None = None,
    losses: list[BallLossCandidateResponse] | None = None,
) -> TechnicalEventAnalysisResponse:
    return TechnicalEventAnalysisResponse(
        controlled_movement_candidates=controlled or [],
        dribble_candidates=dribbles or [],
        ball_loss_candidates=losses or [],
    )


def test_supported_detailed_scores_are_deterministic_and_bounded() -> None:
    engine = DetailedRatingEngine()
    first = engine.evaluate(_physical(), _events([_controlled()], [_dribble()]), 0.8)
    second = engine.evaluate(_physical(), _events([_controlled()], [_dribble()]), 0.8)

    assert first == second
    assert first.speed_and_fitness == 70.0
    assert first.ball_control_and_individual_skill is not None
    assert 0 <= first.ball_control_and_individual_skill <= 100


def test_ball_control_reuses_the_existing_controlled_movement_formula() -> None:
    ratings = DetailedRatingEngine().evaluate(_physical(), _events([_controlled()]), 0.8)

    assert ratings.ball_control_and_individual_skill == pytest.approx(73.0)


def test_insufficient_or_low_quality_evidence_is_null_not_zero() -> None:
    ratings = DetailedRatingEngine().evaluate(
        UnsupportedMetric(reason="insufficient_movement_evidence"), _events(), 0.0
    )

    assert ratings.speed_and_fitness is None
    assert ratings.ball_control_and_individual_skill is None
    assert ratings.speed_and_fitness != 0
    assert ratings.ball_control_and_individual_skill != 0


def test_ball_loss_reduces_ball_control_without_using_unrelated_events() -> None:
    loss = BallLossCandidateResponse(
        event_id="loss-1",
        source_interaction_segment_id=1,
        event_frame=5,
        event_time_seconds=0.5,
        pre_interaction_duration_seconds=1,
        maximum_separation_ratio=2,
        post_evidence_frames=3,
        recovered_within_window=False,
        confidence=0.8,
        status="ball_loss_candidate",
    )
    engine = DetailedRatingEngine()
    without_loss = engine.evaluate(_physical(), _events([_controlled()]), 0.8)
    with_loss = engine.evaluate(_physical(), _events([_controlled()], losses=[loss]), 0.8)

    assert with_loss.ball_control_and_individual_skill is not None
    assert without_loss.ball_control_and_individual_skill is not None
    assert (
        with_loss.ball_control_and_individual_skill < without_loss.ball_control_and_individual_skill
    )
    assert with_loss.passing_and_playmaking is None
    assert with_loss.shooting_and_finishing is None
    assert with_loss.defending_and_duels is None
    assert with_loss.tactical_intelligence_and_teamwork is None
    assert with_loss.positioning_and_off_ball_movement is None


def test_insufficient_physical_status_does_not_expose_movement_activity() -> None:
    ratings = DetailedRatingEngine().evaluate(
        _physical(status="insufficient_evidence"), _events([_controlled()]), 0.8
    )

    assert ratings.speed_and_fitness is None
