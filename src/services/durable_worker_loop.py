"""Inactive, single-slot discovery loop for the durable analysis coordinator.

The caller supplies an already constructed coordinator and repository, owns the
child executor and database pool, and must settle those resources after ``stop``.
This loop never classifies child failures or cancels submitted process work.
"""

from __future__ import annotations

import asyncio
import logging
from math import isfinite
from typing import Protocol

from domain.analysis_job_attempt import RecoveryBatchSize
from services.analysis_job_coordinator import CoordinatorStopped
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
)
from services.analysis_job_worker import NoJobAvailable, RecoverySummary, WorkerSlotUnavailable


class _OneJobCoordinator(Protocol):
    async def run_one(
        self,
    ) -> (
        AnalysisJobCompleted
        | AnalysisJobCompletionAlreadyApplied
        | NoJobAvailable
        | WorkerSlotUnavailable
    ): ...


class _RecoveryRepository(Protocol):
    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary: ...


class DurableWorkerLoop:
    """Discover one job at a time; recover only when no child call is active."""

    def __init__(
        self,
        *,
        coordinator: _OneJobCoordinator,
        recovery_repository: _RecoveryRepository,
        recovery_batch_size: RecoveryBatchSize,
        idle_wait_seconds: float,
        logger: logging.Logger,
    ) -> None:
        if not isinstance(recovery_batch_size, RecoveryBatchSize):
            raise TypeError("Recovery batch size must be a RecoveryBatchSize.")
        if (
            type(idle_wait_seconds) not in (int, float)
            or not isfinite(idle_wait_seconds)
            or not 0 < idle_wait_seconds <= 60
        ):
            raise ValueError("Idle wait must be finite, positive, and at most 60 seconds.")
        self._coordinator = coordinator
        self._recovery_repository = recovery_repository
        self._batch_size = recovery_batch_size
        self._idle_wait_seconds = float(idle_wait_seconds)
        self._logger = logger
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    @property
    def failure(self) -> BaseException | None:
        """The exact background exception, if the loop stopped unexpectedly."""
        return self._failure

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start one incarnation under the caller's running event loop."""
        if self._task is not None:
            raise RuntimeError("Durable worker loop cannot be started twice.")
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._observe_completion)

    async def stop(self) -> None:
        """Stop new discovery and await in-flight work without business cancellation.

        This may remain unbounded if native child shutdown or persistence hangs;
        the caller owns supervision and must not mistake cancellation for a stop.
        """
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                recovered = await self._recovery_repository.recover_expired(
                    batch_size=self._batch_size
                )
                if not isinstance(recovered, RecoverySummary):
                    raise TypeError("Recovery returned an invalid summary.")
                if self._stop.is_set():
                    return
                outcome = await self._coordinator.run_one()
                if isinstance(outcome, WorkerSlotUnavailable):
                    raise CoordinatorStopped("Worker slot is unavailable after recovery.")
                if isinstance(outcome, NoJobAvailable):
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._idle_wait_seconds)
                    except TimeoutError:
                        pass
                elif not isinstance(
                    outcome, (AnalysisJobCompleted, AnalysisJobCompletionAlreadyApplied)
                ):
                    raise TypeError("Coordinator returned an invalid outcome.")
        except BaseException as error:
            self._failure = error
            raise

    def _observe_completion(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError as cancelled:
            error = cancelled
        if error is not None:
            self._logger.error("durable_worker_loop_stopped error_type=%s", type(error).__name__)
