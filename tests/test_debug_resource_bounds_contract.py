"""F14 red contract for bounded debug resources and deterministic cleanup."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from shutil import rmtree as remove_tree
from typing import Any, Never, Protocol, cast

import pytest

import api.routes as routes
from concurrency.cancellation import CancellationManager
from config.debug import DebugSettings
from core.config import Settings
from diagnostics.artifacts import (
    ArtifactError,
    ArtifactManager,
    ArtifactPathError,
    ArtifactQuotaError,
    ArtifactSession,
    ArtifactStateError,
)
from schemas.analysis import CompletedResponse
from services.ball_proximity import BallProximityAnalyzer, NormalizedBallProximityAnalyzer
from services.dominant_target_selection import TargetEligibilityResult, TargetSelectionStatus
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzer, BallInteractionAnalyzerProtocol
from services.movement.analyzer import BottomCenterMovementAnalyzer, MovementAnalyzer
from services.pass_detection import PassDetector
from services.player_detector import BoundingBox
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.scoring.physical_activity import RuleBasedPhysicalActivityScorer
from services.scoring.protocols import PhysicalActivityScorerProtocol
from services.segment_selection import TrackSegment
from services.selection import PlayerTrack, TargetPlayerSelector
from services.shot_detection import ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.video_validator import VideoMetadata, VideoValidator


class _Validator:
    def validate(self, path: Path) -> VideoMetadata:
        del path
        return VideoMetadata("mp4", 1, 10.0, 64, 64, 10.0, 100)


class _Tracker:
    model_version = "test-model"

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        track = PlayerTrack(7, 30, 100, 30, 0, 0.9, 0, False)
        boxes = {frame: BoundingBox(0, 0, 20, 100) for frame in range(30)}
        return TrackingRun(
            (track,),
            TrackingDiagnostics(100, 30, 30, 1, 0),
            {track.track_id: boxes},
            {track.track_id: {frame: 0.9 for frame in boxes}},
        )


class _UnexpectedDependency:
    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"unexpected dependency access: {name}")


class _SnapshotOnlySession:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def materialize_input(self, source_path: Path, max_upload_bytes: int) -> Path:
        self.calls.append((source_path, max_upload_bytes))
        return source_path

    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"debug-disabled path accessed artifact operation: {name}")


def _finalize_bytes(session: ArtifactSession, name: str, payload: bytes) -> Path:
    reservation = session.reserve(name, len(payload))
    session.create(reservation).write_bytes(payload)
    return session.finalize(reservation)


def test_artifact_quota_accepts_below_and_exact_remaining_bytes(tmp_path: Path) -> None:
    for request_id, size in (("below", 4), ("exact", 5)):
        session = ArtifactManager(tmp_path / request_id, max_session_bytes=5).create_session(
            request_id
        )

        output = _finalize_bytes(session, "debug.bin", b"x" * size)

        assert output.stat().st_size == size
        assert session.cleanup().errors == ()


def test_combined_artifact_quota_is_inclusive_and_rejects_one_byte_over(
    tmp_path: Path,
) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=5).create_session(
        "combined"
    )
    _finalize_bytes(session, "first.bin", b"aa")
    _finalize_bytes(session, "second.bin", b"bbb")

    with pytest.raises(ArtifactQuotaError, match="quota"):
        session.reserve("over.bin", 1)

    assert sum(path.stat().st_size for path in session.artifacts()) == 5
    assert session.cleanup().errors == ()


def test_overgrown_partial_is_rejected_and_cleanup_is_idempotent(tmp_path: Path) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=5).create_session("partial")
    reservation = session.reserve("debug.bin", 5)
    partial = session.create(reservation)
    partial.write_bytes(b"xxxxxx")

    with pytest.raises(ArtifactQuotaError, match="reserved quota"):
        session.finalize(reservation)

    assert session.artifacts() == ()
    assert session.cleanup().errors == ()
    assert session.cleanup().errors == ()
    assert not partial.exists()


def test_debug_disabled_route_creates_no_debug_artifact_and_preserves_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"x")
    settings = Settings()
    snapshot_session = _SnapshotOnlySession()
    selected = TrackSegment(7, 3, 4, 29, 2.6, 26, 1.0, 0.9, 100, 100, 0.0, 0, 0.0, 0.9, ())
    completed = CompletedResponse.model_construct(analysis_id="analysis-f14", status="completed")
    debug_sources: list[Path | None] = []

    monkeypatch.setattr(routes, "reproducibility_metadata", lambda *_: {})
    monkeypatch.setattr(
        routes,
        "resolve_dominant_target",
        lambda *_args, **_kwargs: (
            TargetEligibilityResult(
                TargetSelectionStatus.ESTABLISHED,
                "dominant_visual_candidate",
                selected.track_id,
            ),
            selected,
        ),
    )

    def complete(*args: object, **kwargs: object) -> CompletedResponse:
        debug_sources.append(cast(Path | None, args[20]))
        return completed

    monkeypatch.setattr(routes, "_completed", complete)
    unexpected = cast(Any, _UnexpectedDependency())
    result = routes._analyze_uploaded(
        settings,
        cast(VideoValidator, _Validator()),
        _Tracker(),
        cast(TargetPlayerSelector, unexpected),
        cast(FeatureExtractor, unexpected),
        cast(BallProximityAnalyzer, unexpected),
        cast(MovementAnalyzer, unexpected),
        cast(BallInteractionAnalyzerProtocol, unexpected),
        cast(TechnicalEventAnalyzer, unexpected),
        cast(PassDetector, unexpected),
        cast(ShotDetector, unexpected),
        logging.getLogger("test.f14"),
        cast(PhysicalActivityScorerProtocol, unexpected),
        "analysis-f14",
        0.0,
        0.0,
        source_path,
        CancellationManager("analysis-f14"),
        cast(ArtifactSession, snapshot_session),
        {},
    )

    assert result is completed
    assert snapshot_session.calls == [(source_path, settings.max_upload_bytes)]
    assert debug_sources == [None]


class _DebugRenderPublisher(Protocol):
    def publish_debug_render(
        self,
        producer: Callable[[Path], dict[str, str]],
        *,
        expected_outputs: frozenset[str],
    ) -> dict[str, str]: ...


def _publish_debug_render(
    session: ArtifactSession,
    producer: Callable[[Path], dict[str, str]],
    *expected_outputs: str,
) -> dict[str, str]:
    return cast(_DebugRenderPublisher, session).publish_debug_render(
        producer,
        expected_outputs=frozenset(expected_outputs),
    )


def _assert_final_render_path(session: ArtifactSession, value: str) -> Path:
    path = Path(value)
    assert path.is_relative_to(session.directory / "debug_render")
    assert ".partial" not in path.parts
    return path


def test_one_shot_publishes_video_only_from_staging_to_final(tmp_path: Path) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=5).create_session(
        "video-only"
    )
    staging_seen: list[Path] = []

    def render(staging: Path) -> dict[str, str]:
        staging_seen.append(staging)
        assert staging == session.directory / "debug_render.partial"
        assert not (session.directory / "debug_render").exists()
        staging.mkdir()
        video = staging / "debug_video.mp4"
        video.write_bytes(b"video")
        return {"debug_video": str(video)}

    result = _publish_debug_render(session, render, "debug_video")

    video = _assert_final_render_path(session, result["debug_video"])
    assert staging_seen == [session.directory / "debug_render.partial"]
    assert video.is_file()
    assert not video.is_symlink()
    assert video.read_bytes() == b"video"
    assert not staging_seen[0].exists()
    assert session.artifacts() == (video,)


def test_one_shot_publishes_frames_directory_and_nested_descendants(tmp_path: Path) -> None:
    empty_session = ArtifactManager(
        tmp_path / "empty-artifacts", max_session_bytes=0
    ).create_session("empty-frames")

    def render_empty(staging: Path) -> dict[str, str]:
        assert staging == empty_session.directory / "debug_render.partial"
        staging.mkdir()
        frames = staging / "debug_frames"
        frames.mkdir()
        return {"debug_frames": str(frames)}

    empty_result = _publish_debug_render(empty_session, render_empty, "debug_frames")

    empty_frames = _assert_final_render_path(empty_session, empty_result["debug_frames"])
    assert empty_frames == empty_session.directory / "debug_render" / "debug_frames"
    assert empty_frames.is_dir()
    assert not empty_frames.is_symlink()
    assert tuple(empty_frames.iterdir()) == ()
    assert not (empty_session.directory / "debug_render.partial").exists()
    assert empty_session.artifacts() == ()

    session = ArtifactManager(tmp_path / "populated-artifacts", max_session_bytes=6).create_session(
        "frames-only"
    )

    def render(staging: Path) -> dict[str, str]:
        frames = staging / "debug_frames"
        (frames / "nested").mkdir(parents=True)
        (frames / "empty").mkdir()
        (frames / "frame_1.jpg").write_bytes(b"one")
        (frames / "nested" / "frame_2.jpg").write_bytes(b"two")
        return {"debug_frames": str(frames)}

    result = _publish_debug_render(session, render, "debug_frames")

    frames = _assert_final_render_path(session, result["debug_frames"])
    assert frames.is_dir()
    assert not frames.is_symlink()
    assert (frames / "frame_1.jpg").read_bytes() == b"one"
    assert (frames / "nested" / "frame_2.jpg").read_bytes() == b"two"
    assert (frames / "empty").is_dir()
    assert not (session.directory / "debug_render.partial").exists()
    assert set(session.artifacts()) == {
        frames / "frame_1.jpg",
        frames / "nested" / "frame_2.jpg",
    }


def test_one_shot_publishes_combined_outputs_as_one_complete_tree(tmp_path: Path) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=8).create_session(
        "combined-render"
    )
    calls = 0

    def render(staging: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        staging.mkdir()
        video = staging / "debug_video.mp4"
        frames = staging / "debug_frames"
        frames.mkdir()
        video.write_bytes(b"video")
        (frames / "frame.jpg").write_bytes(b"jpg")
        return {"debug_video": str(video), "debug_frames": str(frames)}

    result = _publish_debug_render(session, render, "debug_video", "debug_frames")

    video = _assert_final_render_path(session, result["debug_video"])
    frames = _assert_final_render_path(session, result["debug_frames"])
    assert calls == 1
    assert video.read_bytes() == b"video"
    assert (frames / "frame.jpg").read_bytes() == b"jpg"
    assert set(session.artifacts()) == {video, frames / "frame.jpg"}
    assert not (session.directory / "debug_render.partial").exists()


def test_one_shot_exact_quota_counts_existing_and_render_bytes_once(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"private-input-is-not-charged")
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=7).create_session(
        "exact-tree-quota"
    )
    snapshot = session.materialize_input(source, max_upload_bytes=len(source.read_bytes()))
    source_artifact = _finalize_bytes(session, "source_video.mp4", b"ab")

    def render(staging: Path) -> dict[str, str]:
        staging.mkdir()
        frames = staging / "debug_frames"
        frames.mkdir()
        video = staging / "debug_video.mp4"
        video.write_bytes(b"cd")
        (frames / "frame.jpg").write_bytes(b"efg")
        return {"debug_video": str(video), "debug_frames": str(frames)}

    result = _publish_debug_render(session, render, "debug_video", "debug_frames")

    video = Path(result["debug_video"])
    frame = Path(result["debug_frames"]) / "frame.jpg"
    assert set(session.artifacts()) == {source_artifact, video, frame}
    assert snapshot not in session.artifacts()
    assert sum(path.stat().st_size for path in session.artifacts()) == 7
    with pytest.raises(ArtifactQuotaError, match="quota"):
        session.reserve("one-byte-too-many.bin", 1)


def test_one_shot_rejects_aggregate_tree_one_byte_over_quota(tmp_path: Path) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=5).create_session(
        "tree-over-quota"
    )
    existing = _finalize_bytes(session, "existing.bin", b"p")

    def render(staging: Path) -> dict[str, str]:
        staging.mkdir()
        video = staging / "debug_video.mp4"
        frames = staging / "debug_frames"
        frames.mkdir()
        video.write_bytes(b"abc")
        (frames / "frame.jpg").write_bytes(b"de")
        return {"debug_video": str(video), "debug_frames": str(frames)}

    with pytest.raises(ArtifactQuotaError, match="quota"):
        _publish_debug_render(session, render, "debug_video", "debug_frames")

    assert existing.read_bytes() == b"p"
    assert session.artifacts() == (existing,)
    assert not (session.directory / "debug_render").exists()
    assert not (session.directory / "debug_render.partial").exists()
    remaining = _finalize_bytes(session, "remaining.bin", b"four")
    assert remaining.read_bytes() == b"four"
    assert set(session.artifacts()) == {existing, remaining}
    with pytest.raises(ArtifactQuotaError, match="quota"):
        session.reserve("accounting-was-not-reset.bin", 1)
    assert session.cleanup().errors == ()
    assert not session.directory.exists()


def test_one_shot_rejects_invalid_video_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index, case in enumerate(("foreign", "escape", "symlink", "directory", "special")):
        session = ArtifactManager(
            tmp_path / f"artifacts-{case}", max_session_bytes=10
        ).create_session(f"invalid-video-{index}")
        foreign_output = tmp_path / f"foreign-{index}.mp4"
        with monkeypatch.context() as patch:

            def render(
                staging: Path,
                *,
                selected_case: str = case,
                selected_index: int = index,
            ) -> dict[str, str]:
                staging.mkdir()
                if selected_case == "foreign":
                    output = tmp_path / f"foreign-{selected_index}.mp4"
                    output.write_bytes(b"x")
                    assert output.is_file()
                    assert output.read_bytes() == b"x"
                elif selected_case == "escape":
                    output = staging / ".." / f"escaped-{selected_index}.mp4"
                    output.write_bytes(b"x")
                elif selected_case == "directory":
                    output = staging / "debug_video.mp4"
                    output.mkdir()
                else:
                    output = staging / "debug_video.mp4"
                    output.write_bytes(b"x")
                    if selected_case == "symlink":
                        original = Path.is_symlink

                        def is_symlink(path: Path) -> bool:
                            return path == output or original(path)

                        patch.setattr(Path, "is_symlink", is_symlink)
                    else:
                        original_is_file = Path.is_file

                        def is_file(path: Path) -> bool:
                            return path != output and original_is_file(path)

                        patch.setattr(Path, "is_file", is_file)
                return {"debug_video": str(output)}

            with pytest.raises(ArtifactError):
                _publish_debug_render(session, render, "debug_video")

        assert session.artifacts() == ()
        assert not (session.directory / "debug_render").exists()
        assert session.cleanup().errors == ()
        assert not session.directory.exists()
        if case == "foreign":
            assert foreign_output.exists()
            assert foreign_output.is_file()
            assert foreign_output.read_bytes() == b"x"


def test_one_shot_rejects_invalid_frames_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        "foreign",
        "escape",
        "symlink-directory",
        "symlink-descendant",
        "special-descendant",
        "regular-file",
    )
    for index, case in enumerate(cases):
        session = ArtifactManager(
            tmp_path / f"artifacts-{case}", max_session_bytes=10
        ).create_session(f"invalid-frames-{index}")
        foreign_frames = tmp_path / f"foreign-frames-{index}"
        foreign_sentinel = foreign_frames / "sentinel.bin"
        with monkeypatch.context() as patch:

            def render(
                staging: Path,
                *,
                selected_case: str = case,
                selected_index: int = index,
            ) -> dict[str, str]:
                staging.mkdir()
                if selected_case == "foreign":
                    frames = tmp_path / f"foreign-frames-{selected_index}"
                    frames.mkdir()
                    sentinel = frames / "sentinel.bin"
                    sentinel.write_bytes(b"foreign-frames")
                    assert sentinel.is_file()
                    assert sentinel.read_bytes() == b"foreign-frames"
                elif selected_case == "escape":
                    frames = staging / ".." / f"escaped-frames-{selected_index}"
                    frames.mkdir()
                elif selected_case == "regular-file":
                    frames = staging / "debug_frames"
                    frames.write_bytes(b"x")
                else:
                    frames = staging / "debug_frames"
                    frames.mkdir()
                    descendant = frames / "frame.jpg"
                    descendant.write_bytes(b"x")
                    original_is_symlink = Path.is_symlink
                    if selected_case == "symlink-directory":

                        def is_symlink(path: Path) -> bool:
                            return path == frames or original_is_symlink(path)

                        patch.setattr(Path, "is_symlink", is_symlink)
                    elif selected_case == "symlink-descendant":

                        def is_symlink(path: Path) -> bool:
                            return path == descendant or original_is_symlink(path)

                        patch.setattr(Path, "is_symlink", is_symlink)
                    else:
                        original_is_file = Path.is_file

                        def is_file(path: Path) -> bool:
                            return path != descendant and original_is_file(path)

                        patch.setattr(Path, "is_file", is_file)
                return {"debug_frames": str(frames)}

            with pytest.raises(ArtifactError):
                _publish_debug_render(session, render, "debug_frames")

        assert session.artifacts() == ()
        assert not (session.directory / "debug_render").exists()
        assert session.cleanup().errors == ()
        assert not session.directory.exists()
        if case == "foreign":
            assert foreign_frames.exists()
            assert foreign_frames.is_dir()
            assert foreign_sentinel.exists()
            assert foreign_sentinel.is_file()
            assert foreign_sentinel.read_bytes() == b"foreign-frames"


def test_one_shot_rejects_missing_or_invalid_output_mapping(tmp_path: Path) -> None:
    invalid_mappings: tuple[dict[str, str], ...] = (
        {},
        {"unexpected": "unexpected"},
        cast(dict[str, str], {"debug_video": 1}),
    )
    for index, mapping in enumerate(invalid_mappings):
        session = ArtifactManager(
            tmp_path / f"artifacts-{index}", max_session_bytes=10
        ).create_session(f"invalid-mapping-{index}")

        def render(staging: Path, *, returned: dict[str, str] = mapping) -> dict[str, str]:
            staging.mkdir()
            return returned

        with pytest.raises((ArtifactPathError, ArtifactStateError)):
            _publish_debug_render(session, render, "debug_video")

        assert session.artifacts() == ()
        assert not (session.directory / "debug_render").exists()
        assert session.cleanup().errors == ()
        assert not session.directory.exists()


def test_one_shot_never_overwrites_occupied_final_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index, case in enumerate(("directory", "file", "broken-symlink")):
        session = ArtifactManager(
            tmp_path / f"artifacts-{case}", max_session_bytes=10
        ).create_session(f"occupied-final-{index}")
        final = session.directory / "debug_render"
        if case == "directory":
            final.mkdir()
            marker = final / "existing.txt"
            marker.write_bytes(b"existing")
        elif case == "file":
            final.write_bytes(b"existing-file")
        with monkeypatch.context() as patch:
            if case == "broken-symlink":
                original = Path.is_symlink

                def is_symlink(
                    path: Path,
                    *,
                    occupied: Path = final,
                    original_check: Callable[[Path], bool] = original,
                ) -> bool:
                    return path == occupied or original_check(path)

                patch.setattr(Path, "is_symlink", is_symlink)

            def render(staging: Path) -> dict[str, str]:
                staging.mkdir()
                video = staging / "debug_video.mp4"
                video.write_bytes(b"new")
                return {"debug_video": str(video)}

            with pytest.raises(ArtifactStateError):
                _publish_debug_render(session, render, "debug_video")

        assert session.artifacts() == ()
        if case == "directory":
            assert marker.read_bytes() == b"existing"
        elif case == "file":
            assert final.is_file()
            assert final.read_bytes() == b"existing-file"
        capacity = _finalize_bytes(session, "capacity.bin", b"x" * 10)
        assert session.artifacts() == (capacity,)
        assert not (session.directory / "debug_render.partial").exists()
        assert session.cleanup().errors == ()
        assert not session.directory.exists()


def test_one_shot_renderer_failure_preserves_identity_and_cleans_staging(
    tmp_path: Path,
) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=10).create_session(
        "renderer-failure"
    )
    failure = RuntimeError("distinctive renderer failure")
    staging_seen: list[Path] = []

    def render(staging: Path) -> dict[str, str]:
        staging_seen.append(staging)
        staging.mkdir()
        (staging / "partial.mp4").write_bytes(b"partial")
        raise failure

    with pytest.raises(RuntimeError) as raised:
        _publish_debug_render(session, render, "debug_video")

    assert raised.value is failure
    assert staging_seen == [session.directory / "debug_render.partial"]
    assert not staging_seen[0].exists()
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()
    assert not session.directory.exists()


def test_one_shot_first_cleanup_failure_is_retried_by_session_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=10).create_session(
        "cleanup-retry"
    )
    failure = RuntimeError("primary render failure")
    staging = session.directory / "debug_render.partial"
    removals: list[Path] = []

    def fail_first_removal(path: Path) -> None:
        removals.append(path)
        if path == staging:
            raise OSError("first staging deletion blocked")
        remove_tree(path)

    monkeypatch.setattr("diagnostics.artifacts.rmtree", fail_first_removal)

    def render(output: Path) -> dict[str, str]:
        output.mkdir()
        (output / "partial.mp4").write_bytes(b"partial")
        raise failure

    with pytest.raises(RuntimeError) as raised:
        _publish_debug_render(session, render, "debug_video")

    assert raised.value is failure
    assert staging.exists()
    assert session.artifacts() == ()
    assert session.cleanup().errors == ()
    assert removals == [staging, session.directory]
    assert not session.directory.exists()


def test_one_shot_repeated_cleanup_failure_remains_observable_without_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=10).create_session(
        "cleanup-observable"
    )
    failure = RuntimeError("primary publication failure")
    staging = session.directory / "debug_render.partial"
    removals: list[Path] = []

    def refuse_removal(path: Path) -> None:
        removals.append(path)
        raise OSError(f"deletion blocked: {path.name}")

    monkeypatch.setattr("diagnostics.artifacts.rmtree", refuse_removal)

    def render(output: Path) -> dict[str, str]:
        output.mkdir()
        (output / "partial.mp4").write_bytes(b"partial")
        raise failure

    with pytest.raises(RuntimeError) as raised:
        _publish_debug_render(session, render, "debug_video")

    assert raised.value is failure
    assert staging.exists()
    assert session.artifacts() == ()
    cleanup = session.cleanup()
    assert cleanup.errors == (f"deletion blocked: {session.directory.name}",)
    assert removals == [staging, session.directory]
    assert session.directory.exists()


def test_debug_route_retains_only_after_complete_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"x" * 100)

    class SuccessfulRouteValidator(_Validator):
        def validate(self, path: Path) -> VideoMetadata:
            assert path.stat().st_size == 100
            return VideoMetadata("mp4", 100, 10.0, 64, 64, 10.0, 100)

    settings = Settings(debug=DebugSettings(enabled=True, save_video=True))
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=110).create_session(
        "route-publish"
    )
    selected = TrackSegment(7, 3, 4, 29, 2.6, 26, 1.0, 0.9, 100, 100, 0.0, 0, 0.0, 0.9, ())
    events: list[str] = []
    materialize_calls: list[tuple[Path, int]] = []
    publish_calls: list[frozenset[str]] = []
    published_outputs: list[dict[str, str]] = []
    render_calls: list[tuple[Path, Path]] = []
    original_materialize = session.materialize_input
    original_publish = cast(_DebugRenderPublisher, session).publish_debug_render
    original_retain = session.retain

    monkeypatch.setattr(routes, "reproducibility_metadata", lambda *_: {})
    monkeypatch.setattr(
        routes,
        "resolve_dominant_target",
        lambda *_args, **_kwargs: (
            TargetEligibilityResult(
                TargetSelectionStatus.ESTABLISHED,
                "dominant_visual_candidate",
                selected.track_id,
            ),
            selected,
        ),
    )

    class NoCameraMotion:
        def estimate(self, *_args: object, **_kwargs: object) -> None:
            return None

    def materialize(
        current: ArtifactSession,
        source: Path,
        max_upload_bytes: int,
    ) -> Path:
        assert current is session
        materialize_calls.append((source, max_upload_bytes))
        return original_materialize(source, max_upload_bytes)

    def publish(
        current: ArtifactSession,
        producer: Callable[[Path], dict[str, str]],
        *,
        expected_outputs: frozenset[str],
    ) -> dict[str, str]:
        assert current is session
        publish_calls.append(expected_outputs)
        events.append("publish-start")
        result = original_publish(producer, expected_outputs=expected_outputs)
        published_outputs.append(result)
        events.append("published")
        return result

    def retain(current: ArtifactSession) -> None:
        assert current is session
        assert events == ["publish-start", "render", "published"]
        final_video = session.directory / "debug_render" / "debug_video.mp4"
        assert final_video.exists()
        assert final_video in session.artifacts()
        events.append("retain")
        original_retain()

    def render(
        source: Path,
        output: Path,
        *_args: object,
        **kwargs: object,
    ) -> dict[str, str]:
        render_calls.append((source, output))
        events.append("render")
        assert source == session.directory / "source_video.mp4"
        assert output == session.directory / "debug_render.partial"
        assert not (session.directory / "debug_render").exists()
        assert kwargs["save_video"] is True
        assert kwargs["save_frames"] is False
        output.mkdir()
        video = output / "debug_video.mp4"
        video.write_bytes(b"video")
        return {"debug_video": str(video)}

    monkeypatch.setattr(ArtifactSession, "materialize_input", materialize)
    monkeypatch.setattr(ArtifactSession, "publish_debug_render", publish)
    monkeypatch.setattr(ArtifactSession, "retain", retain)
    monkeypatch.setattr(routes, "CameraMotionEstimator", NoCameraMotion)
    monkeypatch.setattr(routes, "render_debug_video", render)
    result = routes._analyze_uploaded(
        settings,
        cast(VideoValidator, SuccessfulRouteValidator()),
        _Tracker(),
        cast(TargetPlayerSelector, _UnexpectedDependency()),
        FeatureExtractor(),
        NormalizedBallProximityAnalyzer(settings),
        BottomCenterMovementAnalyzer(settings),
        BallInteractionAnalyzer(settings),
        TechnicalEventAnalyzer(settings),
        PassDetector(settings),
        ShotDetector(),
        logging.getLogger("test.f14.route.success"),
        RuleBasedPhysicalActivityScorer(settings),
        "analysis-f14",
        0.0,
        0.0,
        source_path,
        CancellationManager("analysis-f14"),
        session,
        {},
    )

    assert isinstance(result, CompletedResponse)
    assert result.status == "completed"
    assert result.selected_player is not None
    assert result.selected_player.track_id == selected.track_id
    assert result.scores is not None
    assert result.player_rating_summary is not None
    assert materialize_calls == [(source_path, settings.max_upload_bytes)]
    assert publish_calls == [frozenset({"debug_video"})]
    assert render_calls == [
        (
            session.directory / "source_video.mp4",
            session.directory / "debug_render.partial",
        )
    ]
    assert events == ["publish-start", "render", "published", "retain"]
    assert result.debug_artifacts == {"debug_video": "debug_video.mp4"}
    final_video = Path(published_outputs[0]["debug_video"])
    assert final_video == session.directory / "debug_render" / "debug_video.mp4"
    assert ".partial" not in final_video.parts
    assert final_video.read_bytes() == b"video"
    source_artifact = session.directory / "source_video.mp4"
    assert source_artifact.read_bytes() == b"x" * 100
    assert set(session.artifacts()) == {source_artifact, final_video}
    with pytest.raises(ArtifactQuotaError, match="quota"):
        session.reserve("source-charge-proof.bin", 6)
    assert "Debug-video rendering failed; analysis results are unaffected." not in result.warnings
    assert session.cleanup().errors == ()
    assert session.directory.exists()


def test_debug_route_failure_preserves_analysis_and_avoids_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"x")
    settings = Settings(debug=DebugSettings(enabled=True, save_video=True))
    session = ArtifactManager(tmp_path / "artifacts", max_session_bytes=100).create_session(
        "route-failure"
    )
    selected = TrackSegment(7, 3, 4, 29, 2.6, 26, 1.0, 0.9, 100, 100, 0.0, 0, 0.0, 0.9, ())
    render_failure = RuntimeError("distinctive route renderer failure")
    warning = "Debug-video rendering failed; analysis results are unaffected."
    retain_calls: list[ArtifactSession] = []
    render_calls: list[tuple[Path, Path]] = []

    monkeypatch.setattr(routes, "reproducibility_metadata", lambda *_: {})
    monkeypatch.setattr(
        routes,
        "resolve_dominant_target",
        lambda *_args, **_kwargs: (
            TargetEligibilityResult(
                TargetSelectionStatus.ESTABLISHED,
                "dominant_visual_candidate",
                selected.track_id,
            ),
            selected,
        ),
    )
    monkeypatch.setattr(
        ArtifactSession,
        "retain",
        lambda current: retain_calls.append(current),
    )

    class NoCameraMotion:
        def estimate(self, *_args: object, **_kwargs: object) -> None:
            return None

    def fail_render(source: Path, output: Path, *_args: object, **_kwargs: object) -> Never:
        render_calls.append((source, output))
        raise render_failure

    monkeypatch.setattr(routes, "CameraMotionEstimator", NoCameraMotion)
    monkeypatch.setattr(routes, "render_debug_video", fail_render)
    result = routes._analyze_uploaded(
        settings,
        cast(VideoValidator, _Validator()),
        _Tracker(),
        cast(TargetPlayerSelector, _UnexpectedDependency()),
        FeatureExtractor(),
        NormalizedBallProximityAnalyzer(settings),
        BottomCenterMovementAnalyzer(settings),
        BallInteractionAnalyzer(settings),
        TechnicalEventAnalyzer(settings),
        PassDetector(settings),
        ShotDetector(),
        logging.getLogger("test.f14.route.failure"),
        RuleBasedPhysicalActivityScorer(settings),
        "analysis-f14",
        0.0,
        0.0,
        source_path,
        CancellationManager("analysis-f14"),
        session,
        {},
    )

    assert isinstance(result, CompletedResponse)
    assert result.status == "completed"
    assert result.selected_player is not None
    assert result.selected_player.track_id == selected.track_id
    assert result.scores is not None
    assert result.player_rating_summary is not None
    assert warning in result.warnings
    assert result.debug_artifacts == {}
    assert retain_calls == []
    assert render_calls == [
        (
            session.directory / "source_video.mp4",
            session.directory / "debug_render.partial",
        )
    ]
    assert session.cleanup().errors == ()
    assert not session.directory.exists()
