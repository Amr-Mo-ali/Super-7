"""Movement analysis uses only accepted player observations."""

import pytest

from core.config import Settings
from core.exceptions import TrajectoryError
from services.movement.analyzer import BottomCenterMovementAnalyzer
from services.player_detector import BoundingBox


def _box(x: float, y: float = 100) -> BoundingBox:
    return BoundingBox(x, y - 20, x + 20, y)


def test_constant_motion_calculates_distance_and_speed() -> None:
    result = BottomCenterMovementAnalyzer(Settings(movement_smoothing_window=1)).analyze(
        {0: _box(0), 1: _box(10), 2: _box(20)}, 10
    )
    assert result.metrics.covered_distance == pytest.approx(20)
    assert result.metrics.average_speed == pytest.approx(100)
    assert result.metrics.direction_changes == 0


def test_jump_is_rejected_and_not_counted_as_distance() -> None:
    result = BottomCenterMovementAnalyzer(Settings(movement_smoothing_window=1)).analyze(
        {0: _box(0), 1: _box(10), 2: _box(1000), 3: _box(20)}, 10
    )
    assert result.rejected_position_jumps == 1
    assert result.metrics.covered_distance == pytest.approx(20)


def test_short_trajectory_fails_explicitly() -> None:
    with pytest.raises(TrajectoryError):
        BottomCenterMovementAnalyzer(Settings()).analyze({0: _box(0)}, 10)


def test_ninety_degree_turn_is_counted() -> None:
    result = BottomCenterMovementAnalyzer(Settings(movement_smoothing_window=1)).analyze(
        {0: _box(0, 100), 1: _box(10, 100), 2: _box(10, 110)}, 10
    )
    assert result.metrics.direction_changes == 1
