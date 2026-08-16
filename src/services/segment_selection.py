"""Deterministic selection of the best continuous player-track segment."""

from collections import Counter
from dataclasses import dataclass
from math import hypot
from statistics import median

from core.config import Settings
from services.ball_tracker import BallTrackPoint
from services.player_detector import BoundingBox
from services.selection import PlayerTrack, Selection

QUALITY_VERSION = "target_segment_quality_v0.1"
_WEIGHTS = (0.25, 0.25, 0.20, 0.15, 0.10, 0.05)
assert sum(_WEIGHTS) == 1.0


@dataclass(frozen=True, slots=True)
class TrackSegment:
    track_id: int
    segment_id: int
    start_frame: int
    end_frame: int
    duration_seconds: float
    visible_frames: int
    continuity_ratio: float
    mean_confidence: float
    mean_bbox_height: float
    median_bbox_height: float
    normalized_center_displacement: float
    ball_proximity_frames: int
    ball_proximity_ratio: float
    segment_quality: float
    rejection_reasons: tuple[str, ...]


def build_segments(
    boxes: dict[int, dict[int, BoundingBox]],
    confidences: dict[int, dict[int, float]],
    balls: dict[int, BallTrackPoint],
    fps: float,
    settings: Settings,
) -> tuple[TrackSegment, ...]:
    """Split observations on missing-frame or implausible-motion discontinuities."""
    result: list[TrackSegment] = []
    for track_id, observations in boxes.items():
        frames = sorted(observations)
        groups: list[list[int]] = []
        for frame in frames:
            if not groups or _breaks(groups[-1][-1], frame, observations, settings):
                groups.append([frame])
            else:
                groups[-1].append(frame)
        result.extend(
            _segment(
                track_id,
                index + 1,
                group,
                observations,
                confidences.get(track_id, {}),
                balls,
                fps,
                settings,
            )
            for index, group in enumerate(groups)
        )
    return tuple(result)


def _breaks(previous: int, current: int, boxes: dict[int, BoundingBox], settings: Settings) -> bool:
    if current - previous - 1 > settings.target_segment_max_gap_frames:
        return True
    a, b = boxes[previous], boxes[current]
    ah, bh = a.y2 - a.y1, b.y2 - b.y1
    if ah <= 0 or bh <= 0:
        return True
    ac, bc = ((a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2), ((b.x1 + b.x2) / 2, (b.y1 + b.y2) / 2)
    return (
        hypot(ac[0] - bc[0], ac[1] - bc[1]) / max((ah + bh) / 2, 1.0)
        > settings.target_segment_max_normalized_center_jump
    )


def _segment(
    track_id: int,
    segment_id: int,
    frames: list[int],
    boxes: dict[int, BoundingBox],
    confidences: dict[int, float],
    balls: dict[int, BallTrackPoint],
    fps: float,
    settings: Settings,
) -> TrackSegment:
    heights = [boxes[f].y2 - boxes[f].y1 for f in frames]
    values = [confidences.get(f, 0.0) for f in frames]
    duration_frames = frames[-1] - frames[0] + 1
    ball_frames = sum(1 for f in frames if _near_ball(boxes[f], balls.get(f), settings))
    displacements = [
        _center_distance(boxes[a], boxes[b]) / max((heights[i] + heights[i + 1]) / 2, 1.0)
        for i, (a, b) in enumerate(zip(frames, frames[1:], strict=False))
    ]
    continuity = len(frames) / duration_frames
    duration = duration_frames / fps if fps else 0.0
    confidence = sum(values) / len(values) if values else 0.0
    size = sum(heights) / len(heights) if heights else 0.0
    stability = 1.0 / (1.0 + (sum(displacements) / len(displacements) if displacements else 0.0))
    quality = (
        min(1.0, duration / max(settings.target_segment_min_duration_seconds * 2, 0.001)) * 0.25
        + continuity * 0.25
        + confidence * 0.20
        + min(1.0, size / 100.0) * 0.15
        + stability * 0.10
        + (ball_frames / len(frames)) * 0.05
    )
    reasons = []
    if len(frames) < settings.target_segment_min_visible_frames:
        reasons.append("insufficient_visible_frames")
    if duration < settings.target_segment_min_duration_seconds:
        reasons.append("insufficient_duration")
    if confidence < settings.target_segment_min_mean_confidence:
        reasons.append("low_mean_confidence")
    if quality < settings.target_segment_min_quality:
        reasons.append("low_segment_quality")
    return TrackSegment(
        track_id,
        segment_id,
        frames[0],
        frames[-1],
        duration,
        len(frames),
        continuity,
        confidence,
        size,
        median(heights),
        sum(displacements) / len(displacements) if displacements else 0.0,
        ball_frames,
        ball_frames / len(frames),
        quality,
        tuple(reasons),
    )


def _center_distance(a: BoundingBox, b: BoundingBox) -> float:
    return hypot((a.x1 + a.x2 - b.x1 - b.x2) / 2, (a.y1 + a.y2 - b.y1 - b.y2) / 2)


def _near_ball(box: BoundingBox, ball: BallTrackPoint | None, settings: Settings) -> bool:
    if ball is None or not ball.visible or ball.center_point is None or box.y2 <= box.y1:
        return False
    return (
        hypot((box.x1 + box.x2) / 2 - ball.center_point[0], box.y2 - ball.center_point[1])
        / (box.y2 - box.y1)
        <= settings.ball_proximity_threshold
    )


def select_segment(segments: tuple[TrackSegment, ...]) -> Selection | None:
    ranked = rank_segments(segments)
    if not ranked:
        return None
    segment = ranked[0]
    track = PlayerTrack(
        segment.track_id,
        segment.visible_frames,
        segment.end_frame - segment.start_frame + 1,
        segment.visible_frames,
        0,
        segment.mean_confidence,
        segment.ball_proximity_frames,
        True,
    )
    return Selection(
        track,
        "best_continuous_track_segment",
        segment.segment_quality,
        segment.continuity_ratio,
        0.0,
        segment.segment_id,
        segment.start_frame,
        segment.end_frame,
        segment.duration_seconds,
    )


def rank_segments(segments: tuple[TrackSegment, ...]) -> tuple[TrackSegment, ...]:
    """Return eligible segments in the exact order used for target selection."""
    return tuple(
        sorted(
            (segment for segment in segments if not segment.rejection_reasons),
            key=lambda segment: segment.segment_quality,
            reverse=True,
        )
    )


def ranking_components(segment: TrackSegment, settings: Settings) -> dict[str, float]:
    """Expose the existing segment-quality terms without changing their calculation."""
    return {
        "duration": min(
            1.0,
            segment.duration_seconds / max(settings.target_segment_min_duration_seconds * 2, 0.001),
        )
        * 0.25,
        "continuity": segment.continuity_ratio * 0.25,
        "confidence": segment.mean_confidence * 0.20,
        "bbox_height": min(1.0, segment.mean_bbox_height / 100.0) * 0.15,
        "stability": 1.0 / (1.0 + segment.normalized_center_displacement) * 0.10,
        "ball_proximity": segment.ball_proximity_ratio * 0.05,
    }


def rejection_diagnostics(
    tracks: tuple[PlayerTrack, ...], segments: tuple[TrackSegment, ...], fps: float
) -> tuple[list[dict[str, object]], dict[str, int]]:
    by_track = {
        track.track_id: [s for s in segments if s.track_id == track.track_id] for track in tracks
    }
    details: list[dict[str, object]] = []
    breakdown: Counter[str] = Counter()
    for track in tracks:
        own = by_track[track.track_id]
        reasons = sorted({r for s in own for r in s.rejection_reasons}) or ["has_valid_segment"]
        breakdown.update(r for r in reasons if r != "has_valid_segment")
        details.append(
            {
                "track_id": track.track_id,
                "visible_frames": track.visible_frames,
                "visibility_ratio": track.visibility_ratio,
                "longest_continuous_visible_segment": track.longest_segment,
                "longest_segment_duration_seconds": track.longest_segment / fps if fps else 0.0,
                "mean_detection_confidence": track.average_confidence,
                "mean_bbox_height": max((s.mean_bbox_height for s in own), default=0.0),
                "lost_gap_count": track.lost_track_count,
                "maximum_gap_frames": max(
                    (s.end_frame - s.start_frame + 1 - s.visible_frames for s in own), default=0
                ),
                "ball_proximity_frames": sum(s.ball_proximity_frames for s in own),
                "rejection_reasons": reasons,
            }
        )
    return details, dict(breakdown)
