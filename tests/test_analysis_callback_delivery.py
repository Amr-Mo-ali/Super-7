"""Database-free controls for the inactive one-event delivery boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from services.analysis_callback_delivery import CallbackTransport, StoredCallbackEvent, deliver_one

_EVENT_ID = UUID(int=41)
_JOB_ID = UUID(int=42)


def _event(**changes: object) -> StoredCallbackEvent:
    event = StoredCallbackEvent(
        event_id=_EVENT_ID,
        job_id=_JOB_ID,
        callback_url="https://callback.example.test/result",
        payload_schema_version="analysis_callback_snapshot_v1",
        payload={
            "request_id": str(_JOB_ID),
            "video_id": "video-1",
            "player_id": "player-1",
            "status": "COMPLETED",
            "summary": {"preserved": True},
            "ratings": {"overall": None},
            "events": {},
        },
        delivery_state="PENDING",
        next_attempt_at=datetime.now(UTC),
        due=True,
        job_state="COMPLETED",
        terminal_kind="COMPLETED",
        attempt_status="SUCCEEDED",
        video_id="video-1",
        player_id="player-1",
    )
    return replace(event, **cast(Any, changes))


async def _send(repo: _Repository, transport: CallbackTransport) -> str:
    return await deliver_one(
        repository=repo,
        transport=transport,
        event_id=_EVENT_ID,
        timeout_seconds=1,
        retry_delay_ms=100,
    )


class _Repository:
    def __init__(self, event: StoredCallbackEvent | None) -> None:
        self.event = event
        self.active = False
        self.writes: list[str] = []
        self.fail_write = False

    async def load(self, event_id: UUID) -> StoredCallbackEvent | None:
        assert not self.active and event_id == _EVENT_ID
        self.active = True
        try:
            return self.event
        finally:
            self.active = False

    async def record_delivered(self, event_id: UUID) -> bool:
        assert not self.active and event_id == _EVENT_ID
        if self.fail_write:
            raise RuntimeError("persistence-failed")
        self.writes.append("DELIVERED")
        assert self.event is not None
        self.event = replace(self.event, delivery_state="DELIVERED")
        return True

    async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
        assert not self.active and event_id == _EVENT_ID and delay_ms == 100
        if self.fail_write:
            raise RuntimeError("persistence-failed")
        self.writes.append("RETRYING")
        assert self.event is not None
        self.event = replace(
            self.event,
            delivery_state="RETRYING",
            due=False,
            next_attempt_at=self.event.next_attempt_at + timedelta(milliseconds=delay_ms),
        )
        return True


def test_one_attempt_exact_wire_retry_due_and_no_transaction_during_transport() -> None:
    async def scenario() -> None:
        repo = _Repository(_event())
        received: list[tuple[str, dict[str, object], dict[str, str]]] = []

        async def transport(url: str, body: bytes, headers: object, timeout: float) -> int:
            assert not repo.active and timeout == 1
            assert isinstance(headers, dict)
            received.append((url, json.loads(body), headers))
            return 503 if len(received) == 1 else 204

        assert await _send(repo, transport) == "RETRYING"
        assert repo.writes == ["RETRYING"]
        assert await _send(repo, transport) == "NOT_DUE"
        assert len(received) == 1
        assert repo.event is not None
        repo.event = replace(repo.event, due=True)
        assert await _send(repo, transport) == "DELIVERED"
        assert await _send(repo, transport) == "ALREADY_DELIVERED"
        assert repo.writes == ["RETRYING", "DELIVERED"]
        assert len(received) == 2
        assert received[0] == received[1]
        url, body, headers = received[0]
        assert url == repo.event.callback_url
        assert body == {**_event().payload, "callbackEventId": str(_EVENT_ID)}
        assert headers == {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Super7-Callback-Event-Id": str(_EVENT_ID),
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"payload_schema_version": "unknown"},
        {"job_state": "RUNNING"},
        {"attempt_status": "ACTIVE"},
        {"payload": {"request_id": str(_JOB_ID), "callbackEventId": "wrong"}},
        {"payload": {"request_id": "wrong"}},
        {"delivery_state": "EXHAUSTED"},
    ],
)
def test_invalid_stored_event_never_sends(change: dict[str, object]) -> None:
    async def scenario() -> None:
        repo = _Repository(_event(**change))

        async def forbidden(*args: object) -> int:
            raise AssertionError("invalid event reached transport")

        with pytest.raises(ValueError):
            await _send(repo, forbidden)
        assert repo.writes == []

    asyncio.run(scenario())


def test_ambiguous_ack_and_persistence_failure_reuse_one_event() -> None:
    async def scenario() -> None:
        repo = _Repository(_event())
        applied: dict[str, bytes] = {}
        deliveries = 0

        async def receiver(url: str, body: bytes, headers: object, timeout: float) -> int:
            nonlocal deliveries
            deliveries += 1
            key = str(_EVENT_ID)
            assert applied.setdefault(key, body) == body
            if deliveries == 1:
                raise TimeoutError("acknowledgement lost")
            return 200

        assert await _send(repo, receiver) == "RETRYING"
        assert repo.event is not None
        repo.event = replace(repo.event, due=True)
        repo.fail_write = True
        with pytest.raises(RuntimeError, match="persistence-failed"):
            await _send(repo, receiver)
        assert repo.event.delivery_state == "RETRYING"
        repo.fail_write = False
        assert await _send(repo, receiver) == "DELIVERED"
        assert deliveries == 3 and len(applied) == 1

    asyncio.run(scenario())


def test_completed_job_can_deliver_noncompleted_pipeline_status() -> None:
    async def scenario() -> None:
        payload = {**_event().payload, "status": "no_players_detected"}
        repo = _Repository(_event(payload=payload))

        async def transport(url: str, body: bytes, headers: object, timeout: float) -> int:
            assert json.loads(body)["status"] == "no_players_detected"
            return 204

        assert await _send(repo, transport) == "DELIVERED"

    asyncio.run(scenario())


def test_unexpected_and_fatal_transport_errors_keep_identity() -> None:
    async def scenario(error: BaseException) -> None:
        repo = _Repository(_event())

        async def failing(*args: object) -> int:
            raise error

        with pytest.raises(type(error)) as captured:
            await _send(repo, failing)
        assert captured.value is error
        assert repo.writes == []

    asyncio.run(scenario(ValueError("unexpected")))
    asyncio.run(scenario(KeyboardInterrupt("fatal")))


def test_psycopg_boundary_releases_connection_before_http_and_updates_only_state() -> None:
    class Cursor:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[object, ...] | None:
            return self.row

        async def close(self) -> None:
            pass

    class Pool:
        def __init__(self) -> None:
            self.active = False
            self.queries: list[str] = []

        @asynccontextmanager
        async def connection(self, *, timeout_seconds: float) -> AsyncIterator[Pool]:
            assert timeout_seconds == 5 and not self.active
            self.active = True
            try:
                yield self
            finally:
                self.active = False

        async def execute(self, query: str, params: tuple[object, ...]) -> Cursor:
            self.queries.append(query)
            assert self.active
            assert params == (_EVENT_ID,)
            if query.startswith("SELECT"):
                event = _event()
                return Cursor(
                    (
                        event.event_id,
                        event.job_id,
                        event.callback_url,
                        event.payload_schema_version,
                        event.payload,
                        event.delivery_state,
                        event.next_attempt_at,
                        event.due,
                        event.job_state,
                        event.terminal_kind,
                        event.attempt_status,
                        event.video_id,
                        event.player_id,
                    )
                )
            return Cursor((_EVENT_ID,))

    async def scenario() -> None:
        pool = Pool()
        repo = PsycopgAnalysisCallbackDeliveryRepository(pool, connection_acquire_timeout_seconds=5)

        async def transport(url: str, body: bytes, headers: object, timeout: float) -> int:
            assert not pool.active
            assert json.loads(body)["callbackEventId"] == str(_EVENT_ID)
            return 204

        assert (
            await deliver_one(
                repository=repo,
                transport=transport,
                event_id=_EVENT_ID,
                timeout_seconds=1,
                retry_delay_ms=100,
            )
            == "DELIVERED"
        )
        assert not pool.active
        assert len(pool.queries) == 2
        assert "JOIN analysis_jobs" in pool.queries[0]
        assert "JOIN analysis_job_terminals" in pool.queries[0]
        assert "SET delivery_state = 'DELIVERED'" in pool.queries[1]
        assert "payload_json" not in pool.queries[1]
        assert "next_attempt_at =" not in pool.queries[1]

    asyncio.run(scenario())
