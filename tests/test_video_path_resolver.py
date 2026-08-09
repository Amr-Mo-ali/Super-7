"""Security tests for backend filename resolution inside shared video storage."""

from os import R_OK
from pathlib import Path

import pytest

from core.exceptions import VideoAccessError, VideoNotFoundError, VideoPathResolutionError
from services.video_path_resolver import VideoPathResolver


def test_resolves_a_valid_video_filename(tmp_path: Path) -> None:
    video = tmp_path / "test-video.mp4"
    video.write_bytes(b"video")
    assert VideoPathResolver(tmp_path).resolve("test-video.mp4") == video.resolve()


def test_rejects_a_missing_video_file(tmp_path: Path) -> None:
    with pytest.raises(VideoNotFoundError):
        VideoPathResolver(tmp_path).resolve("missing.mp4")


@pytest.mark.parametrize("filename", ["video.txt", "video", "video.exe"])
def test_rejects_invalid_extensions(tmp_path: Path, filename: str) -> None:
    with pytest.raises(VideoPathResolutionError, match="extension"):
        VideoPathResolver(tmp_path).resolve(filename)


@pytest.mark.parametrize("filename", ["../video.mp4", "..\\video.mp4", "nested/video.mp4"])
def test_rejects_path_traversal_and_path_separators(tmp_path: Path, filename: str) -> None:
    with pytest.raises(VideoPathResolutionError, match="safe relative"):
        VideoPathResolver(tmp_path).resolve(filename)


@pytest.mark.parametrize("filename", ["/tmp/video.mp4", "C:\\videos\\video.mp4"])
def test_rejects_absolute_paths(tmp_path: Path, filename: str) -> None:
    with pytest.raises(VideoPathResolutionError, match="safe relative"):
        VideoPathResolver(tmp_path).resolve(filename)


def test_rejects_a_symlink_that_escapes_storage_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"video")
    link = tmp_path / "escape.mp4"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(VideoPathResolutionError, match="escapes"):
        VideoPathResolver(tmp_path).resolve("escape.mp4")


def test_rejects_an_inaccessible_video_file(tmp_path: Path) -> None:
    video = tmp_path / "private.mp4"
    video.write_bytes(b"video")

    def inaccessible(path: Path, mode: int) -> bool:
        return mode != R_OK or path == tmp_path

    with pytest.raises(VideoAccessError, match="readable"):
        VideoPathResolver(tmp_path, inaccessible).resolve("private.mp4")
