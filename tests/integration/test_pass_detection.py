"""Pass detection uses synthetic observations only; no model inference is permitted."""

import pytest

from core.config import Settings
from services.ball_tracker import BallTrackPoint
from services.pass_detection import PASS_DETECTION_VERSION, PassDetector
from services.player_detector import BoundingBox


def _point(frame: int, x: float) -> BallTrackPoint:
    return BallTrackPoint(frame, frame / 10, (x, 100.0), 0.9, True, 1)


def _players(receiver: bool = True) -> dict[int, dict[int, BoundingBox]]:
    players = {1: {frame: BoundingBox(-20, 0, 20, 100) for frame in range(6)}}
    if receiver:
        players[2] = {frame: BoundingBox(280, 0, 320, 100) for frame in range(6)}
    return players


def test_short_pass_is_confirmed_without_inference() -> None:
    result = PassDetector(Settings()).analyze(
        _players(),
        {track: {frame: 0.9 for frame in range(6)} for track in (1, 2)},
        {
            0: _point(0, 0),
            1: _point(1, 5),
            2: _point(2, 10),
            3: _point(3, 150),
            4: _point(4, 225),
            5: _point(5, 300),
        },
        10,
    )
    assert result.version == PASS_DETECTION_VERSION
    assert result.accepted_pass_candidates == 1
    assert result.candidates[0].possessor_track_id == 1
    assert result.candidates[0].receiver_track_id == 2


@pytest.mark.parametrize("travel", [300.0, 600.0], ids=["short_pass", "long_pass"])
def test_pass_distance_is_deterministic(travel: float) -> None:
    players = _players()
    players[2] = {frame: BoundingBox(travel - 20, 0, travel + 20, 100) for frame in range(6)}
    result = PassDetector(Settings()).analyze(
        players,
        {track: {frame: 0.9 for frame in range(6)} for track in players},
        {
            0: _point(0, 0),
            1: _point(1, 5),
            2: _point(2, 10),
            3: _point(3, travel / 2),
            4: _point(4, travel * 3 / 4),
            5: _point(5, travel),
        },
        10,
    )
    assert result.candidates[0].distance == travel / 2


def test_missing_receiver_is_explicit_rejection() -> None:
    result = PassDetector(Settings()).analyze(
        _players(False),
        {1: {frame: 0.9 for frame in range(6)}},
        {
            0: _point(0, 0),
            1: _point(1, 5),
            2: _point(2, 10),
            3: _point(3, 150),
            4: _point(4, 225),
            5: _point(5, 300),
        },
        10,
    )
    assert result.accepted_pass_candidates == 0
    assert result.rejection_breakdown["missing_receiver"] == 1


def test_fragmented_trajectory_is_rejected() -> None:
    result = PassDetector(Settings()).analyze(
        _players(),
        {track: {frame: 0.9 for frame in range(7)} for track in (1, 2)},
        {0: _point(0, 0), 1: _point(1, 5), 2: _point(2, 10), 3: _point(3, 100), 6: _point(6, 300)},
        10,
    )
    assert result.accepted_pass_candidates == 0
    assert result.rejection_breakdown["invalid_trajectory"] == 1


def test_missing_ball_release_is_rejected() -> None:
    result = PassDetector(Settings()).analyze(
        _players(),
        {track: {frame: 0.9 for frame in range(4)} for track in (1, 2)},
        {frame: _point(frame, 5) for frame in range(4)},
        10,
    )
    assert result.rejection_breakdown["missing_release"] == 1
