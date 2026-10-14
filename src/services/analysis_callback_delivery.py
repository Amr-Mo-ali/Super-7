"""Inactive, single-actor delivery of one already-persisted callback event."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StoredCallbackEvent:
    event_id: UUID
    job_id: UUID
    callback_url: str
    payload_schema_version: str
    payload: dict[str, object]
    delivery_state: str
    next_attempt_at: datetime
    due: bool
    job_state: str
    terminal_kind: str
    attempt_status: str
    video_id: str
    player_id: str


class CallbackDeliveryRepository(Protocol):
    async def load(self, event_id: UUID) -> StoredCallbackEvent | None: ...

    async def record_delivered(self, event_id: UUID) -> bool: ...

    async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool: ...


CallbackTransport = Callable[[str, bytes, Mapping[str, str], float], Awaitable[int]]
DeliveryOutcome = Literal["NOT_FOUND", "NOT_DUE", "ALREADY_DELIVERED", "DELIVERED", "RETRYING"]


def _wire_body(event: StoredCallbackEvent) -> bytes:
    if type(event.event_id) is not UUID or event.event_id.int == 0:
        raise ValueError("Invalid stored callback event identity.")
    if type(event.job_id) is not UUID or event.job_id.int == 0:
        raise ValueError("Invalid stored callback job identity.")
    if event.payload_schema_version != "analysis_callback_snapshot_v1":
        raise ValueError("Unsupported stored callback schema.")
    if event.delivery_state not in {"PENDING", "RETRYING", "DELIVERED"}:
        raise ValueError("Unsupported stored callback delivery state.")
    if event.job_state != event.terminal_kind or event.job_state not in {"COMPLETED", "FAILED"}:
        raise ValueError("Stored callback has no matching terminal job.")
    if (event.job_state, event.attempt_status) not in {
        ("COMPLETED", "SUCCEEDED"),
        ("FAILED", "TERMINAL_FAILURE"),
        ("FAILED", "EXHAUSTED"),
    }:
        raise ValueError("Stored callback has no matching terminal attempt.")
    if type(event.callback_url) is not str or not event.callback_url:
        raise ValueError("Stored callback destination is invalid.")
    if type(event.payload) is not dict or "callbackEventId" in event.payload:
        raise ValueError("Stored callback payload is invalid.")
    callback_status = event.payload.get("status")
    if (
        event.payload.get("request_id") != str(event.job_id)
        or event.payload.get("video_id") != event.video_id
        or event.payload.get("player_id") != event.player_id
        or type(callback_status) is not str
        or not callback_status
        or (event.job_state == "FAILED" and callback_status != "failed")
    ):
        raise ValueError("Stored callback payload does not match its job.")
    return json.dumps(
        {**event.payload, "callbackEventId": str(event.event_id)},
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


async def deliver_one(
    *,
    repository: CallbackDeliveryRepository,
    transport: CallbackTransport,
    event_id: UUID,
    timeout_seconds: float,
    retry_delay_ms: int,
) -> DeliveryOutcome:
    """Make at most one HTTP attempt; never hold a repository transaction across it.

    The caller owns the exclusive actor and must inject a destination-safe transport.
    There is deliberately no dispatcher, durable retry counter, or URL exception here.
    """
    if type(event_id) is not UUID or event_id.int == 0:
        raise ValueError("Event ID must be a nonnil UUID.")
    if type(timeout_seconds) not in {float, int} or not 0 < timeout_seconds <= 60:
        raise ValueError("Callback timeout must be positive and bounded.")
    if type(retry_delay_ms) is not int or not 0 < retry_delay_ms <= 60_000:
        raise ValueError("Callback retry delay must be positive and bounded.")

    event = await repository.load(event_id)
    if event is None:
        return "NOT_FOUND"
    if event.event_id != event_id:
        raise ValueError("Stored callback event identity changed.")
    body = _wire_body(event)
    if event.delivery_state == "DELIVERED":
        return "ALREADY_DELIVERED"
    if not event.due:
        return "NOT_DUE"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Super7-Callback-Event-Id": str(event.event_id),
    }
    try:
        status = await asyncio.wait_for(
            transport(event.callback_url, body, headers, float(timeout_seconds)),
            timeout=float(timeout_seconds),
        )
    except (OSError, TimeoutError):
        status = 0
    if type(status) is not int or not 0 <= status <= 599:
        raise ValueError("Callback transport returned an invalid status.")
    if 200 <= status < 300:
        if not await repository.record_delivered(event.event_id):
            raise RuntimeError("Callback delivery state changed before acknowledgement persisted.")
        return "DELIVERED"
    if not await repository.record_retry(event.event_id, delay_ms=retry_delay_ms):
        raise RuntimeError("Callback delivery state changed before retry persisted.")
    return "RETRYING"
