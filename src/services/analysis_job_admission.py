"""Inactive admission contracts for durable analysis jobs."""

from __future__ import annotations

import hashlib
import json
from typing import Final, Protocol
from uuid import UUID

from pydantic import HttpUrl, TypeAdapter, ValidationError

from domain.analysis_job import (
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
    NewAnalysisJob,
)

_FINGERPRINT_VERSION: Final = "super7.analysis-job-fingerprint.v1"
_CALLBACK_URL_ADAPTER: Final = TypeAdapter(HttpUrl)
_AnalysisJobAdmissionOutcome = (
    AnalysisJobAccepted
    | AnalysisJobExisting
    | AnalysisJobIdempotencyConflict
    | AnalysisJobCapacityRejected
)


class IdempotencyLookupProtectionFailure(Exception):
    """Expected operational failure at the raw-key protection boundary."""


class IdempotencyLookupProtector(Protocol):
    """Convert a caller-scoped raw key to protected lookup bytes."""

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes: ...


class AnalysisJobAdmissionRepository(Protocol):
    """Atomic durable admission port."""

    async def accept_or_get(
        self,
        candidate: NewAnalysisJob,
        *,
        max_queue_size: int,
    ) -> _AnalysisJobAdmissionOutcome: ...


def canonicalize_callback_url(callback_url: HttpUrl) -> HttpUrl:
    """Return an already validated callback URL after rejecting fragments."""
    if not isinstance(callback_url, HttpUrl):
        raise TypeError("Callback URL must be validated before canonicalization.")
    if callback_url.fragment is not None:
        raise ValueError("Callback URL must not contain a fragment.")
    return callback_url


def fingerprint_analysis_request(
    *,
    caller_scope: str,
    video_id: str,
    player_id: str,
    video_reference: str,
    callback_url: str,
) -> bytes:
    """Return the frozen binary SHA-256 fingerprint for one canonical request."""
    values = (caller_scope, video_id, player_id, video_reference, callback_url)
    if any(type(value) is not str for value in values):
        raise TypeError("Fingerprint inputs must be strings.")

    validated_callback = _validated_callback_url(callback_url)
    canonical_callback = str(canonicalize_callback_url(validated_callback))
    ordered_value = {
        "fingerprintVersion": _FINGERPRINT_VERSION,
        "callerScope": caller_scope,
        "videoId": video_id,
        "playerId": player_id,
        "videoReference": video_reference,
        "callbackUrl": canonical_callback,
    }
    serialized = json.dumps(
        ordered_value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    return hashlib.sha256(_strict_utf8(serialized)).digest()


def protect_idempotency_lookup(
    protector: IdempotencyLookupProtector,
    *,
    caller_scope: str,
    raw_idempotency_key: str,
) -> bytes:
    """Protect a raw key and expose only nonempty binary lookup material."""
    protection_failed = False
    try:
        protected = protector.protect(
            caller_scope=caller_scope,
            raw_idempotency_key=raw_idempotency_key,
        )
    except IdempotencyLookupProtectionFailure:
        protection_failed = True

    if protection_failed:
        raise ValueError("Idempotency lookup protection failed.")
    if type(protected) is not bytes:
        raise TypeError("Protected idempotency lookup must be binary.")
    if not protected:
        raise ValueError("Protected idempotency lookup must not be empty.")
    return protected


async def admit_analysis_job(
    *,
    repository: AnalysisJobAdmissionRepository,
    protector: IdempotencyLookupProtector,
    job_id: UUID,
    caller_scope: str,
    raw_idempotency_key: str,
    video_id: str,
    player_id: str,
    video_reference: str,
    callback_url: HttpUrl,
    max_queue_size: int,
) -> _AnalysisJobAdmissionOutcome:
    """Build a protected candidate and delegate one atomic admission operation."""
    if type(max_queue_size) is not int:
        raise TypeError("Maximum queue size must be an integer.")
    if max_queue_size <= 0:
        raise ValueError("Maximum queue size must be greater than zero.")

    _validate_admission_inputs(
        job_id=job_id,
        caller_scope=caller_scope,
        raw_idempotency_key=raw_idempotency_key,
        video_id=video_id,
        player_id=player_id,
        video_reference=video_reference,
    )
    canonical_callback = str(canonicalize_callback_url(callback_url))
    protected_lookup = protect_idempotency_lookup(
        protector,
        caller_scope=caller_scope,
        raw_idempotency_key=raw_idempotency_key,
    )
    request_fingerprint = fingerprint_analysis_request(
        caller_scope=caller_scope,
        video_id=video_id,
        player_id=player_id,
        video_reference=video_reference,
        callback_url=canonical_callback,
    )
    candidate = NewAnalysisJob(
        job_id=job_id,
        idempotency_lookup=protected_lookup,
        request_fingerprint=request_fingerprint,
        video_id=video_id,
        player_id=player_id,
        video_reference=video_reference,
        callback_url=canonical_callback,
    )
    return await repository.accept_or_get(candidate, max_queue_size=max_queue_size)


def _validate_admission_inputs(
    *,
    job_id: UUID,
    caller_scope: str,
    raw_idempotency_key: str,
    video_id: str,
    player_id: str,
    video_reference: str,
) -> None:
    if not isinstance(job_id, UUID):
        raise TypeError("Job ID must be a UUID.")
    values = (
        caller_scope,
        raw_idempotency_key,
        video_id,
        player_id,
        video_reference,
    )
    if any(type(value) is not str for value in values):
        raise TypeError("Admission identifiers must be strings.")


def _validated_callback_url(callback_url: str) -> HttpUrl:
    validation_failed = False
    validated: HttpUrl | None = None
    try:
        validated = _CALLBACK_URL_ADAPTER.validate_python(callback_url)
    except (UnicodeError, ValidationError):
        validation_failed = True

    if validation_failed or validated is None:
        raise ValueError("Callback URL is invalid.")
    return validated


def _strict_utf8(value: str) -> bytes:
    encoding_failed = False
    encoded: bytes | None = None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        encoding_failed = True

    if encoding_failed or encoded is None:
        raise ValueError("Fingerprint input is not valid UTF-8.")
    return encoded
