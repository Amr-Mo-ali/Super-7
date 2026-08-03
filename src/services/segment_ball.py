"""Selected-segment primary-ball reconstruction using already detected candidates only."""

from collections.abc import Mapping
from dataclasses import dataclass
from math import hypot
from time import perf_counter

from core.config import Settings
from services.ball_detector import BallDetection
from services.ball_tracker import BallTrackPoint

RECONSTRUCTION_VERSION = "segment_ball_reconstruction_v0.1"
QUALITY_VERSION = "segment_ball_analysis_quality_v0.1"
_WEIGHTS = (0.25, 0.25, 0.15, 0.15, 0.10, 0.10)
if sum(_WEIGHTS) != 1.0:
    raise ValueError("segment ball quality weights must sum to one")


@dataclass(frozen=True, slots=True)
class SegmentBallResult:
    points: dict[int, BallTrackPoint]
    quality: float | None
    failure_reasons: tuple[str, ...]
    detected_frames: int
    interpolated_frames: int
    reconstructed_frames: int
    visibility_ratio: float
    segments_before: int
    segments_after: int
    longest_run: int
    longest_gap: int
    mean_confidence: float | None
    multiple_candidate_ratio: float
    quality_components: dict[str, float]
    processing_time_ms: int


def reconstruct(
    candidates: Mapping[int, tuple[BallDetection, ...]],
    start: int,
    end: int,
    fps: float,
    settings: Settings,
) -> SegmentBallResult:
    started = perf_counter()
    selected: dict[int, BallTrackPoint] = {}
    multiple = 0
    previous: tuple[float, float] | None = None
    for frame in range(start, end + 1):
        options = [
            x
            for x in candidates.get(frame, ())
            if x.confidence >= settings.ball_minimum_detection_confidence
        ]
        if len(options) > 1:
            multiple += 1
        if not options:
            continue
        chosen = min(options, key=lambda x: _cost(x, previous, settings))
        if (
            previous is not None
            and _distance(chosen.center_point, previous) / 100
            > settings.segment_ball_max_normalized_jump
        ):
            continue
        selected[frame] = BallTrackPoint(
            frame, frame / fps, chosen.center_point, chosen.confidence, True, 1
        )
        previous = chosen.center_point
    before = _runs(selected)
    interpolated = _interpolate(selected, settings, fps)
    ordered = dict(sorted(selected.items()))
    runs = _runs(ordered)
    detected = len(ordered) - interpolated
    total = end - start + 1
    longest_run = max((len(run) for run in runs), default=0)
    gaps = [b - a - 1 for a, b in zip(ordered, list(ordered)[1:], strict=False)]
    longest_gap = max(gaps, default=0)
    confidences = [p.confidence for p in ordered.values() if p.confidence is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    reasons: list[str] = []
    if not ordered:
        reasons.append("no_accepted_segment_ball_observations")
    elif longest_run < 1:
        reasons.append("no_continuous_ball_evidence")
    quality: float | None = None
    components = {
        "visibility": len(ordered) / total if total else 0.0,
        "continuity": longest_run / total if total else 0.0,
        "confidence": mean_confidence or 0.0,
        "fragmentation": 1 - max(len(runs) - 1, 0) / max(total, 1),
        "gap": 1 - longest_gap / max(total, 1),
        "ambiguity": 1 - multiple / max(total, 1),
    }
    if not reasons:
        quality = sum(
            weight * components[key] for weight, key in zip(_WEIGHTS, components, strict=True)
        )
    return SegmentBallResult(
        ordered,
        quality,
        tuple(reasons),
        detected,
        interpolated,
        len(ordered),
        components["visibility"],
        len(before),
        len(runs),
        longest_run,
        longest_gap,
        mean_confidence,
        multiple / max(total, 1),
        components,
        round((perf_counter() - started) * 1000),
    )


def _cost(item: BallDetection, previous: tuple[float, float] | None, settings: Settings) -> float:
    continuity = _distance(item.center_point, previous) / 100 if previous else 0.0
    return continuity * 0.7 + (1 - item.confidence) * 0.3


def _distance(a: tuple[float, float], b: tuple[float, float] | None) -> float:
    return hypot(a[0] - b[0], a[1] - b[1]) if b else 0.0


def _interpolate(points: dict[int, BallTrackPoint], settings: Settings, fps: float) -> int:
    count = 0
    for left, right in zip(sorted(points), sorted(points)[1:], strict=False):
        gap = right - left - 1
        a, b = points[left], points[right]
        if (
            not 0 < gap <= settings.segment_ball_max_interpolation_gap_frames
            or a.confidence is None
            or b.confidence is None
            or min(a.confidence, b.confidence) < settings.segment_ball_min_endpoint_confidence
        ):
            continue
        if (
            _distance(a.center_point or (0, 0), b.center_point or (0, 0)) / max(gap + 1, 1) / 100
            > settings.segment_ball_max_normalized_jump
        ):
            continue
        assert a.center_point is not None and b.center_point is not None
        for offset in range(1, gap + 1):
            ratio = offset / (gap + 1)
            center = (
                a.center_point[0] + (b.center_point[0] - a.center_point[0]) * ratio,
                a.center_point[1] + (b.center_point[1] - a.center_point[1]) * ratio,
            )
            points[left + offset] = BallTrackPoint(
                left + offset,
                (left + offset) / fps,
                center,
                min(a.confidence, b.confidence),
                True,
                1,
            )
            count += 1
    return count


def _runs(points: dict[int, BallTrackPoint]) -> list[list[int]]:
    runs: list[list[int]] = []
    for frame in sorted(points):
        if not runs or frame != runs[-1][-1] + 1:
            runs.append([frame])
        else:
            runs[-1].append(frame)
    return runs
