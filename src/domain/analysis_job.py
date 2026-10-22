"""Immutable values for durable AnalysisJob admission."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Final
from uuid import UUID

from pydantic import HttpUrl, TypeAdapter, ValidationError

from domain.video_integrity import VideoIntegrity

__all__ = [
    "AnalysisJob",
    "AnalysisJobAccepted",
    "AnalysisJobCapacityRejected",
    "AnalysisJobExisting",
    "AnalysisJobIdempotencyConflict",
    "AnalysisJobState",
    "NewAnalysisJob",
    "is_analysis_job_transition_allowed",
]

_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_ANALYSIS_VERSION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CALLBACK_URL_ADAPTER: Final = TypeAdapter(HttpUrl)


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
        (AnalysisJobState.RUNNING, AnalysisJobState.QUEUED),
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
    analysis_version: str
    state: AnalysisJobState
    attempt_limit: int
    accepted_at: datetime
    video_integrity: VideoIntegrity | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.job_id)
        _validate_request_text(
            self.video_id, self.player_id, self.video_reference, self.callback_url
        )
        _validate_policy(self.analysis_version, self.attempt_limit)
        _validate_datetime(self.accepted_at)
        _validate_video_integrity(self.video_integrity)


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
    analysis_version: str
    attempt_limit: int
    video_integrity: VideoIntegrity | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.job_id)
        _validate_request_text(
            self.video_id, self.player_id, self.video_reference, self.callback_url
        )
        _validate_policy(self.analysis_version, self.attempt_limit)
        if type(self.idempotency_lookup) is not bytes:
            raise TypeError("Idempotency lookup must be binary.")
        if not self.idempotency_lookup:
            raise ValueError("Idempotency lookup must not be empty.")
        if type(self.request_fingerprint) is not bytes:
            raise TypeError("Request fingerprint must be binary.")
        if len(self.request_fingerprint) != 32:
            raise ValueError("Request fingerprint must contain exactly 32 bytes.")
        _validate_video_integrity(self.video_integrity)


def _validate_video_integrity(value: VideoIntegrity | None) -> None:
    if value is not None and type(value) is not VideoIntegrity:
        raise TypeError("Video integrity must be a validated value.")


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

    def __post_init__(self) -> None:
        _validate_uuid(self.existing_job_id)


@dataclass(frozen=True, slots=True)
class AnalysisJobCapacityRejected:
    """No new queued job can be accepted at the configured capacity."""


def _validate_uuid(value: UUID) -> None:
    if type(value) is not UUID:
        raise TypeError("Job ID must be a UUID.")
    if value.int == 0:
        raise ValueError("Job ID must not be nil.")


def _validate_policy(analysis_version: str, attempt_limit: int) -> None:
    if type(analysis_version) is not str:
        raise TypeError("Admission identifiers must be strings.")
    if not _ANALYSIS_VERSION.fullmatch(analysis_version):
        raise ValueError("Analysis version must have bounded safe syntax.")
    if type(attempt_limit) is not int:
        raise TypeError("Policy value must be an exact integer.")
    if attempt_limit <= 0:
        raise ValueError("Policy value must be positive.")


def _validate_datetime(value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError("Timestamp must be a datetime.")
    valid_offset = False
    try:
        valid_offset = value.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        pass
    if not valid_offset:
        raise ValueError("Timestamp must have a valid timezone offset.")


def _validate_request_identifiers(video_id: str, player_id: str, video_reference: str) -> None:
    if any(type(value) is not str for value in (video_id, player_id, video_reference)):
        raise TypeError("Admission identifiers must be strings.")
    for value in (video_id, player_id):
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("child analysis identifiers must be safe")
    path = Path(video_reference)
    windows_path = PureWindowsPath(video_reference)
    if (
        not video_reference
        or video_reference != video_reference.strip()
        or "\x00" in video_reference
        or "/" in video_reference
        or "\\" in video_reference
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in path.parts
        or video_reference != path.name
    ):
        raise ValueError("video_reference must be a safe relative filename")


def _validate_callback_url(value: str) -> None:
    if type(value) is not str:
        raise TypeError("Admission identifiers must be strings.")
    validated: HttpUrl | None = None
    try:
        validated = _CALLBACK_URL_ADAPTER.validate_python(value)
    except (UnicodeError, ValidationError):
        pass
    # Raise outside the parser's handler so rejected text cannot survive in context.
    if validated is None:
        raise ValueError("Callback URL is invalid.")
    if validated.fragment is not None:
        raise ValueError("Callback URL must not contain a fragment.")
    if not value or value != str(validated):
        raise ValueError("Callback URL is invalid.")


def _validate_request_text(
    video_id: str, player_id: str, video_reference: str, callback_url: str
) -> None:
    if any(
        type(value) is not str for value in (video_id, player_id, video_reference, callback_url)
    ):
        raise TypeError("Admission identifiers must be strings.")
    _validate_request_identifiers(video_id, player_id, video_reference)
    _validate_callback_url(callback_url)
