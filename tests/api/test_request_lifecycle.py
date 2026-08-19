"""Deterministic tests for local analysis lifecycle coordination."""

import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import pytest

from api.request_lifecycle import RequestLifecycle
from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationChecker, CancellationManager, CancellationState
from concurrency.exceptions import AdmissionRejectedError, AnalysisCancelled
from concurrency.executor import AnalysisExecutor, current_request_id
from diagnostics.artifacts import ArtifactManager, ArtifactSession, CleanupResult


class FailOnceArtifactManager(ArtifactManager):
    def __init__(self, root: Path) -> None:
        super().__init__(root, max_session_bytes=1024)
        self._failed = False

    def create_session(self, request_id: str) -> ArtifactSession:
        if not self._failed:
            self._failed = True
            raise RuntimeError("artifact setup failed")
        return super().create_session(request_id)


class CleanupFailureArtifactManager(ArtifactManager):
    def _complete_session(self, session: ArtifactSession, retained: bool) -> CleanupResult:
        del session, retained
        raise RuntimeError("artifact cleanup failed")


def test_successful_execution_preserves_result_and_request_id() -> None:
    async def scenario() -> None:
        lifecycle = RequestLifecycle(AdmissionController(1), AnalysisExecutor())

        def pipeline(cancellation: CancellationManager) -> tuple[str | None, str, int]:
            return current_request_id(), cancellation.snapshot().request_id, 42

        assert await lifecycle.execute("analysis-1", pipeline) == ("analysis-1", "analysis-1", 42)

    asyncio.run(scenario())


def test_admission_failure_does_not_start_pipeline() -> None:
    async def scenario() -> None:
        controller = AdmissionController(1)
        held = await controller.admit()
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())
        started = False

        def pipeline(cancellation: CancellationManager) -> None:
            nonlocal started
            del cancellation
            started = True

        assert held is not None
        with pytest.raises(AdmissionRejectedError):
            await lifecycle.execute("analysis-2", pipeline)
        assert started is False
        await held.release()

    asyncio.run(scenario())


def test_pipeline_exception_propagates_unchanged_and_releases_permit() -> None:
    class PipelineFailure(Exception):
        pass

    async def scenario() -> None:
        controller = AdmissionController(1)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())

        def pipeline(cancellation: CancellationManager) -> None:
            del cancellation
            raise PipelineFailure("unchanged")

        with pytest.raises(PipelineFailure, match="unchanged"):
            await lifecycle.execute("analysis-3", pipeline)
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_cancellation_propagates_cooperatively_and_releases_permit() -> None:
    async def scenario() -> None:
        controller = AdmissionController(1)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())
        snapshots: list[CancellationState] = []

        def pipeline(cancellation: CancellationManager) -> None:
            cancellation.request_cancellation()
            snapshots.append(cancellation.snapshot().state)
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await lifecycle.execute("analysis-4", pipeline)
        assert snapshots == [CancellationState.CANCELLATION_REQUESTED]
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_cleanup_completes_cancellation_before_returning_result() -> None:
    async def scenario() -> None:
        controller = AdmissionController(1)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())
        cancellations: list[CancellationManager] = []

        def pipeline(cancellation: CancellationManager) -> str:
            cancellations.append(cancellation)
            return "completed"

        result = await lifecycle.execute("analysis-5", pipeline)

        assert result == "completed"
        assert cancellations[0].snapshot().state is CancellationState.COMPLETED
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_requests_do_not_share_cancellation_state() -> None:
    async def scenario() -> None:
        lifecycle = RequestLifecycle(AdmissionController(2), AnalysisExecutor())

        def pipeline(cancellation: CancellationManager) -> tuple[str, int]:
            return cancellation.snapshot().request_id, id(cancellation)

        first, second = await asyncio.gather(
            lifecycle.execute("analysis-a", pipeline),
            lifecycle.execute("analysis-b", pipeline),
        )
        assert first[0] == "analysis-a"
        assert second[0] == "analysis-b"
        assert first[1] != second[1]

    asyncio.run(scenario())


def test_concurrent_execution_respects_admitted_capacity() -> None:
    async def scenario() -> None:
        controller = AdmissionController(2)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())

        def pipeline(cancellation: CancellationManager) -> str:
            return cancellation.snapshot().request_id

        results = await asyncio.gather(
            lifecycle.execute("analysis-a", pipeline),
            lifecycle.execute("analysis-b", pipeline),
        )
        assert tuple(results) == ("analysis-a", "analysis-b")
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_lifecycle_matches_direct_pipeline_result() -> None:
    def pipeline(cancellation: CancellationManager) -> tuple[str, bool, tuple[int, ...]]:
        return cancellation.snapshot().request_id, cancellation.is_cancelled(), (1, 2, 3)

    direct = pipeline(CancellationManager("analysis-parity"))

    async def scenario() -> tuple[str, bool, tuple[int, ...]]:
        lifecycle = RequestLifecycle(AdmissionController(1), AnalysisExecutor())
        return await lifecycle.execute("analysis-parity", pipeline)

    assert asyncio.run(scenario()) == direct


def test_artifact_setup_failure_releases_permit_and_next_request_is_admitted() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            controller = AdmissionController(1)
            lifecycle = RequestLifecycle(
                controller, AnalysisExecutor(), FailOnceArtifactManager(Path(directory))
            )

            with pytest.raises(RuntimeError, match="artifact setup failed"):
                await lifecycle.execute_with_artifacts("analysis-setup-failure", lambda _, __: None)

            assert (await controller.metrics()).active_permits == 0
            assert (
                await lifecycle.execute_with_artifacts("analysis-after-failure", lambda _, __: 42)
                == 42
            )
            metrics = await controller.metrics()
            assert metrics.active_permits == 0
            assert metrics.admitted_analyses == 2

    asyncio.run(scenario())


def test_artifact_cleanup_failure_still_releases_permit() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            controller = AdmissionController(1)
            lifecycle = RequestLifecycle(
                controller, AnalysisExecutor(), CleanupFailureArtifactManager(Path(directory), 1024)
            )

            with pytest.raises(RuntimeError, match="artifact cleanup failed"):
                await lifecycle.execute_with_artifacts(
                    "analysis-cleanup-failure", lambda _, __: None
                )

            assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_pipeline_failure_is_preserved_when_cleanup_also_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class PipelineFailure(Exception):
        pass

    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            caplog.set_level(logging.INFO, logger="football_analysis.lifecycle")
            controller = AdmissionController(1)
            lifecycle = RequestLifecycle(
                controller, AnalysisExecutor(), CleanupFailureArtifactManager(Path(directory), 1024)
            )

            def pipeline(_: CancellationManager, __: ArtifactSession) -> None:
                raise PipelineFailure("pipeline request-data must survive")

            with pytest.raises(PipelineFailure, match="pipeline request-data must survive"):
                await lifecycle.execute_with_artifacts("cleanup-primary-failure", pipeline)
            assert (await controller.metrics()).active_permits == 0
            assert await lifecycle.execute("after-cleanup-primary-failure", lambda _: 42) == 42

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    cleanup = next(message for message in messages if "analysis_cleanup_finished" in message)
    assert "cleanup_succeeded=false" in cleanup
    assert "cleanup_error_type=RuntimeError" in cleanup
    assert "pipeline request-data" not in cleanup
    assert "TemporaryDirectory" not in cleanup


def test_artifact_cleanup_observations_cover_completed_and_failed_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            caplog.set_level(logging.INFO, logger="football_analysis.lifecycle")
            lifecycle = RequestLifecycle(
                AdmissionController(1),
                AnalysisExecutor(),
                ArtifactManager(Path(directory), 1024),
            )
            assert await lifecycle.execute_with_artifacts("cleanup-success", lambda _, __: 42) == 42
            with pytest.raises(RuntimeError, match="pipeline failed"):
                await lifecycle.execute_with_artifacts(
                    "cleanup-failure",
                    lambda _, __: (_ for _ in ()).throw(RuntimeError("pipeline failed")),
                )

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "analysis_cleanup_finished analysis_id=cleanup-success cleanup_outcome=completed" in message
        and "cleanup_succeeded=True" in message
        for message in messages
    )
    assert any(
        "analysis_cleanup_finished analysis_id=cleanup-failure cleanup_outcome=failed" in message
        and "cleanup_succeeded=True" in message
        for message in messages
    )
    assert all("TemporaryDirectory" not in message for message in messages)


def test_cancellation_initialization_failure_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_cancellation_creation(_: str) -> CancellationManager:
        raise RuntimeError("cancellation setup failed")

    monkeypatch.setattr("api.request_lifecycle.CancellationManager", fail_cancellation_creation)

    async def scenario() -> None:
        controller = AdmissionController(1)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor())

        with pytest.raises(RuntimeError, match="cancellation setup failed"):
            await lifecycle.execute("analysis-cancellation-setup-failure", lambda _: None)

        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_request_deadline_expiration_cancels_cooperatively_and_releases_permit() -> None:
    async def scenario() -> None:
        controller = AdmissionController(1)
        lifecycle = RequestLifecycle(controller, AnalysisExecutor(), request_deadline_seconds=0.01)
        states: list[CancellationState] = []

        def pipeline(cancellation: CancellationManager) -> None:
            while not cancellation.wait(0.01):
                pass
            states.append(cancellation.snapshot().state)
            CancellationChecker(cancellation).check("deadline test")

        with pytest.raises(AnalysisCancelled):
            await lifecycle.execute("deadline-request", pipeline)
        assert states == [CancellationState.DEADLINE_EXPIRED]
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_shutdown_rejects_new_work_cancels_active_work_and_cleans_artifacts() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            controller = AdmissionController(1)
            lifecycle = RequestLifecycle(
                controller, AnalysisExecutor(), ArtifactManager(Path(directory), 1024)
            )
            started = Event()
            states: list[CancellationState] = []

            def pipeline(cancellation: CancellationManager, session: ArtifactSession) -> None:
                assert session.directory.exists()
                started.set()
                while not cancellation.wait(0.01):
                    pass
                states.append(cancellation.snapshot().state)
                CancellationChecker(cancellation).check("shutdown test")

            active = asyncio.create_task(
                lifecycle.execute_with_artifacts("shutdown-request", pipeline)
            )
            while not started.is_set():
                await asyncio.sleep(0)
            shutdown = asyncio.create_task(lifecycle.shutdown())
            await asyncio.sleep(0)
            assert controller.accepting is False
            with pytest.raises(AdmissionRejectedError):
                await lifecycle.execute("rejected-after-shutdown", lambda _: None)
            await shutdown
            with pytest.raises(AnalysisCancelled):
                await active
            assert states == [CancellationState.SHUTDOWN_REQUESTED]
            assert not (Path(directory) / "shutdown-request").exists()
            assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())
