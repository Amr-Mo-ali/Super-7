"""Minimal coordination of admission, execution, and request-scoped cancellation."""

from collections.abc import Callable
from typing import TypeVar

from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AdmissionRejectedError
from concurrency.executor import AnalysisExecutor
from diagnostics.artifacts import ArtifactManager, ArtifactSession

Result = TypeVar("Result")


class RequestLifecycle:
    """Coordinates existing operational components around one supplied pipeline callable."""

    def __init__(
        self,
        admission: AdmissionController,
        executor: AnalysisExecutor,
        artifacts: ArtifactManager | None = None,
    ) -> None:
        self._admission = admission
        self._executor = executor
        self._artifacts = artifacts

    async def execute(
        self,
        request_id: str,
        pipeline: Callable[[CancellationManager], Result],
    ) -> Result:
        """Run admitted work and always complete cancellation state and release its permit."""
        permit = await self._admission.admit()
        if permit is None:
            raise AdmissionRejectedError("Analysis capacity is exhausted.")
        cancellation = CancellationManager(request_id)
        try:
            return await self._executor.execute(request_id, cancellation, pipeline)
        finally:
            cancellation.complete()
            await permit.release()

    async def execute_with_artifacts(
        self,
        request_id: str,
        pipeline: Callable[[CancellationManager, ArtifactSession], Result],
    ) -> Result:
        """Run admitted work with one request-owned artifact session."""
        if self._artifacts is None:
            raise RuntimeError("ArtifactManager was not configured for this request lifecycle.")
        permit = await self._admission.admit()
        if permit is None:
            raise AdmissionRejectedError("Analysis capacity is exhausted.")
        cancellation = CancellationManager(request_id)
        artifacts = self._artifacts.create_session(request_id)
        try:
            return await self._executor.execute(
                request_id, cancellation, lambda state: pipeline(state, artifacts)
            )
        finally:
            artifacts.cleanup()
            cancellation.complete()
            await permit.release()
