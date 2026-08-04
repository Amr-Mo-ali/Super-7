"""Deterministic tests for synchronous pipeline execution isolation."""

import asyncio
from dataclasses import dataclass
from threading import Event, get_ident

import pytest

from concurrency.executor import AnalysisExecutor, CancellationState, current_request_id


@dataclass(frozen=True)
class PipelineResult:
    request_id: str | None
    value: int


def test_successful_execution_runs_pipeline_off_the_event_loop_thread() -> None:
    async def scenario() -> None:
        executor = AnalysisExecutor()
        event_loop_thread = get_ident()

        def pipeline(cancellation: CancellationState) -> tuple[str | None, int, bool]:
            return current_request_id(), get_ident(), cancellation.is_cancelled()

        request_id, pipeline_thread, cancelled = await executor.execute(
            "analysis-1", CancellationState(), pipeline
        )

        assert request_id == "analysis-1"
        assert pipeline_thread != event_loop_thread
        assert cancelled is False

    asyncio.run(scenario())


def test_pipeline_exception_propagates_with_its_original_type() -> None:
    class PipelineFailure(Exception):
        pass

    async def scenario() -> None:
        executor = AnalysisExecutor()

        def pipeline(cancellation: CancellationState) -> None:
            del cancellation
            raise PipelineFailure("unchanged")

        with pytest.raises(PipelineFailure, match="unchanged"):
            await executor.execute("analysis-2", CancellationState(), pipeline)

    asyncio.run(scenario())


def test_pipeline_cleanup_executes_when_pipeline_raises() -> None:
    async def scenario() -> None:
        executor = AnalysisExecutor()
        cleaned = Event()

        def pipeline(cancellation: CancellationState) -> None:
            del cancellation
            try:
                raise RuntimeError("failed")
            finally:
                cleaned.set()

        with pytest.raises(RuntimeError, match="failed"):
            await executor.execute("analysis-3", CancellationState(), pipeline)
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_concurrent_execution_keeps_request_context_isolated() -> None:
    async def scenario() -> None:
        executor = AnalysisExecutor()

        def pipeline(cancellation: CancellationState) -> PipelineResult:
            return PipelineResult(current_request_id(), id(cancellation))

        first_state = CancellationState()
        second_state = CancellationState()
        first, second = await asyncio.gather(
            executor.execute("analysis-a", first_state, pipeline),
            executor.execute("analysis-b", second_state, pipeline),
        )

        assert first == PipelineResult("analysis-a", id(first_state))
        assert second == PipelineResult("analysis-b", id(second_state))

    asyncio.run(scenario())


def test_cancellation_state_is_observed_and_pipeline_cleanup_executes() -> None:
    async def scenario() -> None:
        executor = AnalysisExecutor()
        cancellation = CancellationState()
        started = Event()
        cleaned = Event()

        def pipeline(state: CancellationState) -> None:
            try:
                started.set()
                if state.wait(timeout=1):
                    raise asyncio.CancelledError()
                raise AssertionError("pipeline did not observe cancellation")
            finally:
                cleaned.set()

        task = asyncio.create_task(executor.execute("analysis-c", cancellation, pipeline))
        await asyncio.to_thread(started.wait)
        cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_event_loop_remains_responsive_while_pipeline_blocks() -> None:
    async def scenario() -> None:
        executor = AnalysisExecutor()
        started = Event()
        release = Event()

        def pipeline(cancellation: CancellationState) -> str:
            del cancellation
            started.set()
            release.wait(timeout=1)
            return "finished"

        task = asyncio.create_task(executor.execute("analysis-d", CancellationState(), pipeline))
        await asyncio.to_thread(started.wait)
        loop_progress = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_progress.set)
        await asyncio.wait_for(loop_progress.wait(), timeout=0.1)
        release.set()
        assert await task == "finished"

    asyncio.run(scenario())


def test_executor_output_matches_direct_pipeline_execution() -> None:
    def pipeline(cancellation: CancellationState) -> tuple[int, tuple[int, ...], bool]:
        return 42, (1, 2, 3), cancellation.is_cancelled()

    direct = pipeline(CancellationState())

    async def scenario() -> tuple[int, tuple[int, ...], bool]:
        return await AnalysisExecutor().execute("analysis-parity", CancellationState(), pipeline)

    assert asyncio.run(scenario()) == direct
