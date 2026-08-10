"""Explicit possession states; no possession inference is implemented here."""

from enum import StrEnum


class PossessionState(StrEnum):
    UNKNOWN = "unknown"
    FREE_BALL = "free_ball"
    PLAYER_CONTROLLED = "player_controlled"
    CONTESTED = "contested"
