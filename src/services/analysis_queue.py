"""Bounded FIFO queue and single worker for background analysis jobs."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter

from pydantic import HttpUrl


class AnalysisJobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    analysis_id: str
    video_id: str
    player_id: str
    video_reference: str
    callback_url: HttpUrl
    submitted_at: datetime

    @classmethod
    def create(
        cls,
        analysis_id: str,
        video_id: str,
        player_id: str,
        video_reference: str,
        callback_url: HttpUrl,
    ) -> "AnalysisJob":
        return cls(
            analysis_id, video_id, player_id, video_reference, callback_url, datetime.now(UTC)
        )


@dataclass(frozen=True, slots=True)
class AnalysisQueueMetrics:
    queued: int
    capacity: int
    accepting: bool
    worker_running: bool
    active_analysis_count: int
    max_active_analyses: int


@dataclass(slots=True)
class _JobTiming:
    enqueued_at: float
    started_at: float | None = None


class AnalysisQueue:
    """Application-owned, bounded queue holding lightweight analysis references only."""

    def __init__(
        self,
        capacity: int,
        max_active_analyses: int = 1,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if max_active_analyses != 1:
            raise ValueError("The controlled MVP currently supports exactly one active analysis.")
        self._queue: asyncio.Queue[AnalysisJob] = asyncio.Queue(maxsize=capacity)
        self._states: dict[str, AnalysisJobState] = {}
        self._timings: dict[str, _JobTiming] = {}
        self._accepting = True
        self._worker_running = False
        self._active_analysis_count = 0
        self._max_active_analyses = max_active_analyses
        self._clock = clock

    async def submit(self, job: AnalysisJob) -> bool:
        if not self._accepting or self._queue.full():
            return False
        self._queue.put_nowait(job)
        self._states[job.analysis_id] = AnalysisJobState.QUEUED
        self._timings[job.analysis_id] = _JobTiming(self._clock())
        return True

    async def next_job(self) -> AnalysisJob:
        return await self._queue.get()

    async def wait_until_idle(self) -> None:
        """Wait until all claimed queue items have reached one terminal bookkeeping path."""
        await self._queue.join()

    def mark_running(self, job: AnalysisJob) -> float | None:
        if self._states.get(job.analysis_id) is AnalysisJobState.CANCELLED:
            return None
        self._states[job.analysis_id] = AnalysisJobState.RUNNING
        timing = self._timings.get(job.analysis_id)
        if timing is None:
            return 0.0
        timing.started_at = self._clock()
        self._active_analysis_count += 1
        return _milliseconds(timing.started_at - timing.enqueued_at)

    def mark_finished(self, job: AnalysisJob, state: AnalysisJobState) -> float | None:
        was_running = self._states.get(job.analysis_id) is AnalysisJobState.RUNNING
        self._states[job.analysis_id] = state
        self._queue.task_done()
        if was_running:
            self._active_analysis_count -= 1
        timing = self._timings.pop(job.analysis_id, None)
        if timing is None:
            return None
        return _milliseconds(self._clock() - timing.enqueued_at)

    def cancel(self, analysis_id: str) -> bool:
        if self._states.get(analysis_id) is not AnalysisJobState.QUEUED:
            return False
        self._states[analysis_id] = AnalysisJobState.CANCELLED
        return True

    def state_of(self, analysis_id: str) -> AnalysisJobState | None:
        return self._states.get(analysis_id)

    def stop_accepting(self) -> tuple[tuple[AnalysisJob, float | None], ...]:
        self._accepting = False
        cancelled: list[tuple[AnalysisJob, float | None]] = []
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._states[job.analysis_id] = AnalysisJobState.CANCELLED
            self._queue.task_done()
            timing = self._timings.pop(job.analysis_id, None)
            duration = None if timing is None else _milliseconds(self._clock() - timing.enqueued_at)
            cancelled.append((job, duration))
        return tuple(cancelled)

    def set_worker_running(self, running: bool) -> None:
        self._worker_running = running

    def metrics(self) -> AnalysisQueueMetrics:
        return AnalysisQueueMetrics(
            queued=self._queue.qsize(),
            capacity=self._queue.maxsize,
            accepting=self._accepting,
            worker_running=self._worker_running,
            active_analysis_count=self._active_analysis_count,
            max_active_analyses=self._max_active_analyses,
        )


class AnalysisWorker:
    """Exactly one lifespan-owned consumer that serializes model execution."""

    def __init__(
        self,
        queue: AnalysisQueue,
        processor: Callable[[AnalysisJob], Awaitable[AnalysisJobState]],
        logger: logging.Logger,
    ) -> None:
        self._queue = queue
        self._processor = processor
        self._logger = logger
        self._task: asyncio.Task[None] | None = None
        self._shutdown_started = False
        self._shutdown_finished = False
        self._cancelled_queued_job_count = 0

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("analysis worker is already running")
        self._queue.set_worker_running(True)
        self._task = asyncio.create_task(self._run(), name="analysis-worker")

    async def shutdown(self) -> None:
        self.begin_shutdown()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._queue.set_worker_running(False)
        if not self._shutdown_finished:
            self._shutdown_finished = True
            metrics = self._queue.metrics()
            self._logger.info(
                "analysis_shutdown_finished queue_depth=%s queue_capacity=%s "
                "active_analysis_count=%s max_active_analyses=%s accepting=%s "
                "worker_running=%s cancelled_queued_job_count=%s shutdown_outcome=completed",
                metrics.queued,
                metrics.capacity,
                metrics.active_analysis_count,
                metrics.max_active_analyses,
                metrics.accepting,
                metrics.worker_running,
                self._cancelled_queued_job_count,
            )

    def begin_shutdown(self) -> None:
        """Stop admission and record every in-memory job cancelled during shutdown."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        before = self._queue.metrics()
        cancelled = self._queue.stop_accepting()
        self._cancelled_queued_job_count = len(cancelled)
        after = self._queue.metrics()
        self._logger.info(
            "analysis_shutdown_started queue_depth=%s queue_capacity=%s "
            "active_analysis_count=%s max_active_analyses=%s accepting_before=%s "
            "accepting_after=%s cancelled_queued_job_count=%s",
            before.queued,
            before.capacity,
            before.active_analysis_count,
            before.max_active_analyses,
            before.accepting,
            after.accepting,
            self._cancelled_queued_job_count,
        )
        for job, end_to_end_duration_ms in cancelled:
            _log_job(
                self._logger,
                "analysis_job_cancelled",
                job,
                self._queue.metrics(),
                final_state=AnalysisJobState.CANCELLED,
                end_to_end_duration_ms=end_to_end_duration_ms,
                cancellation_reason="shutdown",
            )

    async def _run(self) -> None:
        while True:
            job = await self._queue.next_job()
            queue_wait_ms = self._queue.mark_running(job)
            if queue_wait_ms is None:
                self._queue.mark_finished(job, AnalysisJobState.CANCELLED)
                _log_job(
                    self._logger,
                    "analysis_job_cancelled",
                    job,
                    self._queue.metrics(),
                    final_state=AnalysisJobState.CANCELLED,
                )
                continue
            _log_job(
                self._logger,
                "analysis_job_started",
                job,
                self._queue.metrics(),
                queue_wait_ms=queue_wait_ms,
            )
            try:
                state = await self._processor(job)
            except asyncio.CancelledError:
                self._finish_started_job(
                    job,
                    AnalysisJobState.CANCELLED,
                    cancellation_reason="worker_shutdown",
                )
                raise
            except Exception:
                state = AnalysisJobState.FAILED
                self._logger.exception("analysis_job_failed analysis_id=%s", job.analysis_id)
            self._finish_started_job(job, state)

    def _finish_started_job(
        self,
        job: AnalysisJob,
        state: AnalysisJobState,
        **fields: object,
    ) -> None:
        end_to_end_duration_ms = self._queue.mark_finished(job, state)
        _log_job(
            self._logger,
            "analysis_job_terminal",
            job,
            self._queue.metrics(),
            final_state=state,
            end_to_end_duration_ms=end_to_end_duration_ms,
            **fields,
        )


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _log_job(
    logger: logging.Logger,
    event: str,
    job: AnalysisJob,
    metrics: AnalysisQueueMetrics,
    **fields: object,
) -> None:
    from diagnostics.job_events import log_job_event

    log_job_event(
        logger,
        event,
        analysis_id=job.analysis_id,
        video_id=job.video_id,
        player_id=job.player_id,
        queue_depth=metrics.queued,
        queue_capacity=metrics.capacity,
        active_analysis_count=metrics.active_analysis_count,
        max_active_analyses=metrics.max_active_analyses,
        accepting=metrics.accepting,
        fields=fields,
    )
