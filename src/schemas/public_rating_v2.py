"""Compact, dashboard-facing Public Rating JSON V2 schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class PublicRatingValue(BaseModel):
    value: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: str
    level: str | None = None
    explanation: str | None = None
    reason: str | None = None
    limitations: list[str] = Field(default_factory=list)
    version: str


class PublicGameIntelligence(BaseModel):
    value: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: str
    level: str | None = None
    reason: str | None = None
    version: str
    components: dict[str, PublicRatingValue]
    effective_weights: dict[str, float]
    limitations: list[str]


class PublicEvent(BaseModel):
    id: str
    type: str
    status: Literal["candidate"] = "candidate"
    confidence: float = Field(ge=0, le=1)
    start_seconds: float
    end_seconds: float
    duration_seconds: float = Field(ge=0)
    details: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class PublicRatingV2Response(BaseModel):
    analysis: dict[str, str]
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
    analysis: dict[str, str]
    reason: str
    reason_code: str
    warnings: list[str]
    retryable: bool
