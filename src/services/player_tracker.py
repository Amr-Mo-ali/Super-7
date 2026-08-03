"""Automatic player/ball tracking boundary and safe default implementation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2

from core.config import Settings
from core.exceptions import RealDetectorNotConfiguredError
from services.ball_detector import BallDetector
from services.ball_tracker import BallTrackPoint, NearestNeighborBallTracker
from services.player_detector import BoundingBox, PlayerDetectorProtocol
from services.selection import PlayerTrack
from services.tracker import ByteTrackTracker
from services.video_validator import VideoMetadata


@dataclass(frozen=True, slots=True)
class TrackingDiagnostics:
    frames_processed: int
    frames_with_player_detections: int
    total_person_detections: int
    tracks_created: int
    ball_detections: int
    raw_ball_detections: int = 0
    filtered_ball_detections: int = 0
    accepted_ball_track_observations: int = 0
    frames_with_multiple_ball_candidates: int = 0
    rejected_ball_candidates: int = 0
    unique_track_ids: int = 0
    rejected_tracks: tuple[dict[str, object], ...] = ()
    rejected_track_reason_breakdown: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class TrackingRun:
    tracks: tuple[PlayerTrack, ...]
    diagnostics: TrackingDiagnostics
    player_boxes: dict[int, dict[int, BoundingBox]] | None = None
    player_confidences: dict[int, dict[int, float]] | None = None
    ball_points: dict[int, BallTrackPoint] | None = None
    ball_detection_confidences: tuple[float, ...] = ()
    ball_track_segments: int = 0
    ball_warning: str | None = None


class AutomaticPlayerTracker(Protocol):
    """Detects players, assigns stable tracks, and calculates ball proximity."""

    model_version: str

    def analyze(self, video_path: Path, metadata: VideoMetadata) -> TrackingRun: ...


class UnconfiguredPlayerTracker:
    """Fails safely rather than silently substituting fake detection output."""

    model_version = "unconfigured"

    def analyze(self, video_path: Path, metadata: VideoMetadata) -> TrackingRun:
        """Require an explicit real runtime detector/tracker adapter."""
        del video_path, metadata
        raise RealDetectorNotConfiguredError("real_player_detector_not_configured")


class DetectionOnlyPlayerTracker:
    """Runs the real detector over decoded frames; tracking is intentionally unavailable."""

    def __init__(
        self,
        detector: PlayerDetectorProtocol,
        tracker: ByteTrackTracker,
        settings: Settings,
        ball_detector: BallDetector | None = None,
        ball_tracker_factory: type[NearestNeighborBallTracker] = NearestNeighborBallTracker,
    ) -> None:
        self._detector = detector
        self._tracker = tracker
        self._ball_detector = ball_detector
        self._settings = settings
        self._ball_tracker_factory = ball_tracker_factory
        self.model_version = f"{settings.model_path}+{settings.ball_model_path}+bytetrack"

    def analyze(self, video_path: Path, metadata: VideoMetadata) -> TrackingRun:
        """Decode every frame and accumulate truthful player-detection diagnostics."""
        capture = cv2.VideoCapture(str(video_path))
        processed = with_people = detections = 0
        observations: dict[int, list[tuple[int, float]]] = {}
        boxes: dict[int, dict[int, BoundingBox]] = {}
        confidences_by_track: dict[int, dict[int, float]] = {}
        ball_points: dict[int, BallTrackPoint] = {}
        ball_confidences: list[float] = []
        filtered_ball_detections = accepted_ball_observations = multiple_ball_frames = 0
        ball_warning: str | None = None
        ball_tracker = (
            self._ball_tracker_factory(self._settings) if self._ball_detector is not None else None
        )
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                processed += 1
                found = self._detector.detect(frame, processed - 1, (processed - 1) / metadata.fps)
                for track in self._tracker.update(found):
                    observations.setdefault(track.track_id, []).append(
                        (track.frame_index, track.confidence)
                    )
                    boxes.setdefault(track.track_id, {})[track.frame_index] = BoundingBox(
                        *track.bounding_box
                    )
                    confidences_by_track.setdefault(track.track_id, {})[track.frame_index] = (
                        track.confidence
                    )
                detections += len(found)
                if found:
                    with_people += 1
                if self._ball_detector is not None and ball_tracker is not None:
                    try:
                        found_balls = self._ball_detector.detect(
                            frame, processed - 1, (processed - 1) / metadata.fps
                        )
                        ball_confidences.extend(item.confidence for item in found_balls)
                        filtered = tuple(
                            item
                            for item in found_balls
                            if item.confidence >= self._settings.ball_minimum_detection_confidence
                        )
                        filtered_ball_detections += len(filtered)
                        if len(filtered) > 1:
                            multiple_ball_frames += 1
                        point = ball_tracker.update(
                            processed - 1, (processed - 1) / metadata.fps, found_balls
                        )
                        ball_points[processed - 1] = point
                        accepted_ball_observations += int(point.visible)
                    except Exception:
                        ball_warning = "Ball detection was unavailable for this video."
                        ball_points = {}
        finally:
            capture.release()
        summaries = tuple(
            self._summary(track_id, values, processed) for track_id, values in observations.items()
        )
        return TrackingRun(
            summaries,
            TrackingDiagnostics(
                processed,
                with_people,
                detections,
                self._tracker.tracks_created,
                len(ball_confidences),
                len(ball_confidences),
                filtered_ball_detections,
                accepted_ball_observations,
                multiple_ball_frames,
                ball_tracker.rejected_candidates if ball_tracker is not None else 0,
                self._tracker.tracks_created,
            ),
            boxes,
            confidences_by_track,
            ball_points,
            tuple(ball_confidences),
            ball_tracker.segments if ball_tracker is not None else 0,
            ball_warning,
        )

    @staticmethod
    def _summary(track_id: int, values: list[tuple[int, float]], processed: int) -> PlayerTrack:
        frames = sorted(frame for frame, _ in values)
        longest = current = 0
        previous: int | None = None
        for frame in frames:
            current = current + 1 if previous is not None and frame == previous + 1 else 1
            longest = max(longest, current)
            previous = frame
        confidence = sum(value for _, value in values) / len(values)
        return PlayerTrack(
            track_id, len(values), processed, longest, len(frames) - longest, confidence, 0, False
        )
