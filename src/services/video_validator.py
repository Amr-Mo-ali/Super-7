"""OpenCV-backed validation for downloaded video files."""

from dataclasses import dataclass
from pathlib import Path

import cv2

from core.config import Settings
from core.exceptions import InvalidVideoError

_SUPPORTED_SUFFIXES = frozenset({".avi", ".mkv", ".mov", ".mp4"})


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """Validated video metadata, independent of the HTTP response model."""

    format: str
    file_size_bytes: int
    duration_seconds: float
    width: int
    height: int
    fps: float
    frame_count: int


class VideoValidator:
    """Validates video format, decodability, timing, and image properties."""

    def __init__(self, settings: Settings) -> None:
        """Create a validator using injected upload and metadata constraints."""
        self._settings = settings

    def validate(self, path: Path) -> VideoMetadata:
        """Extract and validate metadata from a decodable local video file."""
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise InvalidVideoError("Unsupported video format.")
        if not path.is_file() or path.stat().st_size == 0:
            raise InvalidVideoError("Downloaded video is empty or unavailable.")
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                raise InvalidVideoError("Uploaded file is not a decodable video.")
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            decoded, _ = capture.read()
        finally:
            capture.release()
        if not decoded or frame_count <= 0 or fps <= 0:
            raise InvalidVideoError("Video has no decodable frames or valid FPS.")
        duration_seconds = frame_count / fps
        if duration_seconds > self._settings.max_duration_seconds:
            raise InvalidVideoError("Video duration exceeds the configured limit.")
        if width < self._settings.min_width or height < self._settings.min_height:
            raise InvalidVideoError("Video resolution is below the configured minimum.")
        if fps < self._settings.min_fps:
            raise InvalidVideoError("Video FPS is below the configured minimum.")
        return VideoMetadata(
            format=path.suffix.removeprefix(".").lower(),
            file_size_bytes=path.stat().st_size,
            duration_seconds=round(duration_seconds, 3),
            width=width,
            height=height,
            fps=round(fps, 3),
            frame_count=frame_count,
        )
