"""Deterministic provisional visible-movement activity score."""

from time import perf_counter

from core.config import Settings
from core.exceptions import PhysicalScoreConfigurationError
from services.movement.schemas import MovementResult
from services.scoring.level_mapper import ScoreLevelMapper
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult

VERSION = "physical_activity_video_v0.1"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class RuleBasedPhysicalActivityScorer:
    def __init__(self, settings: Settings, mapper: ScoreLevelMapper | None = None) -> None:
        self._settings, self._mapper = settings, mapper or ScoreLevelMapper()

    def score(
        self,
        movement: MovementResult | None,
        visibility_ratio: float,
        visible_frames: int,
        longest_segment: int,
        track_confidence: float,
        movement_quality: float | None,
        movement_source: str,
    ) -> PhysicalScoreResult:
        started = perf_counter()
        if movement is None or movement_quality is None:
            return self._insufficient(started)
        weights = (
            self._settings.physical_score_activity_weight,
            self._settings.physical_score_active_time_weight,
            self._settings.physical_score_visibility_weight,
            self._settings.physical_score_continuity_weight,
            self._settings.physical_score_direction_weight,
        )
        if any(weight < 0 for weight in weights) or abs(sum(weights) - 1) > 1e-9:
            raise PhysicalScoreConfigurationError("Physical score weights must sum to 1.")
        duration = (
            movement.trajectory[-1].timestamp_seconds - movement.trajectory[0].timestamp_seconds
            if len(movement.trajectory) > 1
            else 0.0
        )
        observations = len(movement.trajectory)
        accepted_ratio = observations / max(observations + movement.rejected_position_jumps, 1)
        if (
            movement_quality < self._settings.physical_score_min_movement_quality
            or visibility_ratio < self._settings.physical_score_min_visibility_ratio
            or duration < self._settings.physical_score_min_visible_seconds
            or observations < self._settings.physical_score_min_movement_observations
            or accepted_ratio < self._settings.physical_score_min_accepted_interval_ratio
        ):
            return self._insufficient(started)
        active = _clamp(1 - movement.metrics.stationary_time_seconds / duration)
        continuity = _clamp(longest_segment / visible_frames) if visible_frames else 0.0
        direction_rate = movement.metrics.direction_changes / duration
        direction = (
            direction_rate / (direction_rate + self._settings.physical_score_direction_rate_scale)
            if direction_rate
            else 0.0
        )
        evidence = PhysicalScoreEvidence(
            _clamp(movement.metrics.movement_intensity),
            active,
            _clamp(visibility_ratio),
            continuity,
            _clamp(direction),
            _clamp(movement_quality),
            duration,
            observations,
            _clamp(accepted_ratio),
        )
        raw = sum(
            weight * component
            for weight, component in zip(
                weights,
                (
                    evidence.movement_intensity,
                    evidence.active_time_ratio,
                    evidence.visibility_ratio,
                    evidence.continuity_ratio,
                    evidence.direction_component,
                ),
                strict=True,
            )
        )
        value = _clamp(raw) * 100
        level, label, midpoint = self._mapper.map(value)
        base_confidence = (
            sum(
                (
                    evidence.movement_analysis_quality,
                    _clamp(track_confidence),
                    evidence.visibility_ratio,
                    evidence.accepted_interval_ratio,
                    _clamp(duration / self._settings.physical_score_min_visible_seconds),
                )
            )
            / 5
        )
        capped = movement_source == "raw_image_space"
        confidence = (
            min(base_confidence, self._settings.physical_score_raw_image_confidence_cap)
            if capped
            else base_confidence
        )
        return PhysicalScoreResult(
            value,
            level,
            label,
            midpoint,
            _clamp(confidence),
            "provisional_video_based",
            VERSION,
            None,
            evidence,
            (
                "image_space_measurements",
                "camera_motion_not_compensated",
                "short_video",
                "not_a_complete_fitness_assessment",
            ),
            "The score estimates visible movement activity in this video only.",
            raw,
            capped,
            round((perf_counter() - started) * 1000),
        )

    def _insufficient(self, started: float) -> PhysicalScoreResult:
        return PhysicalScoreResult(
            None,
            None,
            None,
            None,
            None,
            "insufficient_evidence",
            VERSION,
            "The video did not contain enough reliable movement evidence.",
            None,
            ("image_space_measurements", "not_a_complete_fitness_assessment"),
            "The score estimates visible movement activity in this video only.",
            None,
            False,
            round((perf_counter() - started) * 1000),
        )
