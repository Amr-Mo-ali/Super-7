"""Centralized provisional product constants for Player Rating V1."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlayerRatingConfig:
    technical_weight: float = 0.45
    physical_activity_weight: float = 0.30
    ball_involvement_weight: float = 0.25
    minimum_overall_categories: int = 2
    ball_involvement_seconds_scale: float = 5.0
    version: str = "player_rating_v1"


# Product constants for the deliberately limited video-only heuristic.  They are
# kept here so the calculation has no hidden display scales or scattered weights.
GAME_INTELLIGENCE_WEIGHTS: dict[str, float] = {
    "ball_involvement": 0.30,
    "decision_consistency": 0.20,
    "spatial_activity_proxy": 0.20,
    "movement_efficiency_proxy": 0.15,
    "technical_involvement": 0.15,
}
MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS = 3
MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS = 4.0
PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS = 20.0
MAX_GAME_INTELLIGENCE_CONFIDENCE = 0.65
