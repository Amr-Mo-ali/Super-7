"""Framework-neutral ball detection boundary."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from services.player_detector import BoundingBox


@dataclass(frozen=True, slots=True)
class BallDetection:
    frame_index: int
    timestamp_seconds: float
    confidence: float
    bounding_box: BoundingBox
    center_point: tuple[float, float]


class BallDetector(Protocol):
    def detect(
        self, frame: np.ndarray, frame_index: int, timestamp_seconds: float
    ) -> Sequence[BallDetection]: ...
