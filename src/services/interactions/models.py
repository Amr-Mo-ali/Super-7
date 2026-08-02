"""Framework-neutral contracts for proximity-based interaction evidence."""

from dataclasses import dataclass
from typing import Literal

from services.player_detector import BoundingBox

InteractionState = Literal["candidate", "non_candidate", "missing_evidence"]


@dataclass(frozen=True, slots=True)
class PlayerObservation:
    frame_index: int
    timestamp_seconds: float
    bounding_box: BoundingBox
    confidence: float


@dataclass(frozen=True, slots=True)
class BallObservation:
    frame_index: int
    timestamp_seconds: float
    center_point: tuple[float, float]
    confidence: float
    accepted_by_ball_tracker: bool = True


@dataclass(frozen=True, slots=True)
class FrameEvidence:
    frame_index: int
    state: InteractionState
    distance_pixels: float | None = None
    normalized_distance: float | None = None
    player_confidence: float | None = None
    ball_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class InteractionSegment:
    segment_id: int
    start_frame: int
    end_frame: int
    start_time_seconds: float
    end_time_seconds: float
    duration_seconds: float
    observed_frame_count: int
    bridged_gap_frames: int
    candidate_frame_count: int
    evidence_coverage_ratio: float
    mean_distance_pixels: float
    minimum_distance_pixels: float
    mean_normalized_distance: float
    minimum_normalized_distance: float
    mean_player_confidence: float
    mean_ball_confidence: float
    confidence: float
    status: Literal["possible_ball_interaction"] = "possible_ball_interaction"


@dataclass(frozen=True, slots=True)
class InteractionDiagnostics:
    interaction_aligned_frames: int
    interaction_candidate_frames: int
    interaction_non_candidate_frames: int
    interaction_missing_evidence_frames: int
    raw_interaction_segments: int
    accepted_interaction_segments: int
    rejected_short_interaction_segments: int
    rejected_low_confidence_interaction_segments: int
    bridged_interaction_gaps: int
    maximum_bridged_gap_frames: int
    interaction_evidence_coverage_ratio: float
    interaction_confidence_version: str
    interaction_analysis_quality: float
    processing_time_ms: int


@dataclass(frozen=True, slots=True)
class InteractionAnalysisResult:
    segments: tuple[InteractionSegment, ...]
    possible_ball_interaction_count: int
    possible_ball_interaction_time_seconds: float
    longest_possible_ball_interaction_seconds: float
    mean_possible_ball_interaction_confidence: float | None
    interaction_candidate_frames: int
    interaction_observed_frames: int
    interaction_evidence_coverage_ratio: float
    confidence_version: str
    diagnostics: InteractionDiagnostics
    warnings: tuple[str, ...]
    reason: str | None = None
