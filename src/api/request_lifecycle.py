"""Minimal coordination of admission, cancellation, deadlines, and cleanup."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from threading import Lock
from time import perf_counter
from typing import TypeVar

from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AdmissionRejectedError
from concurrency.executor import AnalysisExecutor
from diagnostics.artifacts import ArtifactManager, ArtifactSession

Result = TypeVar("Result")


class RequestLifecycle:
    """Coordinate request-local execution while preserving existing cleanup ownership."""

    def __init__(
        self,
        admission: AdmissionController,
        executor: AnalysisExecutor,
        artifacts: ArtifactManager | None = None,
        request_deadline_seconds: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if request_deadline_seconds is not None and request_deadline_seconds <= 0:
            raise ValueError("request_deadline_seconds must be positive when configured.")
        self._admission = admission
        self._executor = executor
        self._artifacts = artifacts
        self._request_deadline_seconds = request_deadline_seconds
        self._logger = logger or logging.getLogger("football_analysis.lifecycle")
        self._active: dict[str, tuple[CancellationManager, asyncio.Event]] = {}
        self._shutting_down = False
        self._lock = Lock()

    @property
    def admission(self) -> AdmissionController:
        return self._admission

    @property
    def executor(self) -> AnalysisExecutor:
        return self._executor

    @property
    def artifacts(self) -> ArtifactManager | None:
        return self._artifacts

    @property
    def shutting_down(self) -> bool:
        """Return whether this lifecycle has started graceful shutdown."""
        with self._lock:
            return self._shutting_down

    async def execute(
        self,
        request_id: str,
        pipeline: Callable[[CancellationManager], Result],
    ) -> Result:
        """Run admitted work and always cancel its deadline and release its permit."""
        permit = await self._admission.admit()
        if permit is None:
            raise AdmissionRejectedError("Analysis capacity is exhausted.")
        cancellation: CancellationManager | None = None
        completion: asyncio.Event | None = None
        deadline_task: asyncio.Task[None] | None = None
        try:
            cancellation = CancellationManager(request_id)
            completion = self._register(cancellation)
            deadline_task = self._start_deadline(cancellation)
            return await self._executor.execute(request_id, cancellation, pipeline)
        finally:
            try:
                await self._stop_deadline(deadline_task)
            finally:
                try:
                    if cancellation is not None:
                        cancellation.complete()
                finally:
                    try:
                        await permit.release()
                    finally:
                        if completion is not None:
                            self._complete(request_id, completion)

    async def execute_with_artifacts(
        self,
        request_id: str,
        pipeline: Callable[[CancellationManager, ArtifactSession], Result],
    ) -> Result:
        """Run admitted work with one request-owned artifact session."""
        if self._artifacts is None:
            raise RuntimeError("ArtifactManager was not configured for this request lifecycle.")
        permit = await self._admission.admit()
        if permit is None:
            raise AdmissionRejectedError("Analysis capacity is exhausted.")
        cancellation: CancellationManager | None = None
        artifacts: ArtifactSession | None = None
        completion: asyncio.Event | None = None
        deadline_task: asyncio.Task[None] | None = None
        execution_outcome = "completed"
        primary_failed = False
        try:
            cancellation = CancellationManager(request_id)
            completion = self._register(cancellation)
            deadline_task = self._start_deadline(cancellation)
            session = self._artifacts.create_session(request_id)
            artifacts = session
            return await self._executor.execute(
                request_id, cancellation, lambda state: pipeline(state, session)
            )
        except asyncio.CancelledError:
            execution_outcome = "cancelled"
            primary_failed = True
            raise
        except Exception:
            execution_outcome = "failed"
            primary_failed = True
            raise
        finally:
            try:
                if artifacts is not None:
                    cleanup_started = perf_counter()
                    try:
                        cleanup = artifacts.cleanup()
                    except Exception as error:
                        self._logger.warning(
                            "analysis_cleanup_finished analysis_id=%s cleanup_outcome=%s "
                            "cleanup_succeeded=false cleanup_error_count=1 cleanup_duration_ms=%s "
                            "cleanup_error_type=%s",
                            request_id,
                            execution_outcome,
                            _milliseconds(perf_counter() - cleanup_started),
                            type(error).__name__,
                        )
                        if not primary_failed:
                            raise
                    else:
                        self._logger.info(
                            "analysis_cleanup_finished analysis_id=%s cleanup_outcome=%s "
                            "cleanup_succeeded=%s cleanup_error_count=%s cleanup_duration_ms=%s",
                            request_id,
                            execution_outcome,
                            not cleanup.errors,
                            len(cleanup.errors),
                            _milliseconds(perf_counter() - cleanup_started),
                        )
            finally:
                try:
                    await self._stop_deadline(deadline_task)
                finally:
                    try:
                        if cancellation is not None:
                            cancellation.complete()
                    finally:
                        try:
                            await permit.release()
                        finally:
                            if completion is not None:
                                self._complete(request_id, completion)

    async def shutdown(self) -> None:
        """Reject new work, cancel admitted work, and await its ordinary cleanup path."""
        with self._lock:
            self._shutting_down = True
            active = tuple(self._active.values())
        await self._admission.close()
        for cancellation, _ in active:
            if not cancellation.is_cancelled():
                cancellation.request_shutdown()
        if active:
            await asyncio.gather(*(completion.wait() for _, completion in active))

    def _register(self, cancellation: CancellationManager) -> asyncio.Event:
        completion = asyncio.Event()
        request_id = cancellation.snapshot().request_id
        with self._lock:
            if request_id in self._active:
                raise RuntimeError("An active lifecycle request already uses this request ID.")
            self._active[request_id] = (cancellation, completion)
            shutting_down = self._shutting_down
        if shutting_down:
            cancellation.request_shutdown()
        return completion

    def _complete(self, request_id: str, completion: asyncio.Event) -> None:
        with self._lock:
            self._active.pop(request_id, None)
        completion.set()

    def _start_deadline(self, cancellation: CancellationManager) -> asyncio.Task[None] | None:
        if self._request_deadline_seconds is None:
            return None
        return asyncio.create_task(self._expire_deadline(cancellation))

    async def _expire_deadline(self, cancellation: CancellationManager) -> None:
        assert self._request_deadline_seconds is not None
        try:
            await asyncio.sleep(self._request_deadline_seconds)
        except asyncio.CancelledError:
            return
        if not cancellation.is_cancelled():
            cancellation.expire_deadline()

    @staticmethod
    async def _stop_deadline(deadline_task: asyncio.Task[None] | None) -> None:
        if deadline_task is None:
            return
        deadline_task.cancel()
        with suppress(asyncio.CancelledError):
            await deadline_task


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))
