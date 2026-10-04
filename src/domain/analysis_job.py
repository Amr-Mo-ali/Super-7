"""Immutable values for durable AnalysisJob admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID


class AnalysisJobState(StrEnum):
    """Durable analysis lifecycle states."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_ALLOWED_TRANSITIONS: Final = frozenset(
    {
        (AnalysisJobState.QUEUED, AnalysisJobState.RUNNING),
        (AnalysisJobState.QUEUED, AnalysisJobState.CANCELLED),
        (AnalysisJobState.RUNNING, AnalysisJobState.COMPLETED),
        (AnalysisJobState.RUNNING, AnalysisJobState.FAILED),
        (AnalysisJobState.RUNNING, AnalysisJobState.CANCELLED),
    }
)


def is_analysis_job_transition_allowed(
    source: AnalysisJobState,
    target: AnalysisJobState,
) -> bool:
    """Return whether one durable lifecycle transition is permitted."""
    return (source, target) in _ALLOWED_TRANSITIONS


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    """Immutable durable job value returned by admission."""

    job_id: UUID
    video_id: str
    player_id: str
    video_reference: str
    callback_url: str
    state: AnalysisJobState
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class NewAnalysisJob:
    """Validated candidate containing protected lookup material only."""

    job_id: UUID
    idempotency_lookup: bytes
    request_fingerprint: bytes
    video_id: str
    player_id: str
    video_reference: str
    callback_url: str

    def __post_init__(self) -> None:
        if type(self.idempotency_lookup) is not bytes:
            raise TypeError("Idempotency lookup must be binary.")
        if not self.idempotency_lookup:
            raise ValueError("Idempotency lookup must not be empty.")
        if type(self.request_fingerprint) is not bytes:
            raise TypeError("Request fingerprint must be binary.")
        if len(self.request_fingerprint) != 32:
            raise ValueError("Request fingerprint must contain exactly 32 bytes.")


@dataclass(frozen=True, slots=True)
class AnalysisJobAccepted:
    """A new durable job was accepted."""

    job: AnalysisJob


@dataclass(frozen=True, slots=True)
class AnalysisJobExisting:
    """An identical idempotent request resolved to its existing job."""

    job: AnalysisJob


@dataclass(frozen=True, slots=True)
class AnalysisJobIdempotencyConflict:
    """An idempotency lookup already belongs to a different request."""

    existing_job_id: UUID


@dataclass(frozen=True, slots=True)
class AnalysisJobCapacityRejected:
    """No new queued job can be accepted at the configured capacity."""
