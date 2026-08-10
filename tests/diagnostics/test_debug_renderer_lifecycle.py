"""Explicit OpenCV-handle lifecycle tests for debug rendering."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from services.debug_renderer import render_debug_video
from services.player_detector import BoundingBox
from services.selection import PlayerTrack, Selection


class FakeCapture:
    def __init__(
        self, frames: Sequence[np.ndarray], *, read_error: Exception | None = None
    ) -> None:
        self._frames = iter(frames)
        self._read_error = read_error
        self.release_calls = 0

    def get(self, _: int) -> float:
        return 10.0

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._read_error is not None:
            raise self._read_error
        try:
            return True, next(self._frames)
        except StopIteration:
            return False, None

    def release(self) -> None:
        self.release_calls += 1


class FakeWriter:
    def __init__(self, *, write_error: Exception | None = None) -> None:
        self._write_error = write_error
        self.frames: list[np.ndarray] = []
        self.release_calls = 0

    def write(self, frame: np.ndarray) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.frames.append(frame)

    def release(self) -> None:
        self.release_calls += 1


def _selection() -> Selection:
    return Selection(PlayerTrack(7, 1, 1, 1, 0, 0.9, 0, False), "test", 1, 1, 0)


def _install_capture(monkeypatch: pytest.MonkeyPatch, capture: FakeCapture) -> None:
    monkeypatch.setattr("services.debug_renderer.cv2.VideoCapture", lambda _: capture)


def test_renderer_releases_capture_and_writer_after_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = FakeCapture((np.zeros((8, 8, 3), dtype=np.uint8),))
    writer = FakeWriter()
    saved: list[str] = []
    _install_capture(monkeypatch, capture)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter_fourcc", lambda *_: 1)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter", lambda *_: writer)
    monkeypatch.setattr("services.debug_renderer.cv2.imwrite", lambda path, _: saved.append(path))

    result = render_debug_video(
        tmp_path / "source.mp4",
        tmp_path,
        _selection(),
        None,
        None,
        None,
        None,
        save_video=True,
        save_frames=True,
    )

    assert result == {
        "debug_video": str(tmp_path / "debug_video.mp4"),
        "debug_frames": str(tmp_path / "debug_frames"),
    }
    assert len(writer.frames) == 1
    assert len(saved) == 1
    assert capture.release_calls == 1
    assert writer.release_calls == 1


def test_renderer_releases_capture_and_writer_after_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = FakeCapture((np.zeros((8, 8, 3), dtype=np.uint8),))
    writer = FakeWriter(write_error=RuntimeError("write failed"))
    _install_capture(monkeypatch, capture)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter_fourcc", lambda *_: 1)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter", lambda *_: writer)

    with pytest.raises(RuntimeError, match="write failed"):
        render_debug_video(
            tmp_path / "source.mp4",
            tmp_path,
            _selection(),
            None,
            None,
            None,
            None,
            save_video=True,
            save_frames=False,
        )

    assert capture.release_calls == 1
    assert writer.release_calls == 1


def test_renderer_releases_capture_after_read_or_writer_creation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_capture = FakeCapture((), read_error=RuntimeError("read failed"))
    _install_capture(monkeypatch, read_capture)

    with pytest.raises(RuntimeError, match="read failed"):
        render_debug_video(
            tmp_path / "source.mp4",
            tmp_path,
            _selection(),
            None,
            None,
            None,
            None,
            save_video=False,
            save_frames=False,
        )

    creation_capture = FakeCapture(())
    _install_capture(monkeypatch, creation_capture)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter_fourcc", lambda *_: 1)

    def fail_writer(*_: object) -> FakeWriter:
        raise RuntimeError("writer creation failed")

    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter", fail_writer)
    with pytest.raises(RuntimeError, match="writer creation failed"):
        render_debug_video(
            tmp_path / "source.mp4",
            tmp_path,
            _selection(),
            None,
            None,
            None,
            None,
            save_video=True,
            save_frames=False,
        )

    assert read_capture.release_calls == 1
    assert creation_capture.release_calls == 1


def test_renderer_preserves_annotation_and_frame_save_errors_while_releasing_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    annotation_capture = FakeCapture((frame,))
    annotation_writer = FakeWriter()
    _install_capture(monkeypatch, annotation_capture)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter_fourcc", lambda *_: 1)
    monkeypatch.setattr("services.debug_renderer.cv2.VideoWriter", lambda *_: annotation_writer)

    def fail_annotation(*_: object) -> None:
        raise RuntimeError("annotation failed")

    monkeypatch.setattr("services.debug_renderer.cv2.rectangle", fail_annotation)
    with pytest.raises(RuntimeError, match="annotation failed"):
        render_debug_video(
            tmp_path / "source.mp4",
            tmp_path,
            _selection(),
            {7: {0: BoundingBox(1, 1, 4, 4)}},
            None,
            None,
            None,
            save_video=True,
            save_frames=False,
        )

    save_capture = FakeCapture((frame,))
    _install_capture(monkeypatch, save_capture)
    monkeypatch.setattr("services.debug_renderer.cv2.rectangle", lambda *_: None)

    def fail_frame_save(*_: object) -> bool:
        raise RuntimeError("frame save failed")

    monkeypatch.setattr("services.debug_renderer.cv2.imwrite", fail_frame_save)
    with pytest.raises(RuntimeError, match="frame save failed"):
        render_debug_video(
            tmp_path / "source.mp4",
            tmp_path,
            _selection(),
            None,
            None,
            None,
            None,
            save_video=False,
            save_frames=True,
        )

    assert annotation_capture.release_calls == 1
    assert annotation_writer.release_calls == 1
    assert save_capture.release_calls == 1
