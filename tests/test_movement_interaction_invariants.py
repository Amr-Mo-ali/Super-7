"""Property-style invariants for deterministic movement and interaction results."""

import pytest

from core.config import Settings
from services.ball_proximity import NormalizedBallProximityAnalyzer
from services.ball_tracker import BallTrackPoint
from services.movement.analyzer import BottomCenterMovementAnalyzer
from services.player_detector import BoundingBox


@pytest.mark.parametrize("positions", [(0, 0, 0), (0, 5, 10), (0, 10, 5)])
def test_movement_metrics_remain_bounded_for_small_valid_trajectories(
    positions: tuple[int, int, int],
) -> None:
    boxes = {frame: BoundingBox(x, 80, x + 20, 100) for frame, x in enumerate(positions)}
    result = BottomCenterMovementAnalyzer(Settings(movement_smoothing_window=1)).analyze(boxes, 10)

    assert 0 <= result.metrics.movement_intensity <= 1
    assert 0 <= result.metrics.distance_component <= 1
    assert 0 <= result.metrics.speed_component <= 1
    assert 0 <= result.metrics.activity_component <= 1
    assert result.metrics.covered_distance >= 0
    assert result.metrics.maximum_speed >= result.metrics.average_speed >= 0


def test_ball_proximity_is_independent_of_mapping_insertion_order() -> None:
    boxes = {frame: BoundingBox(0, 0, 10, 10) for frame in (0, 1, 2)}
    points = {
        2: BallTrackPoint(2, 0.2, (5, 10), 0.9, True, 1),
        0: BallTrackPoint(0, 0.0, (5, 10), 0.9, True, 1),
        1: BallTrackPoint(1, 0.1, (5, 10), 0.9, True, 1),
    }
    result = NormalizedBallProximityAnalyzer(Settings()).analyze(boxes, points, 10)

    assert result.ball_proximity_frames == 3
    assert result.possible_ball_interaction_count == 1
