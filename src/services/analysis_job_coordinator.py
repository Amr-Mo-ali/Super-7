"""Inactive one-slot, one-job durable success experiment boundary.

The caller owns the process pool and must observe ``outstanding_tasks`` after a
fail-stop outcome. This module does not poll, send callbacks, or classify child
failures into durable business outcomes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Protocol, cast

from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, WorkerClaim, WorkerSlotId
from schemas.analysis import AnalyzeResponse
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    AnalysisJobFinalizationPersistenceUnavailable,
    AnalysisJobSuccessFinalizationRepository,
    FinalizableAnalysisResult,
    finalize_analysis_job_success,
)
from services.analysis_job_worker import (
    AnalysisJobWorkerRepository,
    ClaimLost,
    LeaseRenewed,
    NoJobAvailable,
    WorkerSlotUnavailable,
    claim_next_analysis_job,
    renew_analysis_job_lease,
)
from services.process_analysis_pool import ValidatedChildSuccess
from services.process_contracts import (
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    ParentCancelled,
    ParentFailure,
)


class _ChildExecutor(Protocol):
    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled: ...


class AnalysisResultV1Projector(Protocol):
    """Freeze the current result and callback snapshots for a validated child."""

    def __call__(
        self,
        *,
        child: ChildAnalysisSuccess,
        response: AnalyzeResponse,
        claim: WorkerClaim,
        model_catalog: Mapping[str, str],
    ) -> FinalizableAnalysisResult: ...


class CoordinatorStopped(RuntimeError):
    """This slot incarnation cannot safely claim another job."""


class CoordinatorOwnershipLost(CoordinatorStopped):
    """Ownership was lost before a terminal write could begin."""


class CoordinatorUnresolved(CoordinatorStopped):
    """A child or persistence operation still needs lifecycle-owner observation."""


class CoordinatorUnclassifiedFailure(CoordinatorStopped):
    """The child did not produce a validated success; no durable code was guessed."""


@dataclass(frozen=True, slots=True)
class CoordinatorTiming:
    lease_duration: LeaseDuration
    heartbeat_seconds: float
    operation_watchdog_seconds: float
    jitter_seconds: float
    finalization_budget_seconds: float
    retry_delays_seconds: tuple[float, float]

    def __post_init__(self) -> None:
        values = (
            self.heartbeat_seconds,
            self.operation_watchdog_seconds,
            self.jitter_seconds,
            self.finalization_budget_seconds,
            *self.retry_delays_seconds,
        )
        if any(
            type(value) not in (int, float) or not isfinite(value) or value < 0 for value in values
        ):
            raise ValueError("Coordinator timing values must be finite and nonnegative.")
        if (
            self.heartbeat_seconds <= 0
            or self.operation_watchdog_seconds <= 0
            or self.finalization_budget_seconds <= 0
            or len(self.retry_delays_seconds) != 2
        ):
            raise ValueError("Coordinator timing bounds must be positive.")
        if (
            self.heartbeat_seconds + self.operation_watchdog_seconds + self.jitter_seconds
            >= self.lease_duration.value.total_seconds()
        ):
            raise ValueError("Coordinator renewal margin must be shorter than the lease.")


class AnalysisJobCoordinator:
    """Execute at most one child at a time; poison this incarnation on uncertainty."""

    def __init__(
        self,
        *,
        worker_repository: AnalysisJobWorkerRepository,
        finalization_repository: AnalysisJobSuccessFinalizationRepository,
        child_executor: _ChildExecutor,
        result_projector: AnalysisResultV1Projector,
        worker_slot_id: WorkerSlotId,
        model_catalog: Mapping[str, str],
        timing: CoordinatorTiming,
    ) -> None:
        self._worker_repository = worker_repository
        self._finalization_repository = finalization_repository
        self._child_executor = child_executor
        self._result_projector = result_projector
        self._worker_slot_id = worker_slot_id
        self._model_catalog = model_catalog
        self._timing = timing
        self._busy = False
        self._stopped = False
        self._tasks: list[asyncio.Task[object]] = []
        self._lease_deadline = 0.0

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def outstanding_tasks(self) -> tuple[asyncio.Task[object], ...]:
        """Tasks the caller must settle before releasing an injected pool."""
        return tuple(task for task in self._tasks if not task.done())

    async def run_one(
        self,
    ) -> (
        AnalysisJobCompleted
        | AnalysisJobCompletionAlreadyApplied
        | NoJobAvailable
        | WorkerSlotUnavailable
    ):
        if self._busy or self._stopped or self.outstanding_tasks:
            raise CoordinatorStopped("Coordinator slot is not reusable.")
        self._busy = True
        stop_heartbeat = asyncio.Event()
        try:
            claim = await self._observe(
                claim_next_analysis_job(
                    repository=self._worker_repository,
                    worker_slot_id=self._worker_slot_id,
                    lease_duration=self._timing.lease_duration,
                )
            )
            if isinstance(claim, NoJobAvailable):
                return claim
            if isinstance(claim, WorkerSlotUnavailable):
                self._stopped = True
                return claim
            await self._renew(claim.authority)
            request = ChildAnalysisRequest(
                str(claim.authority.job_id),
                claim.job.video_id,
                claim.job.player_id,
                claim.job.video_reference,
                analysis_version=claim.job.analysis_version,
            )
            child = self._track(self._child_executor.execute_enveloped(request))
            heartbeat = self._track(self._heartbeat(claim.authority, stop_heartbeat))
            finished, _ = await asyncio.wait(
                {child, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in finished and child not in finished:
                await heartbeat
                raise CoordinatorUnresolved("Heartbeat stopped before child completion.")
            child_outcome = await child
            stop_heartbeat.set()
            await heartbeat  # Settle any renewal before the first finalizer.
            if not isinstance(child_outcome, ValidatedChildSuccess):
                raise CoordinatorUnclassifiedFailure("Child outcome has no approved durable code.")
            frozen = self._result_projector(
                child=child_outcome.child,
                response=child_outcome.response,
                claim=claim,
                model_catalog=self._model_catalog,
            )
            await self._renew(claim.authority)  # Fresh lease after synchronous conversion.
            return await self._finalize(claim, frozen)
        except BaseException:
            self._stopped = True
            raise
        finally:
            stop_heartbeat.set()
            self._busy = False
            if self.outstanding_tasks:
                self._stopped = True

    async def _heartbeat(self, authority: AttemptAuthority, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._timing.heartbeat_seconds)
            except TimeoutError:
                if monotonic() >= self._lease_deadline:
                    raise CoordinatorUnresolved("Last confirmed lease window elapsed.") from None
                await self._renew(authority)

    async def _renew(self, authority: AttemptAuthority) -> None:
        started = monotonic()
        outcome = await self._observe(
            renew_analysis_job_lease(
                repository=self._worker_repository,
                authority=authority,
                lease_duration=self._timing.lease_duration,
            )
        )
        if isinstance(outcome, ClaimLost):
            raise CoordinatorOwnershipLost("Lease renewal rejected the authority.")
        if not isinstance(outcome, LeaseRenewed):
            raise CoordinatorUnresolved("Lease renewal returned an unexpected outcome.")
        deadline = started + self._timing.lease_duration.value.total_seconds()
        if monotonic() >= deadline:
            raise CoordinatorUnresolved("Renewal returned after its conservative lease window.")
        self._lease_deadline = deadline

    async def _finalize(
        self, claim: WorkerClaim, frozen: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
        started = monotonic()
        for number in range(3):
            remaining = self._timing.finalization_budget_seconds - (monotonic() - started)
            if remaining <= 0:
                break
            try:
                outcome = await self._observe(
                    finalize_analysis_job_success(
                        repository=self._finalization_repository,
                        authority=claim.authority,
                        result=frozen,
                    ),
                    timeout_seconds=min(self._timing.operation_watchdog_seconds, remaining),
                )
            except AnalysisJobFinalizationPersistenceUnavailable:
                if number == 2:
                    break
                delay = self._timing.retry_delays_seconds[number]
                if monotonic() + delay >= started + self._timing.finalization_budget_seconds:
                    break
                await asyncio.sleep(delay)
                continue
            if isinstance(outcome, (AnalysisJobCompleted, AnalysisJobCompletionAlreadyApplied)):
                return outcome
            if isinstance(outcome, ClaimLost):
                if number == 0:
                    raise CoordinatorOwnershipLost("Success finalization rejected the authority.")
                raise CoordinatorUnresolved("Ambiguous finalization replay was not confirmed.")
        raise CoordinatorUnresolved("Success finalization remained ambiguous after bounded calls.")

    async def _observe[T](
        self, operation: Coroutine[object, object, T], *, timeout_seconds: float | None = None
    ) -> T:
        task = self._track(operation)
        completed, _ = await asyncio.wait(
            {task}, timeout=timeout_seconds or self._timing.operation_watchdog_seconds
        )
        if not completed:
            raise CoordinatorUnresolved("Persistence call exceeded its observation bound.")
        return await task

    def _track[T](self, operation: Coroutine[object, object, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(operation)
        self._tasks.append(cast(asyncio.Task[object], task))

        def consume(completed: asyncio.Task[T]) -> None:
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass

        task.add_done_callback(consume)
        return task
