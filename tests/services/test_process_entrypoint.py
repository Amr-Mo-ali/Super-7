"""Focused evidence for the unused MVP-2B2 child entry point."""

import pickle
from collections.abc import Iterator
from pathlib import Path

import pytest

from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from schemas.analysis import Diagnostics, NonCompletedResponse
from services import process_entrypoint
from services.process_entrypoint import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisCancelled,
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    ParentCancelled,
    ParentFailure,
    initialize_analysis_child,
    run_child_analysis,
    validate_child_result,
)


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


def _request() -> ChildAnalysisRequest:
    return ChildAnalysisRequest("analysis-1", "video-1", "player-1", "safe.mp4")


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
