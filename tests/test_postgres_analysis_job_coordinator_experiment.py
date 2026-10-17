"""Explicitly opted-in one-video/one-slot disposable PostgreSQL experiment."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from unittest.mock import patch
from uuid import uuid4

import pytest
from test_postgres_analysis_job_finalization_integration import _finalizer
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _repositories,
    _run_database,
)

from adapters.analysis_result_projection import convert_child_success
from config.debug import DebugSettings
from core.config import Settings
from domain.analysis_job import AnalysisJobAccepted, NewAnalysisJob
from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, WorkerSlotId
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    AnalysisJobSuccessFinalizationRepository,
    FinalizableAnalysisResult,
)
from services.analysis_job_worker import ClaimLost, LeaseRenewed
from services.process_analysis_pool import ProcessAnalysisPool, ValidatedChildSuccess
from services.process_contracts import (
    ChildAnalysisCancelled,
    ChildAnalysisRequest,
    ChildAnalysisResult,
    ParentCancelled,
    ParentFailure,
)

_ROOT = Path(__file__).resolve().parents[1]
_VIDEO = (_ROOT / "dataset/raw/social_media/video_003.mp4").resolve()
_MODEL = (_ROOT / "models/yolo11n.pt").resolve()
_IMAGE_OPT_IN = "SUPER7_ONE_JOB_EXPERIMENT_VIDEO"


class _RecordingFinalizer:
    def __init__(self, repository: AnalysisJobSuccessFinalizationRepository) -> None:
        self.repository = repository
        self.input: tuple[AttemptAuthority, FinalizableAnalysisResult] | None = None

    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
        self.input = (authority, result)
        return await self.repository.finalize_success(authority=authority, result=result)


class _ObservedChild:
    def __init__(self, pool: ProcessAnalysisPool) -> None:
        self.pool = pool
        self.running = False

    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled:
        self.running = True
        try:
            return await self.pool.execute_enveloped(request)
        finally:
            self.running = False


class _ExperimentCoordinator(Protocol):
    @property
    def outstanding_tasks(self) -> tuple[asyncio.Task[object], ...]: ...


class _ExperimentPool(Protocol):
    @property
    def outstanding_submissions(self) -> tuple[Future[ChildAnalysisResult], ...]: ...

    async def shutdown(self) -> None: ...


async def _cleanup_experiment(
    coordinator: _ExperimentCoordinator,
    executor: _ExperimentPool,
    *,
    drain_timeout_seconds: float = 60,
) -> None:
    """Observe task and submitted-work liveness, then attempt pool shutdown.

    Pool shutdown still uses wait=True and may remain unbounded for a running child.
    """
    primary = sys.exception()
    cleanup_errors: list[BaseException] = []

    def record(error: BaseException) -> None:
        # A cleanup failure observed under an active primary otherwise points
        # back to it through implicit context, making a cause-chain cycle.
        error.__context__ = None
        error.__cause__ = None
        cleanup_errors.append(error)

    try:
        outstanding = coordinator.outstanding_tasks
        if outstanding:
            _, pending = await asyncio.wait(outstanding, timeout=drain_timeout_seconds)
            if pending:
                raise TimeoutError("Experiment observers did not settle within the drain bound.")
    except BaseException as error:
        record(error)
    try:
        if executor.outstanding_submissions:
            raise AssertionError("Submitted analysis remains active after observer drain.")
    except BaseException as error:
        record(error)
    try:
        await executor.shutdown()
    except BaseException as error:
        record(error)
    try:
        if executor.outstanding_submissions:
            raise AssertionError("Submitted analysis remains active after pool shutdown.")
    except BaseException as error:
        record(error)

    if cleanup_errors:
        # Match the local sequential-cleanup rule: the last fatal cleanup
        # failure wins, while every other observed failure remains accessible.
        fatal_index = next(
            (
                index
                for index in range(len(cleanup_errors) - 1, -1, -1)
                if not isinstance(cleanup_errors[index], Exception)
            ),
            None,
        )
        if fatal_index is not None:
            fatal = cleanup_errors[fatal_index]
            related = (
                ([primary] if primary is not None else [])
                + cleanup_errors[:fatal_index]
                + cleanup_errors[fatal_index + 1 :]
            )
            if len(related) == 1:
                raise fatal from related[0]
            if related:
                raise fatal from BaseExceptionGroup("Experiment cleanup failures.", related)
            raise fatal
        cleanup = (
            cleanup_errors[0]
            if len(cleanup_errors) == 1
            else BaseExceptionGroup("Experiment cleanup failures.", cleanup_errors)
        )
        if primary is not None:
            raise primary from cleanup
        raise cleanup


class _CleanupCoordinator:
    def __init__(self, task: asyncio.Task[object] | None = None) -> None:
        self.task = task

    @property
    def outstanding_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return (self.task,) if self.task is not None and not self.task.done() else ()


class _CleanupPool:
    def __init__(
        self,
        *,
        submission: Future[ChildAnalysisResult] | None = None,
        release: asyncio.Event | None = None,
        shutdown_error: BaseException | None = None,
    ) -> None:
        self.submission = submission
        self.release = release
        self.shutdown_error = shutdown_error
        self.shutdown_calls = 0

    @property
    def outstanding_submissions(self) -> tuple[Future[ChildAnalysisResult], ...]:
        return (
            (self.submission,) if self.submission is not None and not self.submission.done() else ()
        )

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.release is not None:
            self.release.set()
        if self.submission is not None and not self.submission.done():
            self.submission.set_result(ChildAnalysisCancelled("analysis-1", 0))
        if self.shutdown_error is not None:
            raise self.shutdown_error


def test_cleanup_preserves_primary_and_attempts_shutdown_after_observer_timeout() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def blocked() -> object:
            await release.wait()
            return None

        observer = asyncio.create_task(blocked())
        submission: Future[ChildAnalysisResult] = Future()
        assert submission.set_running_or_notify_cancel()
        pool = _CleanupPool(submission=submission, release=release)
        primary = AssertionError("primary-test-failure")
        with pytest.raises(AssertionError) as captured:
            try:
                raise primary
            finally:
                await _cleanup_experiment(
                    _CleanupCoordinator(observer), pool, drain_timeout_seconds=0.01
                )
        assert captured.value is primary
        assert pool.shutdown_calls == 1
        assert not observer.cancelled()
        await asyncio.wait_for(observer, 1)
        assert submission.done()
        related = primary.__cause__
        assert isinstance(related, BaseExceptionGroup)
        assert len(related.exceptions) == 2
        assert isinstance(related.exceptions[0], TimeoutError)
        assert isinstance(related.exceptions[1], AssertionError)
        assert all(error.__context__ is None for error in related.exceptions)

    asyncio.run(scenario())


def test_cleanup_preserves_fatal_primary_and_fatal_cleanup_precedence() -> None:
    async def scenario() -> None:
        fatal_primary = KeyboardInterrupt("fatal-primary")
        ordinary_cleanup = RuntimeError("shutdown-failed")
        ordinary_pool = _CleanupPool(shutdown_error=ordinary_cleanup)
        with pytest.raises(KeyboardInterrupt) as captured_primary:
            try:
                raise fatal_primary
            finally:
                await _cleanup_experiment(_CleanupCoordinator(), ordinary_pool)
        assert captured_primary.value is fatal_primary
        assert captured_primary.value.__cause__ is ordinary_cleanup
        assert ordinary_cleanup.__context__ is None
        assert ordinary_pool.shutdown_calls == 1

        primary = AssertionError("primary-test-failure")
        fatal_cleanup = KeyboardInterrupt("fatal-shutdown")
        fatal_pool = _CleanupPool(shutdown_error=fatal_cleanup)
        with pytest.raises(KeyboardInterrupt) as captured_cleanup:
            try:
                raise primary
            finally:
                await _cleanup_experiment(_CleanupCoordinator(), fatal_pool)
        assert captured_cleanup.value is fatal_cleanup
        assert captured_cleanup.value.__cause__ is primary
        assert fatal_pool.shutdown_calls == 1

        first_fatal = KeyboardInterrupt("fatal-observation")
        later_fatal = SystemExit("fatal-shutdown")

        class FailingObservation:
            @property
            def outstanding_tasks(self) -> tuple[asyncio.Task[object], ...]:
                raise first_fatal

        later_pool = _CleanupPool(shutdown_error=later_fatal)
        with pytest.raises(SystemExit) as captured_later:
            try:
                raise primary
            finally:
                await _cleanup_experiment(FailingObservation(), later_pool)
        assert captured_later.value is later_fatal
        assert later_pool.shutdown_calls == 1
        related = captured_later.value.__cause__
        assert isinstance(related, BaseExceptionGroup)
        assert related.exceptions == (primary, first_fatal)
        assert first_fatal.__context__ is None

    asyncio.run(scenario())


def test_cleanup_failure_without_primary_is_not_suppressed() -> None:
    async def scenario() -> None:
        failure = RuntimeError("shutdown-failed")
        pool = _CleanupPool(shutdown_error=failure)
        with pytest.raises(RuntimeError) as captured:
            await _cleanup_experiment(_CleanupCoordinator(), pool)
        assert captured.value is failure
        assert pool.shutdown_calls == 1

        completed = _CleanupPool()
        await _cleanup_experiment(_CleanupCoordinator(), completed)
        assert completed.shutdown_calls == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_one_real_video_commits_one_result_and_replays_once(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if os.environ.get(_IMAGE_OPT_IN) != str(_VIDEO):
        pytest.skip("One-job experiment requires the exact owner-selected local video opt-in.")
    assert _VIDEO.is_file() and _MODEL.is_file()
    factory = _disposable_factory(request)
    with factory() as database:
        _prepare_database(database)
        _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
        database_settings = _current_settings(database.runtime_settings)

        async def experiment() -> None:
            async with _repositories(database_settings) as (admission, worker, pool):
                job_id = uuid4()
                candidate = NewAnalysisJob(
                    job_id=job_id,
                    idempotency_lookup=hashlib.sha256(job_id.bytes + b"lookup").digest(),
                    request_fingerprint=hashlib.sha256(job_id.bytes + b"fingerprint").digest(),
                    video_id="experiment_video_003",
                    player_id="experiment_player",
                    video_reference=_VIDEO.name,
                    callback_url="https://callback.invalid/one-job-experiment",
                    analysis_version="1.0.0",
                    attempt_limit=1,
                )
                accepted = await admission.accept_or_get(candidate, max_queue_size=1)
                assert isinstance(accepted, AnalysisJobAccepted)
                process_settings = Settings(
                    video_storage_root=str(_VIDEO.parent),
                    model_path=str(_MODEL),
                    ball_model_path=str(_MODEL),
                    debug_output_dir=str(tmp_path / "child-artifacts"),
                    debug=DebugSettings(enabled=False, retained_sessions=0),
                    analysis_version=candidate.analysis_version,
                )
                executor = ProcessAnalysisPool(process_settings)
                executor.start()
                observed_child = _ObservedChild(executor)
                original_renew = worker.renew_lease
                successful_periodic_renewals = 0

                async def observed_renew(
                    *, authority: AttemptAuthority, lease_duration: LeaseDuration
                ) -> LeaseRenewed | ClaimLost:
                    nonlocal successful_periodic_renewals
                    started_during_child = observed_child.running
                    renewal = await original_renew(
                        authority=authority, lease_duration=lease_duration
                    )
                    if started_during_child and isinstance(renewal, LeaseRenewed):
                        successful_periodic_renewals += 1
                    return renewal

                finalizer = _RecordingFinalizer(_finalizer(pool))
                raw_model_pair = f"{_MODEL}+{_MODEL}+bytetrack"
                coordinator = AnalysisJobCoordinator(
                    worker_repository=worker,
                    finalization_repository=finalizer,
                    child_executor=observed_child,
                    result_projector=convert_child_success,
                    worker_slot_id=WorkerSlotId(uuid4()),
                    model_catalog={raw_model_pair: "experiment_yolo11n_pair_v1"},
                    timing=CoordinatorTiming(
                        LeaseDuration(timedelta(seconds=180)),
                        heartbeat_seconds=20,
                        operation_watchdog_seconds=25,
                        jitter_seconds=10,
                        finalization_budget_seconds=90,
                        retry_delays_seconds=(2, 5),
                    ),
                )
                state_query = """SELECT j.state, j.attempt_count, j.analysis_version,
                                        a.attempt_number, a.worker_slot_id, a.lease_id,
                                        a.status, a.lease_expires_at, a.finished_at,
                                        r.terminal_kind, r.result_schema_version, r.result_json,
                                        r.processing_duration_ms, r.finalized_at,
                                        o.callback_event_id, o.payload_schema_version,
                                        o.payload_json, o.delivery_state, o.created_at
                                 FROM analysis_jobs AS j
                                 LEFT JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                                 LEFT JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                                 LEFT JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                                 WHERE j.job_id = %s"""
                try:
                    with patch.object(worker, "renew_lease", side_effect=observed_renew):
                        outcome = await asyncio.wait_for(coordinator.run_one(), timeout=600)
                    assert isinstance(outcome, AnalysisJobCompleted)
                    print(f"one_job_periodic_renewals_observed={successful_periodic_renewals}")
                    assert successful_periodic_renewals >= 1
                    assert finalizer.input is not None
                    authority, frozen = finalizer.input
                    rows = await _fetch_all(pool, state_query, (job_id,))
                    assert len(rows) == 1
                    (
                        state,
                        count,
                        version,
                        attempt,
                        slot,
                        lease,
                        status,
                        expiry,
                        finished,
                        terminal,
                        result_schema,
                        result_json,
                        terminal_duration,
                        terminal_finished,
                        event,
                        callback_schema,
                        callback_json,
                        delivery,
                        created,
                    ) = rows[0]
                    assert (state, count, version, attempt, slot, lease, status, expiry) == (
                        "COMPLETED",
                        1,
                        "1.0.0",
                        authority.attempt_number,
                        authority.worker_slot_id.value,
                        authority.lease_id.value,
                        "SUCCEEDED",
                        None,
                    )
                    assert terminal == "COMPLETED" and result_schema == "analysis_result_v1"
                    assert callback_schema == "analysis_callback_snapshot_v1"
                    assert delivery == "PENDING" and event == outcome.callback_event_id
                    assert finished == terminal_finished == created == outcome.finished_at
                    assert isinstance(result_json, dict) and isinstance(callback_json, dict)
                    assert terminal_duration == frozen.processing_duration_ms
                    pipeline_duration = result_json.get("processing_time_ms")
                    assert result_json["analysis_id"] == callback_json["request_id"] == str(job_id)
                    assert callback_json["video_id"] == candidate.video_id
                    assert callback_json["player_id"] == candidate.player_id
                    replay = await finalizer.repository.finalize_success(
                        authority=authority, result=frozen
                    )
                    assert replay == AnalysisJobCompletionAlreadyApplied(
                        authority, outcome.finished_at, outcome.callback_event_id
                    )
                    replayed_rows = await _fetch_all(pool, state_query, (job_id,))
                    assert replayed_rows == rows
                    assert replayed_rows[0][12] == terminal_duration
                    replayed_result_json = replayed_rows[0][11]
                    assert isinstance(replayed_result_json, dict)
                    assert replayed_result_json.get("processing_time_ms") == pipeline_duration
                    assert await _fetch_all(
                        pool,
                        """SELECT callback_event_id, delivery_state
                           FROM analysis_callback_outbox WHERE job_id = %s""",
                        (job_id,),
                    ) == [(event, "PENDING")]
                    print(
                        "one_job_evidence state=COMPLETED attempt=SUCCEEDED "
                        "terminal=COMPLETED outbox=PENDING replay=already_applied "
                        f"periodic_successful_renewals={successful_periodic_renewals} "
                        f"job_id={job_id} event_id={event}"
                    )
                except Exception as error:
                    try:
                        diagnostic = await _fetch_all(
                            pool,
                            """SELECT j.state, j.attempt_count,
                                      (SELECT count(*) FROM analysis_job_attempts
                                       WHERE job_id = j.job_id),
                                      (SELECT count(*) FROM analysis_job_terminals
                                       WHERE job_id = j.job_id),
                                      (SELECT count(*) FROM analysis_callback_outbox
                                       WHERE job_id = j.job_id)
                               FROM analysis_jobs AS j WHERE j.job_id = %s""",
                            (job_id,),
                        )
                        print(
                            f"one_job_failure error_type={type(error).__name__} "
                            f"periodic_successful_renewals={successful_periodic_renewals} "
                            f"job_id={job_id} J/A/R/O={diagnostic}"
                        )
                    except Exception as observation_error:
                        print(
                            f"one_job_failure error_type={type(error).__name__} "
                            f"observation_error_type={type(observation_error).__name__} "
                            f"periodic_successful_renewals={successful_periodic_renewals} "
                            f"job_id={job_id}"
                        )
                    raise
                finally:
                    await _cleanup_experiment(coordinator, executor)

        _run_database(experiment())
    assert factory.active_count == 0
