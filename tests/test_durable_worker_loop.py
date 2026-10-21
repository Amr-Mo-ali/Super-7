"""Database-free controls for the inactive single-slot discovery loop."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from uuid import UUID

import pytest

from domain.analysis_job_attempt import (
    AttemptAuthority,
    LeaseId,
    RecoveryBatchSize,
    WorkerSlotId,
)
from services.analysis_job_finalization import AnalysisJobCompleted
from services.analysis_job_worker import NoJobAvailable, RecoverySummary
from services.durable_worker_loop import DurableWorkerLoop


def _completed() -> AnalysisJobCompleted:
    return AnalysisJobCompleted(
        AttemptAuthority(UUID(int=1), 1, WorkerSlotId(UUID(int=2)), LeaseId(UUID(int=3))),
        datetime.now(UTC),
        UUID(int=4),
    )


class _Coordinator:
    def __init__(self, *outcomes: AnalysisJobCompleted | NoJobAvailable) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.active = False
        self.first_entered = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.completed = asyncio.Event()
        self.release_first: asyncio.Event | None = None
        self.error: BaseException | None = None

    async def run_one(self) -> AnalysisJobCompleted | NoJobAvailable:
        assert not self.active, "Only one coordinator call may be active."
        self.active = True
        self.calls += 1
        if self.calls == 1:
            self.first_entered.set()
        if self.calls == 2:
            self.second_entered.set()
        try:
            if self.calls == 1 and self.release_first is not None:
                await self.release_first.wait()
            if self.error is not None:
                raise self.error
            outcome = self.outcomes.pop(0) if self.outcomes else NoJobAvailable()
            if isinstance(outcome, AnalysisJobCompleted):
                self.completed.set()
            return outcome
        finally:
            self.active = False


class _Recovery:
    def __init__(self, coordinator: _Coordinator) -> None:
        self.coordinator = coordinator
        self.calls = 0
        self.first_done = asyncio.Event()

    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary:
        assert batch_size == RecoveryBatchSize(2)
        assert not self.coordinator.active, "Recovery must not overlap child work."
        self.calls += 1
        self.first_done.set()
        return RecoverySummary(0, 0)


def _loop(
    coordinator: _Coordinator, recovery: _Recovery, *, idle: float = 0.02
) -> DurableWorkerLoop:
    return DurableWorkerLoop(
        coordinator=coordinator,
        recovery_repository=recovery,
        recovery_batch_size=RecoveryBatchSize(2),
        idle_wait_seconds=idle,
        logger=logging.getLogger("football_analysis.tests.durable_worker_loop"),
    )


def test_idle_wait_discovers_queued_work_without_manual_coordinator_call() -> None:
    async def scenario() -> None:
        coordinator = _Coordinator(NoJobAvailable(), _completed())
        recovery = _Recovery(coordinator)
        worker = _loop(coordinator, recovery)
        worker.start()
        try:
            await asyncio.wait_for(coordinator.first_entered.wait(), 1)
            assert coordinator.calls == 1
            await asyncio.wait_for(coordinator.completed.wait(), 1)
            assert coordinator.calls >= 2
            assert recovery.calls >= 2
            assert worker.failure is None
        finally:
            await asyncio.wait_for(worker.stop(), 1)
        assert not worker.running

    asyncio.run(scenario())


def test_recovery_never_overlaps_child_and_graceful_stop_waits_for_it() -> None:
    async def scenario() -> None:
        coordinator = _Coordinator(_completed())
        coordinator.release_first = asyncio.Event()
        recovery = _Recovery(coordinator)
        worker = _loop(coordinator, recovery)
        worker.start()
        stop_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(coordinator.first_entered.wait(), 1)
            assert recovery.calls == 1
            assert coordinator.active
            stop_task = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            assert not stop_task.done()
            assert coordinator.calls == 1
            coordinator.release_first.set()
            await asyncio.wait_for(stop_task, 1)
            assert coordinator.calls == 1
            assert recovery.calls == 1
            assert not coordinator.active
            assert worker.failure is None
        finally:
            coordinator.release_first.set()
            if stop_task is None or not stop_task.done():
                await asyncio.wait_for(worker.stop(), 1)

    asyncio.run(scenario())


def test_successive_jobs_are_serial_and_recovery_runs_between_them() -> None:
    async def scenario() -> None:
        coordinator = _Coordinator(_completed(), _completed())
        coordinator.release_first = asyncio.Event()
        recovery = _Recovery(coordinator)
        worker = _loop(coordinator, recovery)
        worker.start()
        try:
            await asyncio.wait_for(coordinator.first_entered.wait(), 1)
            assert coordinator.calls == 1 and recovery.calls == 1
            coordinator.release_first.set()
            await asyncio.wait_for(coordinator.second_entered.wait(), 1)
            assert recovery.calls >= 2
            assert coordinator.calls >= 2
            assert worker.failure is None
        finally:
            coordinator.release_first.set()
            await asyncio.wait_for(worker.stop(), 1)

    asyncio.run(scenario())


def test_background_failure_is_visible_and_preserves_exception_identity() -> None:
    async def scenario() -> None:
        coordinator = _Coordinator()
        failure = RuntimeError("private-test-detail")
        coordinator.error = failure
        worker = _loop(coordinator, _Recovery(coordinator))
        worker.start()
        await asyncio.wait_for(coordinator.first_entered.wait(), 1)
        with pytest.raises(RuntimeError) as captured:
            await asyncio.wait_for(worker.stop(), 1)
        assert captured.value is failure
        assert worker.failure is failure
        assert not worker.running

    output = io.StringIO()
    logger = logging.getLogger("football_analysis.tests.durable_worker_loop")
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    try:
        asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
    assert "durable_worker_loop_stopped error_type=RuntimeError" in output.getvalue()
    assert "private-test-detail" not in output.getvalue()


def test_invalid_idle_wait_and_second_start_are_rejected() -> None:
    coordinator = _Coordinator()
    recovery = _Recovery(coordinator)
    for invalid in (0, -1, float("nan"), float("inf"), 61):
        with pytest.raises(ValueError):
            _loop(coordinator, recovery, idle=invalid)

    async def scenario() -> None:
        worker = _loop(coordinator, recovery)
        worker.start()
        try:
            with pytest.raises(RuntimeError, match="cannot be started twice"):
                worker.start()
        finally:
            await asyncio.wait_for(worker.stop(), 1)

    asyncio.run(scenario())
