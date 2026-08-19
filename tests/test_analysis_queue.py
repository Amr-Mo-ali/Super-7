"""Deterministic FIFO and single-worker tests for background analysis jobs."""

import asyncio
import logging
from dataclasses import fields

import pytest
from pydantic import HttpUrl, TypeAdapter

from services.analysis_queue import AnalysisJob, AnalysisJobState, AnalysisQueue, AnalysisWorker


def test_queue_is_bounded_and_jobs_hold_only_lightweight_references() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(1)
        first = _job("one")
        second = _job("two")
        assert await queue.submit(first) is True
        assert await queue.submit(second) is False
        assert {field.name for field in fields(first)} == {
            "analysis_id",
            "video_id",
            "player_id",
            "video_reference",
            "callback_url",
            "submitted_at",
        }

    asyncio.run(scenario())


def test_single_worker_executes_jobs_fifo_without_parallelism() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(3)
        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []
        concurrent = 0
        maximum = 0

        async def processor(job: AnalysisJob) -> AnalysisJobState:
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            order.append(job.analysis_id)
            if job.analysis_id == "one":
                started.set()
                await release.wait()
            concurrent -= 1
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(queue, processor, logging.getLogger("test.queue"))
        assert await queue.submit(_job("one"))
        assert await queue.submit(_job("two"))
        await worker.start()
        await started.wait()
        assert order == ["one"]
        assert queue.metrics().queued == 1
        release.set()
        await _wait_for(lambda: queue.state_of("two") is AnalysisJobState.COMPLETED)
        assert order == ["one", "two"]
        assert maximum == 1
        await worker.shutdown()

    asyncio.run(scenario())


def test_worker_survives_a_failed_job_and_skips_cancelled_jobs() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(3)
        processed: list[str] = []

        async def processor(job: AnalysisJob) -> AnalysisJobState:
            processed.append(job.analysis_id)
            if job.analysis_id == "broken":
                raise RuntimeError("analysis failure")
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(queue, processor, logging.getLogger("test.queue"))
        assert await queue.submit(_job("broken"))
        assert await queue.submit(_job("cancelled"))
        assert queue.cancel("cancelled") is True
        assert await queue.submit(_job("next"))
        await worker.start()
        await _wait_for(lambda: queue.state_of("next") is AnalysisJobState.COMPLETED)
        assert processed == ["broken", "next"]
        assert queue.state_of("broken") is AnalysisJobState.FAILED
        assert queue.state_of("cancelled") is AnalysisJobState.CANCELLED
        await worker.shutdown()

    asyncio.run(scenario())


def test_shutdown_stops_new_queue_admission_and_cancels_waiting_jobs() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(2)
        job = _job("queued")
        assert await queue.submit(job)
        queue.stop_accepting()
        assert queue.metrics().accepting is False
        assert queue.state_of(job.analysis_id) is AnalysisJobState.CANCELLED
        assert await queue.submit(_job("later")) is False

    asyncio.run(scenario())


def test_worker_logs_queue_wait_terminal_active_limit_and_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="test.queue.observability")
        queue = AnalysisQueue(2)

        async def processor(_: AnalysisJob) -> AnalysisJobState:
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(queue, processor, logging.getLogger("test.queue.observability"))
        assert await queue.submit(_job("observed"))
        await worker.start()
        await _wait_for(lambda: queue.state_of("observed") is AnalysisJobState.COMPLETED)
        assert queue.metrics().active_analysis_count == 0
        assert queue.metrics().max_active_analyses == 1
        assert await queue.submit(_job("shutdown"))
        worker.begin_shutdown()
        await worker.shutdown()

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    started = next(message for message in messages if "analysis_job_started" in message)
    terminal = next(message for message in messages if "analysis_job_terminal" in message)
    cancelled = next(message for message in messages if "analysis_job_cancelled" in message)
    assert "queue_wait_ms=" in started
    assert "active_analysis_count=1" in started
    assert "max_active_analyses=1" in started
    assert "final_state=COMPLETED" in terminal
    assert "end_to_end_duration_ms=" in terminal
    assert "cancellation_reason=shutdown" in cancelled
    assert sum("analysis_job_terminal" in message for message in messages) == 1
    assert "analysis_shutdown_started" in messages
    assert "analysis_shutdown_finished" in messages


async def _wait_for(predicate: object) -> None:
    for _ in range(100):
        if callable(predicate) and predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _job(analysis_id: str) -> AnalysisJob:
    return AnalysisJob.create(
        analysis_id,
        f"video-{analysis_id}",
        f"player-{analysis_id}",
        "test-video.mp4",
        TypeAdapter(HttpUrl).validate_python("http://72.62.28.146/api/video-analysis/webhook"),
    )
