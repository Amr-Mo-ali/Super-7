"""Framework-neutral player-detection boundary."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True, slots=True)
class Detection:
    track_id: None
    class_name: str
    confidence: float
    bounding_box: BoundingBox
    frame_index: int
    timestamp: float


class PlayerDetectorProtocol(Protocol):
    """Detects person-class candidates from decoded frames."""

    def detect(
        self, frame: np.ndarray, frame_index: int = 0, timestamp: float = 0.0
    ) -> Sequence[Detection]: ...
    def detect_batch(self, frames: Sequence[np.ndarray]) -> Sequence[Sequence[Detection]]: ...
