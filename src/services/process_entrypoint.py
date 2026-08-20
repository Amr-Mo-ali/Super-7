"""Unused, pickle-safe child analysis entry point for future MVP-2C wiring."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from time import perf_counter

from pydantic import TypeAdapter

from api.routes import _analyze_uploaded
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.logging import get_logger
from diagnostics.artifacts import ArtifactManager
from schemas.analysis import AnalyzeResponse
from services.analysis_composition import AnalysisComponents, create_analysis_components
from services.video_path_resolver import VideoPathResolver

CHILD_ANALYSIS_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_RESPONSE_ADAPTER: TypeAdapter[AnalyzeResponse] = TypeAdapter(AnalyzeResponse)
_runtime: _ChildRuntime | None = None


@dataclass(frozen=True, slots=True)
class ChildAnalysisRequest:
    analysis_id: str
    video_id: str
    player_id: str
    video_reference: str
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value in (self.analysis_id, self.video_id, self.player_id):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError("child analysis identifiers must be safe")
        _validate_reference(self.video_reference)
        _validate_version(self.schema_version)


@dataclass(frozen=True, slots=True)
class ChildAnalysisSuccess:
    analysis_id: str
    response_json: str
    analysis_version: str
    model_version: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not self.analysis_version or _has_control(self.analysis_version):
            raise ValueError("analysis_version must be a safe non-empty value")
        if not self.model_version or _has_control(self.model_version):
            raise ValueError("model_version must be a safe non-empty value")
        try:
            decoded = json.loads(self.response_json)
        except json.JSONDecodeError as error:
            raise ValueError("response_json must be valid JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("response_json must be a JSON object")


@dataclass(frozen=True, slots=True)
class ChildAnalysisFailure:
    analysis_id: str
    error_code: str
    public_message: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.error_code):
            raise ValueError("child failure code must be a safe exception class name")
        if self.public_message != "Analysis could not be completed.":
            raise ValueError("child failures must use the fixed sanitized message")


@dataclass(frozen=True, slots=True)
class ChildAnalysisCancelled:
    analysis_id: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)


type ChildAnalysisResult = ChildAnalysisSuccess | ChildAnalysisFailure | ChildAnalysisCancelled


@dataclass(frozen=True, slots=True)
class ParentFailure:
    error_code: str
    public_message: str


@dataclass(frozen=True, slots=True)
class ParentCancelled:
    analysis_id: str


type ParentChildResult = AnalyzeResponse | ParentFailure | ParentCancelled


@dataclass(slots=True)
class _ChildRuntime:
    settings: Settings
    components: AnalysisComponents
    path_resolver: VideoPathResolver
    artifact_manager: ArtifactManager
    logger: logging.Logger


def initialize_analysis_child(settings: Settings) -> None:
    """Construct one lazy child-owned analysis graph; no model is loaded here."""
    global _runtime
    if _runtime is not None:
        if _runtime.settings != settings:
            raise RuntimeError("analysis child is already initialized with different settings")
        return
    logger = get_logger("football_analysis.child")
    components = create_analysis_components(
        settings,
        player_detector_logger=get_logger("football_analysis.detector"),
        ball_detector_logger=get_logger("football_analysis.ball_detector"),
    )
    _runtime = _ChildRuntime(
        settings,
        components,
        VideoPathResolver(settings.video_storage_root),
        ArtifactManager(
            Path(settings.debug_output_dir),
            settings.max_upload_bytes,
            retained_sessions=settings.debug.retained_sessions,
        ),
        logger,
    )
    logger.info(
        "analysis_child_initialized child_pid=%s analysis_version=%s execution_mode=child",
        os.getpid(),
        settings.analysis_version,
    )


def run_child_analysis(request: ChildAnalysisRequest) -> ChildAnalysisResult:
    """Run one synchronous calculation without callbacks or parent-owned state."""
    runtime = _runtime
    if runtime is None:
        raise RuntimeError("analysis child is not initialized")
    started = perf_counter()
    artifacts = None
    outcome: ChildAnalysisResult | None = None
    try:
        runtime.path_resolver.validate_reference(request.video_reference)
        video_path = runtime.path_resolver.resolve(request.video_reference)
        artifacts = runtime.artifact_manager.create_session(request.analysis_id)
        cancellation = CancellationManager(request.analysis_id)
        components = runtime.components
        result = _analyze_uploaded(
            runtime.settings,
            components.validator,
            components.tracker,
            components.selector,
            components.extractor,
            components.ball_proximity_analyzer,
            components.movement_analyzer,
            components.interaction_analyzer,
            components.technical_event_analyzer,
            components.pass_detector,
            components.shot_detector,
            runtime.logger,
            components.physical_scorer,
            request.analysis_id,
            started,
            started,
            video_path,
            cancellation,
            artifacts,
            {},
        )
        outcome = ChildAnalysisSuccess(
            request.analysis_id,
            result.model_dump_json(),
            runtime.settings.analysis_version,
            components.tracker.model_version,
            _milliseconds(started),
        )
    except AnalysisCancelled:
        outcome = ChildAnalysisCancelled(request.analysis_id, _milliseconds(started))
    except Exception as error:
        outcome = ChildAnalysisFailure(
            request.analysis_id,
            type(error).__name__,
            "Analysis could not be completed.",
            _milliseconds(started),
        )
    finally:
        if artifacts is not None:
            try:
                artifacts.cleanup()
            except Exception as error:
                runtime.logger.warning(
                    "analysis_child_cleanup_failed analysis_id=%s cleanup_error_type=%s",
                    request.analysis_id,
                    type(error).__name__,
                )
                if isinstance(outcome, ChildAnalysisSuccess):
                    outcome = ChildAnalysisFailure(
                        request.analysis_id,
                        type(error).__name__,
                        "Analysis could not be completed.",
                        _milliseconds(started),
                    )
    assert outcome is not None
    return outcome


def validate_child_result(
    expected_analysis_id: str,
    expected_schema_version: int,
    result: ChildAnalysisResult,
) -> ParentChildResult:
    """Validate one child envelope before a future parent performs callback work."""
    if (
        result.analysis_id != expected_analysis_id
        or result.schema_version != expected_schema_version
    ):
        raise ValueError("child result does not match the expected analysis identity or schema")
    if isinstance(result, ChildAnalysisSuccess):
        response: AnalyzeResponse = _RESPONSE_ADAPTER.validate_json(result.response_json)
        if response.analysis_id != result.analysis_id:
            raise ValueError("child response analysis ID does not match its envelope")
        return response
    if isinstance(result, ChildAnalysisFailure):
        return ParentFailure(result.error_code, result.public_message)
    if isinstance(result, ChildAnalysisCancelled):
        return ParentCancelled(result.analysis_id)
    raise ValueError("child result has an unknown shape")


def _validate_reference(value: str) -> None:
    path = Path(value)
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or value != path.name
    ):
        raise ValueError("video_reference must be a safe relative filename")


def _validate_version(value: int) -> None:
    if value != CHILD_ANALYSIS_SCHEMA_VERSION:
        raise ValueError("unsupported child analysis schema version")


def _validate_result(analysis_id: str, duration_ms: int, version: int) -> None:
    if not _SAFE_ID.fullmatch(analysis_id) or duration_ms < 0:
        raise ValueError("child result identity and duration must be safe")
    _validate_version(version)


def _milliseconds(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _reset_child_runtime_for_test() -> None:
    """Test-only internal seam; never used by production runtime."""
    global _runtime
    _runtime = None
