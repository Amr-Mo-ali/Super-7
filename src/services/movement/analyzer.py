"""Deterministic O(n) movement metrics derived from accepted player boxes."""

from collections import deque
from collections.abc import Mapping
from math import acos, degrees, hypot
from typing import Protocol

from core.config import Settings
from core.exceptions import MovementAnalysisError, TrajectoryError
from services.movement.schemas import MovementMetrics, MovementPoint, MovementResult
from services.player_detector import BoundingBox


class MovementAnalyzer(Protocol):
    def analyze(self, boxes: Mapping[int, BoundingBox], fps: float) -> MovementResult: ...


class BottomCenterMovementAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(self, boxes: Mapping[int, BoundingBox], fps: float) -> MovementResult:
        if fps <= 0:
            raise MovementAnalysisError("Movement analysis requires positive FPS.")
        raw = self._trajectory(boxes, fps)
        if len(raw) < 2:
            raise TrajectoryError("Movement analysis requires at least two accepted observations.")
        points, rejected = self._reject_jumps(raw)
        if len(points) < 2:
            raise TrajectoryError("All movement intervals were rejected as implausible.")
        smoothed = self._smooth(points)
        distances: list[float] = []
        speeds: list[float] = []
        vectors: list[tuple[float, float]] = []
        stationary: list[float] = []
        segments = 1
        for previous, current in zip(smoothed, smoothed[1:], strict=False):
            delta_time = current.timestamp_seconds - previous.timestamp_seconds
            if delta_time <= 0:
                continue
            distance = hypot(
                current.position[0] - previous.position[0],
                current.position[1] - previous.position[1],
            )
            distances.append(distance)
            speeds.append(distance / delta_time)
            vectors.append(
                (
                    current.position[0] - previous.position[0],
                    current.position[1] - previous.position[1],
                )
            )
            stationary.append(
                delta_time if speeds[-1] < self._settings.movement_stationary_speed else 0.0
            )
            if current.frame_index != previous.frame_index + 1:
                segments += 1
        if not speeds:
            raise MovementAnalysisError("No positive-duration movement intervals were available.")
        accelerations = [
            (current - previous)
            / (smoothed[index + 1].timestamp_seconds - smoothed[index].timestamp_seconds)
            for index, (previous, current) in enumerate(zip(speeds, speeds[1:], strict=False))
            if smoothed[index + 1].timestamp_seconds > smoothed[index].timestamp_seconds
        ]
        angles = [
            self._angle(first, second) for first, second in zip(vectors, vectors[1:], strict=False)
        ]
        valid_angles = [angle for angle in angles if angle is not None]
        changes = [
            angle
            for angle in valid_angles
            if angle > self._settings.movement_direction_change_degrees
        ]
        raw_runs = self._stationary_runs(stationary)
        stationary_runs = [run for run in raw_runs if run >= self._settings.movement_min_stationary_duration_seconds]
        stationary_frames = sum(round(run * fps) for run in stationary_runs)
        intensity, distance_component, speed_component, activity_component, raw_intensity = self._intensity(
            sum(distances), sum(speeds) / len(speeds), sum(stationary_runs),
            smoothed[-1].timestamp_seconds - smoothed[0].timestamp_seconds,
        )
        metrics = MovementMetrics(
            sum(distances),
            sum(speeds) / len(speeds),
            max(speeds),
            sum(accelerations) / len(accelerations) if accelerations else 0.0,
            max((abs(item) for item in accelerations), default=0.0),
            len(changes),
            sum(valid_angles) / len(valid_angles) if valid_angles else 0.0,
            len(stationary_runs),
            sum(stationary_runs),
            max(stationary_runs, default=0.0),
            intensity,
            distance_component,
            speed_component,
            activity_component,
            raw_intensity,
            stationary_frames,
            len(raw_runs),
            len(raw_runs) - len(stationary_runs),
        )
        return MovementResult(metrics, tuple(points), segments, rejected, len(smoothed))

    def _trajectory(self, boxes: Mapping[int, BoundingBox], fps: float) -> list[MovementPoint]:
        result: list[MovementPoint] = []
        for frame, box in sorted(boxes.items()):
            height = box.y2 - box.y1
            if height <= 0:
                continue
            result.append(
                MovementPoint(frame, frame / fps, ((box.x1 + box.x2) / 2, box.y2), height)
            )
        return result

    def _reject_jumps(self, points: list[MovementPoint]) -> tuple[list[MovementPoint], int]:
        accepted = [points[0]]
        rejected = 0
        for point in points[1:]:
            previous = accepted[-1]
            if (
                hypot(
                    point.position[0] - previous.position[0],
                    point.position[1] - previous.position[1],
                )
                / previous.bbox_height
                > self._settings.movement_max_normalized_jump
            ):
                rejected += 1
            else:
                accepted.append(point)
        return accepted, rejected

    def _smooth(self, points: list[MovementPoint]) -> list[MovementPoint]:
        window: deque[MovementPoint] = deque(maxlen=self._settings.movement_smoothing_window)
        result: list[MovementPoint] = []
        for point in points:
            window.append(point)
            result.append(
                MovementPoint(
                    point.frame_index,
                    point.timestamp_seconds,
                    (
                        sum(item.position[0] for item in window) / len(window),
                        sum(item.position[1] for item in window) / len(window),
                    ),
                    point.bbox_height,
                )
            )
        return result

    def _angle(self, first: tuple[float, float], second: tuple[float, float]) -> float | None:
        first_length, second_length = hypot(*first), hypot(*second)
        if min(first_length, second_length) < self._settings.movement_minimum_vector_pixels:
            return None
        cosine = max(
            -1.0,
            min(
                1.0, (first[0] * second[0] + first[1] * second[1]) / (first_length * second_length)
            ),
        )
        return degrees(acos(cosine))

    @staticmethod
    def _stationary_runs(intervals: list[float]) -> list[float]:
        runs: list[float] = []
        current = 0.0
        for interval in intervals:
            if interval:
                current += interval
            elif current:
                runs.append(current)
                current = 0.0
        if current:
            runs.append(current)
        return runs

    def _intensity(self, distance: float, speed: float, stationary_time: float, duration: float) -> tuple[float, float, float, float, float]:
        weights = self._settings.movement_distance_weight + self._settings.movement_speed_weight + self._settings.movement_activity_weight
        if abs(weights - 1.0) > 1e-9:
            raise MovementAnalysisError("Movement intensity weights must sum to one.")
        diagonal = self._settings.movement_frame_diagonal_pixels
        distance_rate = distance / (diagonal * duration) if duration > 0 else 0.0
        speed_rate = speed / diagonal
        distance_component = distance_rate / (distance_rate + self._settings.movement_distance_rate_scale) if distance_rate else 0.0
        speed_component = speed_rate / (speed_rate + self._settings.movement_speed_rate_scale) if speed_rate else 0.0
        activity_component = max(0.0, min(1.0, 1 - stationary_time / duration)) if duration > 0 else 0.0
        raw = self._settings.movement_distance_weight * distance_component + self._settings.movement_speed_weight * speed_component + self._settings.movement_activity_weight * activity_component
        return max(0.0, min(1.0, raw)), distance_component, speed_component, activity_component, raw
