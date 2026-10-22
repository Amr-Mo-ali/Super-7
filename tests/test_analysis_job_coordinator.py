"""Scripted one-job coordinator evidence; no process, model, or database is used."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from test_analysis_job_finalization_contract import _RAW_MODEL, _SAFE_MODEL, _claim, _response

from adapters.analysis_result_projection import convert_child_success
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    AttemptStatus,
    LeaseDuration,
    LeaseId,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
)
from domain.video_integrity import VideoIntegrity
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
    NoJobAvailable,
    RecoverySummary,
)
from services.durable_worker_loop import DurableWorkerLoop
from services.process_analysis_pool import ValidatedChildSuccess
from services.process_contracts import (
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    ChildFailureKind,
    ParentCancelled,
    ParentFailure,
)


class _Worker:
    def __init__(self, claim: WorkerClaim, *, lose_on_renewal: int | None = None) -> None:
        self.claim = claim
        self.renewals = 0
        self.lose_on_renewal = lose_on_renewal
        self.second_renewal = asyncio.Event()
        self.renewal_active = False
        self.claim_queue: list[WorkerClaim] | None = None
        self.terminal_outcome: (
            AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost | None
        ) = None
        self.failure_calls: list[tuple[AttemptAuthority, AttemptFailureCode]] = []
        self.recovery_enabled = False

    async def claim_next(
        self, *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
    ) -> WorkerClaim | NoJobAvailable:
        if self.claim_queue is not None:
            if not self.claim_queue:
                return NoJobAvailable()
            self.claim = self.claim_queue.pop(0)
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
        if self.terminal_outcome is None:
            raise AssertionError("Unclassified child failures must not be finalized.")
        assert not self.renewal_active
        self.failure_calls.append((authority, failure_code))
        return self.terminal_outcome

    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary:
        if not self.recovery_enabled:
            raise AssertionError("run_one must not schedule recovery.")
        assert batch_size == RecoveryBatchSize(1)
        return RecoverySummary(0, 0)


class _Child:
    def __init__(self) -> None:
        self.future: asyncio.Future[ValidatedChildSuccess | ParentFailure | ParentCancelled] = (
            asyncio.get_running_loop().create_future()
        )
        self.submitted = asyncio.Event()
        self.requests: list[ChildAnalysisRequest] = []

    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled:
        self.requests.append(request)
        self.submitted.set()
        return await self.future


class _Finalizer:
    def __init__(self, worker: _Worker, *, ambiguous_first: bool = False) -> None:
        self.worker = worker
        self.ambiguous_first = ambiguous_first
        self.calls: list[tuple[AttemptAuthority, FinalizableAnalysisResult]] = []
        self.first_call_renewals: int | None = None
        self.completed = asyncio.Event()

    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied:
        assert not self.worker.renewal_active
        if not self.calls:
            self.first_call_renewals = self.worker.renewals
        self.calls.append((authority, result))
        self.completed.set()
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


def _success(claim: WorkerClaim, kind: str = "noncompleted") -> ValidatedChildSuccess:
    response = _response(kind).model_copy(update={"analysis_id": str(claim.authority.job_id)})
    child = ChildAnalysisSuccess(
        str(claim.authority.job_id),
        response.model_dump_json(),
        claim.job.analysis_version,
        _RAW_MODEL,
        17,
    )
    return ValidatedChildSuccess(child, response)


def test_verified_claim_cannot_reach_an_executor_without_integrity_checks() -> None:
    async def scenario() -> None:
        ordinary = _claim()
        claim = replace(
            ordinary,
            job=replace(ordinary.job, video_integrity=VideoIntegrity(5, "0" * 64)),
        )
        worker = _Worker(claim)
        child = _Child()
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=_Finalizer(worker),
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorUnclassifiedFailure):
            await coordinator.run_one()
        assert child.requests == []
        assert worker.renewals == 0
        assert coordinator.stopped

    asyncio.run(scenario())


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
            result_projector=convert_child_success,
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
            result_projector=convert_child_success,
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
            result_projector=convert_child_success,
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
            ParentFailure("VideoNotFoundError", "Analysis could not be completed.")
        )
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorUnclassifiedFailure):
            await coordinator.run_one()
        assert finalizer.calls == []
        assert worker.failure_calls == []
        assert coordinator.stopped
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_missing_video_terminalizes_then_worker_discovers_a_valid_job() -> None:
    class SequencedChild:
        def __init__(self, second: WorkerClaim) -> None:
            self.second = second
            self.requests: list[ChildAnalysisRequest] = []

        async def execute_enveloped(
            self, request: ChildAnalysisRequest
        ) -> ValidatedChildSuccess | ParentFailure:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ParentFailure(
                    "VideoNotFoundError",
                    "Analysis could not be completed.",
                    ChildFailureKind.VIDEO_UNAVAILABLE,
                )
            assert request.analysis_id == str(self.second.authority.job_id)
            return _success(self.second)

    async def scenario() -> None:
        first = _claim()
        second_id = UUID(int=25)
        second = replace(
            first,
            authority=replace(first.authority, job_id=second_id, lease_id=LeaseId(UUID(int=26))),
            job=replace(
                first.job,
                job_id=second_id,
                accepted_at=first.job.accepted_at + timedelta(seconds=1),
            ),
        )
        worker = _Worker(first)
        worker.claim_queue = [first, second]
        worker.terminal_outcome = AttemptFailed(
            first.authority, AttemptFailureCode("video_unavailable")
        )
        worker.recovery_enabled = True
        finalizer = _Finalizer(worker)
        child = SequencedChild(second)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=first.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        loop = DurableWorkerLoop(
            coordinator=coordinator,
            recovery_repository=worker,
            recovery_batch_size=RecoveryBatchSize(1),
            idle_wait_seconds=0.01,
            logger=logging.getLogger("football_analysis.tests.child_terminal"),
        )
        loop.start()
        try:
            await asyncio.wait_for(finalizer.completed.wait(), 1)
        finally:
            await asyncio.wait_for(loop.stop(), 1)
        assert loop.failure is None
        assert worker.failure_calls == [(first.authority, AttemptFailureCode("video_unavailable"))]
        assert len(finalizer.calls) == 1
        assert finalizer.calls[0][0] == second.authority
        assert [request.analysis_id for request in child.requests] == [
            str(first.authority.job_id),
            str(second.authority.job_id),
        ]
        assert not coordinator.stopped
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_exact_terminal_replay_permits_continuation() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        worker.terminal_outcome = AttemptResolutionAlreadyApplied(
            claim.authority, AttemptStatus.TERMINAL_FAILURE, AttemptFailureCode("video_unavailable")
        )
        child = _Child()
        child.future.set_result(
            ParentFailure(
                "VideoNotFoundError",
                "Analysis could not be completed.",
                ChildFailureKind.VIDEO_UNAVAILABLE,
            )
        )
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=_Finalizer(worker),
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        assert await coordinator.run_one() == worker.terminal_outcome
        assert not coordinator.stopped
        assert worker.failure_calls == [(claim.authority, AttemptFailureCode("video_unavailable"))]

    asyncio.run(scenario())


def test_nonterminal_replay_of_missing_video_stops_discovery() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        worker.terminal_outcome = AttemptResolutionAlreadyApplied(
            claim.authority,
            AttemptStatus.RETRYABLE_FAILURE,
            AttemptFailureCode("video_unavailable"),
        )
        child = _Child()
        child.future.set_result(
            ParentFailure(
                "VideoNotFoundError",
                "Analysis could not be completed.",
                ChildFailureKind.VIDEO_UNAVAILABLE,
            )
        )
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=_Finalizer(worker),
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorUnresolved):
            await coordinator.run_one()
        assert coordinator.stopped
        assert worker.failure_calls == [(claim.authority, AttemptFailureCode("video_unavailable"))]

    asyncio.run(scenario())


def test_stale_authority_rejects_missing_video_finalization_and_stops() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        worker.terminal_outcome = ClaimLost(claim.authority)
        child = _Child()
        child.future.set_result(
            ParentFailure(
                "VideoNotFoundError",
                "Analysis could not be completed.",
                ChildFailureKind.VIDEO_UNAVAILABLE,
            )
        )
        finalizer = _Finalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorOwnershipLost):
            await coordinator.run_one()
        assert worker.failure_calls == [(claim.authority, AttemptFailureCode("video_unavailable"))]
        assert finalizer.calls == []
        assert coordinator.stopped
        assert not coordinator.outstanding_tasks
        with pytest.raises(CoordinatorStopped):
            await coordinator.run_one()

    asyncio.run(scenario())


def test_child_cancellation_does_not_finalize_a_failure() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        child.future.set_result(ParentCancelled(str(claim.authority.job_id)))
        finalizer = _Finalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        with pytest.raises(CoordinatorUnclassifiedFailure):
            await coordinator.run_one()
        assert worker.failure_calls == []
        assert finalizer.calls == []
        assert coordinator.stopped
        assert not coordinator.outstanding_tasks

    asyncio.run(scenario())


def test_completed_unavailable_ratings_remain_successful() -> None:
    async def scenario() -> None:
        claim = _claim()
        worker = _Worker(claim)
        child = _Child()
        child.future.set_result(_success(claim, "completed"))
        finalizer = _Finalizer(worker)
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=finalizer,
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=_timing(),
        )
        assert isinstance(await coordinator.run_one(), AnalysisJobCompleted)
        assert worker.failure_calls == []
        frozen = finalizer.calls[0][1]
        assert json.loads(frozen.result_json)["result_availability"] == "UNAVAILABLE"
        assert json.loads(frozen.callback_json)["status"] == "COMPLETED"

    asyncio.run(scenario())


def test_ambiguous_missing_video_finalization_keeps_slot_unusable() -> None:
    class HeldTerminalWorker(_Worker):
        def __init__(self, claim: WorkerClaim) -> None:
            super().__init__(claim)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def finalize_terminal_failure(
            self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
        ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost:
            assert not self.renewal_active
            self.started.set()
            await self.release.wait()
            return AttemptFailed(authority, failure_code)

    async def scenario() -> None:
        claim = _claim()
        worker = HeldTerminalWorker(claim)
        child = _Child()
        child.future.set_result(
            ParentFailure(
                "VideoNotFoundError",
                "Analysis could not be completed.",
                ChildFailureKind.VIDEO_UNAVAILABLE,
            )
        )
        coordinator = AnalysisJobCoordinator(
            worker_repository=worker,
            finalization_repository=_Finalizer(worker),
            child_executor=child,
            result_projector=convert_child_success,
            worker_slot_id=claim.authority.worker_slot_id,
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
            timing=CoordinatorTiming(
                LeaseDuration(timedelta(seconds=180)), 0.01, 0.02, 0.01, 0.04, (0, 0)
            ),
        )
        with pytest.raises(CoordinatorUnresolved):
            await asyncio.wait_for(coordinator.run_one(), 1)
        assert worker.started.is_set()
        assert coordinator.stopped
        assert len(coordinator.outstanding_tasks) == 1
        with pytest.raises(CoordinatorStopped):
            await coordinator.run_one()
        worker.release.set()
        await asyncio.wait_for(asyncio.gather(*coordinator.outstanding_tasks), 1)

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
            result_projector=convert_child_success,
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
