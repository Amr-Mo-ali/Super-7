"""Deterministic detailed ratings from already-computed analysis evidence."""

from math import isfinite

from schemas.analysis import (
    PhysicalScoreResponse,
    TechnicalEventAnalysisResponse,
    UnsupportedMetric,
)
from services.callback_service import DetailedRatings


class DetailedRatingEngine:
    """Populate only detailed axes whose current evidence matches their narrow meaning."""

    def evaluate(
        self,
        physical: PhysicalScoreResponse | UnsupportedMetric,
        technical_events: TechnicalEventAnalysisResponse,
        technical_event_quality: float | None,
    ) -> DetailedRatings:
        return DetailedRatings(
            speed_and_fitness=self._visible_movement_activity(physical),
            ball_control_and_individual_skill=self._ball_control(
                technical_events, technical_event_quality
            ),
        )

    @staticmethod
    def _visible_movement_activity(
        physical: PhysicalScoreResponse | UnsupportedMetric,
    ) -> float | None:
        """Use the existing physical evidence gate, not a physiological-fitness claim."""
        if (
            not isinstance(physical, PhysicalScoreResponse)
            or physical.status != "provisional_video_based"
            or physical.evidence is None
            or not _finite(physical.evidence.movement_intensity)
        ):
            return None
        return _score(physical.evidence.movement_intensity * 100)

    @staticmethod
    def _ball_control(
        events: TechnicalEventAnalysisResponse, technical_event_quality: float | None
    ) -> float | None:
        """Reuse the existing controlled-movement/dribble evidence calculation exactly."""
        if (
            technical_event_quality is None
            or not isfinite(technical_event_quality)
            or technical_event_quality <= 0
        ):
            return None
        controlled = [
            _controlled_component(
                item.confidence,
                item.normalized_player_displacement,
                item.direction_similarity,
                item.duration_seconds,
            )
            for item in events.controlled_movement_candidates
        ]
        dribbles = [
            _dribble_component(
                item.confidence,
                item.movement_evidence_component,
                item.proximity_persistence,
                item.path_straightness,
                item.direction_changes,
            )
            for item in events.dribble_candidates
        ]
        positive = controlled + dribbles
        if not positive:
            return None
        penalty = min(
            0.25,
            sum(item.confidence for item in events.ball_loss_candidates) / len(positive) * 0.15,
        )
        return _score((sum(positive) / len(positive) - penalty) * 100)


def _controlled_component(
    confidence: float,
    displacement: float,
    direction_similarity: float | None,
    duration_seconds: float,
) -> float:
    return (
        confidence * 0.40
        + min(1.0, displacement) * 0.25
        + max(0.0, direction_similarity or 0.0) * 0.20
        + min(1.0, duration_seconds / 2) * 0.15
    )


def _dribble_component(
    confidence: float,
    movement_evidence: float,
    proximity_persistence: float,
    path_straightness: float,
    direction_changes: int,
) -> float:
    return (
        confidence * 0.30
        + movement_evidence * 0.25
        + proximity_persistence * 0.20
        + path_straightness * 0.15
        + min(1.0, direction_changes / 3) * 0.10
    )


def _finite(value: float | None) -> bool:
    return value is not None and isfinite(value)


def _score(value: float) -> float:
    return min(100.0, max(0.0, value)) if isfinite(value) else 0.0
