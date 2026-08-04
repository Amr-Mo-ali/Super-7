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
