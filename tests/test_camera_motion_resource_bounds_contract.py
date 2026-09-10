"""F14-A contract for bounded camera-motion resources and deterministic cleanup."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from services.camera_motion import CameraMotionEstimator, CameraMotionInterval


class _Capture:
    def __init__(
        self,
        frames: tuple[np.ndarray, ...],
        events: list[str] | None = None,
        *,
        read_error_at: int | None = None,
        read_error: Exception | None = None,
        release_error: Exception | None = None,
    ) -> None:
        self._frames = frames
        self._events = events if events is not None else []
        self._read_error_at = read_error_at
        self._read_error = read_error
        self._release_error = release_error
        self.read_calls = 0
        self.release_calls = 0

    def read(self) -> tuple[bool, np.ndarray | None]:
        call = self.read_calls
        self.read_calls += 1
        self._events.append(f"read:{call}")
        if self._read_error_at == call:
            if self._read_error is not None:
                raise self._read_error
            raise RuntimeError("deterministic read failure")
        if call >= len(self._frames):
            return False, None
        return True, self._frames[call]

    def release(self) -> None:
        self.release_calls += 1
        if self._release_error is not None:
            raise self._release_error


class _RecordingEstimator(CameraMotionEstimator):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def _estimate_interval(
        self,
        source: np.ndarray,
        target: np.ndarray,
        source_frame: int,
        target_frame: int,
    ) -> tuple[CameraMotionInterval, np.ndarray | None]:
        del source, target
        self._events.append(f"interval:{source_frame}:{target_frame}")
        return (
            CameraMotionInterval(
                source_frame,
                target_frame,
                0.0,
                0.0,
                1.0,
                0.0,
                0,
                0.0,
                0.0,
                False,
                "synthetic",
            ),
            None,
        )


def _frame() -> np.ndarray:
    return np.zeros((2, 2, 3), dtype=np.uint8)


def test_camera_motion_estimates_first_pair_before_decoding_third_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    capture = _Capture((_frame(), _frame(), _frame()), events)
    monkeypatch.setattr("services.camera_motion.cv2.VideoCapture", lambda _: capture)
    monkeypatch.setattr("services.camera_motion.cv2.cvtColor", lambda frame, _: frame)

    _RecordingEstimator(events).estimate(tmp_path / "source.mp4")

    assert events.index("interval:0:1") < events.index("read:2")
    assert capture.release_calls == 1


def test_camera_motion_stops_decoding_at_selected_end_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _Capture((_frame(), _frame(), _frame(), _frame(), _frame()))
    monkeypatch.setattr("services.camera_motion.cv2.VideoCapture", lambda _: capture)
    monkeypatch.setattr("services.camera_motion.cv2.cvtColor", lambda frame, _: frame)

    _RecordingEstimator([]).estimate(tmp_path / "source.mp4", start_frame=0, end_frame=2)

    assert capture.read_calls == 3
    assert capture.release_calls == 1


def test_camera_motion_releases_capture_when_decode_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _Capture((), read_error_at=0)
    monkeypatch.setattr("services.camera_motion.cv2.VideoCapture", lambda _: capture)

    with pytest.raises(RuntimeError, match="deterministic read failure"):
        CameraMotionEstimator().estimate(tmp_path / "source.mp4")

    assert capture.release_calls == 1


def test_camera_motion_preserves_primary_error_when_release_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_error = RuntimeError("distinctive primary camera failure")
    cleanup_error = OSError("distinctive camera release failure")
    capture = _Capture(
        (),
        read_error_at=0,
        read_error=primary_error,
        release_error=cleanup_error,
    )
    monkeypatch.setattr("services.camera_motion.cv2.VideoCapture", lambda _: capture)

    with pytest.raises(RuntimeError) as raised:
        CameraMotionEstimator().estimate(tmp_path / "source.mp4")

    assert raised.value is primary_error
    assert capture.release_calls == 1


def test_camera_motion_raises_release_error_after_successful_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cleanup_error = OSError("distinctive camera release failure after success")
    capture = _Capture((_frame(), _frame()), events, release_error=cleanup_error)
    monkeypatch.setattr("services.camera_motion.cv2.VideoCapture", lambda _: capture)
    monkeypatch.setattr("services.camera_motion.cv2.cvtColor", lambda frame, _: frame)

    with pytest.raises(OSError) as raised:
        _RecordingEstimator(events).estimate(tmp_path / "source.mp4", end_frame=1)

    assert events == ["read:0", "read:1", "interval:0:1"]
    assert raised.value is cleanup_error
    assert capture.release_calls == 1
