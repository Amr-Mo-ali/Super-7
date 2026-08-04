"""Request-owned local artifact lifecycle management."""

from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from threading import Lock


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

    def __init__(self, root: Path, max_session_bytes: int, retained_sessions: int = 0) -> None:
        if max_session_bytes < 0:
            raise ValueError("max_session_bytes must not be negative.")
        if retained_sessions < 0:
            raise ValueError("retained_sessions must not be negative.")
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
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
        self._retained = False
        self._cleaned = False
        self._lock = Lock()

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

    def artifacts(self) -> tuple[Path, ...]:
        """Return final artifacts only; staged output is never public as valid output."""
        with self._lock:
            return tuple(self._reservations[name].final_path for name in sorted(self._finalized))

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
            partial_paths = tuple(
                reservation.temporary_path
                for reservation in self._reservations.values()
                if reservation.name not in self._finalized
            )
        partial_errors = tuple(
            error for path in partial_paths if (error := self._remove_partial(path)) is not None
        )
        completed = self._manager._complete_session(self, retained)
        return CleanupResult(partial_errors + completed.errors)

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
