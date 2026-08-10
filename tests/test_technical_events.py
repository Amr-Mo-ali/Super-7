"""Deterministic characterization tests for technical-event heuristics.

These tests intentionally exercise the current implementation's thresholds and
diagnostics.  They are not specifications for a redesigned event engine.
"""

from dataclasses import replace
from math import isfinite
from typing import cast

import pytest

from core.config import Settings
from core.exceptions import TechnicalEventInputError
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
from services.technical_events.models import (
    ControlledMovementCandidate,
    TechnicalEventAnalysisResult,
)

FPS = 10.0
QUALITY = 0.9


def _segment(
    segment_id: int = 1, start: int = 0, end: int = 9, duration: float | None = None
) -> InteractionSegment:
    duration = (end - start + 1) / FPS if duration is None else duration
    return InteractionSegment(
        segment_id,
        start,
        end,
        start / FPS,
        end / FPS,
        duration,
        end - start + 1,
        0,
        end - start + 1,
        1.0,
        2.0,
        2.0,
        0.2,
        0.2,
        0.9,
        0.9,
        0.9,
    )


def _interaction(*segments: InteractionSegment, coverage: float = 1.0) -> InteractionAnalysisResult:
    diagnostics = InteractionDiagnostics(
        10, 10, 0, 0, len(segments), len(segments), 0, 0, 0, 0, coverage, "v", QUALITY, 0
    )
    return InteractionAnalysisResult(
        tuple(segments),
        len(segments),
        1.0,
        1.0,
        QUALITY,
        10,
        10,
        coverage,
        "v",
        diagnostics,
        (),
        None,
    )


def _observations(
    positions: list[tuple[float, float]],
    ball_positions: list[tuple[float, float]] | None = None,
    start: int = 0,
) -> tuple[tuple[PlayerObservation, ...], tuple[BallObservation, ...]]:
    ball_positions = positions if ball_positions is None else ball_positions
    players = tuple(
        PlayerObservation(
            start + i, (start + i) / FPS, BoundingBox(x - 5, y - 10, x + 5, y), QUALITY
        )
        for i, (x, y) in enumerate(positions)
    )
    balls = tuple(
        BallObservation(start + i, (start + i) / FPS, point, QUALITY)
        for i, point in enumerate(ball_positions)
    )
    return players, balls


def _movement(points: list[tuple[float, float]], start: int = 0) -> MovementResult:
    trajectory = tuple(
        MovementPoint(start + i, (start + i) / FPS, point, 10.0) for i, point in enumerate(points)
    )
    metrics = MovementMetrics(
        0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0
    )
    return MovementResult(metrics, trajectory, 1, start, start + len(points))


def _analyze(
    positions: list[tuple[float, float]],
    *,
    segment: InteractionSegment | None = None,
    ball_positions: list[tuple[float, float]] | None = None,
    settings: Settings | None = None,
    movement: MovementResult | None = None,
    interactions: InteractionAnalysisResult | None = None,
    player_quality: float = QUALITY,
    ball_quality: float = QUALITY,
    interaction_quality: float = QUALITY,
) -> TechnicalEventAnalysisResult:
    players, balls = _observations(positions, ball_positions)
    segment = _segment(end=len(positions) - 1) if segment is None else segment
    return TechnicalEventAnalyzer(settings or Settings()).analyze(
        players,
        balls,
        interactions or _interaction(segment),
        movement,
        FPS,
        (64, 64),
        player_quality,
        ball_quality,
        interaction_quality,
    )


def _controlled(
    segment_id: int = 1, start: int = 0, end: int = 9, duration: float = 1.0
) -> ControlledMovementCandidate:
    return ControlledMovementCandidate(
        f"controlled-{segment_id}",
        segment_id,
        start,
        end,
        start / FPS,
        end / FPS,
        duration,
        20.0,
        2.0,
        20.0,
        1.0,
        1.0,
        0.9,
    )


def test_valid_controlled_movement_has_stable_id_and_formula() -> None:
    result = _analyze([(float(i * 2), 10.0) for i in range(10)])
    candidate = result.controlled_movement_candidates[0]
    assert candidate.event_id == "controlled-1"
    assert candidate.confidence == pytest.approx(0.25 + 0.25 + 0.20 + 0.15 + 0.15 * QUALITY)


@pytest.mark.parametrize(("duration", "accepted"), [(0.25, True), (0.249, False)])
def test_controlled_duration_boundary(duration: float, accepted: bool) -> None:
    result = _analyze(
        [(float(i * 2), 10.0) for i in range(4)], segment=_segment(end=3, duration=duration)
    )
    assert bool(result.controlled_movement_candidates) is accepted
    breakdown = result.diagnostics.controlled_movement_rejection_breakdown
    assert breakdown is not None
    assert breakdown["duration"] == int(not accepted)


@pytest.mark.parametrize(("displacement", "reason"), [(0.15, None), (0.149, "displacement")])
def test_controlled_displacement_boundary(displacement: float, reason: str | None) -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    assert analyzer._controlled_rejection_reason(1.0, displacement, 1.0, 1.0, 1.0, 1.0) == reason


@pytest.mark.parametrize(
    ("proximity", "direction", "reason"),
    [(0.70, 0.35, None), (0.699, 1.0, "proximity"), (1.0, 0.349, "direction")],
)
def test_controlled_proximity_and_direction_boundaries(
    proximity: float, direction: float, reason: str | None
) -> None:
    assert (
        TechnicalEventAnalyzer(Settings())._controlled_rejection_reason(
            1.0, 1.0, proximity, direction, 1.0, 1.0
        )
        == reason
    )


def test_controlled_fragmented_stationary_and_missing_evidence_are_rejected() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    players, balls = _observations([(0.0, 10.0)] * 10)
    balls = balls[:2]
    accepted, raw, short, low, breakdown, statistics = analyzer._controlled(
        _interaction(_segment()), dict(enumerate(players)), dict(enumerate(balls)), QUALITY
    )
    assert accepted == [] and (raw, short, low) == (1, 1, 0)
    assert breakdown["displacement"] == 1 and statistics[0]["accepted"] is False


def test_controlled_rejection_breakdown_is_deterministic() -> None:
    segments = (_segment(3), _segment(1), _segment(2))
    result = _analyze([(0.0, 10.0)] * 10, interactions=_interaction(*segments))
    assert result.diagnostics.controlled_movement_rejection_breakdown == {
        "duration": 0,
        "displacement": 3,
        "proximity": 0,
        "direction": 0,
        "coverage": 0,
        "confidence": 0,
    }


def test_directional_dribble_and_progressive_carry_are_characterized() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    directional = [
        (0, 0),
        (4, 0),
        (8, 0),
        (8, 4),
        (8, 8),
        (12, 8),
        (16, 8),
        (20, 8),
        (24, 8),
        (28, 8),
    ]
    progressive = [(i * 4.0, 10.0) for i in range(10)]
    for points, subtype in (
        (directional, "directional_dribble_candidate"),
        (progressive, "progressive_carry_candidate"),
    ):
        players, balls = _observations([(float(x), float(y)) for x, y in points])
        accepted, *_ = analyzer._dribbles(
            [_controlled()],
            dict(enumerate(players)),
            dict(enumerate(balls)),
            _movement([(float(x), float(y)) for x, y in points]),
            QUALITY,
        )
        assert accepted[0].candidate_subtype == subtype


def test_dribble_turn_filter_boundaries_and_tracker_oscillation_are_characterized() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    turns, small, adjacent = analyzer._filter_turns([(1, 34.9), (2, 35.0), (3, 60.0), (6, 35.0)])
    assert turns == [(2, 35.0), (6, 35.0)] and (small, adjacent) == (1, 1)
    oscillating = [(0, 0), (4, 0), (0, 0), (4, 0), (0, 0), (4, 0), (0, 0), (4, 0), (0, 0), (4, 0)]
    players, balls = _observations([(float(x), float(y)) for x, y in oscillating])
    accepted, _, low_move, _, stats, breakdown = analyzer._dribbles(
        [_controlled()],
        dict(enumerate(players)),
        dict(enumerate(balls)),
        _movement([(float(x), float(y)) for x, y in oscillating]),
        QUALITY,
    )
    # With the current four-frame suppression, alternating tracker positions are
    # reduced to three turns and accepted; this is a documented open question.
    assert len(accepted) == 1 and low_move == 0 and breakdown["excessive_turn_frequency"] == 0
    assert cast(int, stats[0]["filtered_direction_changes"]) > 0


def test_dribble_frequency_trajectory_and_confidence_boundaries() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    assert analyzer._filter_turns([(1, 35.0)])[0] == [(1, 35.0)]
    settings = replace(
        Settings(),
        dribble_max_direction_changes_per_second=1.0,
        dribble_minimum_turn_frame_separation=1,
    )
    points = [(0, 0), (4, 0), (8, 0), (8, 4), (8, 8), (12, 8), (16, 8), (20, 8), (24, 8), (28, 8)]
    players, balls = _observations([(float(x), float(y)) for x, y in points])
    accepted, _, _, _, stats, breakdown = TechnicalEventAnalyzer(settings)._dribbles(
        [_controlled()],
        dict(enumerate(players)),
        dict(enumerate(balls)),
        _movement([(float(x), float(y)) for x, y in points]),
        QUALITY,
    )
    assert accepted == [] and breakdown["excessive_turn_frequency"] == 1
    assert 0 <= cast(float, stats[0]["raw_dribble_confidence"]) <= 1


def test_ball_loss_acceptance_recovery_and_window_boundary() -> None:
    segment = _segment(end=3, duration=0.4)
    positions = [(0.0, 10.0)] * 9
    away_balls = [(0.0, 10.0)] * 4 + [
        (16.0, 10.0),
        (20.0, 10.0),
        (24.0, 10.0),
        (28.0, 10.0),
        (32.0, 10.0),
    ]
    accepted = _analyze(positions, segment=segment, ball_positions=away_balls)
    assert accepted.ball_loss_candidates[0].event_id == "ball-loss-1"
    recovered_balls = away_balls.copy()
    recovered_balls[8] = (0.0, 10.0)
    recovered = _analyze(positions, segment=segment, ball_positions=recovered_balls)
    assert (
        recovered.ball_loss_candidates == ()
        and recovered.diagnostics.ball_loss_rejected_recovery == 1
    )


def test_ball_loss_missing_and_post_window_observations_are_characterized() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    segment = _segment(end=3, duration=0.4)
    players, balls = _observations([(0.0, 10.0)] * 9, [(0.0, 10.0)] * 4 + [(20.0, 10.0)] * 5)
    _, _, missing, recovery = analyzer._losses(
        _interaction(segment), dict(enumerate(players)), dict(enumerate(balls[:6])), QUALITY, FPS
    )
    assert (missing, recovery) == (1, 0)
    # The recovery window ends at frame 8; recovery at frame 9 is intentionally ignored.
    balls_after = tuple(list(balls) + [BallObservation(9, 0.9, (0.0, 10.0), QUALITY)])
    _, _, _, after_recovery = analyzer._losses(
        _interaction(segment),
        dict(enumerate(players)),
        {item.frame_index: item for item in balls_after},
        QUALITY,
        FPS,
    )
    assert after_recovery == 0


@pytest.mark.parametrize("which", ["player", "ball", "interaction"])
def test_quality_gates_return_reason(which: str) -> None:
    values = {"player": QUALITY, "ball": QUALITY, "interaction": QUALITY}
    values[which] = 0.49
    result = _analyze(
        [(float(i * 2), 10.0) for i in range(10)],
        player_quality=values["player"],
        ball_quality=values["ball"],
        interaction_quality=values["interaction"],
    )
    assert result.reason is not None and result.controlled_movement_candidates == ()


def test_invalid_fps_empty_and_duplicate_inputs() -> None:
    analyzer = TechnicalEventAnalyzer(Settings())
    with pytest.raises(TechnicalEventInputError, match="positive FPS"):
        analyzer.analyze((), (), _interaction(), None, 0, (64, 64), QUALITY, QUALITY, QUALITY)
    empty = analyzer.analyze((), (), _interaction(), None, FPS, (64, 64), QUALITY, QUALITY, QUALITY)
    assert empty.controlled_movement_candidates == ()
    players, balls = _observations([(0.0, 10.0), (2.0, 10.0)])
    with pytest.raises(TechnicalEventInputError, match="Duplicate"):
        analyzer.analyze(
            (players[0], players[0]),
            balls,
            _interaction(),
            None,
            FPS,
            (64, 64),
            QUALITY,
            QUALITY,
            QUALITY,
        )


def test_order_limits_confidence_and_accounting_invariants() -> None:
    settings = replace(Settings(), technical_event_max_returned_events=1)
    segments = (_segment(2), _segment(1))
    result = _analyze(
        [(float(i * 2), 10.0) for i in range(10)],
        settings=settings,
        interactions=_interaction(*segments),
    )
    diagnostics = result.diagnostics
    assert [item.event_id for item in result.controlled_movement_candidates] == ["controlled-2"]
    assert diagnostics.controlled_movement_raw_candidates == 2
    assert diagnostics.controlled_movement_accepted_candidates == 1
    assert (
        diagnostics.controlled_movement_raw_candidates
        != diagnostics.controlled_movement_accepted_candidates
        + diagnostics.controlled_movement_rejected_short
        + diagnostics.controlled_movement_rejected_low_confidence
    )
    assert all(
        isfinite(item.confidence) and 0 <= item.confidence <= 1
        for item in result.controlled_movement_candidates
        + result.dribble_candidates
        + result.ball_loss_candidates
    )
    assert all(
        item.start_frame <= item.end_frame and item.duration_seconds >= 0
        for item in result.controlled_movement_candidates
    )
