"""Deterministic, feature-based global camera-motion estimation."""

from collections import Counter
from dataclasses import dataclass
from math import atan2, degrees, hypot
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

CAMERA_MOTION_VERSION = "camera_motion_compensation_v0.1"


@dataclass(frozen=True, slots=True)
class CameraMotionConfig:
    min_features: int = 20
    min_inliers: int = 12
    min_inlier_ratio: float = 0.45
    max_translation_ratio: float = 0.25
    max_rotation_degrees: float = 12.0
    max_scale_change: float = 0.08
    min_confidence: float = 0.45
    scene_cut_histogram_delta: float = 0.45


@dataclass(frozen=True, slots=True)
class CameraMotionInterval:
    source_frame: int
    target_frame: int
    translation_x: float
    translation_y: float
    scale: float
    rotation_degrees: float
    inlier_count: int
    inlier_ratio: float
    confidence: float
    accepted: bool
    rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class CameraMotionResult:
    intervals: tuple[CameraMotionInterval, ...]
    cumulative_transforms: dict[int, np.ndarray]
    processing_time_ms: int

    @property
    def accepted_intervals(self) -> int:
        return sum(item.accepted for item in self.intervals)

    @property
    def rejected_intervals(self) -> int:
        return len(self.intervals) - self.accepted_intervals

    @property
    def coverage_ratio(self) -> float:
        return self.accepted_intervals / len(self.intervals) if self.intervals else 0.0

    @property
    def scene_cut_count(self) -> int:
        return sum(item.rejection_reason == "scene_cut" for item in self.intervals)


class CameraMotionEstimator:
    """Estimates global affine motion from image features, never object detections."""

    def __init__(self, config: CameraMotionConfig | None = None) -> None:
        self._config = config or CameraMotionConfig()

    def estimate(
        self, video_path: Path, start_frame: int = 0, end_frame: int | None = None
    ) -> CameraMotionResult:
        capture = cv2.VideoCapture(str(video_path))
        frames: list[np.ndarray] = []
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index >= start_frame and (end_frame is None or index <= end_frame):
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            index += 1
        capture.release()
        return self.estimate_frames(frames, start_frame)

    def estimate_frames(self, frames: list[np.ndarray], first_frame: int = 0) -> CameraMotionResult:
        started = perf_counter()
        intervals: list[CameraMotionInterval] = []
        cumulative: dict[int, np.ndarray] = {first_frame: np.eye(3, dtype=np.float64)}
        current: np.ndarray | None = cumulative[first_frame]
        for offset, (source, target) in enumerate(zip(frames, frames[1:], strict=False)):
            frame = first_frame + offset
            interval, affine = self._estimate_interval(source, target, frame, frame + 1)
            intervals.append(interval)
            if interval.accepted and affine is not None and current is not None:
                lifted = np.vstack((affine, np.array([0.0, 0.0, 1.0])))
                current = lifted @ current
                cumulative[frame + 1] = current
            else:
                # A rejected interval is an explicit boundary; never bridge it.
                current = None
        return CameraMotionResult(
            tuple(intervals), cumulative, round((perf_counter() - started) * 1000)
        )

    def _estimate_interval(
        self, source: np.ndarray, target: np.ndarray, source_frame: int, target_frame: int
    ) -> tuple[CameraMotionInterval, np.ndarray | None]:
        if self._histogram_delta(source, target) > self._config.scene_cut_histogram_delta:
            return self._rejected(source_frame, target_frame, "scene_cut"), None
        features = cv2.goodFeaturesToTrack(source, maxCorners=500, qualityLevel=0.01, minDistance=7)
        if features is None or len(features) < self._config.min_features:
            return self._rejected(source_frame, target_frame, "too_few_features"), None
        next_points, status, _ = cv2.calcOpticalFlowPyrLK(source, target, features, None)  # type: ignore[call-overload]
        if next_points is None or status is None:
            return self._rejected(source_frame, target_frame, "feature_tracking_failed"), None
        valid = status.ravel() == 1
        previous, observed = features[valid], next_points[valid]
        if len(previous) < self._config.min_features:
            return self._rejected(source_frame, target_frame, "too_few_features"), None
        affine, inliers = cv2.estimateAffinePartial2D(
            previous, observed, method=cv2.RANSAC, ransacReprojThreshold=3
        )
        if affine is None or inliers is None or not np.isfinite(affine).all():
            return self._rejected(source_frame, target_frame, "invalid_transform"), None
        count = int(inliers.sum())
        ratio = count / len(previous)
        scale = hypot(float(affine[0, 0]), float(affine[1, 0]))
        rotation = degrees(atan2(float(affine[1, 0]), float(affine[0, 0])))
        tx, ty = float(affine[0, 2]), float(affine[1, 2])
        diagonal = hypot(*source.shape[:2])
        confidence = min(1.0, ratio * min(1.0, count / self._config.min_inliers))
        reason = (
            "too_few_inliers"
            if count < self._config.min_inliers
            else "low_inlier_ratio"
            if ratio < self._config.min_inlier_ratio
            else "implausible_transform"
            if hypot(tx, ty) / max(diagonal, 1) > self._config.max_translation_ratio
            or abs(rotation) > self._config.max_rotation_degrees
            or abs(scale - 1) > self._config.max_scale_change
            else "low_confidence"
            if confidence < self._config.min_confidence
            else None
        )
        interval = CameraMotionInterval(
            source_frame,
            target_frame,
            tx,
            ty,
            scale,
            rotation,
            count,
            ratio,
            confidence,
            reason is None,
            reason,
        )
        return interval, affine if reason is None else None

    def _rejected(self, source: int, target: int, reason: str) -> CameraMotionInterval:
        return CameraMotionInterval(source, target, 0.0, 0.0, 1.0, 0.0, 0, 0.0, 0.0, False, reason)

    @staticmethod
    def _histogram_delta(source: np.ndarray, target: np.ndarray) -> float:
        left = cv2.calcHist([source], [0], None, [32], [0, 256])
        right = cv2.calcHist([target], [0], None, [32], [0, 256])
        return float(cv2.compareHist(left, right, cv2.HISTCMP_BHATTACHARYYA))


def diagnostics(result: CameraMotionResult) -> dict[str, object]:
    """Produce compact, serializable camera-motion diagnostics."""
    accepted = [item for item in result.intervals if item.accepted]
    reasons = Counter(item.rejection_reason for item in result.intervals if item.rejection_reason)

    def values(key: str) -> list[float]:
        return [abs(float(getattr(item, key))) for item in accepted]

    def average(numbers: list[float]) -> float:
        return sum(numbers) / len(numbers) if numbers else 0.0

    return {
        "enabled": bool(result.intervals),
        "estimator_version": CAMERA_MOTION_VERSION,
        "evaluated_intervals": len(result.intervals),
        "accepted_intervals": result.accepted_intervals,
        "rejected_intervals": result.rejected_intervals,
        "coverage_ratio": result.coverage_ratio,
        "mean_confidence": average([item.confidence for item in accepted]),
        "scene_cut_count": result.scene_cut_count,
        "rejection_breakdown": dict(reasons),
        "mean_translation_pixels": average(
            [hypot(item.translation_x, item.translation_y) for item in accepted]
        ),
        "max_translation_pixels": max(
            [hypot(item.translation_x, item.translation_y) for item in accepted], default=0.0
        ),
        "mean_rotation_degrees": average(values("rotation_degrees")),
        "max_rotation_degrees": max(values("rotation_degrees"), default=0.0),
        "mean_scale_change": average([abs(item.scale - 1) for item in accepted]),
        "processing_time_ms": result.processing_time_ms,
    }
