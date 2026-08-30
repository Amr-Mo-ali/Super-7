"""Contract tests for the proposed Sprint 1 dominant visual target policy.

The policy module is intentionally not implemented in this tests-only phase.  Imports are local so
each failing test identifies its missing contract behavior instead of aborting test collection.
"""

from __future__ import annotations

from math import inf, nan

from core.config import Settings
from services.segment_selection import TrackSegment
from services.selection import PlayerTrack


def _policy():  # type: ignore[no-untyped-def]
    """Expose the minimal planned internal API without making an absent module a collection error."""
    from services.dominant_target_selection import (  # noqa: PLC0415
        TargetSelectionStatus,
        evaluate_dominant_target,
        select_winning_track_segment,
        unique_track_evidence,
    )

    return (
        TargetSelectionStatus,
        evaluate_dominant_target,
        select_winning_track_segment,
        unique_track_evidence,
    )


def _track(
    track_id: int,
    *,
    visible_frames: int = 10,
    processed_frames: int = 100,
    longest_segment: int = 10,
    confidence: float = 0.9,
) -> PlayerTrack:
    return PlayerTrack(
        track_id,
        visible_frames,
        processed_frames,
        longest_segment,
        0,
        confidence,
        0,
        False,
    )


def _segment(
    track_id: int,
    segment_id: int,
    *,
    quality: float = 0.9,
    rejection_reasons: tuple[str, ...] = (),
) -> TrackSegment:
    return TrackSegment(
        track_id,
        segment_id,
        0,
        39,
        4.0,
        40,
        1.0,
        0.9,
        100.0,
        100.0,
        0.0,
        0,
        0.0,
        quality,
        rejection_reasons,
    )


def _evidence(
    track: PlayerTrack,
    frame_keys: dict[int, object],
    *,
    processed: int = 100,
    fps: float = 10.0,
):  # type: ignore[no-untyped-def]
    *_, unique_track_evidence = _policy()
    return unique_track_evidence(track, frame_keys, frames_processed=processed, fps=fps)


def _selection(
    tracks: tuple[PlayerTrack, ...],
    frames: dict[int, dict[int, object]],
    *,
    settings: Settings | None = None,
    processed: int = 100,
    fps: float = 10.0,
):  # type: ignore[no-untyped-def]
    _, evaluate_dominant_target, *_ = _policy()
    return evaluate_dominant_target(
        tracks,
        frames,
        frames_processed=processed,
        fps=fps,
        settings=settings or Settings(),
    )


def test_unique_frame_keys_determine_visible_count() -> None:
    evidence = _evidence(_track(1), {2: object(), 4: object(), 9: object()})
    assert evidence.unique_visible_frames == 3


def test_duplicate_tracker_returns_do_not_inflate_unique_selection_evidence() -> None:
    track = _track(1, visible_frames=4)
    evidence = _evidence(track, {7: object(), 8: object()})
    assert evidence.unique_visible_frames == 2
    assert evidence.unique_visible_frames != track.visible_frames


def test_visibility_ratio_uses_unique_frames_over_processed_frames() -> None:
    evidence = _evidence(_track(1), {1: object(), 2: object(), 3: object()}, processed=12)
    assert evidence.visibility_ratio == 0.25


def test_visible_duration_uses_unique_frames_over_fps() -> None:
    evidence = _evidence(_track(1), {1: object(), 2: object(), 3: object()}, fps=2.0)
    assert evidence.visible_duration_seconds == 1.5


def test_zero_or_negative_fps_is_not_valid_track_evidence() -> None:
    for fps in (0.0, -1.0):
        evidence = _evidence(_track(1), {1: object()}, fps=fps)
        assert not evidence.is_structurally_valid
        assert evidence.visible_duration_seconds is None


def test_zero_or_negative_processed_frames_is_not_valid_track_evidence() -> None:
    for processed in (0, -1):
        evidence = _evidence(_track(1), {1: object()}, processed=processed)
        assert not evidence.is_structurally_valid
        assert evidence.visibility_ratio is None


def test_nonfinite_input_never_escapes_as_qualifying_evidence() -> None:
    for value in (nan, inf, -inf):
        evidence = _evidence(_track(1), {1: object()}, fps=value)
        assert not evidence.is_structurally_valid
        assert evidence.visibility_ratio is None or evidence.visibility_ratio >= 0.0


def test_track_below_configured_minimum_visibility_does_not_qualify() -> None:
    settings = Settings(minimum_visibility_ratio=0.2)
    result = _selection(
        (_track(1),), {1: {frame: object() for frame in range(19)}}, settings=settings
    )
    assert result.status.name == "NOT_ESTABLISHED"
    assert result.reason == "no_qualifying_visual_target"


def test_track_below_configured_continuous_frame_requirement_does_not_qualify() -> None:
    settings = Settings(minimum_continuous_track_length=5)
    result = _selection(
        (_track(1, longest_segment=4),),
        {1: {frame: object() for frame in range(30)}},
        settings=settings,
    )
    assert result.status.name == "NOT_ESTABLISHED"


def test_track_below_configured_confidence_requirement_does_not_qualify() -> None:
    settings = Settings(minimum_detection_confidence=0.5)
    result = _selection(
        (_track(1, confidence=0.49),),
        {1: {frame: object() for frame in range(30)}},
        settings=settings,
    )
    assert result.status.name == "NOT_ESTABLISHED"


def test_current_minimum_boundaries_qualify() -> None:
    settings = Settings(
        minimum_visibility_ratio=0.2,
        minimum_continuous_track_length=5,
        minimum_detection_confidence=0.5,
    )
    result = _selection(
        (_track(1, longest_segment=5, confidence=0.5),),
        {1: {frame: object() for frame in range(20)}},
        settings=settings,
    )
    assert result.status.name == "ESTABLISHED"


def test_track_qualification_is_deterministic() -> None:
    tracks = (_track(2), _track(1))
    frames = {
        1: {frame: object() for frame in range(40)},
        2: {frame: object() for frame in range(20)},
    }
    assert (
        _selection(tracks, frames).selected_track_id
        == _selection(tuple(reversed(tracks)), frames).selected_track_id
    )


def test_one_qualifying_track_without_plausible_alternative_is_dominant() -> None:
    result = _selection((_track(1),), {1: {frame: object() for frame in range(40)}})
    assert result.status.name == "ESTABLISHED" and result.selected_track_id == 1


def test_close_slightly_nonqualifying_valid_alternative_is_ambiguous() -> None:
    settings = Settings(minimum_visibility_ratio=0.2, selection_margin=0.08)
    result = _selection(
        (_track(1), _track(2, longest_segment=4)),
        {1: {frame: object() for frame in range(25)}, 2: {frame: object() for frame in range(20)}},
        settings=settings,
    )
    assert result.status.name == "NOT_ESTABLISHED" and result.reason == "ambiguous_visual_target"


def test_two_qualifying_tracks_inside_margin_are_ambiguous() -> None:
    result = _selection(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(27)}, 2: {frame: object() for frame in range(20)}},
    )
    assert result.reason == "ambiguous_visual_target"


def test_exact_operational_margin_demonstrates_dominance_inclusively() -> None:
    result = _selection(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(28)}, 2: {frame: object() for frame in range(20)}},
    )
    assert result.status.name == "ESTABLISHED" and result.selected_track_id == 1


def test_visibility_gap_below_operational_margin_is_ambiguous() -> None:
    result = _selection(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(27)}, 2: {frame: object() for frame in range(20)}},
    )
    assert result.reason == "ambiguous_visual_target"


def test_exact_visibility_tie_is_always_ambiguous() -> None:
    result = _selection(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(30)}, 2: {frame: object() for frame in range(30)}},
    )
    assert result.status.name == "NOT_ESTABLISHED" and result.reason == "ambiguous_visual_target"


def test_reordering_tracks_does_not_change_winner_or_ambiguity() -> None:
    tracks = (_track(1), _track(2))
    frames = {
        1: {frame: object() for frame in range(40)},
        2: {frame: object() for frame in range(20)},
    }
    first = _selection(tracks, frames)
    second = _selection(tuple(reversed(tracks)), dict(reversed(tuple(frames.items()))))
    assert (first.status, first.selected_track_id, first.reason) == (
        second.status,
        second.selected_track_id,
        second.reason,
    )


def test_clearly_dominant_qualifying_track_wins() -> None:
    result = _selection(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(70)}, 2: {frame: object() for frame in range(20)}},
    )
    assert result.status.name == "ESTABLISHED" and result.selected_track_id == 1


def test_nonfinite_or_observation_empty_track_cannot_win() -> None:
    invalid = _track(1, confidence=nan)
    result = _selection((invalid, _track(2)), {1: {}, 2: {frame: object() for frame in range(40)}})
    assert result.status.name == "ESTABLISHED" and result.selected_track_id == 2


def test_different_track_ids_remain_separate_alternatives() -> None:
    result = _selection(
        (_track(7), _track(8)),
        {7: {frame: object() for frame in range(25)}, 8: {frame: object() for frame in range(20)}},
    )
    assert result.reason == "ambiguous_visual_target"


def test_track_a_wins_even_when_track_b_has_higher_quality_segment() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    tracks = (_track(1), _track(2))
    frames = {
        1: {frame: object() for frame in range(60)},
        2: {frame: object() for frame in range(20)},
    }
    target = evaluate_dominant_target(
        tracks, frames, frames_processed=100, fps=10.0, settings=Settings()
    )
    chosen = select_winning_track_segment(
        target, (_segment(1, 1, quality=0.5), _segment(2, 1, quality=0.99))
    )
    assert target.selected_track_id == 1 and chosen.track_id == 1


def test_two_segments_from_winning_track_are_not_identity_alternatives() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    target = evaluate_dominant_target(
        (_track(1),),
        {1: {frame: object() for frame in range(40)}},
        frames_processed=100,
        fps=10.0,
        settings=Settings(),
    )
    chosen = select_winning_track_segment(
        target, (_segment(1, 1, quality=0.5), _segment(1, 2, quality=0.9))
    )
    assert target.selected_track_id == 1 and chosen.segment_id == 2


def test_only_winning_track_segments_are_considered() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    target = evaluate_dominant_target(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(60)}, 2: {frame: object() for frame in range(20)}},
        frames_processed=100,
        fps=10.0,
        settings=Settings(),
    )
    chosen = select_winning_track_segment(
        target, (_segment(1, 1, quality=0.6), _segment(2, 1, quality=0.99))
    )
    assert chosen.track_id == target.selected_track_id == 1


def test_best_qualifying_segment_within_winning_track_is_selected() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    target = evaluate_dominant_target(
        (_track(1),),
        {1: {frame: object() for frame in range(40)}},
        frames_processed=100,
        fps=10.0,
        settings=Settings(),
    )
    assert (
        select_winning_track_segment(
            target, (_segment(1, 1, quality=0.5), _segment(1, 2, quality=0.9))
        ).segment_id
        == 2
    )


def test_winning_track_without_qualifying_segment_never_falls_back() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    target = evaluate_dominant_target(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(60)}, 2: {frame: object() for frame in range(20)}},
        frames_processed=100,
        fps=10.0,
        settings=Settings(),
    )
    unavailable = _segment(1, 1, rejection_reasons=("low_segment_quality",))
    assert select_winning_track_segment(target, (unavailable, _segment(2, 1, quality=0.99))) is None


def test_different_track_ids_are_not_stitched_or_merged_for_segments() -> None:
    _, evaluate_dominant_target, select_winning_track_segment, _ = _policy()
    target = evaluate_dominant_target(
        (_track(1), _track(2)),
        {1: {frame: object() for frame in range(60)}, 2: {frame: object() for frame in range(20)}},
        frames_processed=100,
        fps=10.0,
        settings=Settings(),
    )
    assert select_winning_track_segment(target, (_segment(2, 1),)) is None
