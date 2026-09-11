"""Request-owned local artifact lifecycle management."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from threading import Lock

from core.exceptions import InvalidVideoError, UploadTooLargeError

_INPUT_COPY_CHUNK_BYTES = 1024 * 1024
_DEBUG_RENDER_OUTPUT_PATHS = {
    "debug_video": Path("debug_video.mp4"),
    "debug_frames": Path("debug_frames"),
}


class ArtifactError(Exception):
    """Base error for invalid local artifact lifecycle operations."""


class ArtifactPathError(ArtifactError):
    """Raised when an artifact name could escape its request directory."""


class ArtifactQuotaError(ArtifactError):
    """Raised when a request artifact reservation exceeds its byte quota."""


class ArtifactStateError(ArtifactError):
    """Raised when an artifact lifecycle transition is invalid."""


@dataclass(frozen=True, slots=True)
class ArtifactReservation:
    """One request-owned staged artifact reservation."""

    request_id: str
    name: str
    temporary_path: Path
    final_path: Path
    reserved_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Non-throwing cleanup outcome that cannot mask a primary failure."""

    errors: tuple[str, ...]


class ArtifactManager:
    """Creates request-scoped sessions with bounded retention and quota ownership."""

    def __init__(
        self, root: Path, max_session_bytes: int, retained_sessions: int | None = None
    ) -> None:
        if max_session_bytes < 0:
            raise ValueError("max_session_bytes must not be negative.")
        if retained_sessions is not None and retained_sessions < 0:
            raise ValueError("retained_sessions must not be negative.")
        self._root = root.resolve()
        self._max_session_bytes = max_session_bytes
        self._retained_sessions = retained_sessions
        self._lock = Lock()
        self._sessions: dict[str, ArtifactSession] = {}
        self._retained: dict[Path, int] = {}
        self._next_retention_order = 0

    def create_session(self, request_id: str) -> "ArtifactSession":
        """Create one request-owned artifact directory beneath the configured root."""
        if not request_id or not request_id.replace("-", "").replace("_", "").isalnum():
            raise ArtifactPathError(
                "request_id must contain only letters, digits, hyphens, or underscores."
            )
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            if request_id in self._sessions:
                raise ArtifactStateError("An artifact session already exists for this request.")
            directory = self._safe_directory(request_id)
            if directory.exists():
                raise ArtifactStateError("An artifact directory already exists for this request.")
            directory.mkdir(parents=False, exist_ok=False)
            session = ArtifactSession(self, request_id, directory, self._max_session_bytes)
            self._sessions[request_id] = session
            return session

    def _complete_session(self, session: "ArtifactSession", retained: bool) -> CleanupResult:
        with self._lock:
            self._sessions.pop(session.request_id, None)
            if retained:
                self._next_retention_order += 1
                self._retained[session.directory] = self._next_retention_order
                if self._retained_sessions is None:
                    return CleanupResult(())
                stale = sorted(self._retained.items(), key=lambda item: item[1])[
                    : max(0, len(self._retained) - self._retained_sessions)
                ]
                errors = tuple(self._remove_directory(path) for path, _ in stale)
                for path, _ in stale:
                    self._retained.pop(path, None)
                return CleanupResult(tuple(error for error in errors if error is not None))
        error = self._remove_directory(session.directory)
        return CleanupResult((error,) if error else ())

    def _safe_directory(self, request_id: str) -> Path:
        directory = (self._root / request_id).resolve()
        if directory.parent != self._root:
            raise ArtifactPathError("request_id escapes the artifact root.")
        return directory

    @staticmethod
    def _remove_directory(path: Path) -> str | None:
        try:
            rmtree(path)
        except FileNotFoundError:
            return None
        except OSError as error:
            return str(error)
        return None


class ArtifactSession:
    """One request's staged artifacts and deterministic cleanup state."""

    def __init__(
        self,
        manager: ArtifactManager,
        request_id: str,
        directory: Path,
        max_session_bytes: int,
    ) -> None:
        self._manager = manager
        self.request_id = request_id
        self.directory = directory
        self._max_session_bytes = max_session_bytes
        self._reserved_bytes = 0
        self._reservations: dict[str, ArtifactReservation] = {}
        self._created: set[str] = set()
        self._finalized: set[str] = set()
        self._published_files: set[Path] = set()
        self._retained = False
        self._cleaned = False
        self._input_snapshot: Path | None = None
        self._lock = Lock()

    def materialize_input(self, source_path: Path, max_upload_bytes: int) -> Path:
        """Copy one bounded private input snapshot without consuming artifact quota."""
        if max_upload_bytes < 0:
            raise ValueError("max_upload_bytes must not be negative.")
        with self._lock:
            self._validate_active()
            if self._input_snapshot is not None:
                raise ArtifactStateError("A private input snapshot already exists.")
            if not source_path.is_file():
                raise InvalidVideoError("Downloaded video is empty or unavailable.")
            suffix = source_path.suffix.lower()
            partial_path = self.directory / f"private_input{suffix}.partial"
            snapshot_path = self.directory / f"private_input{suffix}"
            try:
                with source_path.open("rb") as source:
                    with partial_path.open("xb") as destination:
                        copied_bytes = 0
                        while True:
                            read_size = min(
                                _INPUT_COPY_CHUNK_BYTES,
                                max_upload_bytes - copied_bytes + 1,
                            )
                            chunk = source.read(read_size)
                            if not chunk:
                                break
                            copied_bytes += len(chunk)
                            if copied_bytes > max_upload_bytes:
                                raise UploadTooLargeError(
                                    "Video exceeds the configured size limit."
                                )
                            destination.write(chunk)
                        if copied_bytes == 0:
                            raise InvalidVideoError("Downloaded video is empty or unavailable.")
                        destination.flush()
                partial_path.replace(snapshot_path)
            except BaseException:
                partial_path.unlink(missing_ok=True)
                snapshot_path.unlink(missing_ok=True)
                raise
            self._input_snapshot = snapshot_path
            return snapshot_path

    def reserve(self, name: str, reserved_bytes: int) -> ArtifactReservation:
        """Reserve bounded request-local capacity for one basename-only artifact."""
        if reserved_bytes < 0:
            raise ArtifactQuotaError("reserved_bytes must not be negative.")
        with self._lock:
            self._validate_active()
            self._validate_name(name)
            if name in self._reservations:
                raise ArtifactStateError("Artifact name is already reserved for this request.")
            if self._reserved_bytes + reserved_bytes > self._max_session_bytes:
                raise ArtifactQuotaError("Artifact reservation exceeds the request quota.")
            final_path = self.directory / name
            reservation = ArtifactReservation(
                self.request_id,
                name,
                final_path.with_name(f"{name}.partial"),
                final_path,
                reserved_bytes,
            )
            self._reservations[name] = reservation
            self._reserved_bytes += reserved_bytes
            return reservation

    def create(self, reservation: ArtifactReservation) -> Path:
        """Create an empty staged output file for a reservation."""
        with self._lock:
            self._validate_reservation(reservation)
            if reservation.name in self._created:
                raise ArtifactStateError("Artifact output has already been created.")
            reservation.temporary_path.touch(exist_ok=False)
            self._created.add(reservation.name)
            return reservation.temporary_path

    def finalize(self, reservation: ArtifactReservation) -> Path:
        """Atomically publish a staged artifact after quota verification."""
        with self._lock:
            self._validate_reservation(reservation)
            if reservation.name not in self._created:
                raise ArtifactStateError("Artifact output must be created before finalization.")
            if reservation.name in self._finalized:
                raise ArtifactStateError("Artifact output has already been finalized.")
            if not reservation.temporary_path.is_file():
                raise ArtifactStateError("Staged artifact output is unavailable.")
            if reservation.temporary_path.stat().st_size > reservation.reserved_bytes:
                raise ArtifactQuotaError("Artifact output exceeds its reserved quota.")
            reservation.temporary_path.replace(reservation.final_path)
            self._finalized.add(reservation.name)
            return reservation.final_path

    def publish_debug_render(
        self,
        producer: Callable[[Path], dict[str, str]],
        *,
        expected_outputs: frozenset[str],
    ) -> dict[str, str]:
        """Publish one complete debug-render tree after validation and quota preflight."""
        staging = self.directory / "debug_render.partial"
        final = self.directory / "debug_render"
        with self._lock:
            self._validate_active()
            if not expected_outputs or not expected_outputs <= _DEBUG_RENDER_OUTPUT_PATHS.keys():
                raise ArtifactStateError("Debug render outputs are unsupported.")
            if self._path_occupied(staging) or self._path_occupied(final):
                raise ArtifactStateError("Debug render output path is already occupied.")

            published = False
            previous_reserved_bytes = self._reserved_bytes
            previous_published_files = self._published_files.copy()
            try:
                outputs = producer(staging)
                output_paths = self._validate_debug_render_outputs(
                    staging, outputs, expected_outputs
                )
                staged_files = self._regular_files_in_tree(staging)
                staged_bytes = sum(path.stat().st_size for path in staged_files)
                if self._reserved_bytes + staged_bytes > self._max_session_bytes:
                    raise ArtifactQuotaError("Debug render output exceeds the request quota.")
                if self._path_occupied(final):
                    raise ArtifactStateError("Debug render output path is already occupied.")

                final_outputs = {
                    name: str(final / path.relative_to(staging))
                    for name, path in output_paths.items()
                }
                final_files = tuple(final / path.relative_to(staging) for path in staged_files)
                staging.rename(final)
                published = True
                try:
                    self._reserved_bytes += staged_bytes
                    self._published_files.update(final_files)
                except BaseException:
                    self._reserved_bytes = previous_reserved_bytes
                    self._published_files = previous_published_files
                    self._discard_render_tree(final)
                    raise
                return final_outputs
            except BaseException:
                if not published:
                    self._discard_render_tree(staging)
                raise

    def artifacts(self) -> tuple[Path, ...]:
        """Return final artifacts only; staged output is never public as valid output."""
        with self._lock:
            finalized = {self._reservations[name].final_path for name in sorted(self._finalized)}
            return tuple(sorted(finalized | self._published_files, key=str))

    def retain(self) -> None:
        """Request retention of finalized outputs when cleanup closes this session."""
        with self._lock:
            self._validate_active()
            self._retained = True

    def cleanup(self) -> CleanupResult:
        """Close this session once without masking a primary caller failure."""
        with self._lock:
            if self._cleaned:
                return CleanupResult(())
            self._cleaned = True
            retained = self._retained
            input_snapshot = self._input_snapshot
            self._input_snapshot = None
            partial_paths = tuple(
                reservation.temporary_path
                for reservation in self._reservations.values()
                if reservation.name not in self._finalized
            )
        input_errors = (
            ()
            if input_snapshot is None
            else tuple(
                error for error in (self._remove_partial(input_snapshot),) if error is not None
            )
        )
        partial_errors = tuple(
            error for path in partial_paths if (error := self._remove_partial(path)) is not None
        )
        completed = self._manager._complete_session(self, retained)
        return CleanupResult(input_errors + partial_errors + completed.errors)

    def _validate_active(self) -> None:
        if self._cleaned:
            raise ArtifactStateError("Artifact session has already been cleaned up.")

    def _validate_reservation(self, reservation: ArtifactReservation) -> None:
        self._validate_active()
        if (
            reservation.request_id != self.request_id
            or self._reservations.get(reservation.name) is not reservation
        ):
            raise ArtifactStateError("Artifact reservation does not belong to this session.")

    @staticmethod
    def _validate_debug_render_outputs(
        staging: Path,
        outputs: object,
        expected_outputs: frozenset[str],
    ) -> dict[str, Path]:
        if not isinstance(outputs, Mapping) or frozenset(outputs) != expected_outputs:
            raise ArtifactStateError("Debug render output mapping is invalid.")
        validated: dict[str, Path] = {}
        for name in expected_outputs:
            value = outputs[name]
            if not isinstance(value, str):
                raise ArtifactPathError("Debug render output path is invalid.")
            path = Path(value)
            expected_path = staging / _DEBUG_RENDER_OUTPUT_PATHS[name]
            if path != expected_path:
                raise ArtifactPathError("Debug render output escapes its staging directory.")
            if name == "debug_video":
                if path.is_symlink() or not path.is_file():
                    raise ArtifactStateError("Debug video output must be a regular file.")
            elif path.is_symlink() or not path.is_dir():
                raise ArtifactStateError("Debug frames output must be a directory.")
            validated[name] = path
        return validated

    @staticmethod
    def _regular_files_in_tree(root: Path) -> tuple[Path, ...]:
        if root.is_symlink() or not root.is_dir():
            raise ArtifactStateError("Debug render staging output must be a directory.")
        pending = [root]
        files: list[Path] = []
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if path.is_symlink():
                    raise ArtifactStateError("Debug render output must not contain symlinks.")
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    files.append(path)
                else:
                    raise ArtifactStateError("Debug render output must contain regular files only.")
        return tuple(sorted(files, key=str))

    @staticmethod
    def _path_occupied(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @staticmethod
    def _discard_render_tree(path: Path) -> None:
        try:
            rmtree(path)
        except BaseException:
            pass

    @staticmethod
    def _validate_name(name: str) -> None:
        path = Path(name)
        if not name or "/" in name or "\\" in name or path.name != name or name in {".", ".."}:
            raise ArtifactPathError("Artifact name must be a single safe filename.")

    @staticmethod
    def _remove_partial(path: Path) -> str | None:
        try:
            path.unlink()
        except FileNotFoundError:
            return None
        except OSError as error:
            return str(error)
        return None
