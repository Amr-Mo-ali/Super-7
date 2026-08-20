"""Focused evidence for the unused MVP-2B2 child entry point."""

import logging
import pickle
from collections.abc import Iterator
from pathlib import Path

import pytest
from application_log_capture import capture_application_logs

from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.logging import configure_logging as configure_application_logging
from diagnostics.artifacts import ArtifactSession, CleanupResult
from schemas.analysis import Diagnostics, NonCompletedResponse
from services import process_entrypoint
from services.process_contracts import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    ParentCancelled,
    ParentFailure,
    validate_child_result,
)
from services.process_entrypoint import initialize_analysis_child, run_child_analysis


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
