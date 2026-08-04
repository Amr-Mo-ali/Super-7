"""Deterministic pass-candidate detection from existing player and ball tracks."""

from collections import Counter
from dataclasses import dataclass
from math import hypot
from time import perf_counter

from core.config import Settings
from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox

PASS_DETECTION_VERSION = "pass_detection_v0.1"


@dataclass(frozen=True, slots=True)
class PassCandidate:
    pass_id: str
    possessor_track_id: int
    receiver_track_id: int
    start_frame: int
    release_frame: int
    end_frame: int
    duration_seconds: float
    distance: float
    confidence: float
    release_speed: float
    trajectory_points: tuple[tuple[float, float], ...]
    trajectory_duration: float
    trajectory_length: float
    trajectory_direction: tuple[float, float]
    trajectory_quality: float
    status: str = "pass_candidate"


@dataclass(frozen=True, slots=True)
class PassDetectionResult:
    candidates: tuple[PassCandidate, ...]
    raw_pass_candidates: int
    accepted_pass_candidates: int
    rejected_pass_candidates: int
    rejection_breakdown: dict[str, int]
    processing_time_ms: int
    version: str = PASS_DETECTION_VERSION


class PassDetector:
    """Uses only supplied observations; it never invokes a detection model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(
        self,
        player_boxes: dict[int, dict[int, BoundingBox]],
        player_confidences: dict[int, dict[int, float]],
        ball_points: dict[int, BallTrackPoint],
        fps: float,
    ) -> PassDetectionResult:
        started = perf_counter()
        rejected: Counter[str] = Counter()
        candidates: list[PassCandidate] = []
        if fps <= 0:
            return self._result(candidates, 0, Counter({"invalid_fps": 1}), started)
        possessions = self._possessions(player_boxes, ball_points)
        raw = len(possessions)
        for possessor, start, end, continuity in possessions:
            release = self._release(possessor, end, player_boxes, ball_points, fps)
            if release is None:
                rejected["missing_release"] += 1
                continue
            release_frame, release_speed, release_quality = release
            trajectory = self._trajectory(release_frame, ball_points, fps)
            if trajectory is None:
                rejected["invalid_trajectory"] += 1
                continue
            points, final_frame, length, direction, trajectory_quality = trajectory
            receiver = self._receiver(
                possessor, final_frame, direction, player_boxes, player_confidences, ball_points
            )
            if receiver is None:
                rejected["missing_receiver"] += 1
                continue
            receiver_id, receiver_quality, visibility = receiver
            confidence = self._clamp(
                0.25 * release_quality
                + 0.25 * trajectory_quality
                + 0.25 * receiver_quality
                + 0.15 * continuity
                + 0.10 * visibility
            )
            candidates.append(
                PassCandidate(
                    f"pass-{len(candidates) + 1}",
                    possessor,
                    receiver_id,
                    start,
                    release_frame,
                    final_frame,
                    (final_frame - start) / fps,
                    length,
                    confidence,
                    release_speed,
                    tuple(points),
                    (final_frame - release_frame) / fps,
                    length,
                    direction,
                    trajectory_quality,
                )
            )
        return self._result(candidates, raw, rejected, started)

    def _possessions(
        self, players: dict[int, dict[int, BoundingBox]], balls: dict[int, BallTrackPoint]
    ) -> list[tuple[int, int, int, float]]:
        observations: list[tuple[int, int]] = []
        for frame, ball in sorted(balls.items()):
            if not ball.visible or ball.center_point is None:
                continue
            nearby = [
                (self._normalized_distance(ball.center_point, box), track_id)
                for track_id, boxes in players.items()
                if (box := boxes.get(frame)) is not None
            ]
            if nearby and min(nearby)[0] <= self._settings.pass_possession_proximity_ratio:
                observations.append((frame, min(nearby)[1]))
        result: list[tuple[int, int, int, float]] = []
        for frame, track_id in observations:
            if (
                result
                and track_id == result[-1][0]
                and frame - result[-1][2] <= self._settings.pass_max_gap_frames
            ):
                old = result[-1]
                result[-1] = (old[0], old[1], frame, old[3])
            else:
                result.append((track_id, frame, frame, 1.0))
        return [
            item
            for item in result
            if item[2] - item[1] + 1 >= self._settings.pass_min_possession_frames
        ]

    def _release(
        self,
        possessor: int,
        end: int,
        players: dict[int, dict[int, BoundingBox]],
        balls: dict[int, BallTrackPoint],
        fps: float,
    ) -> tuple[int, float, float] | None:
        prior = balls.get(end)
        box = players.get(possessor, {}).get(end)
        if prior is None or prior.center_point is None or box is None:
            return None
        prior_distance = self._normalized_distance(prior.center_point, box)
        for frame in range(end + 1, end + self._settings.pass_release_window_frames + 1):
            point = balls.get(frame)
            if point is None or point.center_point is None:
                continue
            distance = self._normalized_distance(point.center_point, box)
            speed = (
                hypot(
                    point.center_point[0] - prior.center_point[0],
                    point.center_point[1] - prior.center_point[1],
                )
                * fps
            )
            if distance > prior_distance and speed >= self._settings.pass_min_release_speed_pixels:
                return (
                    frame,
                    speed,
                    self._clamp(
                        (distance - prior_distance) / self._settings.pass_possession_proximity_ratio
                    ),
                )
        return None

    def _trajectory(
        self,
        release: int,
        balls: dict[int, BallTrackPoint],
        fps: float,
    ) -> tuple[list[tuple[float, float]], int, float, tuple[float, float], float] | None:
        frames = [
            frame
            for frame in range(release, release + self._settings.pass_max_trajectory_frames + 1)
            if frame in balls and balls[frame].visible and balls[frame].center_point is not None
        ]
        if len(frames) < self._settings.pass_min_trajectory_frames:
            return None
        if any(
            right - left > self._settings.pass_max_gap_frames
            for left, right in zip(frames, frames[1:], strict=False)
        ):
            return None
        points = [balls[frame].center_point for frame in frames]
        assert all(point is not None for point in points)
        typed_points = [point for point in points if point is not None]
        length = sum(
            hypot(b[0] - a[0], b[1] - a[1])
            for a, b in zip(typed_points, typed_points[1:], strict=False)
        )
        if length < self._settings.pass_min_trajectory_length_pixels:
            return None
        dx, dy = typed_points[-1][0] - typed_points[0][0], typed_points[-1][1] - typed_points[0][1]
        norm = hypot(dx, dy)
        if norm == 0:
            return None
        quality = self._clamp(
            (len(frames) / (frames[-1] - frames[0] + 1))
            * min(1.0, length / self._settings.pass_trajectory_quality_length_pixels)
        )
        return typed_points, frames[-1], length, (dx / norm, dy / norm), quality

    def _receiver(
        self,
        possessor: int,
        frame: int,
        direction: tuple[float, float],
        players: dict[int, dict[int, BoundingBox]],
        confidences: dict[int, dict[int, float]],
        balls: dict[int, BallTrackPoint],
    ) -> tuple[int, float, float] | None:
        ball = balls[frame].center_point
        assert ball is not None
        options: list[tuple[float, int, float]] = []
        for track_id, boxes in players.items():
            if track_id == possessor or (box := boxes.get(frame)) is None:
                continue
            distance = self._normalized_distance(ball, box)
            if distance > self._settings.pass_receiver_proximity_ratio:
                continue
            center = ((box.x1 + box.x2) / 2, box.y2)
            vector = (center[0] - ball[0], center[1] - ball[1])
            norm = hypot(*vector)
            consistency = (
                max(0.0, (vector[0] * direction[0] + vector[1] * direction[1]) / norm)
                if norm
                else 0.0
            )
            visibility = len(boxes) / max(len(balls), 1)
            quality = self._clamp(
                (1 - distance / self._settings.pass_receiver_proximity_ratio) * 0.5
                + consistency * 0.3
                + confidences.get(track_id, {}).get(frame, 0.0) * 0.2
            )
            options.append((quality, track_id, visibility))
        if not options:
            return None
        quality, track_id, visibility = max(options)
        return track_id, quality, self._clamp(visibility)

    def _result(
        self, candidates: list[PassCandidate], raw: int, rejected: Counter[str], started: float
    ) -> PassDetectionResult:
        return PassDetectionResult(
            tuple(candidates),
            raw,
            len(candidates),
            sum(rejected.values()),
            dict(rejected),
            round((perf_counter() - started) * 1000),
        )

    @staticmethod
    def _normalized_distance(point: tuple[float, float], box: BoundingBox) -> float:
        return hypot(point[0] - (box.x1 + box.x2) / 2, point[1] - box.y2) / max(
            box.y2 - box.y1, 1.0
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))
