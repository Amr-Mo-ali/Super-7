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
    shutdown_started = next(
        message for message in messages if "analysis_shutdown_started" in message
    )
    shutdown_finished = next(
        message for message in messages if "analysis_shutdown_finished" in message
    )
    assert "queue_depth=1" in shutdown_started
    assert "queue_capacity=2" in shutdown_started
    assert "active_analysis_count=0" in shutdown_started
    assert "accepting_before=True" in shutdown_started
    assert "accepting_after=False" in shutdown_started
    assert "cancelled_queued_job_count=1" in shutdown_started
    assert "worker_running=False" in shutdown_finished
    assert "cancelled_queued_job_count=1" in shutdown_finished


def test_worker_cancellation_finalizes_the_started_analysis_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> AnalysisQueue:
        caplog.set_level(logging.INFO, logger="test.queue.cancellation")
        queue = AnalysisQueue(2)
        started = asyncio.Event()
        blocked = asyncio.Event()
        started_jobs: list[str] = []

        async def processor(job: AnalysisJob) -> AnalysisJobState:
            started_jobs.append(job.analysis_id)
            started.set()
            await blocked.wait()
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(
            queue,
            processor,
            logging.getLogger("test.queue.cancellation"),
            shutdown_grace_seconds=0.01,
        )
        assert await queue.submit(_job("running"))
        assert await queue.submit(_job("waiting"))
        await worker.start()
        await started.wait()
        await worker.shutdown()
        worker.begin_shutdown()
        await worker.shutdown()
        await asyncio.wait_for(queue.wait_until_idle(), timeout=0.1)
        assert started_jobs == ["running"]
        assert queue.state_of("running") is AnalysisJobState.CANCELLED
        assert queue.state_of("waiting") is AnalysisJobState.CANCELLED
        assert queue.metrics().active_analysis_count == 0
        assert queue.metrics().queued == 0
        return queue

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    terminal = [
        message
        for message in messages
        if "analysis_job_terminal" in message and "analysis_id=running" in message
    ]
    assert len(terminal) == 1
    assert "final_state=CANCELLED" in terminal[0]
    assert "cancellation_reason=worker_shutdown" in terminal[0]
    assert not any("analysis_job_started analysis_id=waiting" in message for message in messages)
    assert sum("analysis_shutdown_started" in message for message in messages) == 1
    assert sum("analysis_shutdown_finished" in message for message in messages) == 1


def test_worker_cancellation_during_inline_callback_phase_finalizes_the_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="test.queue.callback_cancellation")
        queue = AnalysisQueue(1)
        callback_started = asyncio.Event()
        callback_blocked = asyncio.Event()

        async def processor(_: AnalysisJob) -> AnalysisJobState:
            callback_started.set()
            await callback_blocked.wait()
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(
            queue,
            processor,
            logging.getLogger("test.queue.callback_cancellation"),
            shutdown_grace_seconds=0.01,
        )
        assert await queue.submit(_job("callback-running"))
        await worker.start()
        await callback_started.wait()
        await worker.shutdown()
        await asyncio.wait_for(queue.wait_until_idle(), timeout=0.1)
        assert queue.state_of("callback-running") is AnalysisJobState.CANCELLED
        assert queue.metrics().active_analysis_count == 0

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    terminal = [
        message
        for message in messages
        if "analysis_job_terminal" in message and "analysis_id=callback-running" in message
    ]
    assert len(terminal) == 1
    assert "final_state=CANCELLED" in terminal[0]


def test_shutdown_records_active_completion_and_does_not_start_waiting_job() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(2)
        started = asyncio.Event()
        release = asyncio.Event()
        processed: list[str] = []

        async def processor(job: AnalysisJob) -> AnalysisJobState:
            processed.append(job.analysis_id)
            started.set()
            await release.wait()
            return AnalysisJobState.COMPLETED

        worker = AnalysisWorker(queue, processor, logging.getLogger("test.queue.grace"), 0.1)
        assert await queue.submit(_job("running"))
        assert await queue.submit(_job("waiting"))
        await worker.start()
        await started.wait()
        worker.begin_shutdown()
        shutdown = asyncio.create_task(worker.shutdown())
        release.set()
        await shutdown
        assert queue.state_of("running") is AnalysisJobState.COMPLETED
        assert queue.state_of("waiting") is AnalysisJobState.CANCELLED
        assert processed == ["running"]

    asyncio.run(scenario())


def test_shutdown_records_active_failure_within_grace() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def processor(_: AnalysisJob) -> AnalysisJobState:
            started.set()
            await release.wait()
            raise RuntimeError("expected failure")

        worker = AnalysisWorker(
            queue, processor, logging.getLogger("test.queue.grace_failure"), 0.1
        )
        assert await queue.submit(_job("failing"))
        await worker.start()
        await started.wait()
        worker.begin_shutdown()
        shutdown = asyncio.create_task(worker.shutdown())
        release.set()
        await shutdown
        assert queue.state_of("failing") is AnalysisJobState.FAILED

    asyncio.run(scenario())


def test_idle_shutdown_is_prompt() -> None:
    async def scenario() -> None:
        queue = AnalysisQueue(1)

        async def processor(_: AnalysisJob) -> AnalysisJobState:
            raise AssertionError("idle worker must not process a job")

        worker = AnalysisWorker(queue, processor, logging.getLogger("test.queue.idle"), 0.01)
        await worker.start()
        await asyncio.wait_for(worker.shutdown(), timeout=0.1)
        assert queue.metrics().worker_running is False

    asyncio.run(scenario())


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
