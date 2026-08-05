"""Small, deterministic adapter from existing analysis results to rating values."""

from collections.abc import Mapping
from math import isfinite

from core.config import Settings
from services.interactions.models import InteractionAnalysisResult
from services.player_rating.config import PlayerRatingConfig
from services.player_rating.game_intelligence import GameIntelligenceResult
from services.player_rating.models import PlayerRatingSummary, PlayerRatingValue, RatingCategory
from services.scoring.models import PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult
from services.technical_events.models import TechnicalEventAnalysisResult

_UNSUPPORTED: tuple[RatingCategory, ...] = (
    "soccer_intelligence",
    "tactical_vision",
    "mental_stability",
    "professionalism",
    "growth_potential",
    "market_readiness",
    "scalability",
)


class PlayerRatingEngine:
    """Does not invoke detectors, trackers, or existing scoring algorithms."""

    def __init__(
        self, settings: Settings | None = None, config: PlayerRatingConfig | None = None
    ) -> None:
        self._settings = settings or Settings()
        self._config = config or PlayerRatingConfig()

    def summarize(
        self,
        technical: TechnicalScoreResult | None,
        physical: PhysicalScoreResult | None,
        interactions: InteractionAnalysisResult | None,
        events: TechnicalEventAnalysisResult | None,
        game_intelligence: GameIntelligenceResult | None = None,
    ) -> PlayerRatingSummary:
        categories = (
            self._technical(technical),
            self._physical(physical),
            self._ball_involvement(interactions, events),
            *((self._game_intelligence(game_intelligence),) if game_intelligence else ()),
            *(self._unsupported(category) for category in _UNSUPPORTED),
        )
        available = tuple(item for item in categories if item.status == "available")
        duration = self._evidence_duration(physical, interactions, events)
        return PlayerRatingSummary(
            categories,
            self._overall(available, duration),
            len(available),
            len(_UNSUPPORTED),
            duration,
            self._config.version,
        )

    def _game_intelligence(self, result: GameIntelligenceResult) -> PlayerRatingValue:
        if result.value is None:
            return self._insufficient(
                "game_intelligence", result.reason or "insufficient_game_intelligence_evidence"
            )
        return self._available(
            "game_intelligence",
            result.value,
            result.confidence,
            result.explanation,
            result.limitations,
            {"available_component_count": result.available_component_count},
        )

    def _technical(self, result: TechnicalScoreResult | None) -> PlayerRatingValue:
        if result is None or result.value is None or result.confidence is None:
            return self._insufficient("technical_skill", "insufficient_event_evidence")
        return self._available(
            "technical_skill",
            result.value,
            result.confidence,
            "Provisional technical-skill estimate from candidate-event evidence.",
            ("candidate_events_are_not_confirmed_actions", "short_video_not_complete_assessment"),
            result.evidence,
        )

    def _physical(self, result: PhysicalScoreResult | None) -> PlayerRatingValue:
        if (
            result is None
            or result.value is None
            or result.confidence is None
            or result.evidence is None
        ):
            return self._insufficient("physical_activity", "insufficient_movement_evidence")
        return self._available(
            "physical_activity",
            result.value,
            result.confidence,
            "Physical Activity estimates visible image-space movement in this video, not fitness.",
            tuple(dict.fromkeys((*result.limitations, "not_physical_fitness"))),
            {
                "movement_duration_seconds": result.evidence.movement_duration_seconds,
                "movement_observations": result.evidence.movement_observations,
                "movement_analysis_quality": result.evidence.movement_analysis_quality,
            },
        )

    def _ball_involvement(
        self,
        interactions: InteractionAnalysisResult | None,
        events: TechnicalEventAnalysisResult | None,
    ) -> PlayerRatingValue:
        if (
            interactions is None
            or interactions.interaction_evidence_coverage_ratio
            < self._settings.technical_event_min_evidence_coverage
            or interactions.possible_ball_interaction_count == 0
        ):
            return self._insufficient("ball_involvement", "insufficient_interaction_evidence")
        controlled = (
            sum(item.duration_seconds for item in events.controlled_movement_candidates)
            if events
            else 0.0
        )
        duration = interactions.possible_ball_interaction_time_seconds + controlled
        value = 100 * self._clamp(duration / self._config.ball_involvement_seconds_scale)
        confidence = self._clamp(
            interactions.interaction_evidence_coverage_ratio
            * interactions.diagnostics.interaction_analysis_quality
        )
        return self._available(
            "ball_involvement",
            value,
            confidence,
            "Ball Involvement summarizes observed ball proximity and interaction evidence.",
            (
                "ball_proximity_does_not_prove_possession",
                "candidate_events_are_not_confirmed_actions",
            ),
            {
                "interaction_duration_seconds": interactions.possible_ball_interaction_time_seconds,
                "interaction_count": interactions.possible_ball_interaction_count,
                "interaction_evidence_coverage": interactions.interaction_evidence_coverage_ratio,
                "controlled_movement_duration_seconds": controlled,
            },
        )

    def _overall(
        self, available: tuple[PlayerRatingValue, ...], duration: float
    ) -> PlayerRatingValue:
        if len(available) < self._config.minimum_overall_categories:
            return self._insufficient("overall", "insufficient_supported_categories")
        weights = {
            "technical_skill": self._config.technical_weight,
            "physical_activity": self._config.physical_activity_weight,
            "ball_involvement": self._config.ball_involvement_weight,
        }
        included = {item.category: weights[item.category] for item in available}
        total = sum(included.values())
        normalized = {key: value / total for key, value in included.items()}
        value = sum(
            item.value * normalized[item.category] for item in available if item.value is not None
        )
        confidence = self._clamp(
            sum(item.confidence for item in available)
            / len(available)
            * min(1.0, duration / self._config.ball_involvement_seconds_scale)
            * len(available)
            / 3
        )
        return self._available(
            "overall",
            value,
            confidence,
            "Overall Rating uses only currently available supported categories.",
            ("provisional_product_weights", "unavailable_categories_are_not_zero"),
            {
                "categories_used": tuple(normalized),
                **{f"weight_{key}": weight for key, weight in normalized.items()},
            },
        )

    def _available(
        self,
        category: RatingCategory,
        value: float,
        confidence: float,
        explanation: str,
        limitations: tuple[str, ...],
        evidence: Mapping[str, float | int | str | tuple[str, ...]],
    ) -> PlayerRatingValue:
        bounded = self._clamp(value / 100) * 100
        return PlayerRatingValue(
            category,
            bounded,
            0.0,
            100.0,
            self._clamp(confidence),
            "available",
            self._level(bounded),
            explanation,
            limitations,
            dict(evidence),
            self._config.version,
        )

    def _insufficient(self, category: RatingCategory, reason: str) -> PlayerRatingValue:
        return PlayerRatingValue(
            category,
            None,
            0.0,
            100.0,
            0.0,
            "insufficient_evidence",
            None,
            "A numeric rating is unavailable because the evidence gate was not met.",
            (reason,),
            {"reason": reason},
            self._config.version,
        )

    def _unsupported(self, category: RatingCategory) -> PlayerRatingValue:
        return PlayerRatingValue(
            category,
            None,
            0.0,
            100.0,
            0.0,
            "unsupported",
            None,
            "This rating cannot be inferred reliably from the current video pipeline.",
            ("unsupported_by_current_pipeline",),
            {"reason": "unsupported_by_current_pipeline"},
            self._config.version,
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value)) if isfinite(value) else 0.0

    @staticmethod
    def _level(value: float) -> str:
        return (
            "very_low"
            if value < 20
            else "low"
            if value < 35
            else "developing"
            if value < 50
            else "moderate"
            if value < 65
            else "good"
            if value < 80
            else "very_good"
            if value < 90
            else "excellent"
        )

    @staticmethod
    def _evidence_duration(
        physical: PhysicalScoreResult | None,
        interactions: InteractionAnalysisResult | None,
        events: TechnicalEventAnalysisResult | None,
    ) -> float:
        values = [
            physical.evidence.movement_duration_seconds if physical and physical.evidence else 0.0,
            interactions.possible_ball_interaction_time_seconds if interactions else 0.0,
        ]
        if events:
            values.extend(item.duration_seconds for item in events.controlled_movement_candidates)
        return max(values, default=0.0)
