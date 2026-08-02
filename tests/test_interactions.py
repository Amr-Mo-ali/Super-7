"""Deterministic unit tests for possible-ball-interaction heuristics."""

import pytest

from core.config import Settings
from core.exceptions import InteractionConfidenceError, InteractionInputError
from services.interactions.analyzer import BallInteractionAnalyzer
from services.interactions.models import (
    BallObservation,
    InteractionAnalysisResult,
    PlayerObservation,
)
from services.player_detector import BoundingBox


def _player(frame: int, confidence: float = 0.9) -> PlayerObservation:
    return PlayerObservation(frame, frame / 10, BoundingBox(0, 0, 10, 10), confidence)


def _ball(frame: int, x: float = 5, confidence: float = 0.9) -> BallObservation:
    return BallObservation(frame, frame / 10, (x, 10), confidence)


def _analyze(
    players: tuple[PlayerObservation, ...], balls: tuple[BallObservation, ...]
) -> InteractionAnalysisResult:
    return BallInteractionAnalyzer(
        Settings(interaction_min_segment_frames=2, interaction_min_segment_duration_seconds=0)
    ).analyze(players, balls, 10, (64, 64), 0.9, 0.9)


def test_aligns_frames_and_normalizes_bottom_center_distance() -> None:
    result = _analyze((_player(0), _player(1)), (_ball(0, 5), _ball(1, 15)))
    segment = result.segments[0]
    assert segment.mean_distance_pixels == pytest.approx(5)
    assert segment.mean_normalized_distance == pytest.approx(0.5)


def test_short_missing_evidence_gap_is_bridged_but_non_candidate_splits() -> None:
    bridged = _analyze((_player(0), _player(1), _player(2)), (_ball(0), _ball(2)))
    assert bridged.possible_ball_interaction_count == 1
    assert bridged.segments[0].bridged_gap_frames == 1
    split = _analyze(
        (_player(0), _player(1), _player(2), _player(3)),
        (_ball(0), _ball(1, 50), _ball(2), _ball(3)),
    )
    assert split.diagnostics.raw_interaction_segments == 2


def test_duplicate_frames_are_rejected() -> None:
    with pytest.raises(InteractionInputError):
        _analyze((_player(0), _player(0)), (_ball(0),))


def test_low_quality_degrades_without_segments() -> None:
    result = BallInteractionAnalyzer(Settings()).analyze(
        tuple(_player(frame) for frame in range(5)),
        tuple(_ball(frame) for frame in range(5)),
        10,
        (64, 64),
        0.1,
        0.9,
    )
    assert result.segments == ()
    assert result.reason is not None


def test_confidence_weights_must_sum_to_one() -> None:
    with pytest.raises(InteractionConfidenceError):
        BallInteractionAnalyzer(Settings(interaction_distance_weight=0.9)).analyze(
            tuple(_player(frame) for frame in range(5)),
            tuple(_ball(frame) for frame in range(5)),
            10,
            (64, 64),
            0.9,
            0.9,
        )
