"""Framework-neutral data contracts for honest player-rating summaries."""

from dataclasses import dataclass
from typing import Literal

RatingStatus = Literal["available", "insufficient_evidence", "unsupported"]
RatingCategory = Literal[
    "technical_skill",
    "physical_activity",
    "ball_involvement",
    "soccer_intelligence",
    "tactical_vision",
    "mental_stability",
    "professionalism",
    "growth_potential",
    "market_readiness",
    "scalability",
    "overall",
]
EvidenceValue = float | int | str | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlayerRatingValue:
    category: RatingCategory
    value: float | None
    scale_min: float
    scale_max: float
    confidence: float
    status: RatingStatus
    level: str | None
    explanation: str
    limitations: tuple[str, ...]
    evidence: dict[str, EvidenceValue]
    version: str


@dataclass(frozen=True, slots=True)
class PlayerRatingSummary:
    categories: tuple[PlayerRatingValue, ...]
    overall: PlayerRatingValue
    available_category_count: int
    unsupported_category_count: int
    evidence_duration_seconds: float
    version: str
