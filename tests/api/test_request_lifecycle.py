"""Deterministic tests for local analysis lifecycle coordination."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from api.request_lifecycle import RequestLifecycle
from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationManager, CancellationState
from concurrency.exceptions import AdmissionRejectedError
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
