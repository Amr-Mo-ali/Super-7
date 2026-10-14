"""Unit coverage for the unused parent-owned process-pool adapter."""

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from multiprocessing.context import BaseContext
from threading import get_ident
from typing import cast

import pytest

from core.config import Settings
from schemas.analysis import Diagnostics, NonCompletedResponse
from services.process_analysis_pool import ProcessAnalysisPool, ValidatedChildSuccess
from services.process_contracts import (
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisResult,
    ChildAnalysisSuccess,
    ParentCancelled,
    ParentFailure,
)
from services.process_entrypoint import initialize_analysis_child


class RecordingExecutor:
    def __init__(self, outcomes: list[ChildAnalysisResult | Exception]) -> None:
        self.outcomes = outcomes
        self.submissions: list[ChildAnalysisRequest] = []
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self.shutdown_thread_id: int | None = None

    def submit(
        self,
        function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
        request: ChildAnalysisRequest,
    ) -> Future[ChildAnalysisResult]:
        assert function.__name__ == "run_child_analysis"
        self.submissions.append(request)
        future: Future[ChildAnalysisResult] = Future()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            future.set_exception(outcome)
        else:
            future.set_result(outcome)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))
        self.shutdown_thread_id = get_ident()


class RecordingFactory:
    def __init__(self, executor: RecordingExecutor) -> None:
        self.executor = executor
        self.calls: list[tuple[int, str, Callable[[Settings], None], tuple[Settings]]] = []

    def __call__(
        self,
        *,
        max_workers: int,
        mp_context: BaseContext,
        initializer: Callable[[Settings], None],
        initargs: tuple[Settings],
    ) -> RecordingExecutor:
        self.calls.append((max_workers, mp_context.get_start_method(), initializer, initargs))
        return self.executor


class RecordingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _request(analysis_id: str = "analysis-1") -> ChildAnalysisRequest:
    return ChildAnalysisRequest(analysis_id, "video-1", "player-1", "safe.mp4")


def _response(analysis_id: str) -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id=analysis_id,
        status="no_players_detected",
        warnings=[],
        diagnostics=Diagnostics(
            frames_processed=0,
            frames_with_player_detections=0,
            total_person_detections=0,
            tracks_created=0,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
    )


def _success(
    envelope_id: str = "analysis-1", response_id: str | None = None
) -> ChildAnalysisSuccess:
    response = _response(response_id or envelope_id)
    return ChildAnalysisSuccess(envelope_id, response.model_dump_json(), "v1", "model", 0)


def _schema_mismatch() -> ChildAnalysisResult:
    """Build an untrusted deserialized envelope that bypasses constructor validation."""
    result = object.__new__(ChildAnalysisCancelled)
    object.__setattr__(result, "analysis_id", "analysis-1")
    object.__setattr__(result, "processing_duration_ms", 0)
    object.__setattr__(result, "schema_version", 999)
    return cast(ChildAnalysisResult, result)


def test_pool_construction_is_inert_and_start_creates_one_spawn_executor() -> None:
    executor = RecordingExecutor([])
    factory = RecordingFactory(executor)
    pool = ProcessAnalysisPool(Settings(), executor_factory=factory)
    assert factory.calls == []

    pool.start()

    assert factory.calls == [(1, "spawn", initialize_analysis_child, (Settings(),))]
    with pytest.raises(RuntimeError, match="already started"):
        pool.start()


def test_execute_requires_start_and_validates_success_failure_and_cancellation() -> None:
    async def scenario() -> None:
        executor = RecordingExecutor(
            [
                _success(),
                ChildAnalysisFailure(
                    "analysis-2", "AnalysisBoom", "Analysis could not be completed.", 0
                ),
                ChildAnalysisCancelled("analysis-3", 0),
            ]
        )
        pool = ProcessAnalysisPool(Settings(), executor_factory=RecordingFactory(executor))
        with pytest.raises(RuntimeError, match="not been started"):
            await pool.execute(_request())
        pool.start()
        assert await pool.execute(_request()) == _response("analysis-1")
        assert await pool.execute(_request("analysis-2")) == ParentFailure(
            "AnalysisBoom", "Analysis could not be completed."
        )
        assert await pool.execute(_request("analysis-3")) == ParentCancelled("analysis-3")

    asyncio.run(scenario())


def test_durable_execution_retains_success_envelope_without_changing_legacy_result() -> None:
    async def scenario() -> None:
        raw = _success()
        executor = RecordingExecutor([raw, raw])
        pool = ProcessAnalysisPool(Settings(), executor_factory=RecordingFactory(executor))
        pool.start()
        with pytest.raises(ValueError, match="requires an analysis version"):
            await pool.execute_enveloped(_request())
        durable = ChildAnalysisRequest(
            "analysis-1", "video-1", "player-1", "safe.mp4", analysis_version="1.0.0"
        )
        assert await pool.execute_enveloped(durable) == ValidatedChildSuccess(
            raw, _response("analysis-1")
        )
        assert await pool.execute(_request()) == _response("analysis-1")
        assert executor.submissions == [durable, _request()]

    asyncio.run(scenario())


def test_cancelled_observer_does_not_hide_running_submitted_work() -> None:
    async def scenario() -> None:
        class HoldingExecutor(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__([])
                self.future: Future[ChildAnalysisResult] = Future()
                assert self.future.set_running_or_notify_cancel()
                self.submitted = asyncio.Event()

            def submit(
                self,
                function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
                request: ChildAnalysisRequest,
            ) -> Future[ChildAnalysisResult]:
                assert function.__name__ == "run_child_analysis"
                self.submissions.append(request)
                self.submitted.set()
                return self.future

        executor = HoldingExecutor()
        pool = ProcessAnalysisPool(Settings(), executor_factory=RecordingFactory(executor))
        pool.start()
        request = ChildAnalysisRequest(
            "analysis-1", "video-1", "player-1", "safe.mp4", analysis_version="1.0.0"
        )
        observer = asyncio.create_task(pool.execute_enveloped(request))
        await asyncio.wait_for(executor.submitted.wait(), 1)
        assert pool.outstanding_submissions == (executor.future,)
        observer.cancel()
        assert isinstance(
            (await asyncio.gather(observer, return_exceptions=True))[0], asyncio.CancelledError
        )
        assert observer.done() and executor.future.running() and not executor.future.done()
        assert pool.outstanding_submissions == (executor.future,)
        executor.future.set_result(_success())
        assert not pool.outstanding_submissions
        await pool.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("complete_before_cancel_settles", [False, True])
def test_late_failed_submission_is_observed_and_safely_reported(
    complete_before_cancel_settles: bool,
) -> None:
    async def scenario() -> None:
        class TrackedFuture(Future[ChildAnalysisResult]):
            def __init__(self) -> None:
                super().__init__()
                self.exception_calls = 0
                assert self.set_running_or_notify_cancel()

            def exception(self, timeout: float | None = None) -> BaseException | None:
                self.exception_calls += 1
                return super().exception(timeout)

        class HoldingExecutor(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__([])
                self.future = TrackedFuture()
                self.submitted = asyncio.Event()

            def submit(
                self,
                function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
                request: ChildAnalysisRequest,
            ) -> Future[ChildAnalysisResult]:
                self.submissions.append(request)
                self.submitted.set()
                return self.future

        executor = HoldingExecutor()
        handler = RecordingLogHandler()
        logger = logging.Logger("test.process_pool.late_failure", level=logging.ERROR)
        logger.addHandler(handler)
        pool = ProcessAnalysisPool(
            Settings(), executor_factory=RecordingFactory(executor), logger=logger
        )
        pool.start()
        request = ChildAnalysisRequest(
            "analysis-1", "video-1", "player-1", "safe.mp4", analysis_version="1.0.0"
        )
        observer = asyncio.create_task(pool.execute_enveloped(request))
        await asyncio.wait_for(executor.submitted.wait(), 1)
        observer.cancel()
        if not complete_before_cancel_settles:
            assert isinstance(
                (await asyncio.gather(observer, return_exceptions=True))[0], asyncio.CancelledError
            )
            assert pool.outstanding_submissions == (executor.future,)
        executor.future.set_exception(RuntimeError("private child exception detail"))
        assert isinstance(
            (await asyncio.gather(observer, return_exceptions=True))[0], asyncio.CancelledError
        )
        await asyncio.sleep(0)
        assert executor.future.exception_calls >= 1
        assert not pool.outstanding_submissions
        failures = [
            record
            for record in handler.records
            if record.getMessage().startswith("analysis_process_pool_execution_failed")
        ]
        assert len(failures) == 1
        assert failures[0].getMessage() == (
            "analysis_process_pool_execution_failed analysis_id=analysis-1 error_type=RuntimeError"
        )
        assert failures[0].exc_info is None
        assert all(
            "private child exception detail" not in record.getMessage()
            for record in handler.records
        )
        await pool.shutdown()

    asyncio.run(scenario())


def test_cancelled_queued_submission_releases_tracking_without_failure_report() -> None:
    async def scenario() -> None:
        class PendingExecutor(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__([])
                self.future: Future[ChildAnalysisResult] = Future()
                self.submitted = asyncio.Event()

            def submit(
                self,
                function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
                request: ChildAnalysisRequest,
            ) -> Future[ChildAnalysisResult]:
                self.submissions.append(request)
                self.submitted.set()
                return self.future

        executor = PendingExecutor()
        handler = RecordingLogHandler()
        logger = logging.Logger("test.process_pool.cancelled", level=logging.ERROR)
        logger.addHandler(handler)
        pool = ProcessAnalysisPool(
            Settings(), executor_factory=RecordingFactory(executor), logger=logger
        )
        pool.start()
        request = ChildAnalysisRequest(
            "analysis-1", "video-1", "player-1", "safe.mp4", analysis_version="1.0.0"
        )
        observer = asyncio.create_task(pool.execute_enveloped(request))
        await asyncio.wait_for(executor.submitted.wait(), 1)
        observer.cancel()
        assert isinstance(
            (await asyncio.gather(observer, return_exceptions=True))[0], asyncio.CancelledError
        )
        await asyncio.sleep(0)
        assert executor.future.cancelled()
        assert not pool.outstanding_submissions
        assert not any("execution_failed" in record.getMessage() for record in handler.records)
        await pool.shutdown()

        success_pool = ProcessAnalysisPool(
            Settings(),
            executor_factory=RecordingFactory(RecordingExecutor([_success()])),
            logger=logger,
        )
        success_pool.start()
        assert await success_pool.execute(_request()) == _response("analysis-1")
        assert not success_pool.outstanding_submissions
        assert not any("execution_failed" in record.getMessage() for record in handler.records)
        await success_pool.shutdown()

    asyncio.run(scenario())


def test_execute_rejects_child_identity_schema_or_response_mismatch() -> None:
    async def scenario() -> None:
        executor = RecordingExecutor(
            [ChildAnalysisCancelled("other", 0), _schema_mismatch(), _success(response_id="other")]
        )
        pool = ProcessAnalysisPool(Settings(), executor_factory=RecordingFactory(executor))
        pool.start()
        with pytest.raises(ValueError, match="expected analysis identity"):
            await pool.execute(_request())
        with pytest.raises(ValueError, match="expected analysis identity"):
            await pool.execute(_request())
        with pytest.raises(ValueError, match="response analysis ID"):
            await pool.execute(_request())

    asyncio.run(scenario())


def test_executor_failures_are_sanitized_in_the_parent() -> None:
    async def scenario() -> None:
        executor = RecordingExecutor(
            [BrokenProcessPool("secret process detail"), RuntimeError("secret")]
        )
        handler = RecordingLogHandler()
        logger = logging.Logger("test.process_pool.ordinary_failure", level=logging.ERROR)
        logger.addHandler(handler)
        pool = ProcessAnalysisPool(
            Settings(), executor_factory=RecordingFactory(executor), logger=logger
        )
        pool.start()
        expected = ParentFailure("ProcessPoolError", "Analysis could not be completed.")
        assert await pool.execute(_request()) == expected
        assert await pool.execute(_request()) == expected
        failures = [
            record
            for record in handler.records
            if record.getMessage().startswith("analysis_process_pool_execution_failed")
        ]
        assert len(failures) == 2
        assert not any("secret" in record.getMessage() for record in failures)
        assert not pool.outstanding_submissions

    asyncio.run(scenario())


def test_shutdown_is_idempotent_offloads_blocking_work_and_rejects_execution() -> None:
    async def scenario() -> None:
        executor = RecordingExecutor([])
        pool = ProcessAnalysisPool(Settings(), executor_factory=RecordingFactory(executor))
        pool.start()
        event_loop_thread = get_ident()

        await pool.shutdown()
        await pool.shutdown()

        assert executor.shutdown_calls == [(True, True)]
        assert executor.shutdown_thread_id != event_loop_thread
        with pytest.raises(RuntimeError, match="has been shut down"):
            await pool.execute(_request())
        with pytest.raises(RuntimeError, match="has been shut down"):
            pool.start()

    asyncio.run(scenario())
