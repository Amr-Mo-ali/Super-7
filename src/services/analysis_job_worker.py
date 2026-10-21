"""Inactive worker port and single-delegation services; no execution or persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from domain.analysis_job import _validate_datetime
from domain.analysis_job_attempt import (
    AttemptAuthority,
    AttemptFailureCode,
    AttemptStatus,
    LeaseDuration,
    RecoveryBatchSize,
    WorkerClaim,
    WorkerSlotId,
    _instant_key,
    _validate_authority,
    _validate_failure_code,
    _validate_positive_int,
)

__all__ = [
    "AnalysisJobWorkerPersistenceInvariantError",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerRepository",
    "AttemptExhausted",
    "AttemptFailed",
    "AttemptRequeued",
    "AttemptResolutionAlreadyApplied",
    "ClaimLost",
    "LeaseRenewed",
    "NoJobAvailable",
    "RecoverySummary",
    "WorkerSlotUnavailable",
    "claim_next_analysis_job",
    "finalize_retryable_failure",
    "finalize_terminal_failure",
    "recover_expired_analysis_jobs",
    "renew_analysis_job_lease",
]


class AnalysisJobWorkerPersistenceUnavailable(RuntimeError):
    """Expected worker persistence operation is unavailable."""


class AnalysisJobWorkerPersistenceInvariantError(RuntimeError):
    """Stored worker/job data violates the durable contract."""


@dataclass(frozen=True, slots=True)
class NoJobAvailable:
    """The slot is free but no eligible job was found."""


@dataclass(frozen=True, slots=True)
class WorkerSlotUnavailable:
    """The slot requires recovery; no mutation authority is granted."""

    worker_slot_id: WorkerSlotId

    def __post_init__(self) -> None:
        _validate_slot(self.worker_slot_id)


@dataclass(frozen=True, slots=True)
class ClaimLost:
    authority: AttemptAuthority

    def __post_init__(self) -> None:
        _validate_authority(self.authority)


@dataclass(frozen=True, slots=True)
class LeaseRenewed:
    authority: AttemptAuthority
    heartbeat_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _validate_authority(self.authority)
        _validate_datetime(self.heartbeat_at)
        _validate_datetime(self.lease_expires_at)
        if _instant_key(self.lease_expires_at) <= _instant_key(self.heartbeat_at):
            raise ValueError("Lease expiry must follow heartbeat.")


@dataclass(frozen=True, slots=True)
class AttemptRequeued:
    authority: AttemptAuthority
    attempt_limit: int

    def __post_init__(self) -> None:
        _validate_authority(self.authority)
        _validate_positive_int(self.attempt_limit)


@dataclass(frozen=True, slots=True)
class AttemptFailed:
    authority: AttemptAuthority
    failure_code: AttemptFailureCode

    def __post_init__(self) -> None:
        _validate_authority(self.authority)
        _validate_failure_code(self.failure_code)


@dataclass(frozen=True, slots=True)
class AttemptExhausted:
    authority: AttemptAuthority
    attempt_limit: int
    failure_code: AttemptFailureCode

    def __post_init__(self) -> None:
        _validate_authority(self.authority)
        _validate_positive_int(self.attempt_limit)
        _validate_failure_code(self.failure_code)


@dataclass(frozen=True, slots=True)
class AttemptResolutionAlreadyApplied:
    authority: AttemptAuthority
    status: AttemptStatus
    failure_code: AttemptFailureCode

    def __post_init__(self) -> None:
        _validate_authority(self.authority)
        if not isinstance(self.status, AttemptStatus):
            raise TypeError("Resolution status must be an AttemptStatus.")
        if self.status not in (
            AttemptStatus.RETRYABLE_FAILURE,
            AttemptStatus.TERMINAL_FAILURE,
            AttemptStatus.EXHAUSTED,
        ):
            raise ValueError("Resolution status must be a classified outcome.")
        _validate_failure_code(self.failure_code)


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    requeued_count: int
    exhausted_count: int

    def __post_init__(self) -> None:
        for count in (self.requeued_count, self.exhausted_count):
            if type(count) is not int:
                raise TypeError("Recovery count must be an exact integer.")
            if count < 0:
                raise ValueError("Recovery count must not be negative.")


class FailureCallbackSnapshotV1Builder(Protocol):
    """Freeze the existing failure callback from authoritative job values."""

    def __call__(self, *, job_id: UUID, video_id: str, player_id: str, safe_code: str) -> bytes: ...


class AnalysisJobWorkerRepository(Protocol):
    """Transaction-owning port; callers supply no current time or runtime resources."""

    async def claim_next(
        self, *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
    ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable: ...

    async def renew_lease(
        self, *, authority: AttemptAuthority, lease_duration: LeaseDuration
    ) -> LeaseRenewed | ClaimLost: ...

    async def finalize_retryable_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost: ...

    async def finalize_terminal_failure(
        self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
    ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost: ...

    async def recover_expired(self, *, batch_size: RecoveryBatchSize) -> RecoverySummary: ...


async def claim_next_analysis_job(
    *,
    repository: AnalysisJobWorkerRepository,
    worker_slot_id: WorkerSlotId,
    lease_duration: LeaseDuration,
) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable:
    _validate_slot(worker_slot_id)
    _validate_duration(lease_duration)
    return await repository.claim_next(worker_slot_id=worker_slot_id, lease_duration=lease_duration)


async def renew_analysis_job_lease(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    lease_duration: LeaseDuration,
) -> LeaseRenewed | ClaimLost:
    _validate_authority(authority)
    _validate_duration(lease_duration)
    return await repository.renew_lease(authority=authority, lease_duration=lease_duration)


async def finalize_retryable_failure(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    failure_code: AttemptFailureCode,
) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost:
    _validate_authority(authority)
    _validate_failure_code(failure_code)
    return await repository.finalize_retryable_failure(
        authority=authority, failure_code=failure_code
    )


async def finalize_terminal_failure(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    failure_code: AttemptFailureCode,
) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost:
    _validate_authority(authority)
    _validate_failure_code(failure_code)
    return await repository.finalize_terminal_failure(
        authority=authority, failure_code=failure_code
    )


async def recover_expired_analysis_jobs(
    *, repository: AnalysisJobWorkerRepository, batch_size: RecoveryBatchSize
) -> RecoverySummary:
    if not isinstance(batch_size, RecoveryBatchSize):
        raise TypeError("Recovery batch must be a RecoveryBatchSize.")
    return await repository.recover_expired(batch_size=batch_size)


def _validate_slot(value: WorkerSlotId) -> None:
    if not isinstance(value, WorkerSlotId):
        raise TypeError("Worker slot must be a WorkerSlotId.")


def _validate_duration(value: LeaseDuration) -> None:
    if not isinstance(value, LeaseDuration):
        raise TypeError("Lease duration must be a LeaseDuration.")
