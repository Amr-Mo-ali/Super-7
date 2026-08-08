"""Request-local ownership tests for the player-tracker boundary."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from services.ball_detector import BallDetection
from services.ball_tracker import BallTrackPoint, NearestNeighborBallTracker
from services.player_detector import BoundingBox, Detection
from services.player_tracker import DetectionOnlyPlayerTracker
from services.tracker import Track
from services.video_validator import VideoMetadata


class FakeCapture:
    def __init__(self, frames: Sequence[np.ndarray]) -> None:
        self._frames = iter(frames)
        self.released = False

    def read(self) -> tuple[bool, np.ndarray | None]:
        try:
            return True, next(self._frames)
        except StopIteration:
            return False, None

    def release(self) -> None:
        self.released = True


class FakeDetector:
    def detect(
        self, frame: np.ndarray, frame_index: int = 0, timestamp: float = 0.0
    ) -> Sequence[Detection]:
        del frame
        return (
            Detection(
                None,
                "person",
                0.9,
                BoundingBox(1, 2, 5, 8),
                frame_index,
                timestamp,
            ),
        )

    def detect_batch(self, frames: Sequence[np.ndarray]) -> Sequence[Sequence[Detection]]:
        return tuple(self.detect(frame, index, float(index)) for index, frame in enumerate(frames))


class FakeTracker:
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.received_frames: list[int] = []
        self._seen: set[int] = set()
        self.tracks_created = 0
        self.lost_tracks = 0
        self.track_switches = 0

    def update(self, detections: Sequence[Detection]) -> Sequence[Track]:
        if self.cancel:
            raise AnalysisCancelled("cancelled fake request")
        self.received_frames.extend(item.frame_index for item in detections)
        self._seen.update(item.frame_index for item in detections)
        self.tracks_created = len(self._seen)
        self.lost_tracks = len(self.received_frames)
        self.track_switches = len(self._seen) + len(self.received_frames)
        return tuple(
            Track(1, item.frame_index, item.confidence, (1.0, 2.0, 5.0, 8.0)) for item in detections
        )


class FakeBallDetector:
    def detect(
        self, frame: np.ndarray, frame_index: int, timestamp_seconds: float
    ) -> Sequence[BallDetection]:
        del frame
        box = BoundingBox(2, 2, 4, 4)
        return (BallDetection(frame_index, timestamp_seconds, 0.9, box, (3.0, 3.0)),)


class CountingBallTracker(NearestNeighborBallTracker):
    instances: list["CountingBallTracker"] = []

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.update_frames: list[int] = []
        self.instances.append(self)

    def update(
        self, frame_index: int, timestamp_seconds: float, detections: Sequence[BallDetection]
    ) -> BallTrackPoint:
        self.update_frames.append(frame_index)
        return super().update(frame_index, timestamp_seconds, detections)


def _metadata(frames: int = 2) -> VideoMetadata:
    return VideoMetadata("avi", 1, frames / 10, 64, 64, 10, frames)


def _install_captures(
    monkeypatch: pytest.MonkeyPatch, batches: Sequence[Sequence[np.ndarray]]
) -> list[FakeCapture]:
    captures: list[FakeCapture] = []
    pending = iter(batches)

    def capture_factory(_: str) -> FakeCapture:
        capture = FakeCapture(next(pending))
        captures.append(capture)
        return capture

    monkeypatch.setattr("services.player_tracker.cv2.VideoCapture", capture_factory)
    return captures


def test_tracker_factory_is_request_local_for_sequential_analyses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = (np.zeros((8, 8, 3), dtype=np.uint8),) * 2
    captures = _install_captures(monkeypatch, (frames, frames))
    created: list[FakeTracker] = []

    def tracker_factory() -> FakeTracker:
        tracker = FakeTracker()
        created.append(tracker)
        return tracker

    subject = DetectionOnlyPlayerTracker(FakeDetector(), tracker_factory, Settings())

    first = subject.analyze(Path("first.avi"), _metadata())
    second = subject.analyze(Path("second.avi"), _metadata())

    assert subject.model_version == "yolo11n.pt+yolo11n.pt+bytetrack"
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].received_frames == [0, 1]
    assert created[1].received_frames == [0, 1]
    assert created[0]._seen == {0, 1}
    assert created[1]._seen == {0, 1}
    assert first.diagnostics.tracks_created == 2
    assert second.diagnostics.tracks_created == 2
    assert created[1].lost_tracks == 2
    assert created[1].track_switches == 4
    assert all(capture.released for capture in captures)


def test_failed_or_cancelled_analysis_does_not_reuse_its_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    _install_captures(monkeypatch, ((frame,), (frame,)))
    created: list[FakeTracker] = []

    def tracker_factory() -> FakeTracker:
        tracker = FakeTracker(cancel=not created)
        created.append(tracker)
        return tracker

    subject = DetectionOnlyPlayerTracker(FakeDetector(), tracker_factory, Settings())

    with pytest.raises(AnalysisCancelled, match="cancelled fake request"):
        subject.analyze(Path("cancelled.avi"), _metadata(1))
    completed = subject.analyze(Path("next.avi"), _metadata(1))

    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].received_frames == []
    assert created[1].received_frames == [0]
    assert completed.diagnostics.tracks_created == 1


def test_request_local_ball_tracker_behavior_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    _install_captures(monkeypatch, ((frame,), (frame,)))
    CountingBallTracker.instances = []

    subject = DetectionOnlyPlayerTracker(
        FakeDetector(),
        FakeTracker,
        Settings(),
        FakeBallDetector(),
        CountingBallTracker,
    )

    first = subject.analyze(Path("first.avi"), _metadata(1))
    second = subject.analyze(Path("second.avi"), _metadata(1))

    assert len(CountingBallTracker.instances) == 2
    assert CountingBallTracker.instances[0] is not CountingBallTracker.instances[1]
    assert [item.update_frames for item in CountingBallTracker.instances] == [[0], [0]]
    assert first.diagnostics.accepted_ball_track_observations == 1
    assert second.diagnostics.accepted_ball_track_observations == 1
