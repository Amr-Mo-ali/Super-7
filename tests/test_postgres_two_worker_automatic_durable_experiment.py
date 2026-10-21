"""Opt-in two-child, one-dispatcher experiment against disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Protocol, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import psutil  # type: ignore[import-untyped]
import pytest
from test_postgres_analysis_callback_delivery_experiment import (
    _delivery_grant,
    _delivery_privileges,
    _local_transport,
    _Receiver,
)
from test_postgres_analysis_job_coordinator_experiment import _MODEL
from test_postgres_analysis_job_worker_integration import (
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _run_database,
)
from test_postgres_automatic_durable_local_runtime import _app, _settings
from test_postgres_endpoint_callback_experiment import _STATE_QUERY

from adapters.analysis_callback_presentation import failure_callback_snapshot_v1
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from adapters.psycopg_analysis_job_worker_repository import PsycopgAnalysisJobWorkerRepository
from core.config import Settings
from core.database_config import DatabasePoolSettings, DatabaseSettings
from domain.analysis_job_attempt import (
    AttemptAuthority,
    LeaseDuration,
    RecoveryBatchSize,
    WorkerSlotId,
)
from services.analysis_job_coordinator import AnalysisJobCoordinator, CoordinatorTiming
from services.analysis_job_worker import ClaimLost, LeaseRenewed
from services.durable_local_runtime import DurableLocalRuntime
from services.durable_worker_loop import DurableWorkerLoop
from services.process_analysis_pool import ProcessAnalysisPool, ValidatedChildSuccess
from services.process_contracts import ChildAnalysisRequest, ParentCancelled, ParentFailure

_OPT_IN = "SUPER7_TWO_WORKER_EXPERIMENT"
_VIDEOS = (
    (
        Path(r"E:\super7\dataset\raw\social_media\video_003.mp4"),
        2_053_502,
        "86b9448e30105ad8f421c94f027b19c73cb96849aaa6420d4ba16f9386aea9ea",
    ),
    (
        Path(r"E:\super7\dataset\raw\social_media\video_014.mp4"),
        4_259_610,
        "f907677e5e43a7fad32d2eb23b5ca71c33bb36544506884c6bc1defeaeda1764",
    ),
)


@dataclass(frozen=True)
class _ChildObservation:
    job_id: UUID
    submitted_at: datetime
    returned_at: datetime
    processing_duration_ms: int


class _ObservedExecutor:
    """Retain parent submission bounds and the child-reported processing duration."""

    def __init__(self, pool: ProcessAnalysisPool) -> None:
        self.pool = pool
        self.observations: list[_ChildObservation] = []

    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled:
        submitted_at = datetime.now(UTC)
        result = await self.pool.execute_enveloped(request)
        returned_at = datetime.now(UTC)
        if isinstance(result, ValidatedChildSuccess):
            self.observations.append(
                _ChildObservation(
                    UUID(request.analysis_id),
                    submitted_at,
                    returned_at,
                    result.child.processing_duration_ms,
                )
            )
        return result


class _Renew(Protocol):
    async def __call__(
        self, *, authority: AttemptAuthority, lease_duration: LeaseDuration
    ) -> LeaseRenewed | ClaimLost: ...


class _SecondWorker:
    """Experiment-only second slot sharing the app's PostgreSQL pool, not its dispatcher."""

    def __init__(
        self, primary: DurableLocalRuntime, settings: Settings, database: DatabaseSettings
    ) -> None:
        acquire = database.pool.acquire_timeout_seconds
        self.repository = PsycopgAnalysisJobWorkerRepository(
            pool=primary._pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=5_000,
            lock_timeout_ms=1_000,
            lease_id_factory=uuid4,
            failure_snapshot_builder=failure_callback_snapshot_v1,
        )
        finalizer = PsycopgAnalysisJobFinalizationRepository(
            pool=primary._pool,
            connection_acquire_timeout_seconds=acquire,
            operation_timeout_ms=5_000,
            lock_timeout_ms=1_000,
        )
        self.executor = ProcessAnalysisPool(settings)
        self.observed = _ObservedExecutor(self.executor)
        self.coordinator = AnalysisJobCoordinator(
            worker_repository=self.repository,
            finalization_repository=finalizer,
            child_executor=self.observed,
            result_projector=convert_child_success,
            worker_slot_id=WorkerSlotId(uuid4()),
            model_catalog={f"{_MODEL}+{_MODEL}+bytetrack": "experiment_yolo11n_pair_v1"},
            timing=primary.coordinator._timing,
        )
        self.worker = DurableWorkerLoop(
            coordinator=self.coordinator,
            recovery_repository=self.repository,
            recovery_batch_size=RecoveryBatchSize(2),
            idle_wait_seconds=0.1,
            logger=logging.getLogger("football_analysis.tests.two_worker.second"),
        )

    async def start(self) -> None:
        self.executor.start()
        try:
            self.worker.start()
        except BaseException:
            await self.executor.shutdown()
            raise

    async def stop(self) -> None:
        primary = sys.exception()
        errors: list[BaseException] = []

        def record(error: BaseException) -> None:
            # A caught cleanup error can implicitly point back to the active
            # primary. Remove only that link before reporting it as a cause.
            if error.__cause__ is primary:
                error.__cause__ = None
            if error.__context__ is primary:
                error.__context__ = None
            errors.append(error)

        try:
            await self.worker.stop()
        except BaseException as error:
            record(error)
        try:
            outstanding = self.coordinator.outstanding_tasks
            if outstanding:
                _, pending = await asyncio.wait(outstanding, timeout=60)
                if pending:
                    raise TimeoutError(
                        "Experiment observers did not settle within the drain bound."
                    )
        except BaseException as error:
            record(error)
        try:
            if self.executor.outstanding_submissions:
                raise AssertionError("Submitted analysis remains active after observer drain.")
        except BaseException as error:
            record(error)
        try:
            await self.executor.shutdown()
        except BaseException as error:
            record(error)
        try:
            if self.executor.outstanding_submissions:
                raise AssertionError("Submitted analysis remains active after pool shutdown.")
        except BaseException as error:
            record(error)

        if not errors:
            return
        # Match the established sequential cleanup precedence: the last
        # observed fatal wins, with every other failure retained as evidence.
        fatal_index = next(
            (
                index
                for index in range(len(errors) - 1, -1, -1)
                if not isinstance(errors[index], Exception)
            ),
            None,
        )
        if fatal_index is not None:
            fatal = errors[fatal_index]
            related = ([primary] if primary is not None else []) + (
                errors[:fatal_index] + errors[fatal_index + 1 :]
            )
            for linked in (fatal.__cause__, fatal.__context__):
                if (
                    linked is not None
                    and linked is not primary
                    and all(observed is not linked for observed in related)
                ):
                    related.append(linked)
            if len(related) == 1:
                raise fatal from related[0]
            if related:
                raise fatal from BaseExceptionGroup("Second-worker cleanup failures.", related)
            raise fatal
        related = list(errors)
        if primary is not None:
            for linked in (primary.__cause__, primary.__context__):
                if (
                    linked is not None
                    and linked is not primary
                    and all(observed is not linked for observed in related)
                ):
                    related.append(linked)
        cleanup = (
            related[0]
            if len(related) == 1
            else BaseExceptionGroup("Second-worker cleanup failures.", related)
        )
        if primary is not None:
            raise primary from cleanup
        raise cleanup


def _assert_authority_continuity(
    active_rows: list[tuple[object, ...]], terminal_row: tuple[object, ...]
) -> None:
    """Match one job's full persisted active fence to its terminal attempt."""
    matching = [row for row in active_rows if row[0] == terminal_row[0]]
    assert len(matching) == 1
    active = matching[0]
    assert active[4] == "ACTIVE"
    assert type(active[1]) is int and active[1] > 0
    assert all(
        type(value) is UUID and value.int != 0 for value in (active[0], active[2], active[3])
    )
    assert active[:4] == (terminal_row[0], terminal_row[8], terminal_row[9], terminal_row[10])


def _child_pids(pool: ProcessAnalysisPool) -> tuple[int, ...]:
    executor = pool._executor
    processes = getattr(executor, "_processes", {}) if executor is not None else {}
    return tuple(cast(int, pid) for pid in processes)


class _ResourceSamples:
    def __init__(self, first: ProcessAnalysisPool, second: ProcessAnalysisPool) -> None:
        self.pools = (first, second)
        self.parent = psutil.Process(os.getpid())
        self.samples: list[tuple[datetime, int, tuple[tuple[int, int, float], ...], bool]] = []

    def sample(self) -> None:
        children: list[tuple[int, int, float]] = []
        for pool in self.pools:
            for pid in _child_pids(pool):
                try:
                    process = psutil.Process(pid)
                    cpu = process.cpu_times()
                    children.append((pid, process.memory_info().rss, cpu.user + cpu.system))
                except psutil.Error:
                    pass
        try:
            parent_rss = self.parent.memory_info().rss
        except psutil.Error:
            parent_rss = 0
        both_submitted = all(pool.outstanding_submissions for pool in self.pools)
        self.samples.append((datetime.now(UTC), parent_rss, tuple(children), both_submitted))

    def overlapping_cpu_samples(self) -> int:
        count = 0
        for previous, current in zip(self.samples, self.samples[1:], strict=False):
            if not previous[3] or not current[3]:
                continue
            before = {pid: cpu for pid, _, cpu in previous[2]}
            advances = [pid for pid, _, cpu in current[2] if cpu > before.get(pid, cpu)]
            if len(set(advances)) >= 2:
                count += 1
        return count


def _offline_database() -> DatabaseSettings:
    return DatabaseSettings(
        "127.0.0.1",
        5432,
        "unused",
        "unused",
        "unused",
        2,
        DatabasePoolSettings(0, 6, 5, 10, 5, 5),
        "sprint2_slice4_result_outbox",
    )


def _offline_workers() -> tuple[DurableLocalRuntime, _SecondWorker]:
    settings = _settings(Path(os.environ.get("TEMP", "C:/Temp")))
    database = _offline_database()
    primary = DurableLocalRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret="ephemeral-local-test-credential",
        idempotency_hmac_secret=b"test-local-hmac-secret-never-deployed".ljust(32, b"x"),
        attempt_limit=1,
        timing=CoordinatorTiming(LeaseDuration(timedelta(seconds=180)), 20, 25, 10, 90, (2, 5)),
        recovery_batch_size=RecoveryBatchSize(2),
        worker_idle_seconds=0.1,
        model_catalog={f"{_MODEL}+{_MODEL}+bytetrack": "experiment_yolo11n_pair_v1"},
        callback_transport=_local_transport,
        logger=logging.getLogger("football_analysis.tests.two_worker.offline"),
        operation_timeout_ms=5_000,
        lock_timeout_ms=1_000,
    )
    second = _SecondWorker(primary, settings, database)
    return primary, second


def test_second_worker_is_a_distinct_one_child_slot_without_a_second_dispatcher() -> None:
    primary, second = _offline_workers()
    assert second.coordinator._worker_slot_id != primary.coordinator._worker_slot_id
    assert second.executor is not primary._executor
    assert second.repository._pool is primary._pool
    assert primary.dispatcher is not None
    assert not hasattr(second, "dispatcher")


def test_second_worker_cleanup_preserves_primary_and_independent_stop_cause() -> None:
    _, second = _offline_workers()
    calls: list[str] = []
    independent = ValueError("independent cause")
    stop_error = RuntimeError("stop failed")
    stop_error.__cause__ = independent
    primary = AssertionError("primary assertion")

    async def stop() -> None:
        calls.append("stop")
        raise stop_error

    async def shutdown() -> None:
        calls.append("shutdown")

    async def scenario() -> None:
        with (
            patch.object(second.worker, "stop", new=stop),
            patch.object(second.executor, "shutdown", new=shutdown),
            pytest.raises(AssertionError) as captured,
        ):
            try:
                raise primary
            finally:
                await second.stop()
        assert captured.value is primary
        assert primary.__cause__ is stop_error
        assert stop_error.__cause__ is independent
        assert stop_error.__context__ is None

    asyncio.run(scenario())
    assert calls == ["stop", "shutdown"]


def test_second_worker_cleanup_retains_combined_failures_and_no_primary() -> None:
    _, second = _offline_workers()
    calls: list[str] = []
    stop_error = RuntimeError("stop failed")
    shutdown_error = ValueError("shutdown failed")
    primary = AssertionError("primary assertion")
    primary_cause = LookupError("independent primary cause")
    primary.__cause__ = primary_cause

    async def stop() -> None:
        calls.append("stop")
        raise stop_error

    async def shutdown() -> None:
        calls.append("shutdown")
        raise shutdown_error

    async def scenario() -> None:
        with (
            patch.object(second.worker, "stop", new=stop),
            patch.object(second.executor, "shutdown", new=shutdown),
        ):
            with pytest.raises(AssertionError) as captured:
                try:
                    raise primary
                finally:
                    await second.stop()
            assert captured.value is primary
            related = primary.__cause__
            assert isinstance(related, ExceptionGroup)
            assert related.exceptions == (stop_error, shutdown_error, primary_cause)
            assert all(error.__context__ is None for error in related.exceptions)
            with pytest.raises(ExceptionGroup) as no_primary:
                await second.stop()
            assert no_primary.value.exceptions == (stop_error, shutdown_error)

    asyncio.run(scenario())
    assert calls == ["stop", "shutdown", "stop", "shutdown"]


def test_second_worker_cleanup_last_fatal_precedes_primary_and_success_is_quiet() -> None:
    _, second = _offline_workers()
    calls: list[str] = []
    stop_fatal = KeyboardInterrupt("stop fatal")
    shutdown_fatal = SystemExit("shutdown fatal")
    independent = ValueError("independent fatal cause")
    shutdown_fatal.__cause__ = independent
    primary = AssertionError("primary assertion")

    async def stop() -> None:
        calls.append("stop")
        raise stop_fatal

    async def shutdown() -> None:
        calls.append("shutdown")
        raise shutdown_fatal

    async def success() -> None:
        calls.append("success")

    async def scenario() -> None:
        with (
            patch.object(second.worker, "stop", new=stop),
            patch.object(second.executor, "shutdown", new=shutdown),
            pytest.raises(SystemExit) as captured,
        ):
            try:
                raise primary
            finally:
                await second.stop()
        assert captured.value is shutdown_fatal
        related = shutdown_fatal.__cause__
        assert isinstance(related, BaseExceptionGroup)
        assert related.exceptions == (primary, stop_fatal, independent)
        assert stop_fatal.__context__ is None
        with (
            patch.object(second.worker, "stop", new=success),
            patch.object(second.executor, "shutdown", new=success),
        ):
            await second.stop()

    asyncio.run(scenario())
    assert calls == ["stop", "shutdown", "success", "success"]


@pytest.mark.parametrize("changed", ("attempt_number", "lease_id"))
def test_authority_continuity_rejects_changed_fence_with_same_slot(changed: str) -> None:
    job_id, slot_id, lease_id = uuid4(), uuid4(), uuid4()
    other_job = (uuid4(), 1, uuid4(), uuid4(), "ACTIVE")
    active = (job_id, 1, slot_id, lease_id, "ACTIVE")
    terminal: list[object] = [None] * 25
    terminal[0], terminal[8], terminal[9], terminal[10] = active[:4]
    _assert_authority_continuity([other_job, active], tuple(terminal))
    if changed == "attempt_number":
        terminal[8] = 2
    else:
        terminal[10] = uuid4()
    assert terminal[9] == slot_id
    with pytest.raises(AssertionError):
        _assert_authority_continuity([other_job, active], tuple(terminal))


@pytest.mark.integration
def test_two_automatic_workers_overlap_and_deliver_through_one_dispatcher(
    request: pytest.FixtureRequest,
) -> None:
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip("Two-worker real-video experiment needs explicit opt-in.")
    tmp_path = cast(Path, request.getfixturevalue("tmp_path"))
    for video, expected_size, expected_hash in _VIDEOS:
        assert video.is_file() and video.stat().st_size == expected_size
        assert hashlib.sha256(video.read_bytes()).hexdigest() == expected_hash
    assert _MODEL.is_file()

    factory = _disposable_factory(request)
    receiver = _Receiver()
    wall_started = monotonic()
    try:
        with factory() as disposable:
            _prepare_database(disposable)
            _grant_runtime_worker_privileges(
                disposable.migration_settings, disposable.runtime_settings
            )
            _delivery_grant(disposable.migration_settings, disposable.runtime_settings, True)
            _delivery_privileges(disposable.runtime_settings, update=True)
            database = _current_settings(disposable.runtime_settings)
            settings = _settings(tmp_path)

            async def experiment() -> None:
                app, primary = _app(settings, database, receiver)
                second = _SecondWorker(primary, settings, database)
                first_observed = _ObservedExecutor(primary._executor)
                primary.coordinator._child_executor = first_observed
                renewals: dict[UUID, int] = {}
                original_first = primary.coordinator._worker_repository.renew_lease
                original_second = second.repository.renew_lease

                def observe_renew(original: _Renew, pool: ProcessAnalysisPool) -> _Renew:
                    async def call(
                        *, authority: AttemptAuthority, lease_duration: LeaseDuration
                    ) -> LeaseRenewed | ClaimLost:
                        outcome = await original(authority=authority, lease_duration=lease_duration)
                        if pool.outstanding_submissions and isinstance(outcome, LeaseRenewed):
                            renewals[authority.job_id] = renewals.get(authority.job_id, 0) + 1
                        return outcome

                    return call

                with (
                    patch.object(
                        primary.coordinator._worker_repository,
                        "renew_lease",
                        side_effect=observe_renew(original_first, primary._executor),
                    ),
                    patch.object(
                        second.repository,
                        "renew_lease",
                        side_effect=observe_renew(original_second, second.executor),
                    ),
                ):
                    async with app.router.lifespan_context(app):
                        await second.start()
                        resources = _ResourceSamples(primary._executor, second.executor)
                        try:
                            async with httpx.AsyncClient(
                                transport=httpx.ASGITransport(app=app), base_url="http://test"
                            ) as client:
                                responses = await asyncio.gather(
                                    *(
                                        client.post(
                                            "/analyze",
                                            headers={
                                                "Authorization": "Bearer ephemeral-local-test-credential",
                                                "Idempotency-Key": f"apex-two-worker-{number}",
                                            },
                                            json={
                                                "videoId": f"two_worker_video_{number}",
                                                "playerId": "two_worker_player",
                                                "videoUrl": video.name,
                                                "callbackUrl": receiver.url,
                                            },
                                        )
                                        for number, (video, _, _) in enumerate(_VIDEOS, 1)
                                    )
                                )
                            assert [response.status_code for response in responses] == [202, 202]
                            job_ids = tuple(
                                UUID(response.json()["analysisId"]) for response in responses
                            )
                            assert len(set(job_ids)) == 2
                            simultaneous: list[tuple[object, ...]] | None = None

                            async def completed() -> list[tuple[object, ...]]:
                                nonlocal simultaneous
                                while True:
                                    resources.sample()
                                    attempts = await _fetch_all(
                                        primary._pool,
                                        """SELECT a.job_id, a.attempt_number, a.worker_slot_id,
                                                  a.lease_id, a.status, a.started_at,
                                                  a.finished_at, j.state, clock_timestamp()
                                           FROM analysis_job_attempts AS a
                                           JOIN analysis_jobs AS j ON j.job_id = a.job_id
                                           WHERE a.job_id IN (%s, %s) ORDER BY a.job_id""",
                                        job_ids,
                                    )
                                    active = [row for row in attempts if row[4] == "ACTIVE"]
                                    assert len({row[0] for row in active}) == len(active)
                                    assert len({row[2] for row in active}) == len(active)
                                    if len(active) == 2 and simultaneous is None:
                                        simultaneous = attempts
                                    rows = [
                                        (await _fetch_all(primary._pool, _STATE_QUERY, (job_id,)))
                                        for job_id in job_ids
                                    ]
                                    if all(
                                        len(group) == 1 and group[0][22] == "DELIVERED"
                                        for group in rows
                                    ):
                                        return [group[0] for group in rows]
                                    for worker in (primary.worker, second.worker):
                                        if worker.failure is not None:
                                            raise worker.failure
                                    if primary.dispatcher.failure is not None:
                                        raise primary.dispatcher.failure
                                    await asyncio.sleep(0.25)

                            rows = await asyncio.wait_for(completed(), timeout=900)
                            assert simultaneous is not None, (
                                "No database observation found two ACTIVE attempts together."
                            )
                            assert {row[2] for row in simultaneous} == {
                                primary.coordinator._worker_slot_id.value,
                                second.coordinator._worker_slot_id.value,
                            }
                            assert all(
                                row[6] is None
                                and row[7] == "RUNNING"
                                and cast(datetime, row[5]) <= cast(datetime, row[8])
                                for row in simultaneous
                            )
                            observations = (
                                first_observed.observations + second.observed.observations
                            )
                            assert len(first_observed.observations) == 1
                            assert len(second.observed.observations) == 1
                            assert len(observations) == 2
                            assert {item.job_id for item in observations} == set(job_ids)
                            both_submitted_samples = sum(sample[3] for sample in resources.samples)
                            assert both_submitted_samples >= 1, (
                                "No sample found both submitted child futures outstanding."
                            )
                            assert all(renewals.get(job_id, 0) >= 1 for job_id in job_ids)
                            assert resources.overlapping_cpu_samples() >= 1, (
                                "Both child PIDs never advanced CPU time in the same sampled "
                                "interval while both submissions remained outstanding."
                            )
                            child_pids = (
                                _child_pids(primary._executor),
                                _child_pids(second.executor),
                            )
                            assert all(len(pids) == 1 for pids in child_pids)
                            assert child_pids[0] != child_pids[1]
                            assert max(item.submitted_at for item in observations) < min(
                                item.returned_at for item in observations
                            )
                            assert max(cast(datetime, entry[5]) for entry in simultaneous) < min(
                                cast(datetime, row[13]) for row in rows
                            )
                            assert len(receiver.requests) == 2
                            for number, (job_id, row) in enumerate(
                                zip(job_ids, rows, strict=True), 1
                            ):
                                assert row[:4] == (job_id, "COMPLETED", 1, "1.0.0")
                                assert row[4] == f"two_worker_video_{number}"
                                assert row[6] == _VIDEOS[number - 1][0].name
                                assert row[11] == "SUCCEEDED" and row[14] == "COMPLETED"
                                assert row[22] == "DELIVERED"
                                _assert_authority_continuity(simultaneous, row)
                                counts = await _fetch_all(
                                    primary._pool,
                                    """SELECT
                                        (SELECT count(*) FROM analysis_job_attempts
                                         WHERE job_id = %s),
                                        (SELECT count(*) FROM analysis_job_terminals
                                         WHERE job_id = %s),
                                        (SELECT count(*) FROM analysis_callback_outbox
                                         WHERE job_id = %s)""",
                                    (job_id, job_id, job_id),
                                )
                                assert counts == [(1, 1, 1)]
                                event_id = cast(UUID, row[19])
                                assert (
                                    receiver.applied[str(event_id)]
                                    == json.dumps(
                                        {
                                            **cast(Mapping[str, object], row[21]),
                                            "callbackEventId": str(event_id),
                                        },
                                        separators=(",", ":"),
                                        allow_nan=False,
                                    ).encode()
                                )
                                assert (
                                    sum(
                                        headers["X-Super7-Callback-Event-Id"] == str(event_id)
                                        for _, _, headers in receiver.requests
                                    )
                                    == 1
                                )
                                print(
                                    f"two_worker_job job_id={job_id} slot={row[9]} "
                                    f"event_id={event_id} J={row[1]} A={row[11]} "
                                    f"R={row[14]} O={row[22]} "
                                    f"processing_duration_ms={row[17]} "
                                    f"periodic_renewals={renewals[job_id]} "
                                    f"claim_started_at={next(entry[5] for entry in simultaneous if entry[0] == job_id)} "
                                    f"attempt_finished_at={row[13]}"
                                )
                            for item in observations:
                                print(
                                    f"two_worker_child job_id={item.job_id} "
                                    f"submitted_at={item.submitted_at} "
                                    f"returned_at={item.returned_at} "
                                    f"child_processing_duration_ms={item.processing_duration_ms}"
                                )
                            assert len(receiver.applied) == 2 and receiver.conflicts == 0
                            parent_peak = max(sample[1] for sample in resources.samples)
                            child_peak = max(
                                (
                                    sum(child[1] for child in sample[2])
                                    for sample in resources.samples
                                ),
                                default=0,
                            )
                            print(
                                f"two_worker_resource wall_seconds={monotonic() - wall_started:.2f} "
                                f"parent_rss_peak_bytes={parent_peak} "
                                f"summed_child_rss_peak_bytes={child_peak} "
                                f"overlapping_cpu_samples={resources.overlapping_cpu_samples()} "
                                f"both_submitted_samples={both_submitted_samples} "
                                f"child_pids={child_pids} "
                                f"poll_sleep_seconds=0.25 samples={len(resources.samples)}"
                            )
                        finally:
                            await second.stop()

            _run_database(experiment())
        assert factory.active_count == 0
        print(f"two_worker_total_wall_seconds={monotonic() - wall_started:.2f}")
    finally:
        primary_error = sys.exception()
        try:
            receiver.close()
        except BaseException as cleanup:
            if primary_error is not None and isinstance(cleanup, Exception):
                if cleanup.__context__ is primary_error:
                    cleanup.__context__ = None
                raise primary_error from cleanup
            raise
