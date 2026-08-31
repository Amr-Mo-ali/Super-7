"""Red contract for dominant-target orchestration in ``_analyze_uploaded``."""

import logging
from pathlib import Path
from typing import cast

import pytest

import api.routes as routes
from concurrency.cancellation import CancellationManager
from core.config import Settings
from schemas.analysis import CompletedResponse
from services.dominant_target_selection import TargetEligibilityResult, TargetSelectionStatus
from services.player_detector import BoundingBox
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.segment_selection import TrackSegment
from services.selection import PlayerTrack, Selection
from services.video_validator import VideoMetadata


class _Validator:
    def validate(self, path: Path) -> VideoMetadata:
        del path
        return VideoMetadata("mp4", 1, 10.0, 64, 64, 10.0, 100)


class _Tracker:
    model_version = "test-model"

    def __init__(self, run: TrackingRun) -> None:
        self._run = run
        self.calls = 0

    def analyze(self, path: Path, metadata: VideoMetadata) -> TrackingRun:
        del path, metadata
        self.calls += 1
        return self._run


def _run() -> TrackingRun:
    track = PlayerTrack(7, 30, 100, 30, 0, 0.9, 0, False)
    boxes = {frame: BoundingBox(0, 0, 20, 100) for frame in range(30)}
    return TrackingRun(
        (track,),
        TrackingDiagnostics(100, 30, 30, 1, 0),
        {track.track_id: boxes},
        {track.track_id: {frame: 0.9 for frame in boxes}},
    )


def _segment() -> TrackSegment:
    return TrackSegment(7, 3, 4, 29, 2.6, 26, 1.0, 0.9, 100, 100, 0.0, 0, 0.0, 0.9, ())


def _analyze(
    tracker: _Tracker,
    monkeypatch: pytest.MonkeyPatch,
    completed: CompletedResponse | None = None,
) -> object:
    if completed is not None:
        monkeypatch.setattr(routes, "_completed", lambda *_args, **_kwargs: completed)
    return routes._analyze_uploaded(
        Settings(),
        _Validator(),
        tracker,
        object(),
        object(),
        object(),
        object(),
        object(),
        object(),
        object(),
        object(),
        logging.getLogger("test.dominant-target-route"),
        object(),
        "analysis-1",
        0.0,
        0.0,
        Path(__file__),
        CancellationManager("analysis-1"),
        cast(object, object()),
        {},
    )


def test_established_target_uses_the_single_tracking_run_and_selected_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _Tracker(_run())
    selected = _segment()
    completed = CompletedResponse.model_construct(analysis_id="analysis-1", status="completed")
    calls: list[tuple[TrackingRun, float, Settings]] = []
    completed_selections: list[Selection] = []

    def resolve_dominant_target(
        run: TrackingRun, *, fps: float, settings: Settings
    ) -> tuple[TargetEligibilityResult, TrackSegment | None]:
        calls.append((run, fps, settings))
        return (
            TargetEligibilityResult(
                TargetSelectionStatus.ESTABLISHED,
                "dominant_visual_candidate",
                selected.track_id,
            ),
            selected,
        )

    monkeypatch.setattr(routes, "resolve_dominant_target", resolve_dominant_target, raising=False)

    def complete(*args: object, **_kwargs: object) -> CompletedResponse:
        completed_selections.append(cast(Selection, args[3]))
        return completed

    monkeypatch.setattr(routes, "_completed", complete)
    result = _analyze(tracker, monkeypatch)

    assert result is completed
    assert tracker.calls == 1
    assert calls == [(tracker._run, 10.0, Settings())]
    assert [
        (item.track.track_id, item.segment_id, item.segment_start_frame, item.segment_end_frame)
        for item in completed_selections
    ] == [(selected.track_id, selected.segment_id, selected.start_frame, selected.end_frame)]


@pytest.mark.parametrize(
    "reason",
    (
        "ambiguous_visual_target",
        "no_qualifying_visual_target",
        "target_not_established",
    ),
)
def test_not_established_target_returns_completed_unavailable_without_rating_completion(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    tracker = _Tracker(_run())
    calls = 0

    def resolve_dominant_target(
        run: TrackingRun, *, fps: float, settings: Settings
    ) -> tuple[TargetEligibilityResult, TrackSegment | None]:
        del run, fps, settings
        return TargetEligibilityResult(TargetSelectionStatus.NOT_ESTABLISHED, reason), None

    def must_not_complete(*_args: object, **_kwargs: object) -> CompletedResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("target-unavailable analysis must not enter rating completion")

    monkeypatch.setattr(routes, "resolve_dominant_target", resolve_dominant_target, raising=False)
    monkeypatch.setattr(routes, "_completed", must_not_complete)
    result = _analyze(tracker, monkeypatch)

    assert isinstance(result, CompletedResponse)
    assert result.status == "completed"
    assert result.result_availability == "UNAVAILABLE"
    assert result.unavailability_reason == reason
    assert result.selected_player is None
    assert result.scores is None
    assert result.player_rating_summary is None
    assert tracker.calls == 1
    assert calls == 0
