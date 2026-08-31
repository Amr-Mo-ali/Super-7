"""Pure track-first dominant visual target selection for Sprint 1."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite

from core.config import Settings
from services.player_tracker import TrackingRun
from services.segment_selection import TrackSegment, build_segments, rank_segments
from services.selection import PlayerTrack


class TargetSelectionStatus(Enum):
    """Provisional visual-target establishment state, never identity verification."""

    ESTABLISHED = "ESTABLISHED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


@dataclass(frozen=True, slots=True)
class TrackEvidence:
    """Unique frame-key evidence for one temporary visual track."""

    unique_visible_frames: int
    visibility_ratio: float | None
    visible_duration_seconds: float | None
    is_structurally_valid: bool


@dataclass(frozen=True, slots=True)
class TargetEligibilityResult:
    """Internal result of provisional visual-target establishment."""

    status: TargetSelectionStatus
    reason: str
    selected_track_id: int | None = None


def unique_track_evidence(
    track: PlayerTrack,
    frame_keys: Mapping[int, object],
    *,
    frames_processed: int,
    fps: float,
) -> TrackEvidence:
    """Build conservative evidence from unique box-frame keys for one track."""
    unique_visible_frames = len(frame_keys)
    if frames_processed <= 0 or fps <= 0 or not isfinite(frames_processed) or not isfinite(fps):
        return TrackEvidence(unique_visible_frames, None, None, False)
    return TrackEvidence(
        unique_visible_frames,
        unique_visible_frames / frames_processed,
        unique_visible_frames / fps,
        True,
    )


def evaluate_dominant_target(
    tracks: tuple[PlayerTrack, ...],
    frame_keys_by_track: Mapping[int, Mapping[int, object]],
    *,
    frames_processed: int,
    fps: float,
    settings: Settings,
) -> TargetEligibilityResult:
    """Establish an unambiguous qualifying track from unique frame-key evidence."""
    evidence_by_track = {
        track.track_id: unique_track_evidence(
            track,
            frame_keys_by_track.get(track.track_id, {}),
            frames_processed=frames_processed,
            fps=fps,
        )
        for track in tracks
    }
    qualified = [
        track for track in tracks if _qualified(track, evidence_by_track[track.track_id], settings)
    ]
    if not qualified:
        return TargetEligibilityResult(
            TargetSelectionStatus.NOT_ESTABLISHED,
            "no_qualifying_visual_target",
        )

    winner = _ordered(qualified, evidence_by_track)[0]
    alternatives = [
        track
        for track in tracks
        if track.track_id != winner.track_id
        and _plausible(track, evidence_by_track[track.track_id])
    ]
    if alternatives:
        runner_up = _ordered(alternatives, evidence_by_track)[0]
        winner_ratio = evidence_by_track[winner.track_id].visibility_ratio
        runner_up_ratio = evidence_by_track[runner_up.track_id].visibility_ratio
        assert winner_ratio is not None and runner_up_ratio is not None
        if winner_ratio - runner_up_ratio < settings.selection_margin:
            return TargetEligibilityResult(
                TargetSelectionStatus.NOT_ESTABLISHED,
                "ambiguous_visual_target",
            )
    return TargetEligibilityResult(
        TargetSelectionStatus.ESTABLISHED,
        "dominant_visual_candidate",
        winner.track_id,
    )


def select_winning_track_segment(
    target: TargetEligibilityResult,
    segments: tuple[TrackSegment, ...],
) -> TrackSegment | None:
    """Select the best qualifying segment only within an established winning track."""
    if target.status is not TargetSelectionStatus.ESTABLISHED or target.selected_track_id is None:
        return None
    return next(
        iter(
            rank_segments(
                tuple(
                    segment for segment in segments if segment.track_id == target.selected_track_id
                )
            )
        ),
        None,
    )


def resolve_dominant_target(
    run: TrackingRun,
    *,
    fps: float,
    settings: Settings,
) -> tuple[TargetEligibilityResult, TrackSegment | None]:
    """Resolve one scoreable segment from the existing tracking result only."""
    target = evaluate_dominant_target(
        run.tracks,
        run.player_boxes or {},
        frames_processed=run.diagnostics.frames_processed,
        fps=fps,
        settings=settings,
    )
    if target.status is not TargetSelectionStatus.ESTABLISHED:
        return target, None

    segments = build_segments(
        run.player_boxes or {},
        run.player_confidences or {},
        run.ball_points or {},
        fps,
        settings,
    )
    selected_segment = select_winning_track_segment(target, segments)
    if selected_segment is None:
        return (
            TargetEligibilityResult(
                TargetSelectionStatus.NOT_ESTABLISHED,
                "no_qualifying_visual_target",
            ),
            None,
        )
    if selected_segment.track_id != target.selected_track_id:
        raise ValueError("dominant target segment does not match the established track")
    return target, selected_segment


def _qualified(track: PlayerTrack, evidence: TrackEvidence, settings: Settings) -> bool:
    return (
        evidence.is_structurally_valid
        and evidence.visibility_ratio is not None
        and evidence.visibility_ratio >= settings.minimum_visibility_ratio
        and track.longest_segment >= settings.minimum_continuous_track_length
        and isfinite(track.average_confidence)
        and track.average_confidence >= settings.minimum_detection_confidence
    )


def _plausible(track: PlayerTrack, evidence: TrackEvidence) -> bool:
    return (
        evidence.is_structurally_valid
        and evidence.unique_visible_frames > 0
        and evidence.visibility_ratio is not None
        and isfinite(track.average_confidence)
    )


def _ordered(
    tracks: list[PlayerTrack], evidence_by_track: dict[int, TrackEvidence]
) -> list[PlayerTrack]:
    return sorted(
        tracks,
        key=lambda track: (-_visibility(evidence_by_track[track.track_id]), track.track_id),
    )


def _visibility(evidence: TrackEvidence) -> float:
    assert evidence.visibility_ratio is not None
    return evidence.visibility_ratio
