"""Versioned deterministic confidence calculation."""

from core.config import Settings
from core.exceptions import InteractionConfidenceError

CONFIDENCE_VERSION = "interaction_confidence_v0.1"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def confidence(
    settings: Settings,
    mean_normalized_distance: float,
    coverage: float,
    duration_seconds: float,
    detection_confidence: float,
    player_quality: float,
    ball_quality: float,
) -> float:
    weights = (
        settings.interaction_distance_weight,
        settings.interaction_coverage_weight,
        settings.interaction_duration_weight,
        settings.interaction_detection_weight,
        settings.interaction_quality_weight,
    )
    if any(weight < 0 for weight in weights) or abs(sum(weights) - 1.0) > 1e-9:
        raise InteractionConfidenceError("Interaction confidence weights must sum to 1.")
    if (
        settings.interaction_proximity_threshold_ratio <= 0
        or settings.interaction_duration_scale <= 0
    ):
        raise InteractionConfidenceError("Interaction confidence scales must be positive.")
    components = (
        _clamp(1 - mean_normalized_distance / settings.interaction_proximity_threshold_ratio),
        _clamp(coverage),
        _clamp(duration_seconds / (duration_seconds + settings.interaction_duration_scale)),
        _clamp(detection_confidence),
        _clamp((_clamp(player_quality) + _clamp(ball_quality)) / 2),
    )
    return _clamp(
        sum(weight * component for weight, component in zip(weights, components, strict=True))
    )
