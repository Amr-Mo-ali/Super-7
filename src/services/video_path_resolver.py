"""Safe resolution of backend video filenames within configured shared storage."""

from collections.abc import Callable
from os import R_OK, X_OK, access
from pathlib import Path, PureWindowsPath
from typing import Final

from core.exceptions import (
    VideoAccessError,
    VideoNotFoundError,
    VideoPathResolutionError,
    VideoStorageRootError,
)

_ALLOWED_SUFFIXES: Final = frozenset({".mp4", ".mov", ".mkv", ".avi"})


class VideoPathResolver:
    """Resolve one backend-provided filename without exposing backend filesystem paths."""

    def __init__(
        self,
        storage_root: str | Path,
        access_check: Callable[[Path, int], bool] = access,
    ) -> None:
        self._storage_root = Path(storage_root)
        self._access_check = access_check

    def resolve(self, filename: str) -> Path:
        """Return an accessible regular video file contained by the configured storage root."""
        root = self._resolve_root()
        self._validate_filename(filename)
        candidate = self._storage_root / filename
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise VideoNotFoundError("Requested video file does not exist.") from error
        except OSError as error:
            raise VideoPathResolutionError("Requested video path could not be resolved.") from error
        if not resolved.is_relative_to(root):
            raise VideoPathResolutionError("Requested video path escapes the storage root.")
        if not resolved.is_file():
            raise VideoPathResolutionError("Requested video path is not a regular file.")
        if not self._access_check(resolved, R_OK):
            raise VideoAccessError("Requested video file is not readable.")
        return resolved

    def _resolve_root(self) -> Path:
        try:
            root = self._storage_root.resolve(strict=True)
        except FileNotFoundError as error:
            raise VideoStorageRootError("Configured video storage root does not exist.") from error
        except OSError as error:
            raise VideoStorageRootError("Configured video storage root cannot be resolved.") from error
        if not root.is_dir():
            raise VideoStorageRootError("Configured video storage root is not a directory.")
        if not self._access_check(root, R_OK | X_OK):
            raise VideoStorageRootError("Configured video storage root is not accessible.")
        return root

    @staticmethod
    def _validate_filename(filename: str) -> None:
        if not filename or filename != filename.strip():
            raise VideoPathResolutionError("Video filename must not be empty or padded.")
        path = Path(filename)
        if (
            path.is_absolute()
            or PureWindowsPath(filename).is_absolute()
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
            or ".." in path.parts
        ):
            raise VideoPathResolutionError("Video filename must be a safe relative filename.")
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise VideoPathResolutionError("Video filename has an unsupported extension.")
