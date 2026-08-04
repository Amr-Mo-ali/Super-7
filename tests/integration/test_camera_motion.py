"""Synthetic camera-motion tests; no YOLO or tracker inference is used."""

import cv2
import numpy as np

from services.camera_motion import CameraMotionEstimator
from services.trajectory_compensation import AffineTrajectoryCompensator


def _featured_frame() -> np.ndarray:
    image = np.zeros((160, 200), dtype=np.uint8)
    for x in range(20, 190, 30):
        for y in range(20, 150, 30):
            cv2.circle(image, (x, y), 4, 255, -1)
    return image


def _shift(image: np.ndarray, x: float, y: float) -> np.ndarray:
    return cv2.warpAffine(image, np.array([[1, 0, x], [0, 1, y]], dtype=np.float32), (200, 160))


def test_static_camera_is_near_identity() -> None:
    result = CameraMotionEstimator().estimate_frames([_featured_frame(), _featured_frame()])
    assert result.accepted_intervals == 1
    assert abs(result.intervals[0].translation_x) < 1
    assert abs(result.intervals[0].translation_y) < 1


def test_horizontal_pan_is_removed_from_object_motion() -> None:
    source = _featured_frame()
    result = CameraMotionEstimator().estimate_frames([source, _shift(source, 12, 0)])
    compensated = AffineTrajectoryCompensator(result).compensate(1, (62, 50))
    assert compensated.compensation_available
    assert compensated.compensated_center_x is not None
    assert abs(compensated.compensated_center_x - 50) < 2


def test_vertical_pan_is_removed_and_low_feature_frame_rejects_safely() -> None:
    source = _featured_frame()
    result = CameraMotionEstimator().estimate_frames([source, _shift(source, 0, 10)])
    point = AffineTrajectoryCompensator(result).compensate(1, (50, 60))
    assert point.compensation_available
    assert point.compensated_center_y is not None
    assert abs(point.compensated_center_y - 50) < 2
    rejected = CameraMotionEstimator().estimate_frames([np.zeros((100, 100), dtype=np.uint8)] * 2)
    assert rejected.rejected_intervals == 1
    assert not AffineTrajectoryCompensator(rejected).compensate(1, (10, 10)).compensation_available


def test_scene_cut_resets_cumulative_transform() -> None:
    source = _featured_frame()
    cut = np.full_like(source, 255)
    result = CameraMotionEstimator().estimate_frames([source, cut])
    assert result.scene_cut_count == 1
    assert 1 not in result.cumulative_transforms
