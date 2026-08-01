"""Unit tests for the isolated YOLO player detector adapter."""

import logging

import numpy as np
import pytest

from adapters.yolo_player_detector import YOLOPlayerDetector
from core.config import Settings
from core.exceptions import InvalidFrameError


class _Tensor:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def cpu(self) -> "_Tensor":
        return self

    def tolist(self) -> list[object]:
        return self._values


class _Model:
    def predict(self, frame: np.ndarray, **_: object) -> list[object]:
        boxes = type("Boxes", (), {"xyxy": _Tensor([[1, 2, 10, 20]]), "conf": _Tensor([0.9])})()
        return [type("Result", (), {"boxes": boxes})()]


def test_valid_frame_maps_person_detection() -> None:
    detector = YOLOPlayerDetector(Settings(), logging.getLogger("test"), _Model())
    result = detector.detect(np.zeros((8, 8, 3), dtype=np.uint8), 4, 0.2)
    assert result[0].class_name == "person" and result[0].track_id is None
    assert result[0].frame_index == 4 and result[0].bounding_box.x2 == 10.0


def test_empty_or_invalid_frame_raises_explicit_error() -> None:
    detector = YOLOPlayerDetector(Settings(), logging.getLogger("test"), _Model())
    with pytest.raises(InvalidFrameError):
        detector.detect(np.array([]))
    with pytest.raises(InvalidFrameError):
        detector.detect(np.zeros((8, 8), dtype=np.uint8))


def test_confidence_is_taken_from_model_output() -> None:
    detector = YOLOPlayerDetector(
        Settings(model_confidence=0.8), logging.getLogger("test"), _Model()
    )
    assert detector.detect(np.zeros((8, 8, 3), dtype=np.uint8))[0].confidence == 0.9
