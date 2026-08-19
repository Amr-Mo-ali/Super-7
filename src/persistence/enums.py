"""Stable persisted lifecycle values represented as database check constraints."""

from enum import StrEnum


class AnalysisStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CallbackStatus(StrEnum):
    NOT_READY = "NOT_READY"
    PENDING = "PENDING"
    RETRYING = "RETRYING"
    DELIVERED = "DELIVERED"
    EXHAUSTED = "EXHAUSTED"


class AttemptOutcome(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    INTERRUPTED = "INTERRUPTED"


class DispatchStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
