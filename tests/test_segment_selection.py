"""Synthetic coverage for segment-based target player selection."""

from core.config import Settings
from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox
from services.segment_selection import build_segments, select_segment


def _inputs(
    frames: list[int], confidence: float = 0.9
) -> tuple[dict[int, dict[int, BoundingBox]], dict[int, dict[int, float]]]:
    boxes = {1: {frame: BoundingBox(frame, 0, frame + 20, 100) for frame in frames}}
    return boxes, {1: {frame: confidence for frame in frames}}


def test_fragmented_track_creates_multiple_segments_and_short_gaps_remain_joined() -> None:
    boxes, confidences = _inputs([0, 1, 4, 5, 10, 11])
    segments = build_segments(boxes, confidences, {}, 10, Settings(target_segment_max_gap_frames=2))
    assert [(x.start_frame, x.end_frame, x.visible_frames) for x in segments] == [
        (0, 5, 4),
        (10, 11, 2),
    ]


def test_long_gaps_low_confidence_and_short_segments_are_rejected() -> None:
    boxes, confidences = _inputs([0, 1, 10, 11], confidence=0.1)
    segments = build_segments(
        boxes, confidences, {}, 10, Settings(target_segment_min_visible_frames=3)
    )
    assert len(segments) == 2
    assert all("insufficient_visible_frames" in x.rejection_reasons for x in segments)
    assert all("low_mean_confidence" in x.rejection_reasons for x in segments)


def test_best_valid_segment_can_succeed_when_whole_video_visibility_is_low() -> None:
    boxes, confidences = _inputs(list(range(100, 140)))
    segments = build_segments(boxes, confidences, {}, 10, Settings())
    selected = select_segment(segments)
    assert selected is not None
    assert selected.method == "best_continuous_track_segment"
    assert selected.segment_start_frame == 100 and selected.segment_end_frame == 139


def test_no_valid_segments_returns_none_and_ball_proximity_is_optional() -> None:
    boxes, confidences = _inputs(list(range(10)))
    balls = {
        frame: BallTrackPoint(frame, frame / 10, None, None, False, None) for frame in range(10)
    }
    segments = build_segments(boxes, confidences, balls, 10, Settings())
    assert select_segment(segments) is None
