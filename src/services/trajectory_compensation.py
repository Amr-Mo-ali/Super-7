"""Shared raw/compensated coordinate representation."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from services.camera_motion import CameraMotionResult


@dataclass(frozen=True, slots=True)
class CompensatedObservation:
    frame_index: int
    raw_center_x: float
    raw_center_y: float
    compensated_center_x: float | None
    compensated_center_y: float | None
    compensation_available: bool


class TrajectoryCompensatorProtocol(Protocol):
    def compensate(
        self, frame_index: int, point: tuple[float, float]
    ) -> CompensatedObservation: ...


class AffineTrajectoryCompensator:
    def __init__(self, motion: CameraMotionResult) -> None:
        self._motion = motion

    def compensate(self, frame_index: int, point: tuple[float, float]) -> CompensatedObservation:
        transform = self._motion.cumulative_transforms.get(frame_index)
        if transform is None:
            return CompensatedObservation(frame_index, *point, None, None, False)
        try:
            inverse = np.linalg.inv(transform)
            result = inverse @ np.array([point[0], point[1], 1.0])
            if not np.isfinite(result).all():
                raise ValueError("non-finite compensation")
            return CompensatedObservation(
                frame_index, *point, float(result[0]), float(result[1]), True
            )
        except (np.linalg.LinAlgError, ValueError):
            return CompensatedObservation(frame_index, *point, None, None, False)


class NoOpTrajectoryCompensator:
    def compensate(self, frame_index: int, point: tuple[float, float]) -> CompensatedObservation:
        return CompensatedObservation(frame_index, *point, None, None, False)
