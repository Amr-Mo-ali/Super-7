"""Inactive, one-event PostgreSQL callback read and delivery-state writes."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from services.analysis_callback_delivery import StoredCallbackEvent


class _Cursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def close(self) -> None: ...


class _Connection(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> _Cursor: ...


class _Pool(Protocol):
    def connection(self, *, timeout_seconds: float) -> AbstractAsyncContextManager[_Connection]: ...


_LOAD = """SELECT o.callback_event_id, o.job_id, j.callback_url,
       o.payload_schema_version, o.payload_json, o.delivery_state,
       o.next_attempt_at, o.next_attempt_at <= clock_timestamp(),
       j.state, r.terminal_kind, a.status, j.video_id, j.player_id
FROM analysis_callback_outbox AS o
JOIN analysis_jobs AS j ON j.job_id = o.job_id
JOIN analysis_job_terminals AS r ON r.job_id = o.job_id
JOIN analysis_job_attempts AS a
    ON a.job_id = r.job_id AND a.attempt_number = r.attempt_number
WHERE o.callback_event_id = %s"""
_DELIVERED = """UPDATE analysis_callback_outbox
SET delivery_state = 'DELIVERED'
WHERE callback_event_id = %s AND delivery_state IN ('PENDING', 'RETRYING')
    AND next_attempt_at <= clock_timestamp()
RETURNING callback_event_id"""
_RETRY = """UPDATE analysis_callback_outbox
SET delivery_state = 'RETRYING',
    next_attempt_at = clock_timestamp() + %s::interval
WHERE callback_event_id = %s AND delivery_state IN ('PENDING', 'RETRYING')
    AND next_attempt_at <= clock_timestamp()
RETURNING callback_event_id"""


class PsycopgAnalysisCallbackDeliveryRepository:
    """Each method releases its connection before returning to the HTTP caller."""

    def __init__(self, pool: _Pool, *, connection_acquire_timeout_seconds: float) -> None:
        if not 0 < connection_acquire_timeout_seconds <= 60:
            raise ValueError("Connection acquisition timeout must be positive and bounded.")
        self._pool = pool
        self._acquire = connection_acquire_timeout_seconds

    async def load(self, event_id: UUID) -> StoredCallbackEvent | None:
        async with self._pool.connection(timeout_seconds=self._acquire) as connection:
            cursor = await connection.execute(_LOAD, (event_id,))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if row is None:
            return None
        if len(row) != 13:
            raise ValueError("Stored callback row has unexpected shape.")
        (
            stored_id,
            job_id,
            url,
            schema,
            payload,
            state,
            next_at,
            due,
            job_state,
            terminal_kind,
            attempt_status,
            video_id,
            player_id,
        ) = row
        if (
            type(stored_id) is not UUID
            or type(job_id) is not UUID
            or type(url) is not str
            or type(schema) is not str
            or type(payload) is not dict
            or type(state) is not str
            or not isinstance(next_at, datetime)
            or type(due) is not bool
            or type(job_state) is not str
            or type(terminal_kind) is not str
            or type(attempt_status) is not str
            or type(video_id) is not str
            or type(player_id) is not str
        ):
            raise ValueError("Stored callback row has invalid values.")
        return StoredCallbackEvent(
            stored_id,
            job_id,
            url,
            schema,
            payload,
            state,
            next_at,
            due,
            job_state,
            terminal_kind,
            attempt_status,
            video_id,
            player_id,
        )

    async def record_delivered(self, event_id: UUID) -> bool:
        async with self._pool.connection(timeout_seconds=self._acquire) as connection:
            cursor = await connection.execute(_DELIVERED, (event_id,))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return row == (event_id,)

    async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
        if type(delay_ms) is not int or not 0 < delay_ms <= 60_000:
            raise ValueError("Retry delay must be positive and bounded.")
        async with self._pool.connection(timeout_seconds=self._acquire) as connection:
            cursor = await connection.execute(_RETRY, (f"{delay_ms} milliseconds", event_id))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return row == (event_id,)
