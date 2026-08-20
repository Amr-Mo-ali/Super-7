"""Parent-owned, one-worker process-pool adapter for a future queue-worker integration.

Queued futures can be cancelled during shutdown, but a running child analysis may
finish before shutdown completes. Hard termination, cross-process deadlines, and
worker recycling are deliberately deferred; parent cancellation does not currently
stop child computation.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing.context import BaseContext
from typing import Protocol, cast

from core.config import Settings
from services.process_contracts import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisRequest,
    ChildAnalysisResult,
    ParentChildResult,
    ParentFailure,
    validate_child_result,
)
from services.process_entrypoint import initialize_analysis_child, run_child_analysis


class _ProcessExecutor(Protocol):
    def submit(
        self,
        function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
        request: ChildAnalysisRequest,
    ) -> Future[ChildAnalysisResult]: ...

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None: ...


class ProcessExecutorFactory(Protocol):
    def __call__(
        self,
        *,
        max_workers: int,
        mp_context: BaseContext,
        initializer: Callable[[Settings], None],
        initargs: tuple[Settings],
    ) -> _ProcessExecutor: ...


def _create_process_executor(
    *,
    max_workers: int,
    mp_context: BaseContext,
    initializer: Callable[[Settings], None],
    initargs: tuple[Settings],
) -> _ProcessExecutor:
    return cast(
        _ProcessExecutor,
        ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_context,
            initializer=initializer,
            initargs=initargs,
        ),
    )


class ProcessAnalysisPool:
    """Own exactly one spawned child pool while keeping parent behavior parent-owned."""

    def __init__(
        self,
        settings: Settings,
        executor_factory: ProcessExecutorFactory = _create_process_executor,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._executor_factory = executor_factory
        self._logger = logger or logging.getLogger("football_analysis.process_pool")
        self._executor: _ProcessExecutor | None = None
        self._shutdown = False

    def start(self) -> None:
        """Create the one-worker spawn pool once; construction itself starts no process."""
        if self._shutdown:
            raise RuntimeError("process analysis pool has been shut down")
        if self._executor is not None:
            raise RuntimeError("process analysis pool has already started")
        self._executor = self._executor_factory(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=initialize_analysis_child,
            initargs=(self._settings,),
        )

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult:
        """Submit one child request and validate its serializable result in the parent."""
        executor = self._require_running_executor()
        try:
            future = executor.submit(run_child_analysis, request)
            result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            raise
        except BrokenProcessPool as error:
            self._log_execution_failure(request.analysis_id, error)
            return ParentFailure("ProcessPoolError", "Analysis could not be completed.")
        except Exception as error:
            self._log_execution_failure(request.analysis_id, error)
            return ParentFailure("ProcessPoolError", "Analysis could not be completed.")
        return validate_child_result(
            request.analysis_id,
            CHILD_ANALYSIS_SCHEMA_VERSION,
            result,
        )

    async def shutdown(self) -> None:
        """Stop accepting submissions and offload the executor's blocking shutdown."""
        executor = self._executor
        self._shutdown = True
        if executor is None:
            return
        self._executor = None
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

    def _require_running_executor(self) -> _ProcessExecutor:
        if self._shutdown:
            raise RuntimeError("process analysis pool has been shut down")
        if self._executor is None:
            raise RuntimeError("process analysis pool has not been started")
        return self._executor

    def _log_execution_failure(self, analysis_id: str, error: Exception) -> None:
        self._logger.error(
            "analysis_process_pool_execution_failed analysis_id=%s error_type=%s",
            analysis_id,
            type(error).__name__,
        )
