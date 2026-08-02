"""Response contracts for automatic target selection."""

from typing import Literal

from pydantic import BaseModel, Field


class VideoResponse(BaseModel):
    duration_seconds: float
    fps: float
    width: int
    height: int


class UnsupportedMetric(BaseModel):
    value: None = None
    reason: str


class FeatureMetric(BaseModel):
    value: float | None = None
    reason: str | None = None


class SelectedPlayer(BaseModel):
    track_id: int
    selection_method: str
    selection_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    visible_frames: int
    visibility_ratio: float
    ball_proximity_frames: int
    ball_proximity_ratio: float
    visibility_contribution: float
    ball_proximity_contribution: float


class TrackingResponse(BaseModel):
    frames_processed: int
    lost_track_count: int
    longest_continuous_visible_segment: int


class FeaturesResponse(BaseModel):
    ball_proximity_time_seconds: FeatureMetric
    movement_intensity: UnsupportedMetric
    direction_changes: UnsupportedMetric


class ScoresResponse(BaseModel):
    technical: UnsupportedMetric
    physical: UnsupportedMetric
    game_intelligence: UnsupportedMetric
    mental_resilience: UnsupportedMetric
    professionalism: UnsupportedMetric
    growth_potential: UnsupportedMetric
    market_readiness: UnsupportedMetric


class CompletedResponse(BaseModel):
    analysis_id: str
    status: Literal["completed"]
    video: VideoResponse
    selected_player: SelectedPlayer
    tracking: TrackingResponse
    features: FeaturesResponse
    scores: ScoresResponse
    diagnostics: "Diagnostics"
    warnings: list[str]
    analysis_version: str
    model_version: str
    processing_time_ms: int


class AmbiguousResponse(BaseModel):
    analysis_id: str
    status: Literal["ambiguous_target"]
    selected_player: None = None
    candidate_count: int
    warnings: list[str]


class Diagnostics(BaseModel):
    frames_processed: int
    frames_with_player_detections: int
    total_person_detections: int
    tracks_created: int
    valid_candidate_tracks: int
    ball_detections: int
    ball_visible_frames: int = 0
    ball_track_segments: int = 0
    ball_detection_confidence_mean: float | None = None


class NonCompletedResponse(BaseModel):
    analysis_id: str
    status: Literal[
        "invalid_video",
        "no_players_detected",
        "no_valid_tracks",
        "failed",
        "player_detection_completed_tracking_not_available",
    ]
    selected_player: None = None
    candidate_count: int = 0
    warnings: list[str]
    diagnostics: Diagnostics


AnalyzeResponse = CompletedResponse | AmbiguousResponse | NonCompletedResponse
