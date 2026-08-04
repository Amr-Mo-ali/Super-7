"""Request-scoped cooperative cancellation intent state."""

from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock


class CancellationState(StrEnum):
    """Legal request-scoped cancellation lifecycle states."""

    ACTIVE = "ACTIVE"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    SHUTDOWN_REQUESTED = "SHUTDOWN_REQUESTED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class CancellationSnapshot:
    """Immutable view of one request's cancellation intent."""

    request_id: str
    state: CancellationState
    cancelled: bool


class CancellationManager:
    """Tracks cooperative cancellation intent without interrupting running work."""

    def __init__(self, request_id: str) -> None:
        if not request_id:
            raise ValueError("request_id must not be empty.")
        self._request_id = request_id
        self._state = CancellationState.ACTIVE
        self._event = Event()
        self._lock = Lock()

    def request_cancellation(self) -> None:
        """Record a client-originated cancellation request."""
        self._transition(CancellationState.CANCELLATION_REQUESTED)

    def expire_deadline(self) -> None:
        """Record that the request deadline has expired."""
        self._transition(CancellationState.DEADLINE_EXPIRED)

    def request_shutdown(self) -> None:
        """Record cancellation intent caused by application shutdown."""
        self._transition(CancellationState.SHUTDOWN_REQUESTED)

    def complete(self) -> None:
        """Record normal or cooperative completion of the request lifecycle."""
        with self._lock:
            if self._state is CancellationState.COMPLETED:
                return
            self._state = CancellationState.COMPLETED
            self._event.clear()

    def is_cancelled(self) -> bool:
        """Return whether a cooperative cancellation boundary should stop work."""
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation intent without forcing running work to stop."""
        return self._event.wait(timeout)

    def snapshot(self) -> CancellationSnapshot:
        """Return an immutable, consistent state snapshot."""
        with self._lock:
            return CancellationSnapshot(self._request_id, self._state, self._event.is_set())

    def _transition(self, requested: CancellationState) -> None:
        with self._lock:
            if self._state is requested:
                return
            if self._state is not CancellationState.ACTIVE:
                raise RuntimeError(
                    "Illegal cancellation transition "
                    f"from {self._state.value} to {requested.value}."
                )
            self._state = requested
            self._event.set()
