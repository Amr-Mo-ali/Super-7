"""Focused contract tests for evidence-gated detailed callback ratings."""

from math import nan

import pytest

from schemas.analysis import (
    BallLossCandidateResponse,
    ControlledMovementCandidateResponse,
    DribbleCandidateResponse,
    PassCandidateResponse,
    PassDetectionResponse,
    PhysicalScoreEvidenceResponse,
    PhysicalScoreResponse,
    ShotCandidateResponse,
    ShotDetectionResponse,
    TechnicalEventAnalysisResponse,
    UnsupportedMetric,
)
from services.detailed_rating.engine import DetailedRatingEngine
from services.event_arbitration import EventArbitrator, EventCandidateRef
from services.event_arbitration.models import ArbitrationResult


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


def _pass(
    pass_id: str, confidence: float, possessor: int = 1, start: int = 0
) -> PassCandidateResponse:
    return PassCandidateResponse(
        pass_id=pass_id,
        possessor_track_id=possessor,
        receiver_track_id=2,
        start_frame=start,
        release_frame=start + 2,
        end_frame=start + 5,
        duration_seconds=0.5,
        distance=100,
        confidence=confidence,
        release_speed=200,
        trajectory_points=[(0, 0), (50, 0), (100, 0)],
        trajectory_duration=0.3,
        trajectory_length=100,
        trajectory_direction=(1, 0),
        trajectory_quality=0.9,
        status="pass_candidate",
    )


def _shot(
    shot_id: str, confidence: float, possessor: int = 1, start: int = 0
) -> ShotCandidateResponse:
    return ShotCandidateResponse(
        shot_id=shot_id,
        possessor_track_id=possessor,
        start_frame=start,
        preparation_start_frame=start,
        preparation_end_frame=start + 1,
        release_frame=start + 2,
        end_frame=start + 5,
        duration_seconds=0.5,
        distance=100,
        trajectory_points=[(0, 0), (50, 0), (100, 0)],
        trajectory_duration=0.3,
        mean_speed=200,
        maximum_speed=300,
        release_speed=250,
        release_direction=(1, 0),
        release_acceleration=100,
        preparation_confidence=0.9,
        release_confidence=0.9,
        trajectory_quality=0.9,
        follow_through_confidence=0.9,
        confidence=confidence,
        status="shot_candidate",
    )


def _arbitration(
    passes: list[PassCandidateResponse] | None = None,
    shots: list[ShotCandidateResponse] | None = None,
) -> ArbitrationResult:
    pass_items, shot_items = passes or [], shots or []
    refs = [
        EventCandidateRef(
            item.pass_id,
            "pass",
            item.start_frame,
            item.release_frame,
            item.end_frame,
            item.possessor_track_id,
            item.receiver_track_id,
            item.confidence,
            item.trajectory_quality,
            item.distance,
            "test",
        )
        for item in pass_items
    ] + [
        EventCandidateRef(
            item.shot_id,
            "shot",
            item.start_frame,
            item.release_frame,
            item.end_frame,
            item.possessor_track_id,
            None,
            item.confidence,
            item.trajectory_quality,
            item.distance,
            "test",
            item.preparation_confidence,
            item.release_confidence,
            item.follow_through_confidence,
        )
        for item in shot_items
    ]
    return EventArbitrator().arbitrate(tuple(refs))


def _detailed(
    passes: list[PassCandidateResponse] | None = None,
    shots: list[ShotCandidateResponse] | None = None,
    target: int = 1,
):
    pass_items, shot_items = passes or [], shots or []
    return DetailedRatingEngine().evaluate(
        _physical(),
        _events([_controlled()]),
        0.8,
        PassDetectionResponse(pass_candidates=pass_items),
        ShotDetectionResponse(shot_candidates=shot_items),
        _arbitration(pass_items, shot_items),
        target,
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


def test_target_accepted_pass_and_shot_scores_are_mean_event_confidence() -> None:
    ratings = _detailed(
        [_pass("pass-1", 0.8), _pass("pass-2", 0.6, start=10)],
        [_shot("shot-1", 0.9, start=20), _shot("shot-2", 0.7, start=30)],
    )

    assert ratings.passing_and_playmaking == pytest.approx(70.0)
    assert ratings.shooting_and_finishing == pytest.approx(80.0)
    assert ratings.speed_and_fitness == 70.0
    assert ratings.ball_control_and_individual_skill == pytest.approx(73.0)
    assert ratings.defending_and_duels is None
    assert ratings.tactical_intelligence_and_teamwork is None
    assert ratings.positioning_and_off_ball_movement is None


def test_other_players_events_do_not_contaminate_target_detailed_ratings() -> None:
    ratings = _detailed(
        [_pass("target-pass", 0.6), _pass("other-pass", 1.0, possessor=2, start=10)],
        [_shot("target-shot", 0.7, start=20), _shot("other-shot", 1.0, possessor=2, start=30)],
    )
    other_only = _detailed([_pass("other-pass", 0.9, 2)], [_shot("other-shot", 0.9, 2)])

    assert ratings.passing_and_playmaking == pytest.approx(60.0)
    assert ratings.shooting_and_finishing == pytest.approx(70.0)
    assert other_only.passing_and_playmaking is None
    assert other_only.shooting_and_finishing is None


def test_ambiguous_or_absent_events_do_not_produce_detailed_scores() -> None:
    ambiguous = _detailed([_pass("pass-1", 0.9)], [_shot("shot-1", 0.9)])
    absent = _detailed()

    assert ambiguous.passing_and_playmaking is None
    assert ambiguous.shooting_and_finishing is None
    assert absent.passing_and_playmaking is None
    assert absent.shooting_and_finishing is None


def test_non_finite_event_confidence_is_safely_excluded() -> None:
    pass_item = _pass("pass-1", 0.8).model_copy(update={"confidence": nan})
    shot_item = _shot("shot-1", 0.8).model_copy(update={"confidence": nan})
    ratings = _detailed([pass_item], [shot_item])

    assert ratings.passing_and_playmaking is None
    assert ratings.shooting_and_finishing is None
