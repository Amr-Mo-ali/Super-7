"""Red contracts for elapsed-frame pass and shot speed evidence (audit F09)."""

from math import isfinite

import pytest

from core.config import Settings
from services.ball_tracker import BallTrackPoint
from services.pass_detection import PassDetector
from services.player_detector import BoundingBox
from services.shot_detection import ShotDetectionConfig, ShotDetector

FPS = 30.0
POSSESSOR_BOX = BoundingBox(-5.0, -10.0, 5.0, 0.0)


def _point(frame: int, x: float, *, fps: float = FPS) -> BallTrackPoint:
    return BallTrackPoint(frame, frame / fps, (x, 0.0), 0.9, True, 1)


def _release_inputs(
    later_frame: int, displacement: float
) -> tuple[dict[int, dict[int, BoundingBox]], dict[int, BallTrackPoint]]:
    return (
        {1: {0: POSSESSOR_BOX}},
        {0: _point(0, 0.0), later_frame: _point(later_frame, displacement)},
    )


def _pass_release(later_frame: int, displacement: float) -> tuple[int, float, float] | None:
    players, balls = _release_inputs(later_frame, displacement)
    return PassDetector(Settings())._release(1, 0, players, balls, FPS)


def _shot_release(
    later_frame: int,
    displacement: float,
    *,
    minimum_speed: float = 100.0,
) -> tuple[int, float, tuple[float, float], float, float] | None:
    players, balls = _release_inputs(later_frame, displacement)
    detector = ShotDetector(ShotDetectionConfig(min_release_speed_pixels=minimum_speed))
    return detector._release(1, 0, players, balls, FPS)


def _box(center_x: float) -> BoundingBox:
    return BoundingBox(center_x - 5.0, -10.0, center_x + 5.0, 0.0)


def test_existing_release_thresholds_and_pixel_units_are_unchanged() -> None:
    assert Settings().pass_min_release_speed_pixels == 20.0
    assert ShotDetectionConfig().min_release_speed_pixels == 100.0


def test_consecutive_pass_release_preserves_displacement_times_fps() -> None:
    release = _pass_release(1, 4.0)

    assert release is not None
    assert release[0] == 1
    assert release[1] == pytest.approx(4.0 * FPS)


def test_consecutive_shot_release_preserves_displacement_times_fps() -> None:
    release = _shot_release(1, 4.0)

    assert release is not None
    assert release[0] == 1
    assert release[1] == pytest.approx(4.0 * FPS)


def test_pass_release_uses_elapsed_frames_for_known_sparse_reproduction() -> None:
    release = _pass_release(6, 4.0)
    expected_speed = 4.0 / (6 / FPS)

    assert release is not None
    assert release[0] == 6
    assert release[1] == pytest.approx(expected_speed)
    assert release[1] != pytest.approx(4.0 * FPS)


def test_shot_release_uses_elapsed_frames_for_known_sparse_reproduction() -> None:
    release = _shot_release(6, 4.0, minimum_speed=20.0)
    expected_speed = 4.0 / (6 / FPS)
    expected_acceleration = expected_speed / (6 / FPS)

    assert release is not None
    assert release[0] == 6
    assert release[1] == pytest.approx(expected_speed)
    assert release[1] != pytest.approx(4.0 * FPS)
    assert release[3] == pytest.approx(expected_acceleration)


def test_dense_and_sparse_pass_release_have_equivalent_speed() -> None:
    dense = _pass_release(1, 4.0)
    sparse = _pass_release(6, 24.0)
    expected_speed = 4.0 * FPS

    assert dense is not None and sparse is not None
    assert dense[1] == pytest.approx(expected_speed)
    assert sparse[1] == pytest.approx(expected_speed)


def test_dense_and_sparse_shot_release_have_equivalent_speed() -> None:
    dense = _shot_release(1, 4.0)
    sparse = _shot_release(6, 24.0)
    expected_speed = 4.0 * FPS

    assert dense is not None and sparse is not None
    assert dense[1] == pytest.approx(expected_speed)
    assert sparse[1] == pytest.approx(expected_speed)


def test_sparse_pass_below_threshold_is_not_admitted() -> None:
    players = {
        1: {frame: _box(0.0) for frame in range(3)},
        2: {10: _box(40.0)},
    }
    balls = {
        0: _point(0, 0.0),
        1: _point(1, 0.0),
        2: _point(2, 0.0),
        8: _point(8, 3.0),
        9: _point(9, 20.0),
        10: _point(10, 40.0),
    }

    result = PassDetector(Settings()).analyze(
        players,
        {1: {frame: 0.9 for frame in range(3)}, 2: {10: 0.9}},
        balls,
        FPS,
    )

    assert result.accepted_pass_candidates == 0
    assert result.rejection_breakdown == {"missing_release": 1}


def test_sparse_shot_below_threshold_is_not_admitted() -> None:
    players = {
        1: {
            0: _box(0.0),
            1: _box(3.0),
            2: _box(10.0),
            8: _box(200.0),
            9: _box(210.0),
            10: _box(220.0),
        }
    }
    balls = {
        0: _point(0, 0.0),
        1: _point(1, 3.0),
        2: _point(2, 10.0),
        8: _point(8, 14.0),
        9: _point(9, 75.0),
        10: _point(10, 130.0),
    }

    result = ShotDetector().analyze(
        players,
        {1: {frame: 0.9 for frame in players[1]}},
        balls,
        FPS,
    )

    assert result.accepted_shot_candidates == 0
    assert result.rejection_breakdown == {"missing_release": 1}


def test_sparse_shot_trajectory_uses_each_positive_frame_gap() -> None:
    detector = ShotDetector(ShotDetectionConfig(min_trajectory_length_pixels=0.0))
    dense = detector._trajectory(
        0,
        {frame: _point(frame, frame * 10.0, fps=10.0) for frame in range(3)},
        10.0,
    )
    sparse = detector._trajectory(
        0,
        {frame: _point(frame, frame * 10.0, fps=10.0) for frame in (0, 2, 4)},
        10.0,
    )

    assert dense is not None and sparse is not None
    assert dense[3:5] == pytest.approx((100.0, 100.0))
    assert sparse[3:5] == pytest.approx((100.0, 100.0))


def test_sparse_release_speed_is_corrected_before_shot_acceleration() -> None:
    players = {1: {0: POSSESSOR_BOX}}
    balls = {
        0: _point(0, 0.0),
        5: _point(5, 3.0),
        6: _point(6, 4.0),
    }

    release = ShotDetector(ShotDetectionConfig(min_release_speed_pixels=20.0))._release(
        1, 0, players, balls, FPS
    )

    assert release is not None
    assert release[0] == 6
    assert release[1] == pytest.approx(20.0)
    assert release[3] == pytest.approx(0.0)
    assert isfinite(release[3])


def test_non_forward_observations_cannot_fabricate_release_evidence() -> None:
    players = {1: {0: POSSESSOR_BOX}}
    balls = {-1: _point(-1, 4.0), 0: _point(0, 0.0)}

    assert PassDetector(Settings())._release(1, 0, players, balls, FPS) is None
    assert ShotDetector()._release(1, 0, players, balls, FPS) is None
