"""Bounded FIFO queue and single worker for background analysis jobs."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

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


class AnalysisQueue:
    """Application-owned, bounded queue holding lightweight analysis references only."""

    def __init__(self, capacity: int) -> None:
        self._queue: asyncio.Queue[AnalysisJob] = asyncio.Queue(maxsize=capacity)
        self._states: dict[str, AnalysisJobState] = {}
        self._accepting = True
        self._worker_running = False

    async def submit(self, job: AnalysisJob) -> bool:
        if not self._accepting or self._queue.full():
            return False
        self._queue.put_nowait(job)
        self._states[job.analysis_id] = AnalysisJobState.QUEUED
        return True

    async def next_job(self) -> AnalysisJob:
        return await self._queue.get()

    def mark_running(self, job: AnalysisJob) -> bool:
        if self._states.get(job.analysis_id) is AnalysisJobState.CANCELLED:
            return False
        self._states[job.analysis_id] = AnalysisJobState.RUNNING
        return True

    def mark_finished(self, job: AnalysisJob, state: AnalysisJobState) -> None:
        self._states[job.analysis_id] = state
        self._queue.task_done()

    def cancel(self, analysis_id: str) -> bool:
        if self._states.get(analysis_id) is not AnalysisJobState.QUEUED:
            return False
        self._states[analysis_id] = AnalysisJobState.CANCELLED
        return True

    def state_of(self, analysis_id: str) -> AnalysisJobState | None:
        return self._states.get(analysis_id)

    def stop_accepting(self) -> None:
        self._accepting = False
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._states[job.analysis_id] = AnalysisJobState.CANCELLED
            self._queue.task_done()

    def set_worker_running(self, running: bool) -> None:
        self._worker_running = running

    def metrics(self) -> AnalysisQueueMetrics:
        return AnalysisQueueMetrics(
            queued=self._queue.qsize(),
            capacity=self._queue.maxsize,
            accepting=self._accepting,
            worker_running=self._worker_running,
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

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("analysis worker is already running")
        self._queue.set_worker_running(True)
        self._task = asyncio.create_task(self._run(), name="analysis-worker")

    async def shutdown(self) -> None:
        self._queue.stop_accepting()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._queue.set_worker_running(False)

    async def _run(self) -> None:
        while True:
            job = await self._queue.next_job()
            if not self._queue.mark_running(job):
                self._queue.mark_finished(job, AnalysisJobState.CANCELLED)
                self._logger.info("analysis_job_cancelled analysis_id=%s", job.analysis_id)
                continue
            self._logger.info(
                "analysis_job_started analysis_id=%s video_id=%s player_id=%s queue_depth=%s",
                job.analysis_id,
                job.video_id,
                job.player_id,
                self._queue.metrics().queued,
            )
            try:
                state = await self._processor(job)
            except Exception:
                state = AnalysisJobState.FAILED
                self._logger.exception("analysis_job_failed analysis_id=%s", job.analysis_id)
            self._queue.mark_finished(job, state)
            self._logger.info("analysis_job_%s analysis_id=%s", state.lower(), job.analysis_id)
