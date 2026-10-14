"""Scripted one-job coordinator evidence; no process, model, or database is used."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from test_analysis_job_finalization_contract import _RAW_MODEL, _SAFE_MODEL, _claim, _response

from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    LeaseDuration,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from services.analysis_job_coordinator import (
    AnalysisJobCoordinator,
    CoordinatorOwnershipLost,
    CoordinatorStopped,
    CoordinatorTiming,
    CoordinatorUnclassifiedFailure,
    CoordinatorUnresolved,
)
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    AnalysisJobFinalizationPersistenceUnavailable,
    FinalizableAnalysisResult,
)
from services.analysis_job_worker import (
    AttemptExhausted,
    AttemptFailed,
    AttemptRequeued,
    AttemptResolutionAlreadyApplied,
    ClaimLost,
    LeaseRenewed,
    RecoverySummary,
)
from services.process_analysis_pool import ValidatedChildSuccess
from services.process_contracts import ChildAnalysisRequest, ChildAnalysisSuccess, ParentFailure


class _Worker:
    def __init__(self, claim: WorkerClaim, *, lose_on_renewal: int | None = None) -> None:
        self.claim = claim
        self.renewals = 0
        self.lose_on_renewal = lose_on_renewal
        self.second_renewal = asyncio.Event()
        self.renewal_active = False

    async def claim_next(
        self, *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
    ) -> WorkerClaim:
        assert worker_slot_id == self.claim.authority.worker_slot_id
        assert lease_duration.value > timedelta(0)
        return self.claim

    async def renew_lease(
        self, *, authority: AttemptAuthority, lease_duration: LeaseDuration
    ) -> LeaseRenewed | ClaimLost:
        assert authority == self.claim.authority
        assert lease_duration.value > timedelta(0)
        self.renewal_active = True
        try:
            self.renewals += 1
            if self.renewals == 2:
                self.second_renewal.set()
            if self.renewals == self.lose_on_renewal:
                return ClaimLost(authority)
            return LeaseRenewed(authority, self.claim.heartbeat_at, self.claim.lease_expires_at)
        finally:
            self.renewal_active = False

    async def finalize_retryable_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost:
        raise AssertionError("The success experiment must not classify child failures.")

    async def finalize_terminal_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost:
        raise AssertionError("The success experiment must not classify child failures.")

    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary:
        raise AssertionError("run_one must not schedule recovery.")


class _Child:
    def __init__(self) -> None:
        self.future: asyncio.Future[ValidatedChildSuccess | ParentFailure] = (
            asyncio.get_running_loop().create_future()
        )
        self.submitted = asyncio.Event()
        self.requests: list[ChildAnalysisRequest] = []

    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure:
        self.requests.append(request)
        self.submitted.set()
        return await self.future


class _Finalizer:
    def __init__(self, worker: _Worker, *, ambiguous_first: bool = False) -> None:
        self.worker = worker
        self.ambiguous_first = ambiguous_first
        self.calls: list[tuple[AttemptAuthority, FinalizableAnalysisResult]] = []
        self.first_call_renewals: int | None = None

    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
        assert not self.worker.renewal_active
        if not self.calls:
            self.first_call_renewals = self.worker.renewals
        self.calls.append((authority, result))
        claim = self.worker.claim
        if self.ambiguous_first and len(self.calls) == 1:
            raise AnalysisJobFinalizationPersistenceUnavailable("response was lost")
        if self.ambiguous_first:
            return AnalysisJobCompletionAlreadyApplied(
                authority, claim.heartbeat_at, claim.authority.lease_id.value
            )
        return AnalysisJobCompleted(authority, claim.heartbeat_at, claim.authority.lease_id.value)


def _timing() -> CoordinatorTiming:
    return CoordinatorTiming(LeaseDuration(timedelta(seconds=180)), 0.01, 1.0, 0.01, 2.0, (0, 0))


def _success(claim: WorkerClaim) -> ValidatedChildSuccess:
    response = _response("noncompleted")
    child = ChildAnalysisSuccess(
        str(claim.authority.job_id),
        response.model_dump_json(),
        claim.job.analysis_version,
        _RAW_MODEL,
        17,
    )
    return ValidatedChildSuccess(child, response)


def test_success_renews_during_child_and_settles_before_finalization() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        finalizer = _Finalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        running = asyncio.create_task(coordinator.run_one())
        await asyncio.wait_for(child.submitted.wait(), 1)
        await asyncio.wait_for(worker.second_renewal.wait(), 1)
        assert not running.done()
        child.future.set_result(_success(claim))
        result = await asyncio.wait_for(running, 1)
        assert isinstance(result, AnalysisJobCompleted)
        assert worker.renewals >= 3
        assert len(finalizer.calls) == 1
        assert finalizer.calls[0][0] == claim.authority
        assert child.requests[0].analysis_version == claim.job.analysis_version
        assert not coordinator.outstanding_tasks
        assert not coordinator.stopped

    asyncio.run(scenario())


def test_ambiguous_finalization_replays_identical_frozen_input() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        finalizer = _Finalizer(worker, ambiguous_first=True)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        child.future.set_result(_success(claim))
        result = await coordinator.run_one()
        assert isinstance(result, AnalysisJobCompletionAlreadyApplied)
        assert len(finalizer.calls) == 2
        assert finalizer.calls[0][0] == finalizer.calls[1][0] == claim.authority
        assert finalizer.calls[0][1] is finalizer.calls[1][1]
        assert finalizer.first_call_renewals is not None
        assert worker.renewals == finalizer.first_call_renewals
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_lost_lease_preserves_child_for_owner_and_forbids_reuse() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim, lose_on_renewal=2)
        child = _Child()
        finalizer = _Finalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorOwnershipLost):
            await asyncio.wait_for(coordinator.run_one(), 1)
        assert coordinator.stopped
        assert len(coordinator.outstanding_tasks) == 1
        assert finalizer.calls == []
        with pytest.raises(CoordinatorStopped):
            await coordinator.run_one()
        child.future.set_result(_success(claim))
        await asyncio.wait_for(asyncio.gather(*coordinator.outstanding_tasks), 1)
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_unclassified_child_failure_does_not_finalize() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        finalizer = _Finalizer(worker)
        child.future.set_result(
            ParentFailure("RawExceptionName", "Analysis could not be completed.")
        )
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorUnclassifiedFailure):
            await coordinator.run_one()
        assert finalizer.calls == []
        assert coordinator.stopped
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_unresolved_finalization_keeps_slot_stopped_until_owner_settles_task() -> None:
    class HeldFinalizer(_Finalizer):
        def __init__(self, worker: _Worker) -> None:
            super().__init__(worker)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
            self.started.set()
            await self.release.wait()
            return await super().finalize_success(authority=authority, result=result)

    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        child.future.set_result(_success(claim))
        finalizer = HeldFinalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=CoordinatorTiming(
                LeaseDuration(timedelta(seconds=180)), 0.01, 0.02, 0.01, 0.04, (0, 0)
            ),
        )
        with pytest.raises(CoordinatorUnresolved):
            await asyncio.wait_for(coordinator.run_one(), 1)
        assert finalizer.started.is_set()
        assert len(coordinator.outstanding_tasks) == 1
        assert coordinator.stopped
        with pytest.raises(CoordinatorStopped):
            await coordinator.run_one()
        finalizer.release.set()
        await asyncio.wait_for(asyncio.gather(*coordinator.outstanding_tasks), 1)
        assert not coordinator.outstanding_tasks
        assert len(finalizer.calls) == 1

    asyncio.run(scenario())
