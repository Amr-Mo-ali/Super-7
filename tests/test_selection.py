"""Unit tests for configurable automatic target selection."""

from core.config import Settings
from services.selection import PlayerTrack, WeightedTargetPlayerSelector


def _track(
    identifier: int,
    visible: int = 8,
    ball: int = 0,
    available: bool = True,
    confidence: float = 0.9,
    segment: int = 8,
) -> PlayerTrack:
    return PlayerTrack(identifier, visible, 10, segment, 1, confidence, ball, available)


def test_selects_most_visible_player() -> None:
    result = WeightedTargetPlayerSelector(Settings(selection_margin=0.01)).select(
        (_track(1, 9), _track(2, 7))
    )
    assert result is not None and result.track.track_id == 1


def test_does_not_use_ball_proximity_for_target_selection() -> None:
    result = WeightedTargetPlayerSelector(Settings(selection_margin=0.01)).select(
        (_track(1, 8, 1), _track(2, 7, 7))
    )
    assert result is not None and result.track.track_id == 1
    assert result.method == "visibility_and_track_continuity"


def test_falls_back_when_ball_tracking_unavailable() -> None:
    result = WeightedTargetPlayerSelector(Settings(selection_margin=0.01)).select(
        (_track(1, 9, available=False), _track(2, 7, available=False))
    )
    assert result is not None and result.method == "visibility_and_track_continuity"


def test_returns_none_for_ambiguous_candidates() -> None:
    result = WeightedTargetPlayerSelector(Settings(selection_margin=0.1)).select(
        (_track(1, 8), _track(2, 8))
    )
    assert result is None


def test_rejects_candidate_below_minimum_visibility() -> None:
    result = WeightedTargetPlayerSelector(Settings()).select((_track(1, visible=1),))
    assert result is None


def test_returns_none_for_empty_and_broken_tracks() -> None:
    selector = WeightedTargetPlayerSelector(Settings())
    assert selector.select(()) is None
    assert selector.select((_track(1, segment=2),)) is None
