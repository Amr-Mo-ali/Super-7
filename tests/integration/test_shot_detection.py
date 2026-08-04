"""Synthetic, inference-free tests for shot detection V0.1."""

import pytest

from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox
from services.shot_detection import SHOT_DETECTION_VERSION, ShotDetector


def _ball(frame: int, x: float) -> BallTrackPoint:
    return BallTrackPoint(frame, frame / 10, (x, 100.0), 0.9, True, 1)


def _players(moving: bool = True, second: bool = False) -> dict[int, dict[int, BoundingBox]]:
    positions = (0, 3, 10, 20, 30, 40) if moving else (0, 0, 0, 0, 0, 0)
    result = {1: {frame: BoundingBox(x - 20, 0, x + 20, 100) for frame, x in enumerate(positions)}}
    if second:
        result[2] = {frame: BoundingBox(450, 0, 490, 100) for frame in range(6)}
    return result


def _shot_points(travel: float = 450.0) -> dict[int, BallTrackPoint]:
    return {
        0: _ball(0, 0),
        1: _ball(1, 5),
        2: _ball(2, 10),
        3: _ball(3, travel / 3),
        4: _ball(4, travel * 2 / 3),
        5: _ball(5, travel),
    }


@pytest.mark.parametrize("travel", [450.0, 900.0], ids=["short_shot", "long_shot"])
def test_shot_is_confirmed_without_inference(travel: float) -> None:
    players = _players(second=True)
    result = ShotDetector().analyze(
        players,
        {track: {frame: 0.9 for frame in range(6)} for track in players},
        _shot_points(travel),
        10,
    )
    assert result.version == SHOT_DETECTION_VERSION
    assert result.accepted_shot_candidates == 1
    assert result.candidates[0].distance == travel * 2 / 3


def test_power_shot_exposes_high_release_speed() -> None:
    result = ShotDetector().analyze(
        _players(), {1: {frame: 0.9 for frame in range(6)}}, _shot_points(1200), 10
    )
    assert result.candidates[0].release_speed >= 1000


def test_failed_shot_without_preparation_is_rejected() -> None:
    result = ShotDetector().analyze(
        _players(moving=False), {1: {frame: 0.9 for frame in range(6)}}, _shot_points(), 10
    )
    assert result.rejection_breakdown["missing_preparation"] == 1


def test_fragmented_trajectory_is_rejected() -> None:
    result = ShotDetector().analyze(
        _players(),
        {1: {frame: 0.9 for frame in range(7)}},
        {0: _ball(0, 0), 1: _ball(1, 5), 2: _ball(2, 10), 3: _ball(3, 150), 6: _ball(6, 450)},
        10,
    )
    assert result.rejection_breakdown["invalid_trajectory"] == 1


def test_missing_release_is_rejected() -> None:
    result = ShotDetector().analyze(
        _players(),
        {1: {frame: 0.9 for frame in range(6)}},
        {frame: _ball(frame, 5) for frame in range(6)},
        10,
    )
    assert result.rejection_breakdown["missing_release"] == 1
