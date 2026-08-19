"""Spawn-safe contracts and supervision for the experimental analysis process."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from dataclasses import dataclass
from time import monotonic
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProcessAnalysisRequest:
    """The complete immutable input permitted to cross into the child process."""

    analysis_id: str
    video_id: str
    player_id: str
    video_reference: str
    settings: tuple[tuple[str, str | int | float | bool], ...]
    analysis_version: str


@dataclass(frozen=True, slots=True)
class ProcessAnalysisSuccess:
    analysis_id: str
    serialized_result: str
    processing_duration_ms: int
    model_metadata: str


@dataclass(frozen=True, slots=True)
class ProcessAnalysisFailure:
    analysis_id: str
    error_code: str
    public_message: str
    processing_duration_ms: int


@dataclass(frozen=True, slots=True)
class ProcessAnalysisCancelled:
    analysis_id: str
    outcome: Literal["cancelled"] = "cancelled"


ProcessAnalysisResponse = ProcessAnalysisSuccess | ProcessAnalysisFailure | ProcessAnalysisCancelled


class ProcessAnalysisError(RuntimeError):
    """A safe parent-side failure of the experimental child boundary."""


class ProcessAnalysisSupervisor:
    """Own exactly one spawned child and one in-flight IPC request."""

    def __init__(self, target: object, *, startup_timeout_seconds: float = 10.0) -> None:
        self._context = mp.get_context("spawn")
        self._target = target
        self._startup_timeout_seconds = startup_timeout_seconds
        self._requests: mp.queues.Queue[ProcessAnalysisRequest | None] | None = None
        self._responses: mp.queues.Queue[object] | None = None
        self._process: mp.Process | None = None
        self._busy = False

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def busy(self) -> bool:
        return self._busy

    async def start(self) -> None:
        if self._process is not None:
            return
        self._requests = self._context.Queue(maxsize=1)
        self._responses = self._context.Queue(maxsize=1)
        self._process = self._context.Process(target=self._target, args=(self._requests, self._responses))
        self._process.start()
        response = await self._get_response(self._startup_timeout_seconds)
        if response != "ready":
            await self.shutdown()
            raise ProcessAnalysisError("analysis process did not complete its readiness handshake")

    async def submit(self, request: ProcessAnalysisRequest) -> ProcessAnalysisResponse:
        if self._process is None or self._requests is None:
            raise ProcessAnalysisError("analysis process is not ready")
        if self._busy:
            raise ProcessAnalysisError("analysis process already has an in-flight job")
        if not self._process.is_alive():
            raise ProcessAnalysisError("analysis process exited unexpectedly")
        self._busy = True
        started = monotonic()
        try:
            await asyncio.to_thread(self._requests.put, request, True, 1.0)
            response = await self._get_response(None)
            if not isinstance(
                response, (ProcessAnalysisSuccess, ProcessAnalysisFailure, ProcessAnalysisCancelled)
            ) or response.analysis_id != request.analysis_id:
                raise ProcessAnalysisError("analysis process returned an invalid response")
            return response
        finally:
            self._busy = False
            del started

    async def shutdown(self, grace_seconds: float = 2.0) -> None:
        process, requests = self._process, self._requests
        self._process = None
        self._requests = None
        responses, self._responses = self._responses, None
        self._busy = False
        if process is not None:
            if process.is_alive() and requests is not None:
                await asyncio.to_thread(requests.put, None, True, 1.0)
                await asyncio.to_thread(process.join, grace_seconds)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, grace_seconds)
        for queue in (requests, responses):
            if queue is not None:
                queue.close()
                queue.join_thread()

    async def _get_response(self, timeout: float | None) -> object:
        if self._responses is None:
            raise ProcessAnalysisError("analysis response channel is unavailable")
        try:
            return await asyncio.to_thread(self._responses.get, True, timeout)
        except Exception as error:
            raise ProcessAnalysisError("analysis process did not return a response") from error
