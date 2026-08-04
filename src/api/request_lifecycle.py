"""Minimal coordination of admission, execution, and request-scoped cancellation."""

from collections.abc import Callable
from typing import TypeVar

from concurrency.admission import AdmissionController
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AdmissionRejectedError
from concurrency.executor import AnalysisExecutor

Result = TypeVar("Result")


class RequestLifecycle:
    """Coordinates existing operational components around one supplied pipeline callable."""

    def __init__(self, admission: AdmissionController, executor: AnalysisExecutor) -> None:
        self._admission = admission
        self._executor = executor

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
