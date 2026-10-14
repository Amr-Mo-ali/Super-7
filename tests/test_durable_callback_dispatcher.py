"""Database-free scheduling and restart controls for the one-actor dispatcher."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from services.analysis_callback_delivery import StoredCallbackEvent
from services.durable_callback_dispatcher import DurableCallbackDispatcher


def _event() -> StoredCallbackEvent:
    return StoredCallbackEvent(
        UUID(int=1),
        UUID(int=2),
        "http://127.0.0.1/callback",
        "analysis_callback_snapshot_v1",
        {
            "request_id": str(UUID(int=2)),
            "video_id": "video",
            "player_id": "player",
            "status": "COMPLETED",
        },
        "PENDING",
        datetime.now(UTC),
        True,
        "COMPLETED",
        "COMPLETED",
        "SUCCEEDED",
        "video",
        "player",
    )


class _Repository:
    def __init__(self) -> None:
        self.event = _event()
        self.retry_delays: list[int] = []
        self.delivered = asyncio.Event()
        self.retried = asyncio.Event()
        self.error: BaseException | None = None

    async def next_due_event_id(self) -> UUID | None:
        if self.error is not None:
            raise self.error
        return self.event.event_id if self.event.due else None

    async def load(self, event_id: UUID) -> StoredCallbackEvent | None:
        assert event_id == self.event.event_id
        return self.event

    async def record_delivered(self, event_id: UUID) -> bool:
        assert event_id == self.event.event_id
        self.event = replace(self.event, delivery_state="DELIVERED", due=False)
        self.delivered.set()
        return True

    async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
        assert event_id == self.event.event_id
        self.retry_delays.append(delay_ms)
        self.event = replace(self.event, delivery_state="RETRYING", due=False)
        self.retried.set()
        return True


def _dispatcher(repository: _Repository, status: int) -> DurableCallbackDispatcher:
    async def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        assert url == repository.event.callback_url
        assert str(repository.event.event_id).encode() in body
        assert headers["X-Super7-Callback-Event-Id"] == str(repository.event.event_id)
        assert timeout == 10
        return status

    return DurableCallbackDispatcher(
        repository=repository,
        transport=transport,
        logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
    )


def test_due_retry_uses_persisted_30_second_schedule_and_restart_delivers() -> None:
    async def scenario() -> None:
        repository = _Repository()
        first = _dispatcher(repository, 503)
        first.start()
        try:
            await asyncio.wait_for(repository.retried.wait(), 1)
            assert repository.retry_delays == [30_000]
            assert repository.event.delivery_state == "RETRYING"
            assert not repository.event.due
        finally:
            await asyncio.wait_for(first.stop(), 1)
        assert first.failure is None
        repository.event = replace(repository.event, due=True)
        second = _dispatcher(repository, 204)
        second.start()
        try:
            await asyncio.wait_for(repository.delivered.wait(), 1)
            assert repository.event.delivery_state == "DELIVERED"
            assert not repository.event.due
        finally:
            await asyncio.wait_for(second.stop(), 1)
        assert second.failure is None

    asyncio.run(scenario())


def test_discovery_failure_is_visible_and_preserves_identity() -> None:
    async def scenario() -> None:
        repository = _Repository()
        error = RuntimeError("private-detail")
        repository.error = error
        dispatcher = _dispatcher(repository, 204)
        dispatcher.start()
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError) as captured:
            await asyncio.wait_for(dispatcher.stop(), 1)
        assert captured.value is error
        assert dispatcher.failure is error

    asyncio.run(scenario())


def test_ambiguous_delivery_write_waits_30_seconds_before_rediscovery() -> None:
    async def scenario() -> None:
        failure = RuntimeError("private-persistence-detail")
        sends = 0

        class FailingRepository(_Repository):
            async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
                assert event_id == self.event.event_id
                assert delay_ms == 30_000
                raise failure

        repository = FailingRepository()

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            return 503

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        observed_waits: list[float] = []
        wait_entered = asyncio.Event()

        async def wait(seconds: float) -> None:
            observed_waits.append(seconds)
            wait_entered.set()
            await dispatcher._stop.wait()

        dispatcher._wait = wait  # type: ignore[method-assign]
        dispatcher.start()
        await asyncio.wait_for(wait_entered.wait(), 1)
        await asyncio.wait_for(dispatcher.stop(), 1)
        assert observed_waits == [30]
        assert sends == 1
        assert dispatcher.last_delivery_error is failure

    asyncio.run(scenario())
