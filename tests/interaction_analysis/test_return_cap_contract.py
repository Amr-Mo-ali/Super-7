"""Red regression contract for capped interaction results (audit finding F10)."""

import pytest

from core.config import Settings
from services.interactions.analyzer import BallInteractionAnalyzer
from services.interactions.models import (
    BallObservation,
    InteractionAnalysisResult,
    PlayerObservation,
)
from services.player_detector import BoundingBox

FPS = 30.0
FRAMES_PER_SEGMENT = 6
CANDIDATE_FRAMES_PER_SEGMENT = 5


def _observations(
    segment_count: int,
    *,
    candidate_frames_per_segment: tuple[int, ...] | None = None,
) -> tuple[tuple[PlayerObservation, ...], tuple[BallObservation, ...]]:
    candidate_frame_counts = (
        candidate_frames_per_segment or (CANDIDATE_FRAMES_PER_SEGMENT,) * segment_count
    )
    assert len(candidate_frame_counts) == segment_count
    players: list[PlayerObservation] = []
    balls: list[BallObservation] = []
    for segment_index, candidate_frame_count in enumerate(candidate_frame_counts):
        start = segment_index * FRAMES_PER_SEGMENT
        for offset in range(FRAMES_PER_SEGMENT):
            frame = start + offset
            players.append(PlayerObservation(frame, frame / FPS, BoundingBox(0, 0, 10, 10), 0.9))
            ball_x = 5.0 if offset < candidate_frame_count else 50.0
            balls.append(BallObservation(frame, frame / FPS, (ball_x, 10.0), 0.9))
    return tuple(players), tuple(balls)


def _analyze(
    segment_count: int,
    *,
    cap: int = 100,
    candidate_frames_per_segment: tuple[int, ...] | None = None,
) -> InteractionAnalysisResult:
    players, balls = _observations(
        segment_count,
        candidate_frames_per_segment=candidate_frames_per_segment,
    )
    return BallInteractionAnalyzer(Settings(interaction_max_returned_segments=cap)).analyze(
        players, balls, FPS, (64, 64), 0.9, 0.9
    )


def _rejected_count(result: InteractionAnalysisResult) -> int:
    diagnostics = result.diagnostics
    return (
        diagnostics.rejected_short_interaction_segments
        + diagnostics.rejected_low_confidence_interaction_segments
        + diagnostics.rejected_low_global_quality_interaction_segments
        + diagnostics.rejected_invalid_interaction_segments
    )


@pytest.mark.parametrize("segment_count", (1, 99, 100))
def test_behavior_at_or_below_default_cap_is_unchanged(segment_count: int) -> None:
    result = _analyze(segment_count)

    assert len(result.segments) == segment_count
    assert result.possible_ball_interaction_count == segment_count
    assert result.interaction_candidate_frames == segment_count * CANDIDATE_FRAMES_PER_SEGMENT
    assert result.possible_ball_interaction_time_seconds == pytest.approx(segment_count * 5 / FPS)
    assert result.diagnostics.raw_interaction_segments == segment_count
    assert result.diagnostics.accepted_interaction_segments == segment_count
    assert _rejected_count(result) == 0


def test_one_candidate_over_default_cap_preserves_result_and_full_accounting() -> None:
    result = _analyze(101)
    diagnostics = result.diagnostics
    returned_count = len(result.segments)
    rejected_count = _rejected_count(result)

    assert returned_count == 100
    assert result.possible_ball_interaction_count == returned_count
    assert diagnostics.raw_interaction_segments == 101
    assert diagnostics.accepted_interaction_segments == 101
    assert diagnostics.raw_interaction_segments == (
        diagnostics.accepted_interaction_segments + rejected_count
    )
    assert diagnostics.accepted_interaction_segments - returned_count == 1
    assert rejected_count == 0
    assert result.possible_ball_interaction_time_seconds == pytest.approx(
        returned_count * CANDIDATE_FRAMES_PER_SEGMENT / FPS
    )
    assert result.longest_possible_ball_interaction_seconds == pytest.approx(
        CANDIDATE_FRAMES_PER_SEGMENT / FPS
    )
    assert result.mean_possible_ball_interaction_confidence is not None
    assert result.interaction_candidate_frames == 101 * CANDIDATE_FRAMES_PER_SEGMENT


def test_configured_return_cap_limits_collection_without_reclassifying_valid_candidates() -> None:
    result = _analyze(8, cap=7)

    assert len(result.segments) == result.possible_ball_interaction_count == 7
    assert tuple(segment.segment_id for segment in result.segments) == tuple(range(1, 8))
    assert result.diagnostics.raw_interaction_segments == 8
    assert result.diagnostics.accepted_interaction_segments == 8
    assert result.diagnostics.accepted_interaction_segments - len(result.segments) == 1
    assert _rejected_count(result) == 0


def test_configured_return_cap_preserves_mixed_accepted_and_rejected_accounting() -> None:
    result = _analyze(
        7,
        cap=3,
        candidate_frames_per_segment=(5, 4, 5, 4, 5, 5, 5),
    )

    assert len(result.segments) == result.possible_ball_interaction_count == 3
    assert tuple(segment.segment_id for segment in result.segments) == (1, 2, 3)
    assert result.diagnostics.raw_interaction_segments == 7
    assert result.diagnostics.accepted_interaction_segments == 5
    assert result.diagnostics.rejected_short_interaction_segments == 2
    assert result.diagnostics.raw_interaction_segments == (
        result.diagnostics.accepted_interaction_segments + _rejected_count(result)
    )
    assert result.diagnostics.accepted_interaction_segments - len(result.segments) == 2
