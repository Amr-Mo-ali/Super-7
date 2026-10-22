"""Focused evidence for the unused MVP-2B2 child entry point."""

import asyncio
import logging
import pickle
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from application_log_capture import capture_application_logs

from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.exceptions import IncompleteVideoDecodeError, ModelLoadingError, VideoAccessError
from core.logging import configure_logging as configure_application_logging
from diagnostics.artifacts import ArtifactSession, CleanupResult
from schemas.analysis import Diagnostics, NonCompletedResponse
from services import process_entrypoint
from services.process_analysis_pool import ProcessAnalysisPool
from services.process_contracts import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    ChildFailureKind,
    ParentCancelled,
    ParentFailure,
    validate_child_result,
)
from services.process_entrypoint import initialize_analysis_child, run_child_analysis
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoValidator


@pytest.fixture(autouse=True)
def reset_runtime() -> Iterator[None]:
    process_entrypoint._reset_child_runtime_for_test()
    yield
    process_entrypoint._reset_child_runtime_for_test()


def _settings(tmp_path: Path) -> Settings:
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "safe.mp4").touch()
    return Settings(video_storage_root=str(videos), debug_output_dir=str(tmp_path / "debug"))


def _request(analysis_id: str = "analysis-1") -> ChildAnalysisRequest:
    return ChildAnalysisRequest(analysis_id, "video-1", "player-1", "safe.mp4")


def _response(analysis_id: str) -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id=analysis_id,
        status="no_players_detected",
        warnings=[],
        diagnostics=Diagnostics(
            frames_processed=0,
            frames_with_player_detections=0,
            total_person_detections=0,
            tracks_created=0,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
    )


def test_contracts_pickle_and_reject_unsafe_values() -> None:
    values = (
        _request(),
        ChildAnalysisSuccess(
            "analysis-1", _response("analysis-1").model_dump_json(), "v1", "model", 0
        ),
        ChildAnalysisFailure("analysis-1", "RuntimeError", "Analysis could not be completed.", 0),
        ChildAnalysisCancelled("analysis-1", 0),
    )
    assert pickle.loads(pickle.dumps(Settings())) == Settings()
    assert all(pickle.loads(pickle.dumps(value)) == value for value in values)
    for unsafe in ("", "bad id", "bad\n"):
        with pytest.raises(ValueError):
            ChildAnalysisRequest(unsafe, "video", "player", "safe.mp4")
    for unsafe in (
        "/safe.mp4",
        "C:\\safe.mp4",
        "../safe.mp4",
        "dir/safe.mp4",
        "dir\\safe.mp4",
        "safe\x00.mp4",
        " safe.mp4",
    ):
        with pytest.raises(ValueError):
            ChildAnalysisRequest("id", "video", "player", unsafe)
    with pytest.raises(ValueError):
        ChildAnalysisSuccess("id", "[]", "v", "m", 0)
    with pytest.raises(ValueError):
        ChildAnalysisFailure("id", "bad code", "Analysis could not be completed.", 0)
    with pytest.raises(ValueError, match="analysis_version"):
        ChildAnalysisRequest("id", "video", "player", "safe.mp4", analysis_version="bad version")


def test_durable_request_rejects_unsupported_version_before_analysis(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)
    request = ChildAnalysisRequest(
        "analysis-1", "video-1", "player-1", "safe.mp4", analysis_version="other_version"
    )
    with pytest.raises(RuntimeError, match="does not support the requested version"):
        run_child_analysis(request)


def test_child_success_failure_cancellation_and_parent_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)
    calls: list[object] = []

    def successful(*args: object) -> NonCompletedResponse:
        calls.append(args[2])
        return _response("analysis-1")

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", successful)
    result = run_child_analysis(_request())
    assert isinstance(result, ChildAnalysisSuccess)
    assert validate_child_result("analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, result) == _response(
        "analysis-1"
    )
    assert len(calls) == 1

    def broken(*_: object) -> NonCompletedResponse:
        raise RuntimeError(f"secret {tmp_path}")

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", broken)
    failure = run_child_analysis(_request())
    assert failure == ChildAnalysisFailure(
        "analysis-1",
        "RuntimeError",
        "Analysis could not be completed.",
        failure.processing_duration_ms,
    )
    assert str(tmp_path) not in repr(failure)
    assert validate_child_result(
        "analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, failure
    ) == ParentFailure("RuntimeError", "Analysis could not be completed.")

    def cancelled(*_: object) -> NonCompletedResponse:
        raise AnalysisCancelled()

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", cancelled)
    cancellation = run_child_analysis(_request())
    assert isinstance(cancellation, ChildAnalysisCancelled)
    assert validate_child_result(
        "analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, cancellation
    ) == ParentCancelled("analysis-1")


def test_staged_child_uses_parent_owned_file_and_local_requests_still_use_storage_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()
    staged.write_bytes(b"r2 bytes")
    observed: list[Path] = []

    def successful(*args: object) -> NonCompletedResponse:
        observed.extend(value for value in args if isinstance(value, Path))
        return _response("analysis-1")

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", successful)
    staged_request = ChildAnalysisRequest(
        "analysis-1", "video-1", "player-1", "safe.mp4", staged_video_path=str(staged)
    )
    assert isinstance(run_child_analysis(staged_request), ChildAnalysisSuccess)
    assert observed == [staged.resolve()]
    observed.clear()
    assert isinstance(run_child_analysis(_request()), ChildAnalysisSuccess)
    assert observed == [(Path(settings.video_storage_root) / "safe.mp4").resolve()]

    with pytest.raises(ValueError, match="staged video"):
        ChildAnalysisRequest(
            "analysis-1", "video-1", "player-1", "safe.mp4", staged_video_path="relative/safe.mp4"
        )


def test_only_missing_video_at_resolution_carries_typed_evidence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)
    missing = run_child_analysis(
        ChildAnalysisRequest("missing", "video-1", "player-1", "missing.mp4")
    )
    assert isinstance(missing, ChildAnalysisFailure)
    assert missing.failure_kind is ChildFailureKind.VIDEO_UNAVAILABLE
    assert validate_child_result(
        "missing", CHILD_ANALYSIS_SCHEMA_VERSION, missing
    ) == ParentFailure(
        "VideoNotFoundError", "Analysis could not be completed.", ChildFailureKind.VIDEO_UNAVAILABLE
    )

    runtime = process_entrypoint._runtime
    assert runtime is not None
    runtime.path_resolver = VideoPathResolver(
        settings.video_storage_root, lambda path, _: path.is_dir()
    )
    inaccessible = run_child_analysis(_request("inaccessible"))
    assert isinstance(inaccessible, ChildAnalysisFailure)
    assert inaccessible.failure_kind is None
    assert inaccessible.error_code == "VideoAccessError"

    process_entrypoint._reset_child_runtime_for_test()
    initialize_analysis_child(
        Settings(
            video_storage_root=str(tmp_path / "missing-root"),
            debug_output_dir=str(tmp_path / "debug"),
        )
    )
    root_failure = run_child_analysis(_request("root-failure"))
    assert isinstance(root_failure, ChildAnalysisFailure)
    assert root_failure.failure_kind is None
    assert root_failure.error_code == "VideoStorageRootError"


def test_missing_staged_file_before_resolution_remains_untyped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()
    initialize_analysis_child(settings)

    failure = run_child_analysis(
        ChildAnalysisRequest(
            "analysis-1",
            "video-1",
            "player-1",
            "safe.mp4",
            analysis_version=settings.analysis_version,
            staged_video_path=str(staged),
        )
    )
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "VideoNotFoundError"
    assert failure.failure_kind is None
    assert validate_child_result(
        "analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, failure
    ) == ParentFailure("VideoNotFoundError", "Analysis could not be completed.")
    assert not (Path(settings.debug_output_dir) / "analysis-1").exists()


def _decodable_video(path: Path) -> Path:
    codec = cv2.VideoWriter_fourcc(*"MJPG")  # type: ignore[attr-defined]
    writer = cv2.VideoWriter(str(path), codec, 10.0, (64, 64))
    assert writer.isOpened()
    try:
        for intensity in (0, 100, 255):
            writer.write(np.full((64, 64, 3), intensity, dtype=np.uint8))
    finally:
        writer.release()
    return path


def test_unreadable_staged_bytes_remain_untyped_without_model_inference(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()
    staged.write_bytes(b"not a decodable video")
    initialize_analysis_child(settings)
    request = ChildAnalysisRequest(
        "analysis-1",
        "video-1",
        "player-1",
        "safe.mp4",
        analysis_version=settings.analysis_version,
        staged_video_path=str(staged),
    )
    failure = run_child_analysis(request)
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "InvalidVideoError"
    # Decoder-open failure alone cannot distinguish bad bytes from a missing codec.
    assert failure.failure_kind is None
    assert validate_child_result(
        "analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, failure
    ) == ParentFailure("InvalidVideoError", "Analysis could not be completed.")


def test_staged_file_disappearing_after_resolution_remains_untyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()
    staged.write_bytes(b"present at path resolution")
    initialize_analysis_child(settings)
    materialize = ArtifactSession.materialize_input

    def disappear(session: ArtifactSession, source: Path, limit: int) -> Path:
        source.unlink()
        return materialize(session, source, limit)

    monkeypatch.setattr(ArtifactSession, "materialize_input", disappear)
    failure = run_child_analysis(
        ChildAnalysisRequest(
            "analysis-1",
            "video-1",
            "player-1",
            "safe.mp4",
            analysis_version=settings.analysis_version,
            staged_video_path=str(staged),
        )
    )
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "InvalidVideoError"
    assert failure.failure_kind is None
    assert not (Path(settings.debug_output_dir) / "analysis-1").exists()


def test_valid_content_with_simulated_decoder_unavailability_remains_untyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.avi"
    staged.parent.mkdir()
    _decodable_video(staged)
    assert VideoValidator(settings).validate(staged).frame_count == 3
    initialize_analysis_child(settings)

    class UnavailableDecoder:
        def isOpened(self) -> bool:
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda _: UnavailableDecoder())
    failure = run_child_analysis(
        ChildAnalysisRequest(
            "analysis-1",
            "video-1",
            "player-1",
            "safe.avi",
            analysis_version=settings.analysis_version,
            staged_video_path=str(staged),
        )
    )
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "InvalidVideoError"
    assert failure.failure_kind is None
    assert staged.exists()
    assert not (Path(settings.debug_output_dir) / "analysis-1").exists()


def test_confirmed_resolution_rule_is_typed_only_for_staged_child(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), min_width=65)
    staged = tmp_path / "staged" / "safe.avi"
    staged.parent.mkdir()
    _decodable_video(staged)
    local = Path(settings.video_storage_root) / "safe.avi"
    local.write_bytes(staged.read_bytes())
    initialize_analysis_child(settings)

    def request(analysis_id: str, *, staged_path: str | None) -> ChildAnalysisRequest:
        return ChildAnalysisRequest(
            analysis_id,
            "video-1",
            "player-1",
            "safe.avi",
            analysis_version=settings.analysis_version,
            staged_video_path=staged_path,
        )

    durable = run_child_analysis(request("analysis-staged", staged_path=str(staged)))
    legacy = run_child_analysis(request("analysis-local", staged_path=None))
    assert isinstance(durable, ChildAnalysisFailure)
    assert durable.error_code == "InvalidVideoError"
    assert durable.failure_kind is ChildFailureKind.VIDEO_INVALID
    assert isinstance(legacy, ChildAnalysisFailure)
    assert legacy.error_code == "InvalidVideoError"
    assert legacy.failure_kind is None


def test_confirmed_rule_survives_spawned_child_boundary(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), min_width=65)
    staged = tmp_path / "staged" / "safe.avi"
    staged.parent.mkdir()
    _decodable_video(staged)

    async def scenario() -> None:
        pool = ProcessAnalysisPool(settings)
        pool.start()
        try:
            result = await pool.execute_enveloped(
                ChildAnalysisRequest(
                    "analysis-spawn",
                    "video-1",
                    "player-1",
                    "safe.avi",
                    analysis_version=settings.analysis_version,
                    staged_video_path=str(staged),
                )
            )
            assert result == ParentFailure(
                "InvalidVideoError",
                "Analysis could not be completed.",
                ChildFailureKind.VIDEO_INVALID,
            )
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


def test_missing_staged_file_remains_untyped_across_spawned_child_boundary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()

    async def scenario() -> None:
        pool = ProcessAnalysisPool(settings)
        pool.start()
        try:
            result = await pool.execute_enveloped(
                ChildAnalysisRequest(
                    "analysis-spawn-missing",
                    "video-1",
                    "player-1",
                    "safe.mp4",
                    analysis_version=settings.analysis_version,
                    staged_video_path=str(staged),
                )
            )
            assert result == ParentFailure("VideoNotFoundError", "Analysis could not be completed.")
            assert not pool.outstanding_submissions
        finally:
            await pool.shutdown()

    asyncio.run(scenario())
    assert not (Path(settings.debug_output_dir) / "analysis-spawn-missing").exists()


@pytest.mark.parametrize(
    "error_type", (IncompleteVideoDecodeError, ModelLoadingError, VideoAccessError)
)
def test_staged_child_does_not_classify_operational_or_environment_failures_as_invalid_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    settings = _settings(tmp_path)
    staged = tmp_path / "staged" / "safe.mp4"
    staged.parent.mkdir()
    staged.write_bytes(b"video bytes")
    initialize_analysis_child(settings)

    def fail(*args: object) -> NonCompletedResponse:
        raise error_type("synthetic operational failure")

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", fail)
    failure = run_child_analysis(
        ChildAnalysisRequest(
            "analysis-1",
            "video-1",
            "player-1",
            "safe.mp4",
            analysis_version=settings.analysis_version,
            staged_video_path=str(staged),
        )
    )
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == error_type.__name__
    assert failure.failure_kind is None


def test_parent_rejects_malformed_received_failure_evidence() -> None:
    failure = ChildAnalysisFailure(
        "analysis-1",
        "VideoNotFoundError",
        "Analysis could not be completed.",
        0,
        failure_kind=ChildFailureKind.VIDEO_UNAVAILABLE,
    )
    object.__setattr__(failure, "failure_kind", "VIDEO_UNAVAILABLE")
    with pytest.raises(TypeError, match="typed evidence"):
        validate_child_result("analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, failure)

    object.__setattr__(failure, "failure_kind", ChildFailureKind.VIDEO_UNAVAILABLE)
    object.__setattr__(failure, "public_message", "untrusted detail")
    with pytest.raises(ValueError, match="fixed sanitized message"):
        validate_child_result("analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, failure)


def test_runtime_initialization_and_parent_validation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(RuntimeError, match="not initialized"):
        run_child_analysis(_request())
    initialize_analysis_child(settings)
    initialize_analysis_child(settings)
    with pytest.raises(RuntimeError, match="different"):
        initialize_analysis_child(
            Settings(video_storage_root=str(tmp_path), debug_output_dir="other")
        )
    success = ChildAnalysisSuccess("analysis-1", _response("other").model_dump_json(), "v", "m", 0)
    with pytest.raises(ValueError, match="does not match"):
        validate_child_result("analysis-1", CHILD_ANALYSIS_SCHEMA_VERSION, success)
    with pytest.raises(ValueError, match="expected"):
        validate_child_result(
            "other", CHILD_ANALYSIS_SCHEMA_VERSION, ChildAnalysisCancelled("analysis-1", 0)
        )


def test_child_initialization_configures_logging_before_its_first_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    calls: list[str] = []
    original_configure_logging = configure_application_logging
    application_logger = logging.getLogger("football_analysis")

    def configure() -> None:
        calls.append("configure")
        original_configure_logging()

    class InitializationProbe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name == "football_analysis.child":
                assert calls == ["configure"]

    monkeypatch.setattr("services.process_entrypoint.configure_logging", configure)
    probe = InitializationProbe()
    application_logger.addHandler(probe)
    try:
        initialize_analysis_child(settings)
        initialize_analysis_child(settings)
    finally:
        application_logger.removeHandler(probe)

    assert calls == ["configure"]
    assert (
        len(
            [
                handler
                for handler in application_logger.handlers
                if getattr(handler, "_football_analysis_owned_handler", False)
            ]
        )
        == 1
    )


class AnalysisBoom(RuntimeError):
    pass


class CleanupBoom(RuntimeError):
    pass


def test_child_artifacts_cleanup_on_success_failure_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)

    def successful(*args: object) -> NonCompletedResponse:
        artifacts = args[18]
        assert isinstance(artifacts, ArtifactSession)
        artifacts.create(artifacts.reserve("artifact.bin", 1)).write_bytes(b"x")
        assert artifacts.directory.is_dir()
        return _response(str(args[13]))

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", successful)
    success = run_child_analysis(_request("success"))
    assert isinstance(success, ChildAnalysisSuccess)
    assert not (Path(settings.debug_output_dir) / "success").exists()

    def failed(*_: object) -> NonCompletedResponse:
        raise AnalysisBoom("secret-analysis-marker")

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", failed)
    failure = run_child_analysis(_request("failure"))
    assert isinstance(failure, ChildAnalysisFailure)
    assert failure.error_code == "AnalysisBoom"
    assert not (Path(settings.debug_output_dir) / "failure").exists()

    def cancelled(*_: object) -> NonCompletedResponse:
        raise AnalysisCancelled()

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", cancelled)
    cancellation = run_child_analysis(_request("cancelled"))
    assert isinstance(cancellation, ChildAnalysisCancelled)
    assert not (Path(settings.debug_output_dir) / "cancelled").exists()


@pytest.mark.parametrize(
    ("analysis", "expected_type", "expected_code"),
    [
        ("success", ChildAnalysisFailure, "ArtifactCleanupError"),
        ("failure", ChildAnalysisFailure, "AnalysisBoom"),
        ("cancelled", ChildAnalysisCancelled, None),
    ],
)
def test_cleanup_failure_is_sanitized_and_preserves_primary_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    analysis: str,
    expected_type: type[ChildAnalysisFailure] | type[ChildAnalysisCancelled],
    expected_code: str | None,
) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)
    cleanup_path = Path(settings.debug_output_dir) / analysis

    def cleanup_with_errors(_: ArtifactSession) -> CleanupResult:
        return CleanupResult((f"secret-cleanup-marker {cleanup_path}",))

    monkeypatch.setattr(ArtifactSession, "cleanup", cleanup_with_errors)

    def fake(*args: object) -> NonCompletedResponse:
        if analysis == "failure":
            raise AnalysisBoom("secret-analysis-marker")
        if analysis == "cancelled":
            raise AnalysisCancelled()
        return _response(str(args[13]))

    monkeypatch.setattr(process_entrypoint, "_analyze_uploaded", fake)
    caplog.set_level("WARNING", logger="football_analysis.child")
    with capture_application_logs(caplog):
        result = run_child_analysis(_request(analysis))
    assert isinstance(result, expected_type)
    if expected_code is not None:
        assert isinstance(result, ChildAnalysisFailure)
        assert result.error_code == expected_code
        assert result.public_message == "Analysis could not be completed."
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "analysis_child_cleanup_failed" in messages
    assert f"analysis_id={analysis}" in messages
    assert "cleanup_error_type=ArtifactCleanupError" in messages
    assert "secret-cleanup-marker" not in messages
    assert str(cleanup_path) not in messages
    assert settings.video_storage_root not in messages


def test_unexpected_cleanup_exception_remains_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    initialize_analysis_child(settings)

    def boom(_: ArtifactSession) -> CleanupResult:
        raise CleanupBoom("secret-cleanup-marker")

    monkeypatch.setattr(ArtifactSession, "cleanup", boom)
    monkeypatch.setattr(
        process_entrypoint, "_analyze_uploaded", lambda *args: _response(str(args[13]))
    )
    result = run_child_analysis(_request("exceptional-cleanup"))
    assert isinstance(result, ChildAnalysisFailure)
    assert result.error_code == "CleanupBoom"
