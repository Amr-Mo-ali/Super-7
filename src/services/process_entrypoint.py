"""Unused, pickle-safe child analysis entry point for future MVP-2C wiring."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from api.routes import _analyze_uploaded
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.logging import get_logger
from diagnostics.artifacts import ArtifactManager
from services.analysis_composition import AnalysisComponents, create_analysis_components
from services.process_contracts import (
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisResult,
    ChildAnalysisSuccess,
)
from services.video_path_resolver import VideoPathResolver

__all__ = (
    "initialize_analysis_child",
    "run_child_analysis",
)

_runtime: _ChildRuntime | None = None


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
                cleanup_result = artifacts.cleanup()
                if cleanup_result.errors:
                    runtime.logger.warning(
                        "analysis_child_cleanup_failed analysis_id=%s cleanup_error_type=%s",
                        request.analysis_id,
                        "ArtifactCleanupError",
                    )
                    if isinstance(outcome, ChildAnalysisSuccess):
                        outcome = ChildAnalysisFailure(
                            request.analysis_id,
                            "ArtifactCleanupError",
                            "Analysis could not be completed.",
                            _milliseconds(started),
                        )
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


def _milliseconds(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _reset_child_runtime_for_test() -> None:
    """Test-only internal seam; never used by production runtime."""
    global _runtime
    _runtime = None
