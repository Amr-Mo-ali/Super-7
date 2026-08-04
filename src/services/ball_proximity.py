"""Target-player ball proximity calculations, deliberately not possession inference."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.config import Settings
from core.exceptions import BallProximityAnalysisError
from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox


@dataclass(frozen=True, slots=True)
class BallProximityResult:
    ball_visible_frames: int
    ball_proximity_frames: int
    ball_proximity_ratio: float
    ball_proximity_time_seconds: float
    longest_ball_proximity_segment: int
    possible_ball_interaction_count: int


class BallProximityAnalyzer(Protocol):
    def analyze(
        self,
        player_boxes: Mapping[int, BoundingBox],
        ball_points: Mapping[int, BallTrackPoint],
        fps: float,
    ) -> BallProximityResult: ...


class NormalizedBallProximityAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(
        self,
        player_boxes: Mapping[int, BoundingBox],
        ball_points: Mapping[int, BallTrackPoint],
        fps: float,
    ) -> BallProximityResult:
        if fps <= 0:
            raise BallProximityAnalysisError("FPS must be positive for proximity analysis.")
        proximity: list[int] = []
        visible = 0
        for frame, point in sorted(ball_points.items()):
            if not point.visible or point.center_point is None:
                continue
            visible += 1
            box = player_boxes.get(frame)
            if box is None or box.y2 <= box.y1:
                continue
            foot = ((box.x1 + box.x2) / 2, box.y2)
            distance = (
                (foot[0] - point.center_point[0]) ** 2 + (foot[1] - point.center_point[1]) ** 2
            ) ** 0.5
            if distance / (box.y2 - box.y1) <= self._settings.ball_proximity_threshold:
                proximity.append(frame)
        segments = self._segments(proximity)
        count = sum(1 for _ in segments)
        lengths = [len(segment) for segment in self._segments(proximity)]
        return BallProximityResult(
            visible,
            len(proximity),
            len(proximity) / len(player_boxes) if player_boxes else 0.0,
            len(proximity) / fps,
            max(lengths, default=0),
            count,
        )

    def _segments(self, frames: list[int]) -> tuple[tuple[int, ...], ...]:
        if not frames:
            return ()
        grouped: list[list[int]] = [[frames[0]]]
        for frame in frames[1:]:
            if frame - grouped[-1][-1] <= self._settings.ball_interaction_gap_frames + 1:
                grouped[-1].append(frame)
            else:
                grouped.append([frame])
        return tuple(tuple(group) for group in grouped)
