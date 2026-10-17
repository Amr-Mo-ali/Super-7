"""Security tests for backend filename resolution inside shared video storage."""

from os import R_OK
from pathlib import Path

import pytest

from core.exceptions import (
    VideoAccessError,
    VideoNotFoundError,
    VideoPathResolutionError,
    VideoStorageRootError,
)
from services.video_path_resolver import VideoPathResolver


def test_resolves_a_valid_video_filename(tmp_path: Path) -> None:
    video = tmp_path / "test-video.mp4"
    video.write_bytes(b"video")
    assert VideoPathResolver(tmp_path).resolve("test-video.mp4") == video.resolve()


def test_rejects_a_missing_video_file(tmp_path: Path) -> None:
    with pytest.raises(VideoNotFoundError) as missing:
        VideoPathResolver(tmp_path).resolve("missing.mp4")
    assert type(missing.value) is VideoNotFoundError


def test_missing_storage_root_is_not_a_missing_video(tmp_path: Path) -> None:
    with pytest.raises(VideoStorageRootError):
        VideoPathResolver(tmp_path / "missing-root").resolve("video.mp4")


def test_dangling_symlink_is_not_a_confirmed_missing_video(tmp_path: Path) -> None:
    link = tmp_path / "dangling.mp4"
    try:
        link.symlink_to(tmp_path / "missing-target.mp4")
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(VideoPathResolutionError) as failure:
        VideoPathResolver(tmp_path).resolve(link.name)
    assert type(failure.value) is VideoPathResolutionError


def test_missing_target_of_existing_entry_is_not_missing_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "existing.mp4"
    candidate.write_bytes(b"video")
    original_resolve = Path.resolve

    def missing_target(path: Path, strict: bool = False) -> Path:
        if path == candidate:
            raise FileNotFoundError("simulated target disappeared")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", missing_target)
    with pytest.raises(VideoPathResolutionError) as failure:
        VideoPathResolver(tmp_path).resolve(candidate.name)
    assert type(failure.value) is VideoPathResolutionError


def test_file_lookup_permission_error_is_not_missing_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "inaccessible.mp4"
    original_resolve = Path.resolve
    original_lstat = Path.lstat

    def failed_resolve(path: Path, strict: bool = False) -> Path:
        if path == candidate:
            raise FileNotFoundError("simulated ambiguous resolution")
        return original_resolve(path, strict=strict)

    def failed_lstat(path: Path) -> object:
        if path == candidate:
            raise PermissionError("simulated access denial")
        return original_lstat(path)

    monkeypatch.setattr(Path, "resolve", failed_resolve)
    monkeypatch.setattr(Path, "lstat", failed_lstat)
    with pytest.raises(VideoPathResolutionError) as failure:
        VideoPathResolver(tmp_path).resolve(candidate.name)
    assert type(failure.value) is VideoPathResolutionError


def test_root_disappearing_during_file_lookup_is_not_missing_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "missing.mp4"
    original_resolve = Path.resolve
    root_checks = 0

    def failed_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal root_checks
        if path == candidate:
            raise FileNotFoundError("simulated missing file")
        if path == tmp_path:
            root_checks += 1
            if root_checks == 2:
                raise FileNotFoundError("simulated missing root")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", failed_resolve)
    with pytest.raises(VideoStorageRootError):
        VideoPathResolver(tmp_path).resolve(candidate.name)


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
