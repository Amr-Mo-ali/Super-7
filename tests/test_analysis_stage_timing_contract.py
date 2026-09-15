"""Implemented P1-A analysis-stage timing contract tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from multiprocessing.context import BaseContext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from application_log_capture import capture_application_logs
from pydantic import HttpUrl

import api.routes as routes
from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AnalysisCancelled
from config.debug import DebugSettings
from core.config import Settings
from diagnostics.analysis_stage_timing import _log_analysis_stage_timing
from diagnostics.artifacts import ArtifactManager, ArtifactSession, CleanupResult
from schemas.analysis import (
    AnalyzeRequest,
    AnalyzeResponse,
    CompletedResponse,
    Diagnostics,
    FeatureMetric,
    FeaturesResponse,
    PhysicalScoreResponse,
    SelectedPlayer,
    TechnicalScoreResponse,
    TrackingResponse,
    VideoResponse,
)
from services import process_analysis_pool, process_entrypoint
from services.analysis_composition import AnalysisComponents
from services.analysis_queue import AnalysisJob, AnalysisJobState
from services.callback_service import CallbackPayload, FailedCallbackPayload
from services.dominant_target_selection import (
    TargetEligibilityResult,
    TargetSelectionStatus,
)
from services.feature_extractor import FeatureExtractor
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.player_rating.engine import PlayerRatingEngine
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.process_analysis_pool import ProcessAnalysisPool
from services.process_contracts import (
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisResult,
    ChildAnalysisSuccess,
    ParentChildResult,
    validate_child_result,
)
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult
from services.segment_selection import TrackSegment
from services.selection import PlayerTrack
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata

_EVENT = "analysis_stage_timing"
_SCHEMA_VERSION = "1"
_ALLOWED_FIELDS = {
    "analysis_id",
    "timing_schema_version",
    "stage",
    "role",
    "outcome",
    "duration_ms",
}
_ALLOWED_OUTCOMES = {"success", "unavailable", "failed", "cancelled", "skipped"}
_CHILD_STAGES = {
    "input_materialization",
    "validation",
    "tracking_total",
    "reproducibility_hashing",
    "target_segment_resolution",
    "post_processing_scoring",
    "debug_work",
    "child_serialization",
    "child_cleanup",
}
_PARENT_STAGES = {"process_ipc_parent_validation", "parent_response_mapping"}


@pytest.fixture(autouse=True)
def _reset_child_runtime() -> Iterator[None]:
    process_entrypoint._reset_child_runtime_for_test()
    yield
    process_entrypoint._reset_child_runtime_for_test()


class _StepClock:
    def __init__(self, start: float, step_seconds: float) -> None:
        self._now = start
        self._step = step_seconds

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value

    def advance(self, ticks: int) -> None:
        self._now += self._step * ticks


class _ManualClock:
    def __init__(self, start: float) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _Validator:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def validate(self, path: Path) -> VideoMetadata:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return VideoMetadata("mp4", path.stat().st_size, 0.3, 64, 64, 10.0, 3)


class _Tracker:
    model_version = "contract-model"

    def __init__(self, run: TrackingRun) -> None:
        self.run = run
        self.calls = 0

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        self.calls += 1
        return self.run


class _PhysicalScorer:
    def __init__(self, clock: _ManualClock | None = None) -> None:
        self._clock = clock
        self.calls = 0

    def score(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1
        if self._clock is not None:
            self._clock.advance(64.0)
        return None


class _NullAnalyzer:
    def analyze(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


class _InteractionAnalyzer:
    def analyze(self, *args: object, **kwargs: object) -> InteractionAnalysisResult:
        del args, kwargs
        return InteractionAnalysisResult(
            (),
            0,
            0.0,
            0.0,
            None,
            0,
            0,
            0.0,
            "contract-interaction",
            InteractionDiagnostics(
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
                "contract-interaction",
                0.0,
                0,
            ),
            (),
            "No deterministic interaction evidence.",
        )


class PrimaryFailure(RuntimeError):
    pass


class CleanupFailure(RuntimeError):
    pass


class TimingLogFailure(RuntimeError):
    pass


class _FailingTimingLogger:
    def __init__(self) -> None:
        self.timing_attempts = 0

    def info(self, msg: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        if str(msg).startswith(_EVENT):
            self.timing_attempts += 1
            raise TimingLogFailure("timing logger unavailable")


class _InlineExecutor:
    def __init__(self, parent_clock: _StepClock) -> None:
        self._parent_clock = parent_clock
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(
        self,
        function: Callable[[ChildAnalysisRequest], ChildAnalysisResult],
        request: ChildAnalysisRequest,
    ) -> Future[ChildAnalysisResult]:
        future: Future[ChildAnalysisResult] = Future()
        future.set_result(function(request))
        self._parent_clock.advance(4)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


class _InlineFactory:
    def __init__(self, executor: _InlineExecutor) -> None:
        self.executor = executor

    def __call__(
        self,
        *,
        max_workers: int,
        mp_context: BaseContext,
        initializer: Callable[[Settings], None],
        initargs: tuple[Settings],
    ) -> _InlineExecutor:
        assert max_workers == 1
        assert mp_context.get_start_method() == "spawn"
        assert initializer is process_entrypoint.initialize_analysis_child
        assert len(initargs) == 1
        return self.executor


class _ParentPool:
    def __init__(self, result: ParentChildResult) -> None:
        self.result = result

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult:
        assert request.video_reference == "private-video-name.mp4"
        return self.result


class _Callback:
    def __init__(self, clock: _StepClock | None = None) -> None:
        self._clock = clock
        self.payloads: list[CallbackPayload | FailedCallbackPayload] = []

    async def send_result(
        self, callback_url: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        assert str(callback_url) == "http://192.0.2.10/private-callback"
        self.payloads.append(payload)
        if self._clock is not None:
            self._clock.advance(100)
        return True


def _response(analysis_id: str) -> CompletedResponse:
    technical = TechnicalScoreResult(
        80.0,
        0.8,
        "provisional_event_based",
        None,
        {"controlled_movement_events": 1.0},
        0.8,
        None,
        0.0,
        0.9,
    )
    physical = PhysicalScoreResult(
        70.0,
        5,
        "high",
        70.0,
        0.7,
        "provisional_video_based",
        "physical_activity_v0.1",
        None,
        PhysicalScoreEvidence(0.7, 0.8, 1.0, 1.0, 0.4, 0.9, 6.0, 60, 0.9),
        (),
        "Visible image-space activity evidence.",
        70.0,
        False,
        0,
    )
    scores = FeatureExtractor().scores(physical, technical)
    return CompletedResponse(
        analysis_id=analysis_id,
        status="completed",
        video=VideoResponse(duration_seconds=0.3, fps=10.0, width=64, height=64),
        selected_player=SelectedPlayer(
            track_id=7,
            selection_method="best_continuous_track_segment",
            selection_score=0.9,
            confidence=0.9,
            visible_frames=3,
            visibility_ratio=1.0,
            ball_proximity_frames=0,
            ball_proximity_ratio=0.0,
            visibility_contribution=0.9,
            ball_proximity_contribution=0.0,
            segment_id=1,
            segment_start_frame=0,
            segment_end_frame=2,
            segment_duration_seconds=0.3,
        ),
        tracking=TrackingResponse(
            frames_processed=3,
            lost_track_count=0,
            longest_continuous_visible_segment=3,
        ),
        features=FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(
                reason="Ball evidence is unavailable in this deterministic fixture."
            ),
            movement_intensity=FeatureMetric(value=0.7),
            direction_changes=FeatureMetric(value=1.0),
        ),
        scores=scores,
        player_rating_summary=PlayerRatingEngine().summarize(
            technical,
            physical,
            None,
            None,
        ),
        result_availability="AVAILABLE",
        diagnostics=Diagnostics(
            frames_processed=3,
            frames_with_player_detections=3,
            total_person_detections=3,
            tracks_created=1,
            valid_candidate_tracks=1,
            ball_detections=0,
            selected_track_visible_frames=3,
            technical_event_count=1,
            technical_evidence_score=1.0,
            physical_score_final=70.0,
            physical_score_confidence=0.7,
        ),
        warnings=["deterministic-contract-evidence"],
        analysis_version="contract-analysis",
        model_version="contract-model",
        processing_time_ms=0,
    )


def _tracking_run() -> TrackingRun:
    track = PlayerTrack(7, 3, 3, 3, 0, 0.9, 0, True)
    return TrackingRun(
        (track,),
        TrackingDiagnostics(3, 3, 3, 1, 0),
        ball_warning="deterministic_no_ball_evidence",
    )


def _established_target() -> tuple[TargetEligibilityResult, TrackSegment]:
    return (
        TargetEligibilityResult(
            TargetSelectionStatus.ESTABLISHED,
            "dominant_visual_candidate",
            7,
        ),
        TrackSegment(7, 1, 0, 2, 0.3, 3, 1.0, 0.9, 64.0, 64.0, 0.0, 0, 0.0, 0.9, ()),
    )


def _unavailable_target() -> tuple[TargetEligibilityResult, None]:
    return (
        TargetEligibilityResult(
            TargetSelectionStatus.NOT_ESTABLISHED,
            "no_qualifying_visual_target",
        ),
        None,
    )


def _install_child_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    validator: _Validator | None = None,
    logger: logging.Logger | None = None,
    target: tuple[TargetEligibilityResult, TrackSegment | None] | None = None,
    debug: DebugSettings | None = None,
    patch_completed: bool = True,
    physical_scorer: object | None = None,
) -> tuple[process_entrypoint._ChildRuntime, _Validator, _Tracker]:
    videos = tmp_path / "videos"
    videos.mkdir(exist_ok=True)
    (videos / "safe.mp4").write_bytes(b"synthetic-contract-bytes")
    settings = Settings(
        video_storage_root=str(videos),
        debug_output_dir=str(tmp_path / "debug"),
        debug=debug or DebugSettings(),
    )
    resolved_validator = validator or _Validator()
    tracker = _Tracker(_tracking_run())
    components = cast(
        AnalysisComponents,
        SimpleNamespace(
            validator=resolved_validator,
            tracker=tracker,
            selector=object(),
            extractor=FeatureExtractor(),
            ball_proximity_analyzer=_NullAnalyzer(),
            movement_analyzer=_NullAnalyzer(),
            interaction_analyzer=_InteractionAnalyzer(),
            technical_event_analyzer=_NullAnalyzer(),
            pass_detector=_NullAnalyzer(),
            shot_detector=_NullAnalyzer(),
            physical_scorer=physical_scorer or _PhysicalScorer(),
        ),
    )
    runtime = process_entrypoint._ChildRuntime(
        settings,
        components,
        VideoPathResolver(settings.video_storage_root),
        ArtifactManager(Path(settings.debug_output_dir), settings.max_upload_bytes),
        logger or logging.getLogger("football_analysis.child"),
    )
    monkeypatch.setattr(process_entrypoint, "_runtime", runtime)
    selected = target or _established_target()
    monkeypatch.setattr(routes, "resolve_dominant_target", lambda *args, **kwargs: selected)

    if patch_completed:

        def completed(*args: object, **kwargs: object) -> CompletedResponse:
            del kwargs
            return _response(str(args[15]))

        monkeypatch.setattr(routes, "_completed", completed)
    return runtime, resolved_validator, tracker


def _request(analysis_id: str) -> ChildAnalysisRequest:
    return ChildAnalysisRequest(analysis_id, "private-video-id", "private-player-id", "safe.mp4")


def _direct_child_call(
    runtime: process_entrypoint._ChildRuntime,
    analysis_id: str,
    artifacts: ArtifactSession,
) -> AnalyzeResponse:
    components = runtime.components
    started = 0.0
    return routes._analyze_uploaded(
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
        analysis_id,
        started,
        started,
        Path(runtime.settings.video_storage_root) / "safe.mp4",
        CancellationManager(analysis_id),
        artifacts,
        {},
    )


def _timing_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for record in caplog.records:
        message = record.getMessage()
        parts = message.split()
        if not parts or parts[0] != _EVENT:
            continue
        fields: dict[str, str] = {}
        for part in parts[1:]:
            assert "=" in part, f"unstructured P1-A timing token: {part!r}"
            key, value = part.split("=", 1)
            assert key not in fields, f"duplicate P1-A timing field: {key}"
            fields[key] = value
        records.append(fields)
    return records


def _require_timing_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    records = _timing_records(caplog)
    assert records, "P1-A RED: coarse analysis_stage_timing records do not exist yet"
    return records


def _for_analysis(records: list[dict[str, str]], analysis_id: str) -> list[dict[str, str]]:
    return [record for record in records if record.get("analysis_id") == analysis_id]


def _one_stage(
    records: list[dict[str, str]], stage: str, *, outcome: str | None = None
) -> dict[str, str]:
    matches = [record for record in records if record.get("stage") == stage]
    assert len(matches) == 1, f"expected one aggregate record for {stage}, got {len(matches)}"
    record = matches[0]
    if outcome is not None:
        assert record["outcome"] == outcome
    return record


def _assert_record_shape(record: dict[str, str], *, role: str) -> None:
    assert set(record) == _ALLOWED_FIELDS
    assert record["timing_schema_version"] == _SCHEMA_VERSION
    assert record["role"] == role
    assert record["stage"] in (_CHILD_STAGES if role == "child" else _PARENT_STAGES)
    assert record["outcome"] in _ALLOWED_OUTCOMES
    assert int(record["duration_ms"]) >= 0


def _serialized_timing_records(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        record.getMessage() for record in caplog.records if record.getMessage().startswith(_EVENT)
    )


def test_established_target_records_child_coarse_stages_and_preserves_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    child_clock = _StepClock(10_000.0, 0.125)
    monkeypatch.setattr(routes, "perf_counter", child_clock)
    monkeypatch.setattr(process_entrypoint, "perf_counter", child_clock)
    _install_child_runtime(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="football_analysis")

    with capture_application_logs(caplog):
        result = process_entrypoint.run_child_analysis(_request("established"))

    assert isinstance(result, ChildAnalysisSuccess)
    response = validate_child_result("established", result.schema_version, result)
    assert response == _response("established")
    assert isinstance(response, CompletedResponse)
    assert response.result_availability == "AVAILABLE"
    assert response.selected_player is not None
    assert response.selected_player.segment_id == 1
    assert response.scores is not None
    assert isinstance(response.scores.technical, TechnicalScoreResponse)
    assert response.scores.technical.evidence == {"controlled_movement_events": 1.0}
    assert isinstance(response.scores.physical, PhysicalScoreResponse)
    assert response.scores.physical.evidence is not None
    assert response.player_rating_summary is not None
    assert response.player_rating_summary.available_category_count >= 2
    assert response.player_rating_summary.overall.status == "available"
    records = _for_analysis(_require_timing_records(caplog), "established")
    assert {record["stage"] for record in records} == _CHILD_STAGES
    for stage in _CHILD_STAGES - {"debug_work"}:
        _one_stage(records, stage, outcome="success")
    _one_stage(records, "debug_work", outcome="skipped")
    for record in records:
        _assert_record_shape(record, role="child")


def test_unavailable_target_records_only_truthful_work_and_preserves_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _StepClock(20_000.0, 0.125)
    monkeypatch.setattr(routes, "perf_counter", clock)
    monkeypatch.setattr(process_entrypoint, "perf_counter", clock)
    _install_child_runtime(tmp_path, monkeypatch, target=_unavailable_target())
    caplog.set_level(logging.INFO, logger="football_analysis")

    with capture_application_logs(caplog):
        envelope = process_entrypoint.run_child_analysis(_request("unavailable"))

    assert isinstance(envelope, ChildAnalysisSuccess)
    result = validate_child_result("unavailable", envelope.schema_version, envelope)
    assert isinstance(result, CompletedResponse)
    assert result.result_availability == "UNAVAILABLE"
    assert result.selected_player is None
    assert result.scores is None
    records = _for_analysis(_require_timing_records(caplog), "unavailable")
    for stage in (
        "input_materialization",
        "validation",
        "tracking_total",
        "reproducibility_hashing",
        "child_serialization",
        "child_cleanup",
    ):
        _one_stage(records, stage, outcome="success")
    _one_stage(records, "target_segment_resolution", outcome="unavailable")
    _one_stage(records, "debug_work", outcome="skipped")
    assert not any(
        record.get("stage") == "post_processing_scoring" and record.get("outcome") == "success"
        for record in records
    )


def test_primary_failure_uses_safe_failed_timing_and_keeps_exact_direct_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = PrimaryFailure("private exception message")
    validator = _Validator(primary)
    runtime, _, _ = _install_child_runtime(
        tmp_path,
        monkeypatch,
        validator=validator,
    )
    cleanup_calls: list[str] = []
    original_cleanup = ArtifactSession.cleanup

    def cleanup(session: ArtifactSession) -> CleanupResult:
        cleanup_calls.append(session.request_id)
        return original_cleanup(session)

    monkeypatch.setattr(ArtifactSession, "cleanup", cleanup)
    direct_artifacts = runtime.artifact_manager.create_session("direct-primary")
    try:
        with pytest.raises(PrimaryFailure) as caught:
            _direct_child_call(runtime, "direct-primary", direct_artifacts)
        assert caught.value is primary
    finally:
        direct_artifacts.cleanup()

    caplog.set_level(logging.INFO, logger="football_analysis")
    with capture_application_logs(caplog):
        result = process_entrypoint.run_child_analysis(_request("primary-failure"))

    assert isinstance(result, ChildAnalysisFailure)
    assert result.error_code == "PrimaryFailure"
    assert result.public_message == "Analysis could not be completed."
    assert "private exception message" not in result.public_message
    assert cleanup_calls == ["direct-primary", "primary-failure"]
    records = _for_analysis(_require_timing_records(caplog), "primary-failure")
    failed = _one_stage(records, "validation", outcome="failed")
    _assert_record_shape(failed, role="child")
    for record in records:
        _assert_record_shape(record, role="child")
    assert "private exception message" not in _serialized_timing_records(caplog)

    logging_root = tmp_path / "logging-boundary"
    logging_root.mkdir()
    logging_primary = PrimaryFailure("private logging-boundary detail")
    logging_validator = _Validator(logging_primary)
    logger = _FailingTimingLogger()
    _install_child_runtime(
        logging_root,
        monkeypatch,
        validator=logging_validator,
        logger=cast(logging.Logger, logger),
    )

    failure = process_entrypoint.run_child_analysis(_request("logging-failure"))
    logging_validator.error = None
    success = process_entrypoint.run_child_analysis(_request("logging-success"))

    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "PrimaryFailure"
    assert failure.public_message == "Analysis could not be completed."
    assert isinstance(success, ChildAnalysisSuccess)
    assert validate_child_result("logging-success", success.schema_version, success) == _response(
        "logging-success"
    )
    assert logger.timing_attempts > 0, "P1-A RED: timing logging is not attempted yet"


def test_cleanup_failure_preserves_primary_precedence_and_owns_success_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = PrimaryFailure("private primary detail")
    cleanup_error = CleanupFailure("private cleanup detail")
    runtime, validator, _ = _install_child_runtime(tmp_path, monkeypatch)
    cleanup_calls: list[str] = []

    def broken_cleanup(session: ArtifactSession) -> CleanupResult:
        cleanup_calls.append(session.request_id)
        raise cleanup_error

    monkeypatch.setattr(ArtifactSession, "cleanup", broken_cleanup)
    caplog.set_level(logging.INFO, logger="football_analysis")
    with capture_application_logs(caplog):
        validator.error = primary
        primary_result = process_entrypoint.run_child_analysis(_request("primary-cleanup"))
        validator.error = None
        success_result = process_entrypoint.run_child_analysis(_request("success-cleanup"))

    assert runtime is process_entrypoint._runtime
    assert isinstance(primary_result, ChildAnalysisFailure)
    assert primary_result.error_code == "PrimaryFailure"
    assert primary_result.public_message == "Analysis could not be completed."
    assert isinstance(success_result, ChildAnalysisFailure)
    assert success_result.error_code == "CleanupFailure"
    assert success_result.public_message == "Analysis could not be completed."
    assert cleanup_calls == ["primary-cleanup", "success-cleanup"]
    records = _require_timing_records(caplog)
    for analysis_id in ("primary-cleanup", "success-cleanup"):
        analysis_records = _for_analysis(records, analysis_id)
        cleanup_record = _one_stage(analysis_records, "child_cleanup", outcome="failed")
        _assert_record_shape(cleanup_record, role="child")
        for record in analysis_records:
            _assert_record_shape(record, role="child")
    primary_record = _one_stage(
        _for_analysis(records, "primary-cleanup"), "validation", outcome="failed"
    )
    _assert_record_shape(primary_record, role="child")
    serialized = _serialized_timing_records(caplog)
    assert "private primary detail" not in serialized
    assert "private cleanup detail" not in serialized


def test_cancellation_classification_and_cleanup_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    validator = _Validator(AnalysisCancelled())
    _install_child_runtime(tmp_path, monkeypatch, validator=validator)
    cleanup_calls: list[str] = []
    original_cleanup = ArtifactSession.cleanup

    def cleanup(session: ArtifactSession) -> CleanupResult:
        cleanup_calls.append(session.request_id)
        return original_cleanup(session)

    monkeypatch.setattr(ArtifactSession, "cleanup", cleanup)
    caplog.set_level(logging.INFO, logger="football_analysis")
    with capture_application_logs(caplog):
        result = process_entrypoint.run_child_analysis(_request("cancelled"))

    assert isinstance(result, ChildAnalysisCancelled)
    assert cleanup_calls == ["cancelled"]
    records = _for_analysis(_require_timing_records(caplog), "cancelled")
    _one_stage(records, "validation", outcome="cancelled")
    _one_stage(records, "child_cleanup", outcome="success")
    assert not any(
        record.get("outcome") == "success"
        for record in records
        if record.get("stage")
        in {"tracking_total", "post_processing_scoring", "child_serialization"}
    )


def test_debug_disabled_records_skipped_without_starting_debug_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_child_runtime(tmp_path, monkeypatch, target=_unavailable_target())

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("debug work must not start when disabled")

    monkeypatch.setattr(routes, "copyfile", forbidden)
    monkeypatch.setattr(routes, "render_debug_video", forbidden)
    monkeypatch.setattr(routes, "CameraMotionEstimator", forbidden)
    caplog.set_level(logging.INFO, logger="football_analysis")
    with capture_application_logs(caplog):
        result = process_entrypoint.run_child_analysis(_request("debug-disabled"))

    assert isinstance(result, ChildAnalysisSuccess)
    records = _for_analysis(_require_timing_records(caplog), "debug-disabled")
    debug_record = _one_stage(records, "debug_work", outcome="skipped")
    assert int(debug_record["duration_ms"]) == 0


def test_debug_enabled_aggregates_only_copy_camera_and_published_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _ManualClock(4096.0)
    calls: list[str] = []

    def copy_debug_source(source: Path, destination: Path) -> Path:
        calls.append("source_copy")
        destination.write_bytes(source.read_bytes())
        clock.advance(0.125)
        return destination

    class CameraEstimator:
        def estimate(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("camera_motion")
            clock.advance(0.25)
            return None

    def render_debug(
        output_source: Path, output: Path, *args: object, **kwargs: object
    ) -> dict[str, str]:
        del output_source, args, kwargs
        calls.append("render_publication")
        output.mkdir()
        video = output / "debug_video.mp4"
        video.write_bytes(b"deterministic-debug-artifact")
        clock.advance(0.5)
        return {"debug_video": str(video)}

    monkeypatch.setattr(routes, "perf_counter", clock)
    monkeypatch.setattr(process_entrypoint, "perf_counter", clock)
    monkeypatch.setattr(routes, "copyfile", copy_debug_source)
    monkeypatch.setattr(routes, "CameraMotionEstimator", CameraEstimator)
    monkeypatch.setattr(routes, "render_debug_video", render_debug)
    non_debug_scorer = _PhysicalScorer(clock)
    _install_child_runtime(
        tmp_path,
        monkeypatch,
        debug=DebugSettings(enabled=True, save_video=True),
        patch_completed=False,
        physical_scorer=non_debug_scorer,
    )
    caplog.set_level(logging.INFO, logger="football_analysis")

    with capture_application_logs(caplog):
        envelope = process_entrypoint.run_child_analysis(_request("debug-enabled"))

    assert isinstance(envelope, ChildAnalysisSuccess)
    response = validate_child_result("debug-enabled", envelope.schema_version, envelope)
    assert isinstance(response, CompletedResponse)
    assert response.selected_player is not None
    assert response.scores is not None
    assert response.debug_artifacts == {"debug_video": "debug_video.mp4"}
    assert calls == ["source_copy", "camera_motion", "render_publication"]
    assert non_debug_scorer.calls == 1
    retained = tmp_path / "debug" / "debug-enabled"
    assert (retained / "source_video.mp4").read_bytes() == b"synthetic-contract-bytes"
    assert (retained / "debug_render" / "debug_video.mp4").read_bytes() == (
        b"deterministic-debug-artifact"
    )
    records = _for_analysis(_require_timing_records(caplog), "debug-enabled")
    debug_record = _one_stage(records, "debug_work", outcome="success")
    _assert_record_shape(debug_record, role="child")
    assert int(debug_record["duration_ms"]) == 875


def test_parent_and_child_clocks_are_independent_and_parent_duration_is_nested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_child_clock = _StepClock(16.0, 0.125)
    parent_clock = _StepClock(1_048_576.0, 0.25)
    monkeypatch.setattr(routes, "perf_counter", first_child_clock)
    monkeypatch.setattr(process_entrypoint, "perf_counter", first_child_clock)
    monkeypatch.setattr(process_analysis_pool, "perf_counter", parent_clock, raising=False)
    _install_child_runtime(tmp_path, monkeypatch)
    executor = _InlineExecutor(parent_clock)
    pool = ProcessAnalysisPool(
        Settings(),
        executor_factory=_InlineFactory(executor),
        logger=logging.getLogger("football_analysis.process_pool"),
    )
    pool.start()
    caplog.set_level(logging.INFO, logger="football_analysis")

    async def scenario() -> tuple[ParentChildResult, ParentChildResult]:
        try:
            first = await pool.execute(_request("clock-origin-a"))
            second_child_clock = _StepClock(8192.0, 0.125)
            monkeypatch.setattr(routes, "perf_counter", second_child_clock)
            monkeypatch.setattr(process_entrypoint, "perf_counter", second_child_clock)
            second = await pool.execute(_request("clock-origin-b"))
            return first, second
        finally:
            await pool.shutdown()

    with capture_application_logs(caplog):
        first_result, second_result = asyncio.run(scenario())

    assert first_result == _response("clock-origin-a")
    assert second_result == _response("clock-origin-b")
    all_records = _require_timing_records(caplog)
    parent_durations: list[int] = []
    for analysis_id in ("clock-origin-a", "clock-origin-b"):
        records = _for_analysis(all_records, analysis_id)
        child_records = [record for record in records if record.get("role") == "child"]
        assert child_records
        for record in child_records:
            _assert_record_shape(record, role="child")
            duration = int(record["duration_ms"])
            if record["outcome"] == "skipped":
                assert duration == 0
            else:
                assert duration > 0
                assert duration % 125 == 0
        parent_record = _one_stage(records, "process_ipc_parent_validation", outcome="success")
        _assert_record_shape(parent_record, role="parent")
        parent_durations.append(int(parent_record["duration_ms"]))
    assert parent_durations == [1250, 1250]


def test_parent_mapping_and_timing_logs_are_bounded_safe_and_non_public(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _StepClock(100.0, 0.125)
    monkeypatch.setattr(routes, "perf_counter", clock)
    response = _response("parent-mapping")
    callback = _Callback(clock)
    job = AnalysisJob.create(
        "parent-mapping",
        "private-video-id",
        "private-player-id",
        "private-video-name.mp4",
        HttpUrl("http://192.0.2.10/private-callback"),
    )
    processor = routes.create_process_analysis_job_processor(
        _ParentPool(response),
        callback,
        logging.getLogger("football_analysis.api"),
    )
    caplog.set_level(logging.INFO, logger="football_analysis")

    async def scenario() -> AnalysisJobState:
        return await processor(job)

    with capture_application_logs(caplog):
        state = asyncio.run(scenario())

    assert state is AnalysisJobState.COMPLETED
    assert len(callback.payloads) == 1
    payload = callback.payloads[0]
    assert isinstance(payload, CallbackPayload)
    callback_request = AnalyzeRequest.model_validate(
        {
            "videoId": "private-video-id",
            "playerId": "private-player-id",
            "videoUrl": "private-video-name.mp4",
            "callbackUrl": "http://192.0.2.10/private-callback",
        }
    )
    assert payload == routes._callback_payload(callback_request, response)
    assert payload.status == "COMPLETED"
    assert "stage_timing" not in payload.model_dump(mode="json")
    records = _for_analysis(_require_timing_records(caplog), "parent-mapping")
    mapping = _one_stage(records, "parent_response_mapping", outcome="success")
    _assert_record_shape(mapping, role="parent")
    assert len(records) == 1
    serialized_records = "\n".join(
        record.getMessage() for record in caplog.records if record.getMessage().startswith(_EVENT)
    )
    for sensitive in (
        "private-video-id",
        "private-player-id",
        "private-video-name.mp4",
        "192.0.2.10",
        "private-callback",
        "private exception message",
        "private cleanup detail",
    ):
        assert sensitive not in serialized_records
    assert not any(character in serialized_records for character in "[]{}")
    execution = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("analysis_execution_finished")
    )
    analysis_duration = int(
        next(
            token.split("=", 1)[1]
            for token in execution.split()
            if token.startswith("analysis_duration_ms=")
        )
    )
    assert analysis_duration < 1_700


def test_timing_logging_preserves_propagation_and_uses_one_capture_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application_logger = logging.getLogger("football_analysis")
    root_logger = logging.getLogger()
    timing_logger = logging.getLogger("football_analysis.p1a_propagation_contract")
    capture_handler = caplog.handler

    def handler_index(logger: logging.Logger) -> int | None:
        return next(
            (
                index
                for index, candidate in enumerate(logger.handlers)
                if candidate is capture_handler
            ),
            None,
        )

    def restore_handler(logger: logging.Logger, original_index: int | None) -> None:
        current_index = handler_index(logger)
        if current_index == original_index:
            return
        if current_index is not None:
            logger.removeHandler(capture_handler)
        if original_index is not None:
            logger.handlers.insert(original_index, capture_handler)

    def assert_handler_order(logger: logging.Logger, expected: tuple[logging.Handler, ...]) -> None:
        assert len(logger.handlers) == len(expected)
        assert all(
            actual is original for actual, original in zip(logger.handlers, expected, strict=True)
        )

    def application_result(logger: _FailingTimingLogger, analysis_id: str) -> str:
        _log_analysis_stage_timing(
            cast(logging.Logger, logger),
            analysis_id=f"{analysis_id}-logging-failure",
            stage="validation",
            role="child",
            outcome="failed",
            duration_ms=125,
        )
        return "preserved-application-result"

    original_application_handlers = tuple(application_logger.handlers)
    original_root_handlers = tuple(root_logger.handlers)
    original_application_propagate = application_logger.propagate
    original_application_level = application_logger.level
    original_application_disabled = application_logger.disabled
    original_application_filters = tuple(application_logger.filters)
    original_root_level = root_logger.level
    original_root_disabled = root_logger.disabled
    original_root_filters = tuple(root_logger.filters)
    original_timing_level = timing_logger.level
    original_timing_disabled = timing_logger.disabled
    original_timing_filters = tuple(timing_logger.filters)
    original_application_capture_index = handler_index(application_logger)
    original_root_capture_index = handler_index(root_logger)

    try:
        application_logger.setLevel(logging.INFO)
        for propagate in (True, False):
            restore_handler(application_logger, original_application_capture_index)
            restore_handler(root_logger, original_root_capture_index)
            application_logger.propagate = propagate
            if propagate:
                if handler_index(root_logger) is None:
                    root_logger.addHandler(capture_handler)
                if handler_index(application_logger) is None:
                    application_logger.addHandler(capture_handler)
            elif handler_index(application_logger) is not None:
                application_logger.removeHandler(capture_handler)

            expected_application_handlers = tuple(application_logger.handlers)
            expected_root_handlers = tuple(root_logger.handlers)
            expected_logger_state = (
                application_logger.level,
                application_logger.disabled,
                tuple(application_logger.filters),
                root_logger.level,
                root_logger.disabled,
                tuple(root_logger.filters),
                timing_logger.level,
                timing_logger.disabled,
                tuple(timing_logger.filters),
            )
            analysis_id = f"propagation-{str(propagate).lower()}"
            caplog.clear()

            with capture_application_logs(caplog):
                assert application_logger.propagate is propagate
                _log_analysis_stage_timing(
                    timing_logger,
                    analysis_id=analysis_id,
                    stage="validation",
                    role="child",
                    outcome="success",
                    duration_ms=125,
                )
                assert application_logger.propagate is propagate

            assert application_logger.propagate is propagate
            assert_handler_order(application_logger, expected_application_handlers)
            assert_handler_order(root_logger, expected_root_handlers)
            records = _for_analysis(_timing_records(caplog), analysis_id)
            record = _one_stage(records, "validation", outcome="success")
            _assert_record_shape(record, role="child")
            assert len(records) == 1

            failing_logger = _FailingTimingLogger()
            assert application_result(failing_logger, analysis_id) == "preserved-application-result"
            primary = PrimaryFailure("preserved primary exception")
            observed_primary: PrimaryFailure | None = None
            try:
                raise primary
            except PrimaryFailure as error:
                _log_analysis_stage_timing(
                    cast(logging.Logger, failing_logger),
                    analysis_id=f"{analysis_id}-primary-failure",
                    stage="validation",
                    role="child",
                    outcome="failed",
                    duration_ms=125,
                )
                observed_primary = error

            assert observed_primary is primary
            assert failing_logger.timing_attempts == 2
            assert application_logger.propagate is propagate
            assert_handler_order(application_logger, expected_application_handlers)
            assert_handler_order(root_logger, expected_root_handlers)
            assert (
                application_logger.level,
                application_logger.disabled,
                tuple(application_logger.filters),
                root_logger.level,
                root_logger.disabled,
                tuple(root_logger.filters),
                timing_logger.level,
                timing_logger.disabled,
                tuple(timing_logger.filters),
            ) == expected_logger_state
    finally:
        restore_handler(application_logger, original_application_capture_index)
        restore_handler(root_logger, original_root_capture_index)
        application_logger.propagate = original_application_propagate
        application_logger.setLevel(original_application_level)
        application_logger.disabled = original_application_disabled
        application_logger.filters[:] = original_application_filters
        root_logger.setLevel(original_root_level)
        root_logger.disabled = original_root_disabled
        root_logger.filters[:] = original_root_filters
        timing_logger.setLevel(original_timing_level)
        timing_logger.disabled = original_timing_disabled
        timing_logger.filters[:] = original_timing_filters

    assert_handler_order(application_logger, original_application_handlers)
    assert_handler_order(root_logger, original_root_handlers)
