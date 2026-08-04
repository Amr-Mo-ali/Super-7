"""Deterministic shot-candidate detection from already reconstructed observations."""

from collections import Counter
from dataclasses import dataclass
from math import hypot
from time import perf_counter

from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox

SHOT_DETECTION_VERSION = "shot_detection_v0.1"


@dataclass(frozen=True, slots=True)
class ShotDetectionConfig:
    possession_proximity_ratio: float = 1.2
    min_possession_frames: int = 3
    max_gap_frames: int = 2
    release_window_frames: int = 6
    min_release_speed_pixels: float = 100.0
    max_trajectory_frames: int = 90
    min_trajectory_frames: int = 3
    min_trajectory_length_pixels: float = 100.0
    min_preparation_displacement_pixels: float = 5.0
    min_follow_through_frames: int = 2


@dataclass(frozen=True, slots=True)
class ShotCandidate:
    shot_id: str
    possessor_track_id: int
    start_frame: int
    preparation_start_frame: int
    preparation_end_frame: int
    release_frame: int
    end_frame: int
    duration_seconds: float
    distance: float
    trajectory_points: tuple[tuple[float, float], ...]
    trajectory_duration: float
    mean_speed: float
    maximum_speed: float
    release_speed: float
    release_direction: tuple[float, float]
    release_acceleration: float
    preparation_confidence: float
    release_confidence: float
    trajectory_quality: float
    follow_through_confidence: float
    confidence: float
    status: str = "shot_candidate"


@dataclass(frozen=True, slots=True)
class ShotDetectionResult:
    candidates: tuple[ShotCandidate, ...]
    raw_shot_candidates: int
    accepted_shot_candidates: int
    rejected_shot_candidates: int
    rejection_breakdown: dict[str, int]
    processing_time_ms: int
    version: str = SHOT_DETECTION_VERSION


class ShotDetector:
    """Consumes supplied tracks only; no detection or scoring model is called."""

    def __init__(self, config: ShotDetectionConfig | None = None) -> None:
        self._config = config or ShotDetectionConfig()

    def analyze(
        self,
        player_boxes: dict[int, dict[int, BoundingBox]],
        player_confidences: dict[int, dict[int, float]],
        ball_points: dict[int, BallTrackPoint],
        fps: float,
    ) -> ShotDetectionResult:
        started = perf_counter()
        rejected: Counter[str] = Counter()
        candidates: list[ShotCandidate] = []
        possessions = self._possessions(player_boxes, ball_points)
        for possessor, start, end, continuity in possessions:
            preparation = self._preparation(possessor, start, end, player_boxes)
            if preparation is None:
                rejected["missing_preparation"] += 1
                continue
            release = self._release(possessor, end, player_boxes, ball_points, fps)
            if release is None:
                rejected["missing_release"] += 1
                continue
            release_frame, release_speed, direction, acceleration, release_quality = release
            trajectory = self._trajectory(release_frame, ball_points, fps)
            if trajectory is None:
                rejected["invalid_trajectory"] += 1
                continue
            points, last, length, mean_speed, max_speed, trajectory_quality = trajectory
            follow = self._follow_through(possessor, release_frame, last, player_boxes)
            confidence = self._clamp(
                0.30 * release_quality
                + 0.25 * trajectory_quality
                + 0.20 * preparation[2]
                + 0.15 * follow
                + 0.10 * continuity
            )
            candidates.append(
                ShotCandidate(
                    f"shot-{len(candidates) + 1}",
                    possessor,
                    start,
                    preparation[0],
                    preparation[1],
                    release_frame,
                    last,
                    (last - start) / fps,
                    length,
                    tuple(points),
                    (last - release_frame) / fps,
                    mean_speed,
                    max_speed,
                    release_speed,
                    direction,
                    acceleration,
                    preparation[2],
                    release_quality,
                    trajectory_quality,
                    follow,
                    confidence,
                )
            )
        return ShotDetectionResult(
            tuple(candidates),
            len(possessions),
            len(candidates),
            sum(rejected.values()),
            dict(rejected),
            round((perf_counter() - started) * 1000),
        )

    def _possessions(
        self, players: dict[int, dict[int, BoundingBox]], balls: dict[int, BallTrackPoint]
    ) -> list[tuple[int, int, int, float]]:
        groups: list[tuple[int, int, int]] = []
        for frame, ball in sorted(balls.items()):
            if not ball.visible or ball.center_point is None:
                continue
            choices = [
                (self._distance(ball.center_point, box), track)
                for track, boxes in players.items()
                if (box := boxes.get(frame))
            ]
            if not choices or min(choices)[0] > self._config.possession_proximity_ratio:
                continue
            _, track = min(choices)
            if (
                groups
                and groups[-1][0] == track
                and frame - groups[-1][2] <= self._config.max_gap_frames
            ):
                groups[-1] = (track, groups[-1][1], frame)
            else:
                groups.append((track, frame, frame))
        return [
            (track, start, end, 1.0)
            for track, start, end in groups
            if end - start + 1 >= self._config.min_possession_frames
        ]

    def _preparation(
        self, track: int, start: int, end: int, players: dict[int, dict[int, BoundingBox]]
    ) -> tuple[int, int, float] | None:
        boxes = players.get(track, {})
        first, last = boxes.get(start), boxes.get(end)
        if first is None or last is None:
            return None
        displacement = hypot((last.x1 + last.x2 - first.x1 - first.x2) / 2, last.y2 - first.y2)
        if displacement < self._config.min_preparation_displacement_pixels:
            return None
        return (
            start,
            end,
            self._clamp(displacement / (self._config.min_preparation_displacement_pixels * 4)),
        )

    def _release(
        self,
        track: int,
        end: int,
        players: dict[int, dict[int, BoundingBox]],
        balls: dict[int, BallTrackPoint],
        fps: float,
    ) -> tuple[int, float, tuple[float, float], float, float] | None:
        before, box = balls.get(end), players.get(track, {}).get(end)
        if before is None or before.center_point is None or box is None:
            return None
        initial_distance = self._distance(before.center_point, box)
        for frame in range(end + 1, end + self._config.release_window_frames + 1):
            point = balls.get(frame)
            if point is None or point.center_point is None:
                continue
            dx, dy = (
                point.center_point[0] - before.center_point[0],
                point.center_point[1] - before.center_point[1],
            )
            speed = hypot(dx, dy) * fps
            if (
                self._distance(point.center_point, box) > initial_distance
                and speed >= self._config.min_release_speed_pixels
            ):
                norm = hypot(dx, dy)
                previous = balls.get(frame - 1)
                previous_speed = (
                    hypot(
                        point.center_point[0] - previous.center_point[0],
                        point.center_point[1] - previous.center_point[1],
                    )
                    * fps
                    if previous and previous.center_point
                    else 0.0
                )
                acceleration = max(0.0, speed - previous_speed) * fps
                return (
                    frame,
                    speed,
                    (dx / norm, dy / norm),
                    acceleration,
                    self._clamp(speed / (self._config.min_release_speed_pixels * 5)),
                )
        return None

    def _trajectory(
        self, release: int, balls: dict[int, BallTrackPoint], fps: float
    ) -> tuple[list[tuple[float, float]], int, float, float, float, float] | None:
        frames = [
            frame
            for frame in range(release, release + self._config.max_trajectory_frames + 1)
            if (point := balls.get(frame)) and point.visible and point.center_point
        ]
        if len(frames) < self._config.min_trajectory_frames or any(
            right - left > self._config.max_gap_frames
            for left, right in zip(frames, frames[1:], strict=False)
        ):
            return None
        points = [balls[frame].center_point for frame in frames]
        assert all(point is not None for point in points)
        typed = [point for point in points if point is not None]
        distances = [
            hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(typed, typed[1:], strict=False)
        ]
        length = sum(distances)
        if length < self._config.min_trajectory_length_pixels:
            return None
        speeds = [distance * fps for distance in distances]
        consistency = hypot(typed[-1][0] - typed[0][0], typed[-1][1] - typed[0][1]) / length
        quality = self._clamp((len(frames) / (frames[-1] - frames[0] + 1)) * consistency)
        return typed, frames[-1], length, sum(speeds) / len(speeds), max(speeds), quality

    def _follow_through(
        self, track: int, release: int, end: int, players: dict[int, dict[int, BoundingBox]]
    ) -> float:
        boxes = players.get(track, {})
        observed = [boxes[frame] for frame in range(release, end + 1) if frame in boxes]
        if len(observed) < self._config.min_follow_through_frames:
            return 0.0
        movement = hypot(observed[-1].x1 - observed[0].x1, observed[-1].y2 - observed[0].y2)
        return self._clamp(0.5 + movement / 50)

    @staticmethod
    def _distance(point: tuple[float, float], box: BoundingBox) -> float:
        return hypot(point[0] - (box.x1 + box.x2) / 2, point[1] - box.y2) / max(
            box.y2 - box.y1, 1.0
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))
