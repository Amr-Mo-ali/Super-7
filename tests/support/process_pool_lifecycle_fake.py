"""Deterministic parent-side process-pool lifecycle fake for application tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from services.process_contracts import ChildAnalysisRequest, ParentChildResult

type ProcessOutcome = ParentChildResult | Callable[[ChildAnalysisRequest], ParentChildResult]


class FakeProcessPoolLifecycle:
    def __init__(
        self,
        outcomes: list[ProcessOutcome] | None = None,
        *,
        events: list[str] | None = None,
        start_error: Exception | None = None,
        execute_started: asyncio.Event | None = None,
        execute_release: asyncio.Event | None = None,
    ) -> None:
        self._outcomes = outcomes or []
        self._events = events if events is not None else []
        self._start_error = start_error
        self._execute_started = execute_started
        self._execute_release = execute_release
        self.requests: list[ChildAnalysisRequest] = []
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self._events.append("pool.start")
        if self._start_error is not None:
            raise self._start_error

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult:
        self.requests.append(request)
        self._events.append("pool.execute")
        if self._execute_started is not None:
            self._execute_started.set()
        if self._execute_release is not None:
            await self._execute_release.wait()
        outcome = self._outcomes.pop(0)
        return outcome(request) if callable(outcome) else outcome

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._events.append("pool.shutdown")
