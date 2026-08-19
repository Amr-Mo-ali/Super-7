"""Spawn-safe IPC contracts and a one-child supervisor foundation.

This module is intentionally disconnected from the production route until MVP-2B.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.context import BaseContext, BaseProcess
from multiprocessing.queues import Queue
from queue import Empty, Full
from typing import Protocol

PROCESS_ANALYSIS_SCHEMA_VERSION = 1
_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class ProcessRuntimeConfig:
    """Minimal non-secret configuration; MVP-2B will extend this explicitly."""

    video_storage_root: str
    debug_output_dir: str
    max_upload_bytes: int
    analysis_version: str

    def __post_init__(self) -> None:
        if not self.video_storage_root or not self.debug_output_dir or not self.analysis_version:
            raise ValueError("process runtime configuration values must not be empty")
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisRequest:
    analysis_id: str
    video_id: str
    player_id: str
    video_reference: str
    runtime_config: ProcessRuntimeConfig
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.analysis_id)
        if not self.video_id or not self.player_id:
            raise ValueError("video_id and player_id must not be empty")
        if not self.video_reference or self.video_reference.startswith(("/", "\\")):
            raise ValueError("video_reference must be a safe relative reference")
        _version(self.schema_version)


@dataclass(frozen=True, slots=True)
class ProcessModelMetadata:
    model_version: str

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("model_version must not be empty")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisSuccess:
    analysis_id: str
    result_json: str
    processing_duration_ms: int
    model: ProcessModelMetadata
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _response(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not self.result_json:
            raise ValueError("result_json must not be empty")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisFailure:
    analysis_id: str
    error_code: str
    public_message: str
    processing_duration_ms: int
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _response(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not self.error_code or not self.public_message:
            raise ValueError("failure code and public message must not be empty")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisCancelled:
    analysis_id: str
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.analysis_id)
        _version(self.schema_version)


@dataclass(frozen=True, slots=True)
class ProcessAnalysisReady:
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION


type ProcessAnalysisResponse = (
    ProcessAnalysisSuccess | ProcessAnalysisFailure | ProcessAnalysisCancelled
)
type ChildMessage = ProcessAnalysisReady | ProcessAnalysisResponse
type RequestQueue = Queue[ProcessAnalysisRequest | None]
type ResponseQueue = Queue[ChildMessage]


class ProcessChildTarget(Protocol):
    def __call__(self, requests: RequestQueue, responses: ResponseQueue) -> None: ...


class ProcessAnalysisState(StrEnum):
    NEW = "NEW"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class ProcessAnalysisError(RuntimeError):
    """Sanitized parent-side process-boundary failure."""


class ProcessAnalysisSupervisor:
    """Own exactly one spawned process and one bounded in-flight request."""

    def __init__(
        self,
        target: ProcessChildTarget,
        *,
        context: BaseContext | None = None,
        startup_timeout_seconds: float = 10.0,
        response_timeout_seconds: float = 60.0,
        shutdown_grace_seconds: float = 2.0,
    ) -> None:
        if min(startup_timeout_seconds, response_timeout_seconds, shutdown_grace_seconds) <= 0:
            raise ValueError("process supervisor timeouts must be positive")
        self._context = context or multiprocessing.get_context("spawn")
        self._target = target
        self._startup_timeout_seconds = startup_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._requests: RequestQueue | None = None
        self._responses: ResponseQueue | None = None
        self._process: BaseProcess | None = None
        self._state = ProcessAnalysisState.NEW

    @property
    def state(self) -> ProcessAnalysisState:
        return self._state

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    async def start(self) -> None:
        if self._state is ProcessAnalysisState.READY:
            return
        if self._state not in {ProcessAnalysisState.NEW, ProcessAnalysisState.STOPPED}:
            raise ProcessAnalysisError("analysis process cannot be started in its current state")
        self._state = ProcessAnalysisState.STARTING
        try:
            self._requests = self._context.Queue(maxsize=1)
            self._responses = self._context.Queue(maxsize=1)
            self._process = self._context.Process(
                target=self._target, args=(self._requests, self._responses)
            )
            self._process.start()
            message = await self._receive(self._startup_timeout_seconds)
            if (
                not isinstance(message, ProcessAnalysisReady)
                or message.schema_version != PROCESS_ANALYSIS_SCHEMA_VERSION
            ):
                raise ProcessAnalysisError("analysis process readiness handshake was invalid")
            self._state = ProcessAnalysisState.READY
        except Exception as error:
            self._state = ProcessAnalysisState.FAILED
            await self.shutdown()
            if isinstance(error, ProcessAnalysisError):
                raise
            raise ProcessAnalysisError("analysis process could not be started") from error

    async def submit(self, request: ProcessAnalysisRequest) -> ProcessAnalysisResponse:
        if self._state is not ProcessAnalysisState.READY or self._requests is None:
            raise ProcessAnalysisError("analysis process is not ready")
        self._state = ProcessAnalysisState.BUSY
        try:
            await asyncio.to_thread(self._requests.put, request, True, _POLL_SECONDS)
            message = await self._receive(self._response_timeout_seconds)
            if not isinstance(
                message, (ProcessAnalysisSuccess, ProcessAnalysisFailure, ProcessAnalysisCancelled)
            ):
                raise ProcessAnalysisError("analysis process returned an invalid response")
            if (
                message.schema_version != PROCESS_ANALYSIS_SCHEMA_VERSION
                or message.analysis_id != request.analysis_id
            ):
                raise ProcessAnalysisError("analysis process returned a stale response")
            return message
        except Full as error:
            raise ProcessAnalysisError("analysis process request channel is unavailable") from error
        finally:
            if self._state is ProcessAnalysisState.BUSY:
                self._state = ProcessAnalysisState.READY

    async def shutdown(self) -> None:
        if self._state is ProcessAnalysisState.STOPPED:
            return
        self._state = ProcessAnalysisState.STOPPING
        process, requests, responses = self._process, self._requests, self._responses
        self._process = None
        self._requests = None
        self._responses = None
        try:
            if process is not None and process.is_alive():
                if requests is not None:
                    try:
                        await asyncio.to_thread(requests.put, None, True, _POLL_SECONDS)
                    except (Full, OSError, ValueError):
                        pass
                await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
        finally:
            for channel in (requests, responses):
                if channel is not None:
                    try:
                        await asyncio.to_thread(channel.close)
                        await asyncio.to_thread(channel.join_thread)
                    except (OSError, ValueError):
                        pass
            self._state = ProcessAnalysisState.STOPPED

    async def _receive(self, timeout_seconds: float) -> ChildMessage:
        """Receive with finite polls; cancelling the await cannot cancel its helper thread."""
        if self._responses is None:
            raise ProcessAnalysisError("analysis process response channel is unavailable")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            if self._process is None or not self._process.is_alive():
                self._state = ProcessAnalysisState.FAILED
                raise ProcessAnalysisError("analysis process exited unexpectedly")
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._state = ProcessAnalysisState.FAILED
                raise ProcessAnalysisError("analysis process response timed out")
            try:
                return await asyncio.to_thread(
                    self._responses.get, True, min(_POLL_SECONDS, remaining)
                )
            except Empty:
                continue
            except (EOFError, OSError, ValueError) as error:
                self._state = ProcessAnalysisState.FAILED
                raise ProcessAnalysisError("analysis process response channel failed") from error


def _identity(analysis_id: str) -> None:
    if not analysis_id:
        raise ValueError("analysis_id must not be empty")


def _version(schema_version: int) -> None:
    if schema_version != PROCESS_ANALYSIS_SCHEMA_VERSION:
        raise ValueError("unsupported process analysis schema version")


def _response(analysis_id: str, duration_ms: int, schema_version: int) -> None:
    _identity(analysis_id)
    if duration_ms < 0:
        raise ValueError("processing_duration_ms must not be negative")
    _version(schema_version)
