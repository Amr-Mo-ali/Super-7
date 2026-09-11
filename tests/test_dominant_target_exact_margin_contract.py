"""F11 contract for inclusive dominant-target visibility margins."""

from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import Never, cast

import pytest

import api.routes as routes
from concurrency.cancellation import CancellationManager
from core.config import Settings
from diagnostics.artifacts import ArtifactSession
from schemas.analysis import CompletedResponse
from services.ball_proximity import BallProximityAnalyzer
from services.dominant_target_selection import (
    TargetEligibilityResult,
    TargetSelectionStatus,
    evaluate_dominant_target,
    resolve_dominant_target,
)
from services.feature_extractor import FeatureExtractor
from services.interactions.analyzer import BallInteractionAnalyzerProtocol
from services.movement.analyzer import MovementAnalyzer
from services.pass_detection import PassDetector
from services.player_detector import BoundingBox
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.scoring.protocols import PhysicalActivityScorerProtocol
from services.selection import PlayerTrack, Selection, TargetPlayerSelector
from services.shot_detection import ShotDetector
from services.technical_events.analyzer import TechnicalEventAnalyzer
from services.video_validator import VideoMetadata, VideoValidator


def _track(
    track_id: int,
    visible_frames: int,
    processed_frames: int,
    *,
    longest_segment: int | None = None,
) -> PlayerTrack:
    return PlayerTrack(
        track_id,
        visible_frames,
        processed_frames,
        visible_frames if longest_segment is None else longest_segment,
        0,
        0.9,
        0,
        False,
    )


def _frame_keys(count: int) -> dict[int, object]:
    return {frame: object() for frame in range(count)}


def _select(
    processed_frames: int,
    winner_frames: int,
    runner_up_frames: int | None,
    *,
    settings: Settings | None = None,
    runner_up_longest_segment: int | None = None,
    reverse_inputs: bool = False,
) -> TargetEligibilityResult:
    tracks = [_track(7, winner_frames, processed_frames)]
    frame_keys = {7: _frame_keys(winner_frames)}
    if runner_up_frames is not None:
        tracks.append(
            _track(
                11,
                runner_up_frames,
                processed_frames,
                longest_segment=runner_up_longest_segment,
            )
        )
        frame_keys[11] = _frame_keys(runner_up_frames)
    if reverse_inputs:
        tracks.reverse()
        frame_keys = dict(reversed(tuple(frame_keys.items())))
    return evaluate_dominant_target(
        tuple(tracks),
        frame_keys,
        frames_processed=processed_frames,
        fps=10.0,
        settings=settings or Settings(),
    )


def _exact_margin(winner_frames: int, runner_up_frames: int, processed_frames: int) -> bool:
    return Fraction(winner_frames - runner_up_frames, processed_frames) == Fraction(2, 25)


def test_30_over_100_vs_22_over_100_exact_margin_is_established() -> None:
    assert _exact_margin(30, 22, 100)

    result = _select(100, 30, 22)

    assert result.status is TargetSelectionStatus.ESTABLISHED
    assert result.reason == "dominant_visual_candidate"
    assert result.selected_track_id == 7


def test_one_count_below_margin_is_ambiguous_with_exact_reason() -> None:
    assert Fraction(29 - 22, 100) < Fraction(2, 25)

    result = _select(100, 29, 22)

    assert result.status is TargetSelectionStatus.NOT_ESTABLISHED
    assert result.reason == "ambiguous_visual_target"
    assert result.selected_track_id is None


def test_one_count_above_margin_is_established() -> None:
    assert Fraction(31 - 22, 100) > Fraction(2, 25)

    result = _select(100, 31, 22)

    assert result.status is TargetSelectionStatus.ESTABLISHED
    assert result.reason == "dominant_visual_candidate"
    assert result.selected_track_id == 7


def test_second_exact_boundary_count_combination_is_established() -> None:
    assert _exact_margin(37, 27, 125)

    result = _select(125, 37, 27)

    assert result.status is TargetSelectionStatus.ESTABLISHED
    assert result.selected_track_id == 7


def test_candidate_and_mapping_order_do_not_change_the_winner() -> None:
    first = _select(100, 31, 22)
    reordered = _select(100, 31, 22, reverse_inputs=True)

    assert (first.status, first.reason, first.selected_track_id) == (
        reordered.status,
        reordered.reason,
        reordered.selected_track_id,
    )
    assert first.selected_track_id == 7


def test_equal_candidates_are_ambiguous_under_positive_margin() -> None:
    result = _select(100, 30, 30)

    assert result.status is TargetSelectionStatus.NOT_ESTABLISHED
    assert result.reason == "ambiguous_visual_target"


def test_single_qualifying_candidate_keeps_current_established_behavior() -> None:
    result = _select(100, 30, None)

    assert result.status is TargetSelectionStatus.ESTABLISHED
    assert result.selected_track_id == 7


def test_nonqualifying_plausible_runner_up_still_participates_in_ambiguity() -> None:
    result = _select(100, 25, 20, runner_up_longest_segment=4)

    assert result.status is TargetSelectionStatus.NOT_ESTABLISHED
    assert result.reason == "ambiguous_visual_target"


def test_zero_margin_keeps_deterministic_tie_winner_behavior() -> None:
    result = _select(100, 30, 30, settings=Settings(selection_margin=0.0))

    assert result.status is TargetSelectionStatus.ESTABLISHED
    assert result.selected_track_id == 7


def test_selection_does_not_mutate_or_lower_configured_margin() -> None:
    settings = Settings(selection_margin=0.08)
    configured_margin = settings.selection_margin
    assert isinstance(configured_margin, float)
    assert Fraction(str(configured_margin)) == Fraction(2, 25)

    _select(100, 30, 22, settings=settings)

    assert settings.selection_margin == configured_margin


def _tracking_run() -> TrackingRun:
    processed_frames = 100
    winner_frames = 30
    runner_up_frames = 22
    tracks = (
        _track(7, winner_frames, processed_frames),
        _track(11, runner_up_frames, processed_frames),
    )
    boxes = {
        7: {frame: BoundingBox(0, 0, 20, 100) for frame in range(winner_frames)},
        11: {frame: BoundingBox(30, 0, 50, 100) for frame in range(runner_up_frames)},
    }
    confidences = {
        track_id: {frame: 0.9 for frame in observations} for track_id, observations in boxes.items()
    }
    return TrackingRun(
        tracks,
        TrackingDiagnostics(processed_frames, winner_frames, 52, 2, 0),
        boxes,
        confidences,
    )


def test_resolver_establishes_exact_boundary_and_returns_winning_segment() -> None:
    assert _exact_margin(30, 22, 100)

    target, segment = resolve_dominant_target(_tracking_run(), fps=10.0, settings=Settings())

    assert target.status is TargetSelectionStatus.ESTABLISHED
    assert target.reason == "dominant_visual_candidate"
    assert target.selected_track_id == 7
    assert segment is not None
    assert segment.track_id == 7


class _Validator:
    def validate(self, path: Path) -> VideoMetadata:
        del path
        return VideoMetadata("mp4", 1, 10.0, 64, 64, 10.0, 100)


class _Tracker:
    model_version = "test-model"

    def __init__(self, run: TrackingRun) -> None:
        self._run = run

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        return self._run


class _UnexpectedDependency:
    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"unexpected dependency access: {name}")


class _SnapshotSession:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def materialize_input(self, source_path: Path, max_upload_bytes: int) -> Path:
        self.calls.append((source_path, max_upload_bytes))
        return source_path

    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"unexpected snapshot-session access: {name}")


def test_route_uses_real_resolver_and_preserves_exact_boundary_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = CompletedResponse.model_construct(analysis_id="analysis-f11", status="completed")
    completed_selections: list[Selection] = []

    def complete(*args: object, **_kwargs: object) -> CompletedResponse:
        completed_selections.append(cast(Selection, args[3]))
        return completed

    monkeypatch.setattr(routes, "_completed", complete)
    unexpected = _UnexpectedDependency()
    settings = Settings()
    snapshot_session = _SnapshotSession()
    source_path = Path(__file__)
    result = routes._analyze_uploaded(
        settings,
        cast(VideoValidator, _Validator()),
        _Tracker(_tracking_run()),
        cast(TargetPlayerSelector, unexpected),
        cast(FeatureExtractor, unexpected),
        cast(BallProximityAnalyzer, unexpected),
        cast(MovementAnalyzer, unexpected),
        cast(BallInteractionAnalyzerProtocol, unexpected),
        cast(TechnicalEventAnalyzer, unexpected),
        cast(PassDetector, unexpected),
        cast(ShotDetector, unexpected),
        logging.getLogger("test.f11-route"),
        cast(PhysicalActivityScorerProtocol, unexpected),
        "analysis-f11",
        0.0,
        0.0,
        source_path,
        CancellationManager("analysis-f11"),
        cast(ArtifactSession, snapshot_session),
        {},
    )

    assert result is completed
    assert snapshot_session.calls == [(source_path, settings.max_upload_bytes)]
    assert [selection.track.track_id for selection in completed_selections] == [7]
