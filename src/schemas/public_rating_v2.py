"""Compact, dashboard-facing Public Rating JSON V2 schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field

type PublicRatingStatus = Literal[
    "available",
    "insufficient_evidence",
    "unsupported",
    "provisional_video_based",
    "provisional_event_based",
]
type PublicEventType = Literal["controlled_movement", "dribble", "ball_loss", "pass", "shot"]
type PublicEventStatus = Literal[
    "candidate", "accepted", "suppressed_duplicate", "ambiguous", "unresolved_conflict"
]


class PublicRatingValue(BaseModel):
    value: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: PublicRatingStatus
    level: str | None = None
    explanation: str | None = None
    reason: str | None = None
    limitations: list[str] = Field(default_factory=list)
    version: str


class PublicGameIntelligence(BaseModel):
    value: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: PublicRatingStatus
    level: str | None = None
    reason: str | None = None
    version: str
    components: dict[str, PublicRatingValue]
    effective_weights: dict[str, float]
    limitations: list[str]


class PublicEvent(BaseModel):
    id: str
    type: PublicEventType | None
    status: PublicEventStatus = "candidate"
    confidence: float = Field(ge=0, le=1)
    arbitration_confidence: float | None = Field(default=None, ge=0, le=1)
    start_seconds: float
    release_seconds: float | None = None
    end_seconds: float
    duration_seconds: float = Field(ge=0)
    details: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    candidate_types: list[PublicEventType] = Field(default_factory=list)
    source_candidate_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PublicRatingV2Response(BaseModel):
    request_id: str
    analysis: dict[str, str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    video: dict[str, float | dict[str, int]]
    player: dict[str, float | int]
    ratings: dict[str, PublicRatingValue | PublicGameIntelligence]
    overall: PublicRatingValue
    summary: dict[str, int]
    quality: dict[str, dict[str, float | str]]
    events: dict[str, list[PublicEvent]]
    limitations: list[str]
    warnings: list[str]
    versions: dict[str, str]


class PublicRatingV2Failure(BaseModel):
    request_id: str
    analysis: dict[str, str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    reason: str
    reason_code: str
    warnings: list[str]
    retryable: bool
