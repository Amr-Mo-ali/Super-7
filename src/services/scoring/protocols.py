"""Scoring dependency boundary."""

from typing import Protocol

from services.movement.schemas import MovementResult
from services.scoring.models import PhysicalScoreResult


class PhysicalActivityScorerProtocol(Protocol):
    def score(
        self,
        movement: MovementResult | None,
        visibility_ratio: float,
        visible_frames: int,
        longest_segment: int,
        track_confidence: float,
        movement_quality: float | None,
        movement_source: str,
    ) -> PhysicalScoreResult: ...
