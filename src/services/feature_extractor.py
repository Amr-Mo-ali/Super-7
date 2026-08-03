"""Explicit unavailable-feature responses for the tracking MVP."""

from dataclasses import asdict

from schemas.analysis import (
    FeatureMetric,
    FeaturesResponse,
    PhysicalScoreEvidenceResponse,
    PhysicalScoreResponse,
    ScoresResponse,
    TechnicalScoreResponse,
    UnsupportedMetric,
)
from services.ball_proximity import BallProximityResult
from services.movement.schemas import MovementResult
from services.scoring.models import PhysicalScoreResult
from services.scoring.technical import VERSION as TECHNICAL_VERSION
from services.scoring.technical import TechnicalScoreResult

_REASON = "Not supported by the current automatic-tracking implementation."


class FeatureExtractor:
    """Never infers football skills or events from incomplete evidence."""

    def features(
        self,
        proximity: BallProximityResult | None = None,
        reason: str | None = None,
        movement: MovementResult | None = None,
        movement_reason: str | None = None,
    ) -> FeaturesResponse:
        def movement_metric(value: float) -> FeatureMetric:
            if movement is not None:
                return FeatureMetric(value=value)
            return FeatureMetric(value=None, reason=movement_reason or "Movement analysis failed.")

        return FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(value=None, reason=reason)
            if proximity is None
            else FeatureMetric(value=proximity.ball_proximity_time_seconds),
            movement_intensity=movement_metric(
                movement.metrics.movement_intensity if movement else 0
            ),
            direction_changes=movement_metric(
                float(movement.metrics.direction_changes) if movement else 0
            ),
            average_speed=movement_metric(movement.metrics.average_speed if movement else 0),
            max_speed=movement_metric(movement.metrics.maximum_speed if movement else 0),
            covered_distance=movement_metric(movement.metrics.covered_distance if movement else 0),
            stationary_periods=movement_metric(
                float(movement.metrics.stationary_period_count) if movement else 0
            ),
            covered_distance_pixels=movement_metric(
                movement.metrics.covered_distance if movement else 0
            ),
            average_speed_pixels_per_second=movement_metric(
                movement.metrics.average_speed if movement else 0
            ),
            max_speed_pixels_per_second=movement_metric(
                movement.metrics.maximum_speed if movement else 0
            ),
            stationary_time_seconds=movement_metric(
                movement.metrics.stationary_time_seconds if movement else 0
            ),
            average_acceleration_pixels_per_second_squared=movement_metric(
                movement.metrics.average_acceleration if movement else 0
            ),
            max_acceleration_pixels_per_second_squared=movement_metric(
                movement.metrics.maximum_acceleration if movement else 0
            ),
        )

    def scores(
        self,
        physical: PhysicalScoreResult | None = None,
        technical: TechnicalScoreResult | None = None,
    ) -> ScoresResponse:
        unavailable = UnsupportedMetric(reason=_REASON)
        physical_response = (
            PhysicalScoreResponse(
                value=physical.value,
                level=physical.level,
                level_label=physical.level_label,
                level_midpoint=physical.level_midpoint,
                confidence=physical.confidence,
                status=physical.status,
                version=physical.version,
                reason=physical.reason,
                evidence=PhysicalScoreEvidenceResponse(**asdict(physical.evidence))
                if physical.evidence
                else None,
                limitations=list(physical.limitations),
                explanation=physical.explanation,
            )
            if physical
            else unavailable
        )
        return ScoresResponse(
            technical=(
                TechnicalScoreResponse(
                    value=technical.value,
                    confidence=technical.confidence,
                    status=technical.status,
                    version=TECHNICAL_VERSION,
                    evidence=technical.evidence,
                )
                if technical is not None
                and technical.value is not None
                and technical.confidence is not None
                else UnsupportedMetric(
                    reason=technical.reason
                    if technical and technical.reason
                    else "Technical-event analysis was unavailable."
                )
            ),
            physical=physical_response,
            game_intelligence=UnsupportedMetric(
                reason="Requires tactical context, teammates, opponents, positioning, and decision analysis."
            ),
            mental_resilience=UnsupportedMetric(
                reason="Requires repeated high-pressure observations and human assessment."
            ),
            professionalism=UnsupportedMetric(
                reason="Requires external behavioral and attendance data."
            ),
            growth_potential=UnsupportedMetric(
                reason="Requires longitudinal player data and future outcomes."
            ),
            market_readiness=UnsupportedMetric(
                reason="Requires scouting, competition-level, business, and market data."
            ),
        )
