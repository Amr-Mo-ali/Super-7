"""Explicit unavailable-feature responses for the tracking MVP."""

from schemas.analysis import FeatureMetric, FeaturesResponse, ScoresResponse, UnsupportedMetric
from services.ball_proximity import BallProximityResult
from services.movement.schemas import MovementResult

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
        )

    def scores(self) -> ScoresResponse:
        unavailable = UnsupportedMetric(reason=_REASON)
        return ScoresResponse(
            technical=unavailable,
            physical=unavailable,
            game_intelligence=unavailable,
            mental_resilience=unavailable,
            professionalism=unavailable,
            growth_potential=unavailable,
            market_readiness=unavailable,
        )
