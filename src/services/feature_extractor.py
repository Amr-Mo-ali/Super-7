"""Explicit unavailable-feature responses for the tracking MVP."""

from schemas.analysis import FeatureMetric, FeaturesResponse, ScoresResponse, UnsupportedMetric
from services.ball_proximity import BallProximityResult

_REASON = "Not supported by the current automatic-tracking implementation."


class FeatureExtractor:
    """Never infers football skills or events from incomplete evidence."""

    def features(
        self, proximity: BallProximityResult | None = None, reason: str | None = None
    ) -> FeaturesResponse:
        unavailable = UnsupportedMetric(reason=_REASON)
        return FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(value=None, reason=reason)
            if proximity is None
            else FeatureMetric(value=proximity.ball_proximity_time_seconds),
            movement_intensity=unavailable,
            direction_changes=unavailable,
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
