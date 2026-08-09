"""Stage-boundary cancellation lifecycle coverage."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from api.request_lifecycle import RequestLifecycle
from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationChecker, CancellationManager
from concurrency.exceptions import AnalysisCancelled
from concurrency.executor import AnalysisExecutor
from diagnostics.artifacts import ArtifactManager


def test_cancellation_before_execution_raises_dedicated_exception() -> None:
    token = CancellationManager("before")
    token.request_cancellation()
    with pytest.raises(AnalysisCancelled, match="before execution"):
        CancellationChecker(token).check("execution")


def test_detection_tracking_and_scoring_checkpoints_stop_work() -> None:
    for stage in ("detection", "tracking", "scoring"):
        token = CancellationManager(stage)
        checker = CancellationChecker(token)
        checker.check("previous stage")
        token.request_cancellation()
        with pytest.raises(AnalysisCancelled, match=stage):
            checker.check(stage)


def test_cancellation_releases_permit_and_cleans_artifacts_idempotently() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as directory:
            manager = ArtifactManager(Path(directory), 10)
            controller = AdmissionController(1)
            lifecycle = RequestLifecycle(controller, AnalysisExecutor(), manager)
            captured: list[CancellationManager] = []

            def pipeline(token: CancellationManager, artifacts: object) -> None:
                del artifacts
                captured.append(token)
                token.request_cancellation()
                token.request_cancellation()
                CancellationChecker(token).check("scoring")

            with pytest.raises(AnalysisCancelled):
                await lifecycle.execute_with_artifacts("cancelled", pipeline)
            assert (await controller.metrics()).active_permits == 0
            assert not (Path(directory) / "cancelled").exists()
            captured[0].complete()
            captured[0].complete()

    asyncio.run(scenario())


def test_concurrent_request_cancellation_remains_isolated() -> None:
    async def scenario() -> None:
        lifecycle = RequestLifecycle(AdmissionController(2), AnalysisExecutor())

        def pipeline(token: CancellationManager) -> None:
            token.request_cancellation()
            CancellationChecker(token).check("event analysis")

        results = await asyncio.gather(
            lifecycle.execute("one", pipeline),
            lifecycle.execute("two", pipeline),
            return_exceptions=True,
        )
        assert all(isinstance(result, AnalysisCancelled) for result in results)

    asyncio.run(scenario())
