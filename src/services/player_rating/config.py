"""Centralized provisional product constants for Player Rating V1."""

from dataclasses import dataclass

from config.scoring import (
    GAME_INTELLIGENCE_WEIGHTS,
    MAX_GAME_INTELLIGENCE_CONFIDENCE,
    MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS,
    MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS,
    PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS,
)

__all__ = [
    "GAME_INTELLIGENCE_WEIGHTS",
    "MAX_GAME_INTELLIGENCE_CONFIDENCE",
    "MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS",
    "MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS",
    "PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS",
    "PlayerRatingConfig",
]


@dataclass(frozen=True, slots=True)
class PlayerRatingConfig:
    technical_weight: float = 0.45
    physical_activity_weight: float = 0.30
    ball_involvement_weight: float = 0.25
    minimum_overall_categories: int = 2
    ball_involvement_seconds_scale: float = 5.0
    version: str = "player_rating_v1"
