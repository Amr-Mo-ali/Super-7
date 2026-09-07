"""F08 contract for bounded private snapshots of shared video inputs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from shutil import copyfile as copy_file
from threading import Barrier, Thread
from typing import Any, Never, cast

import pytest

import api.routes as routes
from concurrency.cancellation import CancellationManager
from config.debug import DebugSettings
from core.config import Settings
from core.exceptions import InvalidVideoError, UploadTooLargeError
from diagnostics.artifacts import ArtifactManager, ArtifactSession
from services import process_entrypoint
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.process_contracts import ChildAnalysisFailure, ChildAnalysisRequest
from services.process_entrypoint import initialize_analysis_child, run_child_analysis
from services.video_path_resolver import VideoPathResolver
from services.video_validator import VideoMetadata, VideoValidator


class _ScriptedReader:
    def __init__(self, events: list[bytes | OSError]) -> None:
        self._events = iter(events)
        self.read_sizes: list[int] = []

    def __enter__(self) -> _ScriptedReader:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        event = next(self._events, b"")
        if isinstance(event, OSError):
            raise event
        assert len(event) <= amount
        return event


class _RecordingValidator:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def validate(self, path: Path) -> VideoMetadata:
        self.paths.append(path)
        return VideoMetadata("mp4", path.stat().st_size, 0.3, 64, 64, 10.0, 3)


class _NoPlayerTracker:
    model_version = "test-model"

    def __init__(self) -> None:
        self.paths: list[Path] = []

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        self.paths.append(path)
        return TrackingRun(
            (),
            TrackingDiagnostics(
                metadata.frame_count,
                0,
                0,
                0,
                0,
                rejected_track_reason_breakdown={},
            ),
        )


class _UnexpectedDependency:
    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"unexpected dependency access: {name}")


@pytest.fixture(autouse=True)
def _reset_process_child() -> Iterator[None]:
    process_entrypoint._reset_child_runtime_for_test()
    yield
    process_entrypoint._reset_child_runtime_for_test()


def _write_bytes(path: Path, size: int, value: bytes = b"x") -> Path:
    path.write_bytes(value * size)
    return path


def _session(
    tmp_path: Path, *, artifact_bytes: int = 0, request_id: str = "request-1"
) -> ArtifactSession:
    return ArtifactManager(tmp_path / "artifacts", artifact_bytes).create_session(request_id)


def _install_source_reader(
    monkeypatch: pytest.MonkeyPatch,
    source_path: Path,
    reader: _ScriptedReader,
) -> None:
    original_open = Path.open

    def open_path(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == source_path and mode == "rb":
            return reader
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)


def _run_no_player_analysis(
    settings: Settings,
    source_path: Path,
    session: ArtifactSession,
    validator: _RecordingValidator,
    tracker: _NoPlayerTracker,
) -> object:
    unexpected = cast(Any, _UnexpectedDependency())
    return routes._analyze_uploaded(
        settings,
        cast(VideoValidator, validator),
        tracker,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        logging.getLogger("test.f08"),
        unexpected,
        "analysis-1",
        0.0,
        0.0,
        source_path,
        CancellationManager("analysis-1"),
        session,
        {},
    )


@pytest.mark.parametrize("size", (9, 10))
def test_private_snapshot_accepts_actual_bytes_at_or_below_inclusive_limit(
    tmp_path: Path,
    size: int,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", size)
    session = _session(tmp_path)

    snapshot_path = session.materialize_input(source_path, max_upload_bytes=10)

    assert snapshot_path != source_path
    assert snapshot_path.parent == session.directory
    assert snapshot_path.read_bytes() == b"x" * size
    assert source_path.read_bytes() == b"x" * size
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()
    assert not snapshot_path.exists()


def test_one_byte_over_limit_is_rejected_without_partial_snapshot(tmp_path: Path) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 11)
    session = _session(tmp_path)

    with pytest.raises(UploadTooLargeError):
        session.materialize_input(source_path, max_upload_bytes=10)

    assert source_path.read_bytes() == b"x" * 11
    assert tuple(session.directory.iterdir()) == ()
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()


def test_empty_source_keeps_existing_invalid_video_policy(tmp_path: Path) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 0)
    session = _session(tmp_path)

    with pytest.raises(InvalidVideoError, match="empty or unavailable"):
        session.materialize_input(source_path, max_upload_bytes=10)

    assert tuple(session.directory.iterdir()) == ()
    assert session.cleanup().errors == ()


def test_non_regular_source_keeps_existing_invalid_video_policy(tmp_path: Path) -> None:
    source_path = tmp_path / "directory.mp4"
    source_path.mkdir()
    session = _session(tmp_path)

    with pytest.raises(InvalidVideoError, match="empty or unavailable"):
        session.materialize_input(source_path, max_upload_bytes=10)

    assert tuple(session.directory.iterdir()) == ()
    assert session.cleanup().errors == ()


def test_source_growth_during_materialization_is_counted_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 1)
    reader = _ScriptedReader([b"a" * 10, b"b"])
    _install_source_reader(monkeypatch, source_path, reader)
    session = _session(tmp_path)

    with pytest.raises(UploadTooLargeError):
        session.materialize_input(source_path, max_upload_bytes=10)

    assert reader.read_sizes == [11, 1]
    assert tuple(session.directory.iterdir()) == ()
    assert session.cleanup().errors == ()


def test_source_read_failure_does_not_publish_or_leave_partial_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 1)
    reader = _ScriptedReader([b"abc", OSError("deterministic read failure")])
    _install_source_reader(monkeypatch, source_path, reader)
    session = _session(tmp_path)

    with pytest.raises(OSError, match="deterministic read failure"):
        session.materialize_input(source_path, max_upload_bytes=10)

    assert tuple(session.directory.iterdir()) == ()
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()


def test_replacement_after_resolution_is_bounded_before_downstream_consumption(
    tmp_path: Path,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 9, b"a")
    replacement = _write_bytes(tmp_path / "replacement.mp4", 11, b"b")
    resolved = VideoPathResolver(tmp_path).resolve(source_path.name)
    replacement.replace(source_path)
    session = _session(tmp_path)

    with pytest.raises(UploadTooLargeError):
        session.materialize_input(resolved, max_upload_bytes=10)

    assert source_path.read_bytes() == b"b" * 11
    assert tuple(session.directory.iterdir()) == ()
    assert session.cleanup().errors == ()


def test_child_path_handoff_rejects_changed_source_with_sanitized_failure(
    tmp_path: Path,
) -> None:
    video_root = tmp_path / "videos"
    video_root.mkdir()
    source_path = _write_bytes(video_root / "video.mp4", 9)
    request = ChildAnalysisRequest("analysis-1", "video-1", "player-1", source_path.name)
    _write_bytes(source_path, 11)
    settings = Settings(
        max_upload_bytes=10,
        video_storage_root=str(video_root),
        debug_output_dir=str(tmp_path / "debug"),
    )
    initialize_analysis_child(settings)

    result = run_child_analysis(request)

    assert isinstance(result, ChildAnalysisFailure)
    assert result.error_code == "UploadTooLargeError"
    assert result.public_message == "Analysis could not be completed."
    assert not (Path(settings.debug_output_dir) / request.analysis_id).exists()


def test_downstream_validation_tracking_and_hashing_use_only_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 9, b"a")
    session = _session(tmp_path)
    validator = _RecordingValidator()
    tracker = _NoPlayerTracker()
    hashed_paths: list[Path] = []

    def record_metadata(path: Path, model_version: str) -> dict[str, str]:
        del model_version
        hashed_paths.append(path)
        return {}

    monkeypatch.setattr(routes, "reproducibility_metadata", record_metadata)

    _run_no_player_analysis(Settings(max_upload_bytes=10), source_path, session, validator, tracker)

    snapshot_path = validator.paths[0]
    assert snapshot_path != source_path
    assert tracker.paths == [snapshot_path]
    assert hashed_paths == [snapshot_path]
    assert snapshot_path.read_bytes() == b"a" * 9
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()
    assert not snapshot_path.exists()


def test_source_replacement_after_snapshot_cannot_change_snapshot_bytes(tmp_path: Path) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 9, b"a")
    replacement = _write_bytes(tmp_path / "replacement.mp4", 9, b"b")
    session = _session(tmp_path)
    snapshot_path = session.materialize_input(source_path, max_upload_bytes=10)

    replacement.replace(source_path)

    assert source_path.read_bytes() == b"b" * 9
    assert snapshot_path.read_bytes() == b"a" * 9
    assert session.cleanup().errors == ()
    assert not snapshot_path.exists()


def test_request_distinct_snapshot_sessions_cannot_collide(tmp_path: Path) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 9)
    manager = ArtifactManager(tmp_path / "artifacts", max_session_bytes=0)
    sessions = [manager.create_session("request-a"), manager.create_session("request-b")]
    barrier = Barrier(2)
    snapshots: list[Path] = []

    def materialize(session: ArtifactSession) -> None:
        barrier.wait()
        snapshots.append(session.materialize_input(source_path, max_upload_bytes=10))

    threads = [Thread(target=materialize, args=(session,)) for session in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(snapshots) == 2
    assert snapshots[0] != snapshots[1]
    assert {path.read_bytes() for path in snapshots} == {b"x" * 9}
    assert all(session.artifacts() == () for session in sessions)
    assert all(session.cleanup().errors == () for session in sessions)
    assert all(not path.exists() for path in snapshots)


def test_snapshot_is_quota_independent_and_removed_when_debug_artifact_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_bytes(tmp_path / "video.mp4", 9)
    manager = ArtifactManager(tmp_path / "artifacts", max_session_bytes=9, retained_sessions=1)
    session = manager.create_session("analysis-1")
    validator = _RecordingValidator()
    tracker = _NoPlayerTracker()
    copied_sources: list[Path] = []

    def record_copy(source: Path, destination: Path) -> Path:
        copied_sources.append(Path(source))
        return copy_file(source, destination)

    monkeypatch.setattr(routes, "copyfile", record_copy)
    monkeypatch.setattr(routes, "reproducibility_metadata", lambda *_: {})
    settings = Settings(
        max_upload_bytes=10,
        debug=DebugSettings(enabled=True, save_video=True, retained_sessions=1),
    )

    _run_no_player_analysis(settings, source_path, session, validator, tracker)

    snapshot_path = validator.paths[0]
    debug_artifacts = session.artifacts()
    assert copied_sources == [snapshot_path]
    assert len(debug_artifacts) == 1
    assert debug_artifacts[0].name == "source_video.mp4"
    assert snapshot_path not in debug_artifacts
    assert session.cleanup().errors == ()
    assert not snapshot_path.exists()
    assert debug_artifacts[0].read_bytes() == b"x" * 9
