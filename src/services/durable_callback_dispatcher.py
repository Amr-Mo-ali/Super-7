"""Single-actor, opt-in local dispatcher for persisted due callback events."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from services.analysis_callback_delivery import (
    CallbackDeliveryRepository,
    CallbackTransport,
    deliver_one,
)


class _DueEvents(CallbackDeliveryRepository, Protocol):
    async def next_due_event_id(self) -> UUID | None: ...


class DurableCallbackDispatcher:
    """Discover one event at a time; keep all retry timing in PostgreSQL."""

    def __init__(
        self,
        *,
        repository: _DueEvents,
        transport: CallbackTransport,
        logger: logging.Logger,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._logger = logger
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._last_delivery_error: Exception | None = None

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def last_delivery_error(self) -> Exception | None:
        return self._last_delivery_error

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Durable dispatcher cannot be started twice.")
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._observe_completion)

    async def wait(self) -> None:
        """Observe the actor and propagate its failure to the process owner."""
        if self._task is None:
            raise RuntimeError("Durable dispatcher has not started.")
        await asyncio.shield(self._task)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                event_id = await self._repository.next_due_event_id()
                if event_id is None:
                    await self._wait(1)
                    continue
                if self._stop.is_set():
                    return
                try:
                    await deliver_one(
                        repository=self._repository,
                        transport=self._transport,
                        event_id=event_id,
                        timeout_seconds=10,
                        retry_delay_ms=30_000,
                    )
                except Exception as error:
                    # An ambiguous delivery-state write must not immediately resend.
                    self._last_delivery_error = error
                    self._logger.error(
                        "durable_callback_delivery_failed error_type=%s",
                        type(error).__name__,
                    )
                    await self._wait(30)
        except BaseException as error:
            self._failure = error
            raise

    def _observe_completion(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError as cancelled:
            error = cancelled
        if error is not None:
            self._logger.error(
                "durable_callback_dispatcher_stopped error_type=%s", type(error).__name__
            )
