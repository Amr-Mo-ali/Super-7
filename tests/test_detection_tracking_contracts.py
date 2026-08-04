"""Inference-free contracts for adapter and ByteTrack boundary formats."""

import numpy as np
import pytest

from services.player_detector import BoundingBox, Detection
from services.tracker import ByteTrackDetections, ByteTrackTracker


def _detection() -> Detection:
    return Detection(None, "person", 0.8, BoundingBox(1, 2, 5, 8), 4, 0.4)


def test_bytetrack_payload_has_stable_float32_results_like_shape() -> None:
    payload = ByteTrackTracker._payload([_detection()])

    assert payload.xyxy.dtype == np.float32
    assert payload.conf.dtype == np.float32
    assert payload.cls.dtype == np.float32
    assert payload.xyxy.shape == (1, 4)
    assert payload.xywh.tolist() == [[3.0, 5.0, 4.0, 6.0]]


def test_bytetrack_payload_rejects_invalid_box_shape_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="shape"):
        ByteTrackDetections(np.zeros((1, 3), dtype=np.float32), np.ones(1), np.zeros(1))
    with pytest.raises(ValueError, match="finite"):
        ByteTrackDetections(
            np.array([[0, 0, np.inf, 1]], dtype=np.float32), np.ones(1), np.zeros(1)
        )


def test_bytetrack_payload_for_empty_detection_sequence_is_valid() -> None:
    payload = ByteTrackTracker._payload([])

    assert payload.xyxy.shape == (0, 4)
    assert len(payload) == 0
