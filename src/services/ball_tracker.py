"""Conservative nearest-neighbour tracker for short-lived ball observations."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from core.config import Settings
from core.exceptions import BallTrackerError
from services.ball_detector import BallDetection


@dataclass(frozen=True, slots=True)
class BallTrackPoint:
    frame_index: int
    timestamp_seconds: float
    center_point: tuple[float, float] | None
    confidence: float | None
    visible: bool
    segment_id: int | None


class NearestNeighborBallTracker:
    """Tracks only gated detections; missing frames are represented, never invented."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last: BallDetection | None = None
        self._missing = 0
        self._segment = 0
        self._history: deque[tuple[float, float]] = deque(maxlen=settings.ball_smoothing_window)
        self.segments = 0

    def update(
        self, frame_index: int, timestamp_seconds: float, detections: Sequence[BallDetection]
    ) -> BallTrackPoint:
        try:
            candidates = [
                d
                for d in detections
                if d.confidence >= self._settings.ball_minimum_detection_confidence
            ]
            accepted = self._choose(candidates)
            if accepted is None:
                self._missing += 1
                if self._missing > self._settings.ball_max_missing_frames:
                    self._last = None
                    self._history.clear()
                return BallTrackPoint(frame_index, timestamp_seconds, None, None, False, None)
            if self._last is None:
                self._segment += 1
                self.segments += 1
            self._last, self._missing = accepted, 0
            self._history.append(accepted.center_point)
            center = tuple(
                sum(values) / len(self._history) for values in zip(*self._history, strict=True)
            )
            return BallTrackPoint(
                frame_index, timestamp_seconds, center, accepted.confidence, True, self._segment
            )
        except Exception as error:
            raise BallTrackerError(f"Ball tracking failed: {error}") from error

    def _choose(self, candidates: Sequence[BallDetection]) -> BallDetection | None:
        if not candidates:
            return None
        if self._last is None:
            return max(candidates, key=lambda item: item.confidence)
        last = self._last

        def distance(item: BallDetection) -> float:
            dx = item.center_point[0] - last.center_point[0]
            dy = item.center_point[1] - last.center_point[1]
            return float((dx**2 + dy**2) ** 0.5)

        selected = min(candidates, key=distance)
        return (
            selected
            if distance(selected)
            <= min(self._settings.ball_motion_gate, self._settings.ball_max_center_displacement)
            else None
        )
