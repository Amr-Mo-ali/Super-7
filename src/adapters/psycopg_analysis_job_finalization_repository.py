"""Inactive fenced success finalization with one atomic J/A/R/O transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from adapters._analysis_job_persistence import (
    _acquisition_timeout,
    _Connection,
    _eligible,
    _equivalent,
    _insert_terminal_pair,
    _invalid,
    _millisecond_timeout,
    _Pool,
    _read_attempt,
    _read_job,
    _resolve,
    _run_transaction,
    _terminal_rows,
    _transition_job,
)
from domain.analysis_job import AnalysisJobState
from domain.analysis_job_attempt import AttemptAuthority, AttemptStatus
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    AnalysisJobFinalizationPersistenceInvariantError,
    AnalysisJobFinalizationPersistenceUnavailable,
    FinalizableAnalysisResult,
    _validate_success_input,
)
from services.analysis_job_worker import ClaimLost

__all__ = [
    "PsycopgAnalysisJobFinalizationRepository",
    "AnalysisJobFinalizationPersistenceUnavailable",
    "AnalysisJobFinalizationPersistenceInvariantError",
]


class PsycopgAnalysisJobFinalizationRepository:
    """One short transaction per call, without a UUID factory or runtime composition."""

    def __init__(
        self,
        *,
        pool: _Pool,
        connection_acquire_timeout_seconds: float,
        operation_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> None:
        acquire = _acquisition_timeout(connection_acquire_timeout_seconds)
        _millisecond_timeout(operation_timeout_ms)
        _millisecond_timeout(lock_timeout_ms)
        if operation_timeout_ms <= lock_timeout_ms:
            raise ValueError("Operation timeout must exceed lock timeout.")
        self._pool = pool
        self._acquire = acquire
        self._operation_timeout_ms = operation_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms

    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
        _, callback = _validate_success_input(authority, result)

        async def operation(
            connection: _Connection,
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            job = await _read_job(connection, authority.job_id)
            if job is None or job.count == 0:
                return ClaimLost(authority)
            old = await _read_attempt(connection, job, job.count)
            if old is None:
                _invalid()
            terminal = None
            if job.state in (AnalysisJobState.COMPLETED, AnalysisJobState.FAILED):
                terminal = await _terminal_rows(connection, job, old)
            if (
                callback["video_id"] != job.payload.video_id
                or callback["player_id"] != job.payload.player_id
            ):
                return ClaimLost(authority)
            if old.authority != authority:
                return ClaimLost(authority)
            if job.state is AnalysisJobState.COMPLETED:
                if (
                    old.status is not AttemptStatus.SUCCEEDED
                    or terminal is None
                    or terminal.terminal.result_schema_version != result.result_schema_version
                    or terminal.outbox.schema_version != result.callback_schema_version
                    or terminal.terminal.model_version != result.model_version
                    or job.payload.analysis_version != result.analysis_version
                ):
                    return ClaimLost(authority)
                if not await _equivalent(
                    connection,
                    job.payload.job_id,
                    result_json=result.result_json,
                    callback_json=result.callback_json,
                ):
                    return ClaimLost(authority)
                return AnalysisJobCompletionAlreadyApplied(
                    authority, terminal.terminal.finalized_at, terminal.outbox.event_id
                )
            if job.payload.analysis_version != result.analysis_version:
                return ClaimLost(authority)
            if not await _eligible(connection, job, old):
                return ClaimLost(authority)
            await _terminal_rows(connection, job, old, require_absent=True)
            changed = await _resolve(connection, job, old, AttemptStatus.SUCCEEDED, None)
            if changed is None:
                return ClaimLost(authority)
            terminal_job = await _transition_job(
                connection, job, changed, AnalysisJobState.COMPLETED
            )
            persisted = await _insert_terminal_pair(
                connection,
                terminal_job,
                changed,
                result_json=result.result_json,
                callback_json=result.callback_json,
                model_version=result.model_version,
                processing_duration_ms=result.processing_duration_ms,
                safe_failure_code=None,
                failure_origin=None,
            )
            return AnalysisJobCompleted(
                authority, persisted.terminal.finalized_at, persisted.outbox.event_id
            )

        return await self._run(operation)

    async def _run[T](self, operation: Callable[[_Connection], Awaitable[T]]) -> T:
        return await _run_transaction(
            self._pool,
            self._acquire,
            self._operation_timeout_ms,
            self._lock_timeout_ms,
            operation,
        )
