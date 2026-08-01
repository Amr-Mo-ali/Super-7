"""Unit tests for OpenCV-backed video validation."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from core.config import Settings
from core.exceptions import InvalidVideoError
from services.video_validator import VideoValidator


def test_validator_extracts_metadata_from_decodable_video(tmp_path: Path) -> None:
    """A supported AVI produces validated metadata."""
    video_path = _create_video(tmp_path / "sample.avi")

    metadata = VideoValidator(Settings()).validate(video_path)

    assert metadata.width == 64
    assert metadata.height == 64
    assert metadata.frame_count == 3
    assert metadata.fps == 10.0


def test_validator_rejects_non_video_content_with_video_suffix(tmp_path: Path) -> None:
    """Extension checks cannot bypass decoder validation."""
    invalid_path = tmp_path / "invalid.avi"
    invalid_path.write_bytes(b"not a video")

    with pytest.raises(InvalidVideoError, match="decodable"):
        VideoValidator(Settings()).validate(invalid_path)


def _create_video(path: Path) -> Path:
    """Create a tiny decodable AVI fixture without external media assets."""
    codec = cv2.VideoWriter_fourcc(*"MJPG")  # type: ignore[attr-defined]
    writer = cv2.VideoWriter(str(path), codec, 10.0, (64, 64))
    if not writer.isOpened():
        raise RuntimeError("Test environment cannot create an AVI fixture.")
    try:
        for intensity in (0, 100, 255):
            writer.write(np.full((64, 64, 3), intensity, dtype=np.uint8))
    finally:
        writer.release()
    return path
