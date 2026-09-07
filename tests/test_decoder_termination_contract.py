"""F19 contract for decoder termination at the tracking boundary."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
import pytest

from core.config import Settings
from core.exceptions import IncompleteVideoDecodeError, InvalidVideoError
from services.player_detector import BoundingBox, Detection
from services.player_tracker import DetectionOnlyPlayerTracker, TrackingRun
from services.tracker import Track
from services.video_validator import VideoMetadata, VideoValidator


class _Capture:
    def __init__(self, frames: Sequence[np.ndarray], *, opened: bool = True) -> None:
        self._frames = iter(frames)
        self._opened = opened
        self.read_calls = 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - mirrors OpenCV's public API
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.read_calls += 1
        try:
            return True, next(self._frames)
        except StopIteration:
            return False, None

    def release(self) -> None:
        self.released = True


class _MetadataCapture:
    def __init__(self, frame_count: int) -> None:
        self._frame_count = frame_count
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - mirrors OpenCV's public API
        return True

    def get(self, property_id: int) -> float:
        values = {
            cv2.CAP_PROP_FRAME_COUNT: float(self._frame_count),
            cv2.CAP_PROP_FPS: 10.0,
            cv2.CAP_PROP_FRAME_WIDTH: 64.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 64.0,
        }
        return values[property_id]

    def read(self) -> tuple[bool, np.ndarray]:
        return True, np.zeros((8, 8, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


class _Detector:
    def __init__(self) -> None:
        self.frame_indices: list[int] = []

    def detect(
        self, frame: np.ndarray, frame_index: int = 0, timestamp: float = 0.0
    ) -> Sequence[Detection]:
        del frame
        self.frame_indices.append(frame_index)
        return (
            Detection(
                None,
                "person",
                0.9,
                BoundingBox(0, 0, 10, 20),
                frame_index,
                timestamp,
            ),
        )

    def detect_batch(self, frames: Sequence[np.ndarray]) -> Sequence[Sequence[Detection]]:
        return tuple(self.detect(frame, index, float(index)) for index, frame in enumerate(frames))


class _Tracker:
    tracks_created = 1
    lost_tracks = 0
    track_switches = 0

    def update(self, detections: Sequence[Detection]) -> Sequence[Track]:
        return tuple(
            Track(
                7,
                detection.frame_index,
                detection.confidence,
                (
                    detection.bounding_box.x1,
                    detection.bounding_box.y1,
                    detection.bounding_box.x2,
                    detection.bounding_box.y2,
                ),
            )
            for detection in detections
        )


def _metadata(expected_frames: int) -> VideoMetadata:
    return VideoMetadata(
        "mp4",
        1,
        expected_frames / 10.0,
        64,
        64,
        10.0,
        expected_frames,
    )


def _subject(
    monkeypatch: pytest.MonkeyPatch, decoded_frames: int, *, opened: bool = True
) -> tuple[DetectionOnlyPlayerTracker, _Capture, _Detector]:
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    capture = _Capture((frame,) * decoded_frames, opened=opened)
    detector = _Detector()
    monkeypatch.setattr("services.player_tracker.cv2.VideoCapture", lambda _: capture)
    return DetectionOnlyPlayerTracker(detector, _Tracker, Settings()), capture, detector


def _assert_does_not_return_partial_run(
    subject: DetectionOnlyPlayerTracker,
    metadata: VideoMetadata,
) -> None:
    with pytest.raises(IncompleteVideoDecodeError, match="validated frame boundary"):
        subject.analyze(Path("synthetic.mp4"), metadata)


def test_complete_decode_processes_every_advertised_frame_without_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=3)

    result = subject.analyze(Path("synthetic.mp4"), _metadata(expected_frames=3))

    assert isinstance(result, TrackingRun)
    assert result.diagnostics.frames_processed == 3
    assert detector.frame_indices == [0, 1, 2]
    assert capture.read_calls == 4
    assert capture.released


def test_early_decoder_termination_does_not_return_partial_tracking_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=2)

    _assert_does_not_return_partial_run(subject, _metadata(expected_frames=5))

    assert detector.frame_indices == [0, 1]
    assert capture.read_calls == 3
    assert capture.released


def test_zero_decoded_frames_after_successful_open_does_not_return_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=0)

    _assert_does_not_return_partial_run(subject, _metadata(expected_frames=5))

    assert detector.frame_indices == []
    assert capture.read_calls == 1
    assert capture.released


def test_exactly_allowed_shortfall_returns_complete_tracking_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=1998)

    result = subject.analyze(Path("synthetic.mp4"), _metadata(expected_frames=2001))

    assert result.diagnostics.frames_processed == 1998
    assert detector.frame_indices == list(range(1998))
    assert capture.released


def test_one_frame_beyond_allowed_shortfall_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=1997)

    _assert_does_not_return_partial_run(subject, _metadata(expected_frames=2001))

    assert detector.frame_indices == list(range(1997))
    assert capture.released


def test_decoding_more_than_advertised_count_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=4)

    result = subject.analyze(Path("synthetic.mp4"), _metadata(expected_frames=3))

    assert result.diagnostics.frames_processed == 4
    assert detector.frame_indices == [0, 1, 2, 3]
    assert capture.released


def test_capture_reopen_failure_raises_typed_analysis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, capture, detector = _subject(monkeypatch, decoded_frames=3, opened=False)

    with pytest.raises(IncompleteVideoDecodeError, match="could not be reopened"):
        subject.analyze(Path("synthetic.mp4"), _metadata(expected_frames=3))

    assert detector.frame_indices == []
    assert capture.read_calls == 0
    assert capture.released


@pytest.mark.parametrize("frame_count", (0, -1))
def test_validator_rejects_nonpositive_advertised_frame_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frame_count: int,
) -> None:
    video_path = tmp_path / "synthetic.mp4"
    video_path.write_bytes(b"local-test-fixture")
    capture = _MetadataCapture(frame_count)
    monkeypatch.setattr("services.video_validator.cv2.VideoCapture", lambda _: capture)

    with pytest.raises(InvalidVideoError, match="no decodable frames or valid FPS"):
        VideoValidator(Settings()).validate(video_path)

    assert capture.released
