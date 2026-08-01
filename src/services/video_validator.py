"""Upload persistence and OpenCV-backed video validation."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Final

import cv2
from fastapi import UploadFile

from core.config import Settings
from core.exceptions import InvalidVideoError, UploadTooLargeError

_CHUNK_SIZE: Final = 1024 * 1024
_SUPPORTED_SUFFIXES: Final = frozenset({".avi", ".mkv", ".mov", ".mp4"})


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


@asynccontextmanager
async def temporary_upload(upload: UploadFile, settings: Settings) -> AsyncIterator[Path]:
    """Persist an upload with a size limit and always remove its temporary file."""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise InvalidVideoError("Unsupported video format. Use MP4, MOV, AVI, or MKV.")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
            temporary_path = Path(temporary_file.name)
            size = 0
            while chunk := await upload.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise UploadTooLargeError("Uploaded video exceeds the configured size limit.")
                temporary_file.write(chunk)
        yield temporary_path
    finally:
        await upload.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
            raise InvalidVideoError("Uploaded video is empty or unavailable.")
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
