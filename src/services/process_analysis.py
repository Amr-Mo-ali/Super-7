"""Spawn-safe, unused MVP-2A IPC contracts and one-child supervision."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from queue import Empty, Full
from typing import Protocol, TypeVar, cast

PROCESS_ANALYSIS_SCHEMA_VERSION = 1
_POLL_SECONDS = 0.1
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
T = TypeVar("T")


class _Channel(Protocol[T]):
    def put(self, item: T, block: bool = True, timeout: float | None = None) -> None: ...
    def get(self, block: bool = True, timeout: float | None = None) -> T: ...
    def close(self) -> None: ...
    def join_thread(self) -> None: ...


class _Process(Protocol):
    pid: int | None
    exitcode: int | None

    def start(self) -> None: ...
    def is_alive(self) -> bool: ...
    def join(self, timeout: float | None = None) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class _SpawnContext(Protocol):
    def Queue(self, maxsize: int = 0) -> _Channel[object]: ...
    def Process(
        self, *, target: ProcessChildTarget, args: tuple[RequestChannel, ResponseChannel]
    ) -> _Process: ...


@dataclass(frozen=True, slots=True)
class ProcessRuntimeConfig:
    """Minimal non-secret child configuration; MVP-2B extends it explicitly."""

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
        _safe_reference(self.video_reference)
        _version(self.schema_version)


@dataclass(frozen=True, slots=True)
class ProcessModelMetadata:
    model_version: str

    def __post_init__(self) -> None:
        if not _SAFE_LABEL.fullmatch(self.model_version):
            raise ValueError("model_version must be a safe version label")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisSuccess:
    analysis_id: str
    result_json: str
    processing_duration_ms: int
    model: ProcessModelMetadata
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _response(self.analysis_id, self.processing_duration_ms, self.schema_version)
        try:
            decoded = json.loads(self.result_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("result_json must contain valid JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("result_json must contain a JSON object")


@dataclass(frozen=True, slots=True)
class ProcessAnalysisFailure:
    analysis_id: str
    error_code: str
    public_message: str
    processing_duration_ms: int
    schema_version: int = PROCESS_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _response(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not _SAFE_CODE.fullmatch(self.error_code):
            raise ValueError("error_code must be a stable safe code")
        if not self.public_message or _has_control(self.public_message):
            raise ValueError("public_message must be non-empty and free of control characters")


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

    def __post_init__(self) -> None:
        _version(self.schema_version)


type ProcessAnalysisResponse = (
    ProcessAnalysisSuccess | ProcessAnalysisFailure | ProcessAnalysisCancelled
)
type ChildMessage = ProcessAnalysisReady | ProcessAnalysisResponse
type RequestChannel = _Channel[ProcessAnalysisRequest | None]
type ResponseChannel = _Channel[ChildMessage]
type RequestQueue = RequestChannel
type ResponseQueue = ResponseChannel


class ProcessChildTarget(Protocol):
    def __call__(self, requests: RequestChannel, responses: ResponseChannel) -> None: ...


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


@dataclass(frozen=True, slots=True)
class ProcessShutdownResult:
    child_alive: bool
    exit_code: int | None
    forced_termination: bool


class ProcessAnalysisSupervisor:
    """Own exactly one spawned process; all blocking IPC runs in short thread calls."""

    def __init__(
        self,
        target: ProcessChildTarget,
        *,
        logger: logging.Logger | None = None,
        context: _SpawnContext | None = None,
        startup_timeout_seconds: float = 10.0,
        response_timeout_seconds: float = 60.0,
        shutdown_grace_seconds: float = 2.0,
    ) -> None:
        if min(startup_timeout_seconds, response_timeout_seconds, shutdown_grace_seconds) <= 0:
            raise ValueError("process supervisor timeouts must be positive")
        self._context = context or cast(_SpawnContext, multiprocessing.get_context("spawn"))
        self._target = target
        self._logger = logger or logging.getLogger("football_analysis.process")
        self._startup_timeout_seconds = startup_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._requests: RequestChannel | None = None
        self._responses: ResponseChannel | None = None
        self._process: _Process | None = None
        self._state = ProcessAnalysisState.NEW
        self._last_shutdown = ProcessShutdownResult(False, None, False)

    @property
    def state(self) -> ProcessAnalysisState:
        return self._state

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def last_shutdown(self) -> ProcessShutdownResult:
        return self._last_shutdown

    async def start(self) -> None:
        if self._state is ProcessAnalysisState.READY:
            return
        if self._state not in {ProcessAnalysisState.NEW, ProcessAnalysisState.STOPPED}:
            raise ProcessAnalysisError("analysis process cannot be started in its current state")
        self._state = ProcessAnalysisState.STARTING
        self._logger.info("analysis_process_starting state=%s", self._state)
        try:
            raw_requests = self._context.Queue(maxsize=1)
            raw_responses = self._context.Queue(maxsize=1)
            self._requests = cast(RequestChannel, raw_requests)
            self._responses = cast(ResponseChannel, raw_responses)
            self._process = self._context.Process(
                target=self._target, args=(self._requests, self._responses)
            )
            self._process.start()
            message = await self._receive(self._startup_timeout_seconds)
            if not isinstance(message, ProcessAnalysisReady):
                raise ProcessAnalysisError("analysis process readiness handshake was invalid")
            self._state = ProcessAnalysisState.READY
            self._logger.info("analysis_process_ready child_pid=%s state=%s", self.pid, self._state)
        except BaseException as error:
            self._state = ProcessAnalysisState.FAILED
            self._failed("startup_failed")
            await self.shutdown()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ProcessAnalysisError("analysis process could not be started") from None

    async def submit(self, request: ProcessAnalysisRequest) -> ProcessAnalysisResponse:
        if self._state is not ProcessAnalysisState.READY or self._requests is None:
            raise ProcessAnalysisError("analysis process is not ready")
        self._state = ProcessAnalysisState.BUSY
        self._logger.info(
            "analysis_process_submit_started analysis_id=%s child_pid=%s state=%s",
            request.analysis_id,
            self.pid,
            self._state,
        )
        try:
            await asyncio.to_thread(self._requests.put, request, True, _POLL_SECONDS)
            message = await self._receive(self._response_timeout_seconds)
            if not isinstance(
                message, (ProcessAnalysisSuccess, ProcessAnalysisFailure, ProcessAnalysisCancelled)
            ):
                raise ProcessAnalysisError("analysis process returned an invalid response")
            if message.analysis_id != request.analysis_id:
                raise ProcessAnalysisError("analysis process returned a stale response")
            self._state = ProcessAnalysisState.READY
            self._logger.info(
                "analysis_process_response_received analysis_id=%s child_pid=%s state=%s",
                request.analysis_id,
                self.pid,
                self._state,
            )
            return message
        except asyncio.CancelledError:
            self._state = ProcessAnalysisState.FAILED
            self._failed("submit_cancelled", request.analysis_id)
            raise
        except (Full, ProcessAnalysisError):
            self._state = ProcessAnalysisState.FAILED
            self._failed("submit_failed", request.analysis_id)
            raise ProcessAnalysisError("analysis process request or response failed") from None

    async def shutdown(self) -> ProcessShutdownResult:
        if self._state is ProcessAnalysisState.STOPPED:
            return self._last_shutdown
        self._state = ProcessAnalysisState.STOPPING
        process, requests, responses = self._process, self._requests, self._responses
        self._logger.info(
            "analysis_process_shutdown_started child_pid=%s state=%s", self.pid, self._state
        )
        forced = False
        try:
            if process is not None and process.is_alive():
                if requests is not None:
                    try:
                        await asyncio.to_thread(requests.put, None, True, _POLL_SECONDS)
                    except (Full, OSError, ValueError):
                        pass
                await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
                if process.is_alive():
                    forced = True
                    self._logger.warning(
                        "analysis_process_forced_termination child_pid=%s", process.pid
                    )
                    process.terminate()
                    await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, self._shutdown_grace_seconds)
        finally:
            alive = process is not None and process.is_alive()
            exit_code = None if process is None else process.exitcode
            for channel in (requests, responses):
                if channel is not None:
                    try:
                        await asyncio.to_thread(channel.close)
                        await asyncio.to_thread(channel.join_thread)
                    except (OSError, ValueError):
                        pass
            self._process = None
            self._requests = None
            self._responses = None
            self._last_shutdown = ProcessShutdownResult(alive, exit_code, forced)
            self._state = ProcessAnalysisState.STOPPED
            self._logger.info(
                "analysis_process_shutdown_finished state=%s exit_code=%s forced_termination=%s",
                self._state,
                exit_code,
                forced,
            )
        return self._last_shutdown

    async def _receive(self, timeout_seconds: float) -> ChildMessage:
        """Finite polls prevent a cancelled await from leaving a permanent helper thread."""
        if self._responses is None:
            raise ProcessAnalysisError("analysis process response channel is unavailable")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            if self._process is None or not self._process.is_alive():
                raise ProcessAnalysisError("analysis process exited unexpectedly")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProcessAnalysisError("analysis process response timed out")
            try:
                return await asyncio.to_thread(
                    self._responses.get, True, min(_POLL_SECONDS, remaining)
                )
            except Empty:
                continue
            except (EOFError, OSError, ValueError) as error:
                raise ProcessAnalysisError("analysis process response channel failed") from error

    def _failed(self, outcome: str, analysis_id: str | None = None) -> None:
        fields = f"analysis_id={analysis_id} " if analysis_id is not None else ""
        self._logger.warning(
            "analysis_process_failed %schild_pid=%s outcome=%s state=%s",
            fields,
            self.pid,
            outcome,
            self._state,
        )


def _identity(value: str) -> None:
    if not value or _has_control(value):
        raise ValueError("analysis_id must be a safe non-empty value")


def _safe_reference(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("video_reference must be a normalized safe relative reference")
    windows = PureWindowsPath(value)
    path = PurePosixPath(value)
    if (
        windows.is_absolute()
        or windows.drive
        or path.is_absolute()
        or ".." in path.parts
        or "" in path.parts
    ):
        raise ValueError("video_reference must be a normalized safe relative reference")
    if str(path) != value or value in {".", ".."}:
        raise ValueError("video_reference must be a normalized safe relative reference")


def _version(value: int) -> None:
    if value != PROCESS_ANALYSIS_SCHEMA_VERSION:
        raise ValueError("unsupported process analysis schema version")


def _response(identity: str, duration_ms: int, schema_version: int) -> None:
    _identity(identity)
    if duration_ms < 0:
        raise ValueError("processing_duration_ms must not be negative")
    _version(schema_version)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
