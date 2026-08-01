"""Automatic player/ball tracking boundary and safe default implementation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import cv2

from core.exceptions import RealDetectorNotConfiguredError
from services.selection import PlayerTrack
from services.video_validator import VideoMetadata
from services.player_detector import PlayerDetectorProtocol
from services.tracker import ByteTrackTracker


@dataclass(frozen=True, slots=True)
class TrackingDiagnostics:
    frames_processed: int
    frames_with_player_detections: int
    total_person_detections: int
    tracks_created: int
    ball_detections: int


@dataclass(frozen=True, slots=True)
class TrackingRun:
    tracks: tuple[PlayerTrack, ...]
    diagnostics: TrackingDiagnostics


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
    def __init__(self, detector: PlayerDetectorProtocol, tracker: ByteTrackTracker) -> None:
        self._detector = detector
        self._tracker = tracker
        self.model_version = detector.__class__.__name__

    def analyze(self, video_path: Path, metadata: VideoMetadata) -> TrackingRun:
        """Decode every frame and accumulate truthful player-detection diagnostics."""
        capture = cv2.VideoCapture(str(video_path))
        processed = with_people = detections = 0
        observations: dict[int, list[tuple[int, float]]] = {}
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                processed += 1
                found = self._detector.detect(frame, processed - 1, (processed - 1) / metadata.fps)
                for track in self._tracker.update(found):
                    observations.setdefault(track.track_id, []).append((track.frame_index, track.confidence))
                detections += len(found)
                if found:
                    with_people += 1
        finally:
            capture.release()
        summaries = tuple(self._summary(track_id, values, processed) for track_id, values in observations.items())
        return TrackingRun(summaries, TrackingDiagnostics(processed, with_people, detections, self._tracker.tracks_created, 0))

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
        return PlayerTrack(track_id, len(values), processed, longest, len(frames) - longest, confidence, 0, False)
