"""Process-local, deterministic admission control for analysis executions."""

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class AdmissionMetrics:
    """Snapshot of process-local analysis capacity usage."""

    max_active_analyses: int
    active_permits: int
    admitted_analyses: int
    rejected_analyses: int


class AdmissionPermit:
    """One admitted execution slot, released at most once."""

    def __init__(self, controller: "AdmissionController") -> None:
        self._controller = controller
        self._released = False

    async def __aenter__(self) -> "AdmissionPermit":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    async def release(self) -> None:
        """Release this permit once; repeat calls are harmless."""
        if self._released:
            return
        self._released = True
        await self._controller._release()


class AdmissionController:
    """Bounds process-local concurrent analysis execution without queuing work."""

    def __init__(self, max_active_analyses: int) -> None:
        if max_active_analyses <= 0:
            raise ValueError("max_active_analyses must be positive.")
        self._max_active_analyses = max_active_analyses
        self._active_permits = 0
        self._admitted_analyses = 0
        self._rejected_analyses = 0
        self._lock = Lock()

    async def admit(self) -> AdmissionPermit | None:
        """Return a permit when capacity is available, otherwise reject immediately."""
        with self._lock:
            if self._active_permits >= self._max_active_analyses:
                self._rejected_analyses += 1
                return None
            self._active_permits += 1
            self._admitted_analyses += 1
            return AdmissionPermit(self)

    async def metrics(self) -> AdmissionMetrics:
        """Return a consistent capacity snapshot."""
        with self._lock:
            return AdmissionMetrics(
                self._max_active_analyses,
                self._active_permits,
                self._admitted_analyses,
                self._rejected_analyses,
            )

    async def _release(self) -> None:
        with self._lock:
            if self._active_permits <= 0:
                raise RuntimeError("Admission permit release would make active permits negative.")
            self._active_permits -= 1
