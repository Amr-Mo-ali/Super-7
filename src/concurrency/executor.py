"""Async boundary for running one synchronous analysis pipeline invocation."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from threading import Event
from typing import Protocol, TypeVar

Result = TypeVar("Result")
Signal = TypeVar("Signal", bound="CancellationSignal")
_request_id: ContextVar[str | None] = ContextVar("analysis_request_id", default=None)


class CancellationSignal(Protocol):
    """The cooperative cancellation behavior required by one pipeline invocation."""

    def is_cancelled(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class CancellationState:
    """Request-scoped cooperative cancellation signal for synchronous work."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def current_request_id() -> str | None:
    """Return the executor request ID in the current pipeline invocation."""
    return _request_id.get()


class AnalysisExecutor:
    """Runs supplied synchronous work in a worker thread without translating errors."""

    async def execute(
        self,
        request_id: str,
        cancellation: Signal,
        pipeline: Callable[[Signal], Result],
    ) -> Result:
        """Run one pipeline invocation and propagate its original result or exception."""
        return await asyncio.to_thread(self._execute, request_id, cancellation, pipeline)

    @staticmethod
    def _execute(
        request_id: str,
        cancellation: Signal,
        pipeline: Callable[[Signal], Result],
    ) -> Result:
        context_token = _request_id.set(request_id)
        try:
            if cancellation.is_cancelled():
                raise asyncio.CancelledError()
            return pipeline(cancellation)
        finally:
            _request_id.reset(context_token)
